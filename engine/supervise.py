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
import os, glob, json, datetime as _dt

TERMINAL = ("completed", "failed", "retired", "dead", "superseded", "done", "dropped")
ALIVE_STATES = ("running", "active", "open")


def _age_min(rec, now):
    try:
        last = _dt.datetime.strptime(rec.get("last_heartbeat", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
        return (now - last).total_seconds() / 60.0
    except Exception:
        return 1e9


# ---------------- TASK LEDGER ----------------
def task_open(H, root, *, kind, parent, assignee, project="", claim="", exp="", session="", summary=""):
    """Allocate an incrementing TASK-id and write the task record. Returns task_id."""
    tid = H["next_id"](root, "tasks", "TASK")
    now = H["NOW"]()
    obj = {
        "task_id": tid, "kind": kind, "parent": parent or "", "assignee": assignee or "",
        "session_id": session or "", "project_id": project or "", "claim_id": claim or "",
        "exp_id": exp or "", "status": "open", "summary": summary or "",
        "evidence_delta": "", "opened_at": now, "updated_at": now, "closed_at": "",
        "history": [{"ts": now, "event": "open", "by": parent or "", "note": summary or ""}],
    }
    path = os.path.join(H["reg_dir"](root, "tasks"), f"{tid}.yaml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    H["dump_yaml"](path, obj)
    return tid, path


def _task_path(H, root, tid):
    d = H["reg_dir"](root, "tasks")
    return os.path.join(d, f"{tid}.yaml")


def task_update(H, root, tid, *, assignee=None, status=None, session=None, evidence_delta=None, by="", note=""):
    p = _task_path(H, root, tid)
    rec = H["load_yaml"](p, {}) or {}
    if not rec.get("task_id"):
        raise SystemExit(f"❌ {tid} not found")
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
    live_fresh = {}
    for a in agents:
        if a.get("status") in ALIVE_STATES and a["_age"] <= grace_min:
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
            H["dump_yaml"](a["_fn"], a2)
            # orphan this agent's open tasks
            for t in task_list(H, root):
                if t.get("assignee") == a.get("agent_id") and t.get("status") in ("open", "active"):
                    task_update(H, root, t["task_id"], status="orphaned", by="reaper",
                                note=f"assignee {a.get('agent_id')} {new_status}")
                    notifs.append(("TASK_ORPHANED", t["task_id"], t.get("project_id", "")))
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
        _notify(H, root, rd, kind="RESEED_NEEDED", agent=frm, project=project, role="researcher",
                detail=f"seeder dead/ready-to-reseed: {seeder_state}; open_work={open_work}")
    return rec


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
