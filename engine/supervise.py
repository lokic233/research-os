"""engine/supervise.py — supervision tree + task ledger + lingering-agent reaper.

Fixes the "agent sprawl / lingering session" class of bugs (BUG-31..34):
  - BUG-31  Lingering agents: nothing ever flipped a dead agent's status:running -> stale yamls
            accumulate forever (191 registered / 52 'running' but mostly abandoned r-versions).
  - BUG-32  Heartbeat-only handoff: a retiring sub-monitor passed no STATE about the researcher/
            seeder it owned (alive? dead? ready-to-reseed?). Successor inherited a vacuum.
  - BUG-33  No system-wide task tracking: no incrementing job id, no parent/assignee tree, so the
            orchestrator could not see "who owns what" or detect orphaned work.
  - BUG-34  No structured notification on coverage loss: submonitors only PRINTED an advisory.

Data model (all YAML, atomic via ros.dump_yaml; stdlib + pyyaml only):
  registry/tasks/TASK-NNNN.yaml   incrementing task ledger (next_id reuses ros.py allocator)
  runtime/agents/<id>.yaml         gains optional `parent` edge (supervision tree) + reap status
  orchestrator inbox (Channel)     receives AGENT_DOWN / TASK_ORPHANED notifications

This module is imported by ros.py; it reuses ros.py's helpers (load_yaml/dump_yaml/NOW/next_id/
inst_root/runtime_dir/_cfg/reg_dir) passed in at call time to avoid a circular import.
"""
import os, re, glob, json, datetime as _dt

TERMINAL = ("completed", "failed", "retired", "dead", "superseded", "done", "dropped")
ALIVE_STATES = ("running", "active", "open")
TASK_STATES = ("open", "active", "orphaned", "blocked", "done", "dropped")
TASK_TERMINAL = ("done", "dropped")


def _age_min(rec, now):
    try:
        last = _dt.datetime.strptime(rec.get("last_heartbeat", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
        return (now - last).total_seconds() / 60.0
    except Exception:
        return 1e9


# ---------------- TASK LEDGER ----------------
def _alloc_task_id(H, root):
    """BUG-43 fix: atomic incrementing TASK-id allocation. next_id (max-scan + write) RACES under
    concurrent opens (5 parallel opens collided to 2 ids, losing 3 tasks). Here we claim the next id
    by EXCLUSIVELY creating its file (O_CREAT|O_EXCL); on collision we bump and retry. Guarantees no
    two writers ever get the same id."""
    d = H["reg_dir"](root, "tasks"); os.makedirs(d, exist_ok=True)
    # seed from current max
    mx = 0
    for fn in glob.glob(os.path.join(d, "TASK-*.yaml")):
        m = re.search(r"TASK-(\d+)", os.path.basename(fn))
        if m: mx = max(mx, int(m.group(1)))
    n = mx + 1
    while True:
        tid = f"TASK-{n:04d}"; path = os.path.join(d, f"{tid}.yaml")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)               # claimed the id atomically (empty placeholder; caller fills it)
            return tid, path
        except FileExistsError:
            n += 1                     # someone else took this id — bump and retry
        if n > mx + 10000:
            raise SystemExit("❌ task id allocation runaway")


def task_open(H, root, *, kind, parent, assignee, project="", claim="", exp="", session="", summary=""):
    """Allocate an incrementing TASK-id (ATOMIC, race-safe) and write the task record. Returns task_id."""
    tid, path = _alloc_task_id(H, root)
    now = H["NOW"]()
    obj = {
        "task_id": tid, "kind": kind, "parent": parent or "", "assignee": assignee or "",
        "session_id": session or "", "project_id": project or "", "claim_id": claim or "",
        "exp_id": exp or "", "status": "open", "summary": summary or "",
        "evidence_delta": "", "opened_at": now, "updated_at": now, "closed_at": "",
        "history": [{"ts": now, "event": "open", "by": parent or "", "note": summary or ""}],
    }
    H["dump_yaml"](path, obj)
    return tid, path


def _task_path(H, root, tid):
    d = H["reg_dir"](root, "tasks")
    return os.path.join(d, f"{tid}.yaml")


def task_update(H, root, tid, *, assignee=None, status=None, session=None, evidence_delta=None, by="", note="", _internal=False):
    # BUG-106 fix: task_update was an UNLOCKED load->mutate->dump on the task record — the exact lost-update
    # race class as BUG-59/97 (channel) and BUG-99/100/101 (agent/gpu_queue). Multiple writers concurrently
    # mutate the SAME task ledger entry: the reaper closes/orphans/reassigns tasks (BUG-86/88/96/102), exp
    # complete closes the owner's task (BUG-60b), handoff transfers it, and the researcher updates its own
    # status — all via this function. Two overlapping calls each read the same on-disk record, mutate their
    # in-memory copy, and the LAST dump wins, silently erasing the other's write (status flip, reassignment,
    # AND every history entry in between). Reproduced 18/20 lost updates in isolated /tmp. Fix (no new
    # mechanism): serialize the RMW under the same per-object _file_lock + O_EXCL discipline and RE-READ the
    # record on disk INSIDE the lock so each writer composes on the latest committed state (mirrors BUG-99's
    # heartbeat lock and BUG-101's retire flip). Falls back to the old unlocked path only if H lacks the lock
    # (older helper bundle) so behavior is unchanged where the lock is unavailable.
    p = _task_path(H, root, tid)
    _fl = H.get("_file_lock")

    def _do():
        rec = H["load_yaml"](p, {}) or {}
        if not rec.get("task_id"):
            raise SystemExit(f"❌ {tid} not found")
        # BUG-46 fix: validate status against the known set (a typo'd status silently corrupts the ledger).
        if status is not None and status not in TASK_STATES:
            raise SystemExit(f"❌ invalid task status '{status}'. Valid: {', '.join(TASK_STATES)}")
        # BUG-47 fix: a terminal task (done/dropped) is immutable — refuse to reopen/mutate (except internal
        # reaper bookkeeping which never targets terminal tasks anyway).
        if rec.get("status") in TASK_TERMINAL and not _internal:
            raise SystemExit(f"❌ {tid} is {rec.get('status')} (terminal) — cannot modify a closed task")
        now = H["NOW"]()
        if assignee is not None: rec["assignee"] = assignee
        if session is not None: rec["session_id"] = session
        if status is not None: rec["status"] = status
        if evidence_delta: rec["evidence_delta"] = evidence_delta
        rec["updated_at"] = now
        if status in ("done", "dropped"): rec["closed_at"] = now
        rec.setdefault("history", []).append({"ts": now, "event": status or "update", "by": by, "note": note or (evidence_delta or "")})
        H["dump_yaml"](p, rec)
        return rec

    if _fl is not None:
        with _fl(root, f"task_{tid}", stale_s=15):
            return _do()
    return _do()


def task_list(H, root, *, only_open=False, only_orphans=False, project=None):
    out = []
    for fn in sorted(glob.glob(os.path.join(H["reg_dir"](root, "tasks"), "TASK-*.yaml"))):
        r = H["load_yaml"](fn, {}) or {}
        if not r.get("task_id"): continue
        if only_open and r.get("status") in ("done", "dropped"): continue
        if only_orphans and r.get("status") != "orphaned": continue
        if project and r.get("project_id") != project: continue
        out.append(r)
    return out


# ---------------- REAPER (no lingering agents) ----------------
def reap(H, root, *, apply=False, grace_min=45, converged_pids=None):
    """Flip lingering agents out of an alive state:
       - past-grace AND a fresher live agent owns the same (project,role) -> 'superseded'
       - past-grace AND no live successor, project CONVERGED/no-project    -> 'retired' (clean, no notify)
       - past-grace AND no live successor for an ACTIVE project            -> 'dead' + NOTIFY orchestrator
       Orphan the dead agent's open tasks + notify. Returns (changes, notifications)."""
    converged_pids = set(converged_pids or [])
    rd = H["runtime_dir"](root)
    now = _dt.datetime.now(_dt.timezone.utc)
    agents = []
    for fn in glob.glob(os.path.join(rd, "agents", "*.yaml")):
        a = H["load_yaml"](fn, {}) or {}
        if a.get("agent_id"):
            a["_fn"] = fn; a["_age"] = _age_min(a, now); agents.append(a)
    # freshest live agent per (project, role)
    def key(a): return (a.get("project_id", ""), a.get("role", ""))
    # BUG-87 fix: agents with an empty/unknown role have NO role identity to inherit, so they must NOT
    # be grouped into a shared (project,"") bucket — otherwise any fresh unknown-role agent would silently
    # "supersede" an unrelated stale unknown-role agent, swallowing a real coverage gap WITHOUT the
    # AGENT_DOWN notify. Only well-roled live agents are eligible to supersede; unknown-role lingerers fall
    # through to the DEAD/retired path so genuine gaps are surfaced. (BUG-10 keeps roles from downgrading.)
    def _has_role(a): return bool((a.get("role") or "").strip()) and (a.get("role") or "").strip() != "unknown"
    live_fresh = {}
    for a in agents:
        if a.get("status") in ALIVE_STATES and a["_age"] <= grace_min and _has_role(a):
            k = key(a)
            if k not in live_fresh or a["_age"] < live_fresh[k]["_age"]:
                live_fresh[k] = a
    changes, notifs = [], []
    for a in agents:
        if a.get("status") not in ALIVE_STATES:        # already terminal — leave
            continue
        if a["_age"] <= grace_min:                      # still within grace — alive
            continue
        k = key(a); succ = live_fresh.get(k)
        pid = a.get("project_id", "")
        if succ and succ.get("agent_id") != a.get("agent_id"):
            new_status = "superseded"                   # a fresher r-version is live -> reap the old
            note = f"superseded by {succ.get('agent_id')} (age {round(a['_age'])}m)"
        elif (not pid) or (pid in converged_pids):
            new_status = "retired"                      # converged/unscoped + no successor -> clean retire, NO notify
            note = f"retired (age {round(a['_age'])}m; {'converged ' + pid if pid else 'no project'}, no successor needed)"
        else:
            new_status = "dead"                         # ACTIVE project, no live successor -> real coverage gap
            note = f"DEAD (age {round(a['_age'])}m, no live successor for ACTIVE {pid})"
        changes.append((a.get("agent_id"), a.get("status"), new_status, pid, a.get("role", ""), note))
        if apply:
            a2 = H["load_yaml"](a["_fn"], {}) or {}
            a2["status"] = new_status
            a2["reaped_at"] = H["NOW"]()
            a2["reaped_note"] = note
            if new_status == "superseded" and succ:
                a2["retired_to"] = succ.get("agent_id")
            H["dump_yaml"](a["_fn"], a2)
            # BUG-49 fix: if superseded, re-parent this agent's LIVE children to the live successor so
            # the supervision tree doesn't fragment (a live researcher must hang off its live supervisor).
            if new_status == "superseded" and succ:
                succ_id = succ.get("agent_id")
                for fn2 in glob.glob(os.path.join(rd, "agents", "*.yaml")):
                    ch = H["load_yaml"](fn2, {}) or {}
                    if ch.get("parent") == a.get("agent_id") and ch.get("agent_id") != succ_id:
                        ch["parent"] = succ_id; ch["last_parent_change"] = H["NOW"]()
                        H["dump_yaml"](fn2, ch)
            # orphan this agent's open tasks (+ re-parent tasks parented by a superseded agent)
            for t in task_list(H, root):
                if t.get("assignee") == a.get("agent_id") and t.get("status") in ("open", "active"):
                    # BUG-88 fix: a SUPERSEDED agent has a live same-(project,role) successor that IS its
                    # continuation — transfer its assigned open/active work to that successor (mirrors
                    # handoff() frm->to) instead of orphaning it + emitting a spurious TASK_ORPHANED for
                    # work that already has a live owner. Only DEAD/retired-without-successor agents orphan.
                    if new_status == "superseded" and succ:
                        task_update(H, root, t["task_id"], assignee=succ.get("agent_id"), by="reaper",
                                    note=f"reassigned from superseded {a.get('agent_id')} -> {succ.get('agent_id')}")
                        # BUG-102: a task that is BOTH assigned-to AND parented-by the superseded agent
                        # only matched this assignee branch (the parent-reparent branch below is an `elif`),
                        # so its `parent` field was left dangling at the now-superseded agent — fragmenting
                        # the supervision tree exactly as BUG-49 fixed for agent children. Move the parent
                        # edge to the live successor too (same continuation), so no task hangs off a dead node.
                        if t.get("parent") == a.get("agent_id"):
                            _pp = _task_path(H, root, t["task_id"]); _pr = H["load_yaml"](_pp, {}) or {}
                            _pr["parent"] = succ.get("agent_id"); _pr["updated_at"] = H["NOW"]()
                            _pr.setdefault("history", []).append({"ts": H["NOW"](), "event": "reparent", "by": "reaper",
                                "note": f"supervisor {a.get('agent_id')} superseded -> {succ.get('agent_id')} (dual assignee+parent)"})
                            H["dump_yaml"](_pp, _pr)
                    elif new_status == "retired":
                        # BUG-96: a CLEAN retire (converged project / no project, "no successor needed",
                        # deliberately NO AGENT_DOWN) must NOT orphan its leftover open tasks + emit a
                        # spurious actionable TASK_ORPHANED — there is no work to respawn/reseed on a
                        # converged project (same principle as BUG-28: converged projects generate no
                        # supervision signal). Drop the leftover task cleanly instead. Only DEAD (real
                        # coverage gap on an ACTIVE project) orphans + notifies.
                        task_update(H, root, t["task_id"], status="dropped", by="reaper",
                                    note=f"assignee {a.get('agent_id')} retired (converged/no project — no work to reassign)")
                    else:
                        task_update(H, root, t["task_id"], status="orphaned", by="reaper",
                                    note=f"assignee {a.get('agent_id')} {new_status}")
                        notifs.append(("TASK_ORPHANED", t["task_id"], t.get("project_id", "")))
                elif (new_status == "superseded" and succ and t.get("parent") == a.get("agent_id")
                      and t.get("status") not in ("done", "dropped")):
                    p = _task_path(H, root, t["task_id"]); r = H["load_yaml"](p, {}) or {}
                    r["parent"] = succ.get("agent_id"); r["updated_at"] = H["NOW"]()
                    r.setdefault("history", []).append({"ts": H["NOW"](), "event": "reparent", "by": "reaper",
                                                        "note": f"supervisor {a.get('agent_id')} superseded -> {succ.get('agent_id')}"})
                    H["dump_yaml"](p, r)
            if new_status == "dead":
                _notify(H, root, rd, kind="AGENT_DOWN",
                        agent=a.get("agent_id"), project=a.get("project_id", ""), role=a.get("role", ""),
                        detail=note)
                notifs.append(("AGENT_DOWN", a.get("agent_id"), a.get("project_id", "")))
    return changes, notifs


def _notify(H, root, rd, *, kind, agent, project, role, detail):
    """Drop a structured, action-flagged entry into the orchestrator inbox (reuses Channel if present)."""
    rec = {"ts": H["NOW"](), "agent": "reaper", "role": "supervisor",
           "done": "", "doing": f"{kind}: {agent}", "blocked": "",
           "need": f"{kind} project={project} role={role}: {detail} — orchestrator must respawn/reseed or close the task",
           "next": "", "claim": "", "exp": "", "event": kind, "subject": agent, "project_id": project}
    p = os.path.join(rd, "orchestrator", "inbox.yaml")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    Ch = H.get("Channel")
    if Ch is not None:
        Ch(p, list_key="items", id_prefix="IN").submit({**rec, "needs_action": True})
    else:
        inbox = H["load_yaml"](p, {"items": []}) or {"items": []}
        inbox["items"].append({**rec, "acked": False, "needs_action": True})
        H["dump_yaml"](p, inbox)


# ---------------- STATE-PASSING HANDOFF ----------------
def handoff(H, root, *, frm, to, project="", researcher_state="", seeder_state="",
            open_work="", evidence_delta="", reseed_needed=False, by="", task=""):
    """Record a STRUCTURED handoff (not a heartbeat). Writes a handoff artifact AND, if a task id is
    given, transfers the task to the successor carrying the inherited state. If reseed_needed, notify
    the orchestrator that the monitored researcher/seeder is dead and a fresh one must be dispatched."""
    rd = H["runtime_dir"](root)
    now = H["NOW"]()
    d = os.path.join(rd, "handoffs"); os.makedirs(d, exist_ok=True)
    rec = {"ts": now, "from": frm, "to": to, "project_id": project,
           "researcher_state": researcher_state, "seeder_state": seeder_state,
           "open_work": open_work, "evidence_delta": evidence_delta,
           "reseed_needed": bool(reseed_needed), "task": task or "", "by": by or frm}
    H["dump_yaml"](os.path.join(d, f"{frm}__to__{to}__{now.replace(':','').replace('-','')}.yaml"), rec)
    if task:
        task_update(H, root, task, assignee=to, status="active",
                    evidence_delta=evidence_delta, by=frm,
                    note=f"handoff: researcher={researcher_state}; seeder={seeder_state}; open={open_work}")
    if reseed_needed:
        # BUG-48 fix: cross-check actual researcher liveness before asking for a fresh spawn — a blind
        # RESEED_NEEDED could make the orchestrator double-spawn onto a still-live researcher (the exact
        # corruption we fight). If a live researcher exists on this project, DOWNGRADE to a flagged warning.
        live_res = _live_researchers(H, root, project, grace_min=45)
        if live_res:
            _notify(H, root, rd, kind="RESEED_CONFLICT", agent=frm, project=project, role="researcher",
                    detail=f"reseed requested BUT live researcher(s) exist: {', '.join(live_res)} — "
                           f"VERIFY before spawning (do NOT double-spawn). seeder_state={seeder_state}")
            rec["reseed_conflict"] = live_res
        else:
            _notify(H, root, rd, kind="RESEED_NEEDED", agent=frm, project=project, role="researcher",
                    detail=f"seeder dead/ready-to-reseed: {seeder_state}; open_work={open_work}")
    return rec


def _live_researchers(H, root, project, *, grace_min=45):
    """Live researcher agent-ids on a project (status alive + within grace). Used to prevent double-spawn."""
    rd = H["runtime_dir"](root); now = _dt.datetime.now(_dt.timezone.utc); out = []
    for fn in glob.glob(os.path.join(rd, "agents", "*.yaml")):
        a = H["load_yaml"](fn, {}) or {}
        if (a.get("role") == "researcher" and a.get("project_id") == project
                and a.get("status") in ALIVE_STATES and _age_min(a, now) <= grace_min):
            out.append(a.get("agent_id"))
    return out


# ---------------- SUPERVISION TREE VIEW ----------------
def tree(H, root, *, grace_min=45):
    rd = H["runtime_dir"](root)
    now = _dt.datetime.now(_dt.timezone.utc)
    agents = []
    for fn in glob.glob(os.path.join(rd, "agents", "*.yaml")):
        a = H["load_yaml"](fn, {}) or {}
        if a.get("agent_id"): a["_age"] = _age_min(a, now); agents.append(a)
    # only show non-terminal (live tree) by default
    return agents


# ---------------- ATOMIC RETIRE -> SUCCESSOR (BUG-35/36/37) ----------------
def retire(H, root, *, frm, to, project="", researcher_state="", seeder_state="",
           open_work="", evidence_delta="", reseed_needed=False, by="", grace_min=45):
    """ATOMIC sub-monitor/agent retirement. In ONE engine call:
       1. register the successor `to` (inheriting role/project/parent/session edge) if not present
       2. re-parent EVERY task assigned to `frm` -> `to`              (no orphaned researcher task)
       3. re-parent EVERY agent supervised-by `frm` (parent==frm)     -> parent=`to`  (chain intact)
       4. flip `frm` -> status:retired (+ retired_to edge)            (no lingering predecessor)
       5. write a structured state-passing handoff artifact           (not heartbeat)
       6. if reseed_needed -> notify orchestrator                     (dead seeder -> dispatch fresh)
    Returns a dict report. Idempotent-ish: re-running is safe (already-retired frm is a no-op flip)."""
    rd = H["runtime_dir"](root); now = H["NOW"]()
    if frm == to:
        raise SystemExit(f"❌ retire: successor must differ from predecessor (got {frm} -> {to}); "
                         f"self-retire would brick the lane")
    fp = os.path.join(rd, "agents", f"{frm}.yaml")
    frec = H["load_yaml"](fp, {}) or {}
    if not frec.get("agent_id"):
        raise SystemExit(f"❌ retire: predecessor {frm} not found in runtime/agents")
    # guard: refuse to clobber a DIFFERENT live agent occupying the successor id
    tp0 = os.path.join(rd, "agents", f"{to}.yaml")
    t0 = H["load_yaml"](tp0, {}) or {}
    if t0.get("agent_id"):
        same_role = t0.get("role", frec.get("role")) == frec.get("role", "sub-monitor")
        t0_alive = t0.get("status") in ALIVE_STATES and _age_min(t0, _dt.datetime.now(_dt.timezone.utc)) <= grace_min
        if t0_alive and (not same_role or t0.get("succeeds") not in (frm, None, "")):
            raise SystemExit(f"❌ retire: successor id {to} already exists as a LIVE "
                             f"{t0.get('role')} (status {t0.get('status')}) — refusing to clobber. "
                             f"Pick a fresh successor id.")
    # 1. register/refresh successor inheriting frm's edges
    tp = os.path.join(rd, "agents", f"{to}.yaml")
    trec = H["load_yaml"](tp, {}) or {}
    inherited_parent = frec.get("parent", "") or ""
    trec.update({
        "agent_id": to, "role": frec.get("role", "sub-monitor"),
        "backend": trec.get("backend", frec.get("backend", "")),
        "node": trec.get("node", frec.get("node", "")),
        "status": "running", "spawned_at": trec.get("spawned_at", now), "last_heartbeat": now,
        "project_id": project or frec.get("project_id", ""),
        "parent": inherited_parent,
        "session_id": trec.get("session_id", ""),  # successor sets its own session via heartbeat/register
        "current_claim_id": frec.get("current_claim_id", ""),
        "current_exp_id": frec.get("current_exp_id", ""),
        "heartbeat_count": int(trec.get("heartbeat_count", 0)),
        "succeeds": frm, "note": f"successor to {frm}",
    })
    H["dump_yaml"](tp, trec)
    # 2. re-parent every TASK assigned to frm -> to
    moved_tasks = []
    for t in task_list(H, root):
        if t.get("assignee") == frm and t.get("status") not in ("done", "dropped"):
            task_update(H, root, t["task_id"], assignee=to, status="active", by=frm,
                        note=f"inherited on retire {frm}->{to}; researcher_state={researcher_state}")
            moved_tasks.append(t["task_id"])
        # also re-parent tasks whose PARENT was frm (the researcher tasks frm supervised)
        elif t.get("parent") == frm and t.get("status") not in ("done", "dropped"):
            p = _task_path(H, root, t["task_id"]); r = H["load_yaml"](p, {}) or {}
            r["parent"] = to; r["updated_at"] = now
            r.setdefault("history", []).append({"ts": now, "event": "reparent", "by": frm, "note": f"supervisor {frm}->{to}"})
            H["dump_yaml"](p, r)
            moved_tasks.append(t["task_id"] + "(reparent)")
    # 3. re-parent every AGENT supervised-by frm (parent==frm) -> parent=to
    moved_agents = []
    for fn in glob.glob(os.path.join(rd, "agents", "*.yaml")):
        a = H["load_yaml"](fn, {}) or {}
        if a.get("parent") == frm and a.get("agent_id") != to:
            a["parent"] = to; a["last_parent_change"] = now
            H["dump_yaml"](fn, a)
            moved_agents.append(a.get("agent_id"))
    # 4. flip frm -> retired. BUG-101: steps 1-3 above (task/agent scans, multiple dumps) take real
    #    wall-time; frec was loaded at the TOP of retire(), BEFORE that body. A concurrent heartbeat from
    #    frm — which serializes under _file_lock(agent_frm) and re-reads on disk per BUG-99 — lands inside
    #    that window and bumps frm.yaml; retire's UNLOCKED dump of the STALE frec then erases that update
    #    (lost heartbeat_count/last_heartbeat/session/exp; defeats the very lock heartbeat respects).
    #    Mirror BUG-99: take the same per-agent lock and RE-READ on disk, then force the terminal flip
    #    onto the fresh record so the concurrent heartbeat's liveness fields are preserved.
    _fl = H.get("_file_lock")
    if _fl is not None:
        with _fl(root, f"agent_{frm}", stale_s=15):
            fresh = H["load_yaml"](fp, {}) or frec
            fresh["status"] = "retired"; fresh["retired_at"] = now; fresh["retired_to"] = to
            H["dump_yaml"](fp, fresh)
    else:
        frec["status"] = "retired"; frec["retired_at"] = now; frec["retired_to"] = to
        H["dump_yaml"](fp, frec)
    # 5. structured handoff artifact
    ho = handoff(H, root, frm=frm, to=to, project=project or frec.get("project_id", ""),
            researcher_state=researcher_state, seeder_state=seeder_state, open_work=open_work,
            evidence_delta=evidence_delta, reseed_needed=reseed_needed, by=by or frm, task="")
    conflict = ho.get("reseed_conflict") if isinstance(ho, dict) else None
    return {"from": frm, "to": to, "moved_tasks": moved_tasks, "moved_agents": moved_agents,
            "inherited_parent": inherited_parent, "reseed_needed": bool(reseed_needed),
            "reseed_conflict": conflict}


# ---------------- HIERARCHICAL PROJECT MEMORY (area-3: avoid runtime overflow) ----------------
def project_tree(H, root, *, grace_min=45, converged_pids=None):
    """Compact hierarchical view: project -> live sub-monitor -> claim-seeders (active/retired).
    Reads the COMMITTED registry (claims/agents/tasks), not full runtime histories, so a reader
    never has to load every per-agent jsonl/buglog to know who owns what. Returns a nested dict."""
    converged_pids = set(converged_pids or [])
    rd = H["runtime_dir"](root); now = _dt.datetime.now(_dt.timezone.utc)
    # index agents by project
    agents = []
    for fn in glob.glob(os.path.join(rd, "agents", "*.yaml")):
        a = H["load_yaml"](fn, {}) or {}
        if a.get("agent_id"): a["_age"] = _age_min(a, now); agents.append(a)
    # index claims by project (committed registry = durable memory)
    claims_by_proj = {}
    for fn in glob.glob(os.path.join(H["reg_dir"](root, "claims"), "**", "CLAIM-*.yaml"), recursive=True):
        c = H["load_yaml"](fn, {}) or {}
        cid = c.get("claim_id"); pid = c.get("project_id", "")
        if cid:
            claims_by_proj.setdefault(pid, []).append(
                {"claim_id": cid, "status": c.get("status", "?"), "stage": c.get("stage", "?")})
    out = {}
    for d in sorted(glob.glob(os.path.join(root, "projects", "PROJ-*"))):
        pid = os.path.basename(d)
        conv = pid in converged_pids
        sms = [a for a in agents if a.get("role") == "sub-monitor" and a.get("project_id") == pid]
        live = [a for a in sms if a.get("status") in ALIVE_STATES and a["_age"] <= grace_min]
        live.sort(key=lambda x: x["_age"])
        researchers = [a for a in agents if a.get("role") == "researcher" and a.get("project_id") == pid]
        live_res = [a for a in researchers if a.get("status") in ALIVE_STATES and a["_age"] <= grace_min]
        out[pid] = {
            "converged": conv,
            "live_sub_monitor": (live[0].get("agent_id") if live else None),
            "sub_monitor_generations": len(sms),
            "live_researchers": [a.get("agent_id") for a in live_res],
            "claims": claims_by_proj.get(pid, []),
        }
    return out
