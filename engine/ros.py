#!/usr/bin/env python3
"""
ros.py — research-os engine CLI. Platform-agnostic. Runs IN an instance repo (cwd = instance root,
or pass --instance). Holds the two hard invariants:
  (1) no experiment runs unregistered  -> `ros exp register` is the only door; it mints EXP-id.
  (2) no cemetery idea resurrects       -> `ros seed new` refuses near-duplicates of DEAD-* entries.

Dependencies: pyyaml only. Python 3.9+.
"""
import argparse, os, sys, re, datetime, glob, hashlib

# v2: shared generic queue/inbox abstraction (engine/channeling). Import shim so it resolves whether
# ros.py is run by path or imported. Falls back to None if unavailable (older checkouts) — callers guard.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from channeling import Channel
except Exception:
    Channel = None

def _yaml():
    try:
        import yaml; return yaml
    except ImportError:
        sys.exit("ERROR: pyyaml required -> pip install pyyaml")

YAML = _yaml()
NOW = lambda: datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def inst_root(args):
    return os.path.abspath(getattr(args, "instance", None) or os.getcwd())

def reg_dir(root, *p): return os.path.join(root, "registry", *p)

def load_yaml(path, default=None):
    if not os.path.exists(path): return default
    # BUG-10 fix: parse-tolerant — a torn/corrupt file must not crash callers (e.g. ros report).
    try:
        with open(path) as f: return YAML.safe_load(f) or default
    except Exception:
        # salvage: keep only lines up to the first parse break (drops stray trailing bytes / dup keys)
        try:
            txt = open(path, errors="ignore").read()
            for cut in range(len(txt), 0, -1):
                try:
                    v = YAML.safe_load(txt[:cut])
                    if isinstance(v, (dict, list)): return v
                except Exception:
                    continue
        except Exception:
            pass
        return default if default is not None else {}

def dump_yaml(path, obj):
    # BUG-10 fix: ATOMIC write (tmp + os.replace) so concurrent heartbeat/report can't tear the file.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import tempfile
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            YAML.safe_dump(obj, f, sort_keys=False, default_flow_style=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)   # atomic on POSIX
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        raise

def find_obj(root, kind, oid):
    """Find OID.yaml anywhere under registry/<kind>/ (now nested PROJ-x/<date>/). Returns path or None."""
    hits = glob.glob(os.path.join(reg_dir(root, kind), "**", f"{oid}.yaml"), recursive=True)
    return hits[0] if hits else None

def obj_dir(root, kind, project_id, date=None):
    """Target dir for a NEW object: registry/<kind>/<PROJ>/<date>/."""
    import datetime as _d
    date = _valid_date(date)
    d = os.path.join(reg_dir(root, kind), project_id or "PROJ-0000", date)
    os.makedirs(d, exist_ok=True); return d

def _valid_date(date=None):
    """Return a sane UTC YYYY-MM-DD. BUG-27 fix: reject malformed/sentinel/out-of-window dates
    (e.g. a stray --date 9999-01-01 created a phantom verdicts/<PROJ>/9999-01-01/ dir). A supplied
    date must parse as YYYY-MM-DD and fall within [today-30d, today+1d] UTC; otherwise fall back to
    UTC today (with a stderr warning) so no future/garbage date folders can ever be minted again."""
    import datetime as _d
    today = _d.datetime.now(_d.timezone.utc).date()
    if not date:
        return today.strftime("%Y-%m-%d")
    try:
        d = _d.datetime.strptime(str(date), "%Y-%m-%d").date()
    except Exception:
        sys.stderr.write(f"⚠️ ignoring malformed --date '{date}' (need YYYY-MM-DD); using UTC today.\n")
        return today.strftime("%Y-%m-%d")
    if d > today + _d.timedelta(days=1) or d < today - _d.timedelta(days=30):
        sys.stderr.write(f"⚠️ ignoring out-of-window --date '{date}' (not within today±); using UTC today.\n")
        return today.strftime("%Y-%m-%d")
    return d.strftime("%Y-%m-%d")

def next_id(root, kind, prefix):
    """Next zero-padded id. Experiments live as experiments/<date>/<EXP-id>/ (dir names);
    other objects as registry/<kind>/**/PREFIX-*.yaml. Scan the RIGHT place to avoid id reuse.

    BUG-52 fix: the old (max-scan + return) was NON-ATOMIC — concurrent callers (two sub-monitors/
    orchestrators seeding/verdicting at once) all saw the same max and returned the SAME id, then
    silently clobbered each other (5 parallel `seed new` -> all CLAIM-0001, 4 lost). Now we serialize
    allocation under a per-(root,kind) lockfile AND persist a reservation high-watermark, so an id is
    never handed out twice even before the caller has written its file."""
    import time as _t
    rd = runtime_dir(root); lockdir = os.path.join(rd, "locks"); os.makedirs(lockdir, exist_ok=True)
    lockpath = os.path.join(lockdir, f"next_id.{kind}.{prefix}.lock")
    resv_path = os.path.join(lockdir, f"next_id.{kind}.{prefix}.reserved")
    # acquire exclusive lock (spin with timeout; stale-lock break after 30s)
    fd = None
    for _ in range(600):
        try:
            fd = os.open(lockpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644); break
        except FileExistsError:
            try:
                if _t.time() - os.path.getmtime(lockpath) > 30: os.unlink(lockpath); continue
            except OSError: pass
            _t.sleep(0.05)
    try:
        mx = 0
        if kind == "experiments":
            for dn in glob.glob(os.path.join(root, "experiments", "**", f"{prefix}-*"), recursive=True):
                m = re.search(rf"{prefix}-(\d+)", os.path.basename(dn.rstrip("/")))
                if m: mx = max(mx, int(m.group(1)))
        else:
            d = reg_dir(root, kind); os.makedirs(d, exist_ok=True)
            for fn in glob.glob(os.path.join(d, "**", f"{prefix}-*.yaml"), recursive=True):
                m = re.search(rf"{prefix}-(\d+)", os.path.basename(fn))
                if m: mx = max(mx, int(m.group(1)))
        # fold in the persisted reservation high-watermark (covers the window before caller writes its file)
        try:
            rv = int(open(resv_path).read().strip())
        except Exception:
            rv = 0
        nxt = max(mx, rv) + 1
        try:
            with open(resv_path, "w") as f: f.write(str(nxt))
        except OSError: pass
        return f"{prefix}-{nxt:04d}"
    finally:
        if fd is not None:
            try: os.close(fd); os.unlink(lockpath)
            except OSError: pass

def _fingerprint(text):
    """Normalized keyword set for cemetery dup detection."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    stop = {"the","a","an","of","for","to","is","are","and","or","in","on","with","that","this","by","as","be","it"}
    return set(w for w in words if w not in stop and len(w) > 2)

# ---------------- INVARIANT 2: cemetery dup-check ----------------
def cemetery_conflict(root, claim_text):
    fp = _fingerprint(claim_text)
    if not fp: return None
    best = None
    for fn in glob.glob(reg_dir(root, "cemetery", "**", "DEAD-*.yaml"), recursive=True):
        dead = load_yaml(fn, {})
        cand = set()
        for pat in (dead.get("duplicate_patterns") or []): cand |= _fingerprint(pat)
        cand |= _fingerprint(dead.get("original_claim", ""))
        if not cand: continue
        # asymmetric containment OR jaccard — catches rephrasings that drop/add words
        jacc = len(fp & cand) / max(1, len(fp | cand))
        contain = len(fp & cand) / max(1, min(len(fp), len(cand)))
        score = max(jacc, contain)
        if best is None or score > best["score"]:
            tier = "hard" if score >= 0.7 else ("soft" if score >= 0.45 else None)
            if tier:
                best = {"dead_id": dead.get("dead_id"), "score": round(score,2), "tier": tier,
                        "reason_killed": dead.get("reason_killed"),
                        "revival": dead.get("revival_conditions"), "file": fn}
    return best

def cmd_seed_new(args):
    root = inst_root(args)
    claim_text = args.claim
    conflict = cemetery_conflict(root, claim_text)
    if conflict and not args.force_revive:
        tier = conflict["tier"]
        if tier == "hard" or (tier == "soft" and not args.ack_dup):
            print(f"❌ REFUSED ({tier} match): resembles dead idea {conflict['dead_id']} (score {conflict['score']}).")
            print(f"   reason killed: {conflict['reason_killed']}")
            print(f"   revival conditions: {conflict['revival']}")
            if tier == "soft":
                print(f"   If genuinely different, re-run with --ack-dup to confirm you checked.")
            print("   If you have NEW evidence meeting the revival conditions, re-run with --force-revive '<evidence>'.")
            sys.exit(2)
        print(f"   ⚠️ soft cemetery match to {conflict['dead_id']} (score {conflict['score']}) — acknowledged via --ack-dup.")
    cid = next_id(root, "claims", "CLAIM")
    obj = {
        "claim_id": cid, "project_id": args.project or "PROJ-0000",
        "stage": "seed", "status": "seed", "claim": claim_text,
        "why_it_matters": args.why or "", "novelty_hypothesis": "",
        "mandatory_baselines": [], "academic_map_nodes": [],
        "supporting_evidence": [], "negative_evidence": [], "verdict_history": [],
        "revival_conditions": "", "owner": args.owner or "", "prompt_version": args.prompt_version or "",
        "lifecycle_state": "drafted", "next_action": "prior-art + skeptic check, then design a bounded probe",
        "blocking_on": "", "active_experiments": [],
        "created_at": NOW(), "last_updated": NOW(),
    }
    if conflict and args.force_revive:
        obj["revival_conditions"] = f"REVIVED from {conflict['dead_id']} with new evidence: {args.force_revive}"
    path = os.path.join(obj_dir(root, "claims", obj["project_id"]), f"{cid}.yaml")
    dump_yaml(path, obj)
    print(f"✅ {cid} created -> {os.path.relpath(path, root)}")
    if conflict: print(f"   ⚠️ force-revived past {conflict['dead_id']}")

# ---------------- INVARIANT 1: experiment registration ----------------
def cmd_exp_register(args):
    root = inst_root(args)
    # claim must exist (unless explicitly infra probe)
    if args.claim and args.claim != "none":
        cf = find_obj(root, "claims", args.claim)
        if not cf:
            sys.exit(f"❌ claim {args.claim} not found. Register the claim/seed first.")
    eid = next_id(root, "experiments", "EXP")
    # BUG-5 fix: inherit project_id from the claim (not PROJ-0000 placeholder)
    proj = args.project
    if not proj and args.claim and args.claim != "none":
        _claim = load_yaml(find_obj(root, "claims", args.claim))
        if _claim: proj = _claim.get("project_id")
    proj = proj or "PROJ-0000"
    # BUG-5 fix: needs_gpu is explicit (--gpu) OR a positive gpu-hours budget; CPU/level-0 default = no GPU
    needs_gpu = bool(getattr(args, "gpu", False)) or (args.max_gpu_hours or 0) > 0
    obj = {
        "exp_id": eid, "project_id": proj,
        "claim_id": (None if args.claim=="none" else args.claim),
        "session_id": args.session or "", "level": args.level, "hardware": args.hardware or ("cpu" if not needs_gpu else ""),
        "resource_budget": {"max_wall_clock_minutes": args.max_minutes, "max_gpu_hours": args.max_gpu_hours,
                            "max_memory_gb": args.max_mem_gb, "host_mem_floor_gb": args.host_mem_floor},
        "needs_gpu": needs_gpu, "dispatched_by": "", "node_lease": "",
        "status": "pending", "result_summary": "", "result_effect": "",
        "artifacts_path": "", "reproducibility": "", "linked_verdicts": [],
        "started_at": "", "completed_at": "", "prompt_version": args.prompt_version or "",
    }
    import datetime as _d
    _date = _d.datetime.now(_d.timezone.utc).strftime("%Y-%m-%d")
    _ed = os.path.join(root, "experiments", _date, eid)
    obj["artifacts_path"] = f"experiments/{_date}/{eid}/"
    os.makedirs(os.path.join(_ed, "results"), exist_ok=True)
    os.makedirs(os.path.join(_ed, "logs"), exist_ok=True)
    dump_yaml(os.path.join(_ed, "experiment.yaml"), obj)
    # lifecycle: mark the claim as having a running experiment (resume pointer)
    if args.claim and args.claim != "none":
        _cf = find_obj(root, "claims", args.claim); _c = load_yaml(_cf) if _cf else None
        if _c:
            _c["lifecycle_state"] = "experiment_running"
            _c.setdefault("active_experiments", [])
            if eid not in _c["active_experiments"]: _c["active_experiments"].append(eid)
            _c["next_action"] = f"await {eid} result, then ros exp complete --exp {eid} --effect ..."
            _c["last_updated"] = NOW(); dump_yaml(_cf, _c)
    gpu = obj["needs_gpu"]
    print(f"✅ {eid} registered (level {args.level}, {'GPU' if gpu else 'CPU-only'}). Artifact dir: {obj['artifacts_path']}")
    print(f"   budget: {obj['resource_budget']}")
    if gpu:
        print(f"   ⚠️ GPU experiment — researchers CANNOT run this directly. Orchestrator must:"
              f" `ros exp dispatch --exp {eid} --node <node>` (gated on safety/budget/fragile).")
    if args.level >= 2:
        print(f"   ⚠️ level>=2 also requires orchestrator approval.")

def cmd_exp_complete(args):
    root = inst_root(args)
    hits = glob.glob(os.path.join(root, "experiments", "**", args.exp, "experiment.yaml"), recursive=True)
    ef = hits[0] if hits else None
    exp = load_yaml(ef) if ef else None
    if not exp: sys.exit(f"❌ {args.exp} not found.")
    VALID = {"kill","weaken","keep-exploring","promote","archive","support"}
    if args.effect not in VALID: sys.exit(f"❌ effect must be one of {VALID}")
    exp["status"] = "completed"; exp["result_effect"] = args.effect
    exp["result_summary"] = args.summary; exp["completed_at"] = NOW()
    dump_yaml(ef, exp)
    # ★ BUG-60 FIX (anti-sprawl / false-DEAD): when a researcher finishes its experiment, mark the OWNING
    # RESEARCHER AGENT 'completed' if it has no other pending/running experiment. Previously exp complete
    # updated the experiment + claim but NEVER the agent, so a finished researcher lingered status:running,
    # went past-grace, and the reaper flagged it DEAD (a false coverage gap -> spurious AGENT_DOWN + a
    # tempting respawn of a researcher whose work was already done). Owner is taken from --by (explicit,
    # no fragile id string-matching) or the experiment's recorded session/dispatched_by if it maps to an agent.
    rd_ = runtime_dir(root)
    owner = getattr(args, "by", None) or exp.get("ran_by") or exp.get("dispatched_by") or ""
    if owner:
        ap = os.path.join(rd_, "agents", f"{owner}.yaml")
        if os.path.exists(ap):
            ag = load_yaml(ap, {}) or {}
            # only auto-complete a RESEARCHER (never a sub-monitor/orchestrator/coordinator) and only if
            # it has no OTHER still-open experiment (don't kill an agent mid-second-experiment).
            is_researcher = (ag.get("role") == "researcher") or ("researcher" in (ag.get("agent_id") or ""))
            other_open = False
            for eo in glob.glob(os.path.join(root, "experiments", "**", "experiment.yaml"), recursive=True):
                eob = load_yaml(eo, {}) or {}
                if eob.get("exp_id") == args.exp: continue
                if (eob.get("ran_by") == owner or eob.get("dispatched_by") == owner) and eob.get("status") in ("pending","running","dispatched","faulted"):
                    other_open = True; break
            if is_researcher and ag.get("status") in ("running","active","registered","") and not other_open:
                ag["status"] = "completed"; ag["completed_at"] = NOW()
                ag["completed_note"] = f"EXP {args.exp} terminal ({args.effect}); researcher work done"
                dump_yaml(ap, ag)
                print(f"   researcher {owner} -> completed (EXP-terminal; not a reap-DEAD false-flag)")
                # ★ BUG-60b: close this researcher's OPEN tasks (the work they represent is done) so the
                # task ledger doesn't accumulate stale-open tasks for finished researchers. Done via the
                # supervise helper (validates + appends history). Failures here are non-fatal (best-effort).
                try:
                    import supervise as _sup; _H=_sup_helpers()
                    for _t in _sup.task_list(_H, root):
                        if _t.get("assignee")==owner and _t.get("status") in ("open","active"):
                            _sup.task_update(_H, root, _t["task_id"], status="done", by="exp-complete",
                                             note=f"assignee {owner} completed EXP {args.exp} ({args.effect})",
                                             _internal=True)
                            print(f"   closed task {_t['task_id']} (assignee {owner} done)")
                except Exception as _e:
                    pass
    # BUG-54 fix: release the GPU node lease this exp held (dispatch recorded it). Without this the node
    # stays leased forever after the exp completes -> all future dispatches refused / node looks BUSY.
    _lease = exp.get("node_lease", "")
    if _lease and ":" in _lease:
        _lnode = _lease.split(":", 1)[0]
        q = load_yaml(_gpu_queue_path(root), {"queue": [], "leases": {}}) or {"queue": [], "leases": {}}
        if q.get("leases", {}).get(_lnode) == exp.get("exp_id"):
            q["leases"].pop(_lnode, None); dump_yaml(_gpu_queue_path(root), q)
    # propagate to claim ledger
    cid = exp.get("claim_id")
    note = []
    if cid:
        cf = find_obj(root, "claims", cid); claim = load_yaml(cf) if cf else None
        if claim:
            # BUG-25 GUARD: never let a stray experiment KILL/weaken a PROMOTED (6/6-GREEN) claim without
            # explicit override. A promoted claim is near-immutable; a mis-bound exp must not erase a top result.
            if args.effect in ("kill","weaken") and claim.get("status")=="promoted" and not args.force_demote:
                sys.exit(f"❌ REFUSED: {cid} is PROMOTED (6/6 GREEN) — a '{args.effect}' experiment will not "
                         f"auto-demote it. If this exp genuinely overturns a promoted result, re-run with "
                         f"--force-demote (explicit) AND it should go through a fresh committee, not a lone exp. "
                         f"(Likely cause: the experiment was mis-bound to {cid}; rebind it to the correct claim.)")
            entry = {"exp_id": args.exp, "summary": args.summary, "data_path": exp["artifacts_path"]}
            if args.effect in ("kill","weaken"): claim["negative_evidence"].append(entry)
            else: claim["supporting_evidence"].append(entry)
            if args.effect == "kill": claim["status"] = "killed"
            elif args.effect == "weaken": claim["status"] = "weakened"
            elif args.effect == "promote": claim["status"] = "promoted"
            # lifecycle: experiment done -> evidence_ready (awaiting review) unless killed
            claim.setdefault("active_experiments", [])
            if args.exp in claim["active_experiments"]: claim["active_experiments"].remove(args.exp)
            if args.effect == "kill":
                claim["lifecycle_state"] = "done"; claim["next_action"] = "killed -> cemetery; no further action"
            else:
                claim["lifecycle_state"] = "evidence_ready"
                claim["next_action"] = f"review evidence from {args.exp}; convene committee if candidate-grade"
            claim["last_updated"] = NOW()
            dump_yaml(cf, claim)
            note.append(f"claim {cid} -> {claim['status']}")
            # auto-cemetery on kill
            if args.effect == "kill":
                did = next_id(root, "cemetery", "DEAD")
                dump_yaml(os.path.join(obj_dir(root,"cemetery",claim.get("project_id","PROJ-0001")), f"{did}.yaml"), {
                    "dead_id": did, "original_claim_id": cid, "original_claim": claim.get("claim",""),
                    "reason_killed": args.summary, "killing_verdict": "", "killing_experiments": [args.exp],
                    "duplicate_patterns": [claim.get("claim",""), " ".join(sorted(_fingerprint(claim.get("claim",""))))], "revival_conditions": args.revival or "",
                    "related_prior_work": [], "date_killed": NOW()})
                note.append(f"buried as {did}")
    cm = f"[{args.exp}]" + (f"[{cid}]" if cid else "") + f" commit-and-{args.effect}: {args.summary}"
    print(f"✅ {args.exp} completed: {args.effect}")
    if note: print("   " + "; ".join(note))
    print(f"   COMMIT MESSAGE:\n   {cm}")

def cmd_status(args):
    root = inst_root(args)
    def count(kind, pre): return len(glob.glob(reg_dir(root, kind, "**", f"{pre}-*.yaml"), recursive=True))
    print(f"research-os instance: {root}")
    print(f"  claims:      {count('claims','CLAIM')}")
    print(f"  experiments: {len(glob.glob(os.path.join(root,'experiments','**','experiment.yaml'),recursive=True))}")
    print(f"  verdicts:    {count('verdicts','VERDICT')}")
    print(f"  cemetery:    {count('cemetery','DEAD')}")
    # claim status breakdown
    by = {}
    for fn in glob.glob(reg_dir(root,'claims','**','CLAIM-*.yaml'), recursive=True):
        c = load_yaml(fn,{}); by[c.get('status','?')] = by.get(c.get('status','?'),0)+1
    if by: print("  claim status:", ", ".join(f"{k}={v}" for k,v in sorted(by.items())))

def cmd_init(args):
    root = inst_root(args)
    for d in ["registry/claims","registry/verdicts","registry/cemetery",
              "experiments","prior_art","sessions","learning","projects"]:
        os.makedirs(os.path.join(root,d), exist_ok=True)
        open(os.path.join(root,d,".gitkeep"),"a").close()
    for f,seed in [("registry/academic_map.yaml",{"nodes":[]}),("registry/baselines.yaml",{"families":[]})]:
        p=os.path.join(root,f)
        if not os.path.exists(p): dump_yaml(p, seed)
    print(f"✅ initialized research-os instance at {root}")
    print("   next: create research-os.config.yaml (see engine repo schemas/config.schema.yaml)")


# ============================ runtime: env / agents / liveness ============================








# ============================ runtime: env / agents / liveness ============================
def runtime_dir(root):
    cfg = load_yaml(os.path.join(root, "research-os.config.yaml"), {}) or {}
    rd = (cfg.get("runtime") or {}).get("runtime_dir")
    return os.path.abspath(rd) if rd else os.path.join(root, "runtime")

def _cfg(root):
    return load_yaml(os.path.join(root, "research-os.config.yaml"), {}) or {}

def cmd_env_discover(args):
    """Capability discovery: structure config into a capability inventory; optionally probe live.
    Generic — probe commands come from config, not hardcoded. Writes runtime/orchestrator/capabilities.yaml."""
    root = inst_root(args); cfg = _cfg(root)
    rd = runtime_dir(root); os.makedirs(os.path.join(rd, "orchestrator"), exist_ok=True)
    backends = {}
    for m in (cfg.get("committee") or {}).get("members", []):
        if m.get("backend"): backends[m["backend"]] = {"roles": [m["role"]], "status": "declared"}
    for b in (cfg.get("session_agent_backends") or []):
        backends.setdefault(b, {"roles": [], "status": "declared"})
    nodes = []
    total_gpu = 0; total_mem = 0
    for n in (cfg.get("compute_nodes") or []):
        rec = dict(n); rec.setdefault("status", "declared")
        if args.probe and n.get("probe_cmd"):
            import subprocess
            try:
                out = subprocess.run(n["probe_cmd"], shell=True, capture_output=True, text=True, timeout=30)
                rec["status"] = "alive" if out.returncode == 0 else "unreachable"
                rec["probe_out"] = (out.stdout or out.stderr)[-300:]
            except Exception as e:
                rec["status"] = "unreachable"; rec["probe_out"] = str(e)[:200]
        nodes.append(rec)
        total_gpu += int(n.get("gpu_count", 0) or 0); total_mem += int(n.get("host_mem_gb", 0) or 0)
    cap = {
        "discovered_at": NOW(),
        "control_node": (cfg.get("runtime") or {}).get("control_node", ""),
        "backends": backends,
        "compute_nodes": nodes,
        "resource_envelope": {"total_gpus": total_gpu, "total_host_mem_gb": total_mem,
                              "manageable_levels": list((cfg.get("budgets") or {}).keys())},
        "budgets": cfg.get("budgets", {}),
        "liveness": cfg.get("liveness", {}),
        "safety": cfg.get("safety", {}),
    }
    p = os.path.join(rd, "orchestrator", "capabilities.yaml"); dump_yaml(p, cap)
    print(f"✅ env discovered -> {os.path.relpath(p, root)}")
    print(f"   backends: {len(backends)} ({', '.join(backends) or 'none — set in config'})")
    print(f"   nodes: {len(nodes)}  envelope: {cap['resource_envelope']['total_gpus']} GPU, "
          f"{cap['resource_envelope']['total_host_mem_gb']} GB host RAM")
    if args.probe: 
        for n in nodes: print(f"     - {n.get('name','?')}: {n.get('status')}")

def _gpu_window_open(root):
    """Time-boxed GPU AUTO-APPROVE window (operator opt-in). When open, GPU exps may enter the queue/dispatch
    WITHOUT a committee_approved verdict (the committee scientific gate is waived for the window). The fragile-
    node host_mem_floor watchdog is NEVER waived (hardware safety, not a gate). Returns (open:bool, until:str)."""
    import datetime as _d
    p = os.path.join(runtime_dir(root), "gpu_approve_window.yaml")
    w = load_yaml(p, {}) or {}
    until = w.get("until", "")
    if not until:
        return (False, "")
    try:
        exp = _d.datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_d.timezone.utc)
        return ((_d.datetime.now(_d.timezone.utc) <= exp), until)
    except Exception:
        return (False, until)

def cmd_gpu_approve_window(args):
    """Operator: open/close a time-boxed GPU auto-approve window. `--hours N` opens for N hours from now;
    `--clear` closes it. While open, GPU exps need NO committee_approved to queue/dispatch (host_mem_floor
    safety still enforced). Logs the operator intent."""
    import datetime as _d
    p = os.path.join(runtime_dir(root := inst_root(args)), "gpu_approve_window.yaml")
    if args.clear:
        dump_yaml(p, {"until": "", "set_at": NOW(), "set_by": args.by or "operator", "note": "CLEARED"})
        print("✅ GPU auto-approve window CLOSED."); return
    hours = args.hours if args.hours else 24
    until = (_d.datetime.now(_d.timezone.utc) + _d.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dump_yaml(p, {"until": until, "set_at": NOW(), "set_by": args.by or "operator", "hours": hours,
                  "note": args.note or "all GPU exps auto-approved for the window (host_mem_floor still enforced)"})
    print(f"✅ GPU AUTO-APPROVE window OPEN for {hours}h — until {until} UTC.")
    print(f"   GPU exps may queue/dispatch WITHOUT committee_approved until then. Fragile-node host_mem_floor still REQUIRED.")

def cmd_agent_register(args):
    root = inst_root(args); rd = runtime_dir(root)
    d = os.path.join(rd, "agents"); os.makedirs(d, exist_ok=True)
    rec = {"agent_id": args.id, "role": args.role, "backend": args.backend or "",
           "node": args.node or "", "status": "running", "spawned_at": NOW(),
           "last_heartbeat": NOW(), "current_claim_id": args.claim or "", "current_exp_id": args.exp or "",
           "project_id": getattr(args, "project", "") or "", "session_id": getattr(args, "session", "") or "",
           "parent": getattr(args, "parent", "") or "",
           "gpu": getattr(args, "gpu", "") or "", "note": "", "heartbeat_count": 0}
    dump_yaml(os.path.join(d, f"{args.id}.yaml"), rec)
    print(f"✅ agent {args.id} registered (role={args.role}"
          + (f", parent={args.parent}" if getattr(args, 'parent', '') else "")
          + f"). Heartbeat: `ros heartbeat --agent {args.id}` every ~{(_cfg(root).get('liveness') or {}).get('kick_interval_minutes',15)}m")

def cmd_heartbeat(args):
    root = inst_root(args); rd = runtime_dir(root)
    p = os.path.join(rd, "agents", f"{args.agent}.yaml")
    rec = load_yaml(p) or {}
    if not rec.get("agent_id"):
        # tolerate heartbeat-before-register (auto-create minimal); keep any role we have
        rec.setdefault("agent_id", args.agent); rec.setdefault("spawned_at", NOW()); rec.setdefault("heartbeat_count", 0)
    # BUG-10 fix: never downgrade an existing role to 'unknown'; only set from --role or keep prior
    if args.role: rec["role"] = args.role
    elif not rec.get("role"): rec["role"] = "unknown"
    rec["last_heartbeat"] = NOW()
    rec["heartbeat_count"] = int(rec.get("heartbeat_count", 0)) + 1
    if args.status: rec["status"] = args.status
    if args.note: rec["note"] = args.note
    if args.claim: rec["current_claim_id"] = args.claim
    if args.exp: rec["current_exp_id"] = args.exp
    if getattr(args, "project", None): rec["project_id"] = args.project
    if getattr(args, "session", None): rec["session_id"] = args.session
    if getattr(args, "gpu", None): rec["gpu"] = args.gpu
    dump_yaml(p, rec)
    print(f"💓 {args.agent} heartbeat #{rec['heartbeat_count']} ({rec.get('status','?')})")

def cmd_liveness(args):
    """Patient liveness report. alive = kicked within kick_interval; stale = within grace; dead = past grace.
    The orchestrator/monitor reads this. NEVER declares dead before the grace window."""
    root = inst_root(args); rd = runtime_dir(root); cfg = _cfg(root)
    liv = cfg.get("liveness", {}) or {}
    kick = int(liv.get("kick_interval_minutes", 15)); grace = int(liv.get("patient_grace_minutes", 45))
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    rows = []
    for fn in sorted(glob.glob(os.path.join(rd, "agents", "*.yaml"))):
        a = load_yaml(fn, {})
        try:
            last = _dt.datetime.strptime(a.get("last_heartbeat",""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
            age = (now - last).total_seconds() / 60.0
        except Exception:
            age = 1e9
        # BUG-7 fix: terminal states (completed/failed) are RETIRED — never flagged for revival
        if a.get("status") in ("completed","failed","retired"):
            state = "retired"
        else:
            state = "alive" if age <= kick*1.5 else ("stale" if age <= grace else "DEAD")
        rows.append((a.get("agent_id","?"), a.get("role","?"), a.get("status","?"), round(age,1), state))
    if not rows: print("(no agents registered)"); return
    print(f"liveness (kick={kick}m, patient grace={grace}m):")
    for aid, role, st, age, state in rows:
        mark = {"alive":"💓","stale":"⏳","DEAD":"☠️","retired":"🏁"}[state]
        print(f"  {mark} {aid:24} {role:20} {st:10} last={age}m -> {state}")
    dead = [r[0] for r in rows if r[4]=="DEAD"]
    if dead: print(f"  ⚠️ DEAD (past {grace}m grace, NOT retired — orchestrator should revive): {', '.join(dead)}")



def cmd_exp_dispatch(args):
    """ORCHESTRATOR-ONLY: trigger a GPU experiment onto a node, after the safety/budget/fragile gate.
    This is INVARIANT 3: researchers/committee never touch GPU directly; only the orchestrator dispatches."""
    root = inst_root(args); cfg = _cfg(root)
    hits = glob.glob(os.path.join(root, "experiments", "**", args.exp, "experiment.yaml"), recursive=True)
    ef = hits[0] if hits else None; exp = load_yaml(ef) if ef else None
    if not exp: sys.exit(f"❌ {args.exp} not found.")
    if exp.get("status") not in ("pending", "faulted"): sys.exit(f"❌ {args.exp} is '{exp.get('status')}', not dispatchable (only pending/faulted).")
    # find the node in config
    node = next((n for n in (cfg.get("compute_nodes") or []) if n.get("name")==args.node), None)
    if not node: sys.exit(f"❌ node '{args.node}' not in config compute_nodes.")
    budget = exp.get("resource_budget", {})
    floor = budget.get("host_mem_floor_gb", 0) or 0
    # SAFETY GATE
    if node.get("fragile") and floor <= 0:
        sys.exit(f"❌ SAFETY: node '{args.node}' is fragile and exp {args.exp} has no host_mem_floor_gb. "
                 f"Re-register with --host-mem-floor (watchdog) before dispatch. (See learning/ postmortems.)")
    lvl = exp.get("level", 0)
    # FULLY AUTONOMOUS (no human gate). A GPU exp must be COMMITTEE-APPROVED to be dispatchable —
    # UNLESS the operator opened a time-boxed GPU auto-approve window (then the committee gate is waived).
    _win, _until = _gpu_window_open(inst_root(args))
    if not (exp.get("committee_approved") or args.force or _win):
        sys.exit(f"❌ {args.exp} not committee_approved — enqueue via `ros gpu queue` only after a committee "
                 f"verdict approves the experiment (or --force, or open a window: ros gpu-approve-window --hours N).")
    if _win and not exp.get("committee_approved"):
        print(f"   ⏱️ GPU auto-approve window OPEN (until {_until}) — committee gate waived for {args.exp}.")
    # BUG-54 fix: dispatch must record the node lease in the SHARED gpu_queue leases map (the same map
    # `gpu status`/`gpu poll`/`gpu release` read) — otherwise the node shows FREE after dispatch and a
    # second dispatch double-books the GPU (critical on the fragile MI350X). Refuse if already leased.
    q = load_yaml(_gpu_queue_path(root), {"queue": [], "leases": {}}) or {"queue": [], "leases": {}}
    held = q.get("leases", {}).get(args.node)
    if held and held != exp.get("exp_id"):
        sys.exit(f"❌ SAFETY: node '{args.node}' already leased to {held} — refusing to double-book. "
                 f"Release it first (ros gpu release --node {args.node}) or wait for completion.")
    exp["status"] = "running"; exp["started_at"] = NOW()
    exp["dispatched_by"] = args.by or "orchestrator"
    exp["node_lease"] = f"{args.node}:{exp.get('exp_id')}"
    dump_yaml(ef, exp)
    q.setdefault("leases", {})[args.node] = exp.get("exp_id")
    dump_yaml(_gpu_queue_path(root), q)
    print(f"🚀 dispatched {args.exp} -> {args.node} ({node.get('gpu_type','?')}, fragile={bool(node.get('fragile'))})")
    print(f"   host_mem_floor={floor}GB  budget={budget}  by={exp['dispatched_by']}  lease recorded")
    print(f"   NOTE: enforce the host-RAM watchdog on BOTH allocation AND teardown; use os._exit().")



# ============================ progress reporting (researcher -> orchestrator) ============================
def cmd_report(args):
    """Researcher pushes a STRUCTURED PROGRESS REPORT (not just liveness). Appends to the agent's
    report log AND drops an entry in the orchestrator inbox. Bumps heartbeat. Mandatory every ~10m."""
    import json as _json
    root = inst_root(args); rd = runtime_dir(root)
    os.makedirs(os.path.join(rd, "reports"), exist_ok=True)
    os.makedirs(os.path.join(rd, "orchestrator"), exist_ok=True)
    rec = {"ts": NOW(), "agent": args.agent, "role": args.role or "",
           "done": args.done or "", "doing": args.doing or "", "blocked": args.blocked or "",
           "need": args.need or "", "next": args.next or "", "claim": args.claim or "", "exp": args.exp or ""}
    # append to per-agent report log
    with open(os.path.join(rd, "reports", f"{args.agent}.jsonl"), "a") as f:
        f.write(_json.dumps(rec) + "\n")
    # inbox entry for the orchestrator (questions/blocks flagged for action)
    needs_action = bool(args.blocked or args.need)
    _inbox_path = os.path.join(rd, "orchestrator", "inbox.yaml")
    if Channel is not None:
        # v2: route through the shared Channel (atomic, idempotent-capable). Preserves the {items:[...]}
        # shape + every existing key; adds envelope fields (id/submitted_at/...) which readers ignore.
        Channel(_inbox_path, list_key="items", id_prefix="IN").submit({**rec, "needs_action": needs_action})
    else:
        inbox = load_yaml(_inbox_path, {"items": []}) or {"items": []}
        inbox["items"].append({**rec, "acked": False, "needs_action": needs_action})
        dump_yaml(_inbox_path, inbox)
    # bump heartbeat too (a report IS liveness)
    hp = os.path.join(rd, "agents", f"{args.agent}.yaml"); hb = load_yaml(hp, {}) or {}
    hb.update({"agent_id": args.agent, "last_heartbeat": NOW(), "status": "running",
               "note": (args.doing or args.done or "")[:80],
               "heartbeat_count": int(hb.get("heartbeat_count", 0)) + 1,
               "last_report_ts": NOW()})
    if args.role: hb["role"] = args.role
    dump_yaml(hp, hb)
    flag = " ⚠️ NEEDS ORCHESTRATOR ACTION" if needs_action else ""
    print(f"📋 report logged for {args.agent}{flag}")
    if needs_action and args.ping_orchestrator:
        print(f"   (blocker/question flagged — orchestrator should be pinged via agent_run.message)")

def cmd_inbox(args):
    """ORCHESTRATOR reads pending researcher reports. --action-only shows just blocks/questions.
    This is what prevents the orchestrator from sitting idle: it polls progress + acts on needs."""
    root = inst_root(args); rd = runtime_dir(root)
    _inbox_path = os.path.join(rd, "orchestrator", "inbox.yaml")
    if Channel is not None:
        pred = (lambda i: i.get("needs_action")) if args.action_only else None
        items = Channel(_inbox_path, list_key="items", id_prefix="IN").list(predicate=pred)
    else:
        inbox = load_yaml(_inbox_path, {"items": []}) or {"items": []}
        items = [i for i in inbox["items"] if not i.get("acked")]
        if args.action_only: items = [i for i in items if i.get("needs_action")]
    if not items: print("(inbox empty — no pending reports)"); return
    print(f"ORCHESTRATOR INBOX — {len(items)} pending:")
    for i in items:
        tag = "❗ACTION" if i.get("needs_action") else "  info "
        print(f"  [{tag}] {i.get('ts','')} {i.get('agent','')} [{i.get('role','')}]")
        if i.get("done"):    print(f"          done:    {i['done']}")
        if i.get("doing"):   print(f"          doing:   {i['doing']}")
        if i.get("blocked"): print(f"          BLOCKED: {i['blocked']}")
        if i.get("need"):    print(f"          NEEDS:   {i['need']}")
        if i.get("next"):    print(f"          next:    {i['next']}")

def cmd_inbox_ack(args):
    root = inst_root(args); rd = runtime_dir(root)
    p = os.path.join(rd, "orchestrator", "inbox.yaml")
    if Channel is not None and args.all:
        n = Channel(p, list_key="items", id_prefix="IN").ack(all_items=True, answer=args.answer or "")
    else:
        # per-agent ack: Channel.ack keys on item id, not agent; keep the explicit loop for the agent filter.
        inbox = load_yaml(p, {"items": []}) or {"items": []}
        n = 0
        for i in inbox["items"]:
            if not i.get("acked") and (args.all or i.get("agent") == args.agent):
                i["acked"] = True; i["acked_at"] = NOW(); i["answer"] = args.answer or ""; n += 1
        dump_yaml(p, inbox)
    print(f"✅ acked {n} inbox item(s)" + (f" with answer: {args.answer}" if args.answer else ""))

def cmd_reports_age(args):
    """Monitor helper: which researchers have NOT reported within the window? (silent-researcher detector)"""
    import datetime as _dt
    root = inst_root(args); rd = runtime_dir(root); cfg = _cfg(root)
    win = args.window_min if args.window_min is not None else 10
    now = _dt.datetime.now(_dt.timezone.utc)
    silent = []
    for fn in glob.glob(os.path.join(rd, "agents", "*.yaml")):
        a = load_yaml(fn, {}) or {}
        role = a.get("role","")
        if not (role=="researcher" or "researcher" in a.get("agent_id","")):
            continue  # researchers only (unified role)
        lr = a.get("last_report_ts") or a.get("last_heartbeat","")
        try:
            age = (now - _dt.datetime.strptime(lr,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)).total_seconds()/60
        except: age = 1e9
        if age > win: silent.append((a.get("agent_id","?"), role, round(age,1)))
    if silent:
        print(f"⚠️ {len(silent)} researcher(s) SILENT >{win}m (should have reported):")
        for aid, role, age in silent: print(f"   - {aid} [{role}] last report {age}m ago")
    else:
        print(f"✅ all researchers reported within {win}m")


def _concurrent_invest_target(cfg):
    """GLOBAL concurrent-investment target: how many projects should be ACTIVELY investing at once.
    When the investing count drops below this (a project converges), the orchestrator designs a new
    project to refill to target. Prefer globals.concurrent_invest_projects; default 4."""
    g = (cfg.get("globals") or {}).get("concurrent_invest_projects")
    if g is not None:
        try: return int(g)
        except Exception: pass
    return 4

def _is_converged_project(d):
    """A project is CONVERGED (not counted toward the investing target) if it carries an explicit
    marker: a projects/<PROJ>/.converged file, OR `status: converged|closed|done` in its
    project_overview.md front-matter/first lines. Converged projects keep their folder (history is
    preserved) but free a slot so the orchestrator designs a replacement frontier."""
    if os.path.exists(os.path.join(d, ".converged")): return True
    ov = os.path.join(d, "project_overview.md")
    if os.path.exists(ov):
        try:
            with open(ov) as f:
                head = "".join(f.readline() for _ in range(12))
        except Exception:
            head = ""
        import re as _re
        if _re.search(r'(?im)^\s*status\s*:\s*(converged|closed|done|finalized)\b', head): return True
    return False

def cmd_projects(args):
    root=inst_root(args)
    pdir=os.path.join(root,"projects")
    found=sorted(glob.glob(os.path.join(pdir,"PROJ-*")))
    if not found: print("(no projects — create projects/<PROJ-id>/project_overview.md)"); return
    print(f"projects ({len(found)}):")
    investing=0; converged=[]
    for d in found:
        pid=os.path.basename(d)
        ov=os.path.join(d,"project_overview.md")
        title=""
        if os.path.exists(ov):
            for ln in open(ov):
                if ln.startswith("# "): title=ln[2:].strip(); break
        reports=sorted(glob.glob(os.path.join(d,"*","progress_report.md")))
        last=os.path.basename(os.path.dirname(reports[-1])) if reports else "no report"
        conv=_is_converged_project(d)
        if conv: converged.append(pid)
        else: investing+=1
        flag=" 🏁 CONVERGED" if conv else ""
        print(f"  {pid}: {title}  (latest progress: {last}){flag}")
    # GLOBAL concurrent-investment capacity signal (orchestrator refills to target by designing new projects)
    cfg=_cfg(root); target=_concurrent_invest_target(cfg)
    print(f"\nCONCURRENT INVESTMENT: {investing} investing / target {target}"
          + (f"  (converged: {', '.join(converged)})" if converged else ""))
    if investing < target:
        deficit=target-investing
        print(f"⚠️ BELOW TARGET by {deficit} — ORCHESTRATOR must DESIGN {deficit} new project(s) "
              f"(honest committee, bias agent-infra/inference-opt) and seed each to refill to {target}.")


def cmd_submonitors(args):
    """PROJECT DISCOVERY + sub-monitor health (ORCHESTRATOR duty). Enumerates every active project and
    reports the health of its sub-monitor. The orchestrator runs this on a cadence: any project whose
    sub-monitor is DEAD/stale/MISSING must get a fresh sub-monitor spawned by the ORCHESTRATOR (a dead
    sub-monitor cannot respawn itself — especially on context-window overflow before it could hand off).

    A sub-monitor that died WITHOUT a graceful-handoff audit (no recent 'handed off' report) is the
    context-overflow case: the orchestrator owns the respawn. Exit non-zero if any project needs action,
    so a wrapper/cron can branch on it."""
    import datetime as _dt
    root = inst_root(args); rd = runtime_dir(root); cfg = _cfg(root)
    liv = cfg.get("liveness", {}) or {}
    kick = int(liv.get("kick_interval_minutes", 15)); grace = int(liv.get("patient_grace_minutes", 45))
    now = _dt.datetime.now(_dt.timezone.utc)
    all_projects = sorted(glob.glob(os.path.join(root, "projects", "PROJ-*")))
    # CONVERGED projects (arc closed, .converged marker / status) need NO sub-monitor — exclude them from
    # the discovery list so they don't show as false "needs respawn". Only ACTIVE projects require coverage.
    converged_pids = [os.path.basename(p) for p in all_projects if _is_converged_project(p)]
    projects = [os.path.basename(p) for p in all_projects if not _is_converged_project(p)]
    # index sub-monitor agents by project (role==sub-monitor or 'sub-monitor'/'submonitor' in the id)
    sm_by_proj = {}
    for fn in glob.glob(os.path.join(rd, "agents", "*.yaml")):
        a = load_yaml(fn, {}) or {}
        aid = a.get("agent_id", ""); role = a.get("role", "")
        if role == "sub-monitor" or "sub-monitor" in aid or "submonitor" in aid:
            try:
                last = _dt.datetime.strptime(a.get("last_heartbeat",""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
                age = (now - last).total_seconds() / 60.0
            except Exception:
                age = 1e9
            pid = a.get("project_id", "") or _infer_proj_from_id(aid)
            # BUG-41 fix: prefer a LIVE sub-monitor over a retired/terminal one (status beats age).
            # The old "freshest by age" logic false-flagged "retired-no-successor" whenever a sub-monitor
            # retired in the SAME tick its successor registered (the just-retired r1 tied/beat the live r2).
            st = a.get("status", "?")
            alive = st not in ("completed", "retired", "failed", "dead", "superseded", "done", "dropped")
            cand = {"id": aid, "status": st, "age": round(age, 1), "alive": alive}
            cur = sm_by_proj.get(pid)
            def _better(new, old):
                if old is None: return True
                if new["alive"] != old["alive"]: return new["alive"]   # a live agent always wins
                return new["age"] < old["age"]                          # within same liveness class, freshest
            if _better(cand, cur):
                sm_by_proj[pid] = cand
    need = []
    print(f"SUB-MONITOR DISCOVERY (kick={kick}m, grace={grace}m) — {len(projects)} active project(s)"
          + (f"; {len(converged_pids)} converged (no sub-monitor needed): {', '.join(converged_pids)}" if converged_pids else "") + ":")
    for pid in projects:
        sm = sm_by_proj.get(pid)
        if not sm:
            print(f"  ❌ {pid}: NO sub-monitor — orchestrator must spawn one."); need.append((pid,"missing")); continue
        st = sm["status"]; age = sm["age"]
        if st in ("completed","retired"):
            # retired = should have handed off; only an issue if no successor took over (no fresh sub-monitor)
            print(f"  🏁 {pid}: {sm['id']} retired (last {age}m) — verify a successor exists; if not, respawn.")
            need.append((pid,"retired-no-successor"))
        elif st == "failed" or age > grace:
            # DEAD: did it hand off? a graceful retire leaves a 'handed off' report; absence => context-overflow death
            handed = _had_handoff(rd, sm["id"])
            tag = "DEAD" if age > grace else "failed"
            extra = "" if handed else " (NO handoff audit — likely context overflow; orchestrator owns respawn)"
            print(f"  ☠️ {pid}: {sm['id']} {tag} last={age}m{extra} — orchestrator MUST spawn a replacement.")
            need.append((pid,"dead"))
        elif age > kick*1.5:
            print(f"  ⏳ {pid}: {sm['id']} STALE last={age}m (within grace) — watch; respawn if it crosses {grace}m.")
        else:
            print(f"  💓 {pid}: {sm['id']} alive last={age}m.")
    if need:
        print(f"\n⚠️ {len(need)} project(s) need a sub-monitor (re)spawn by the ORCHESTRATOR:")
        for pid, why in need: print(f"   - {pid}: {why}")
        sys.exit(3)
    print("\n✅ every active project has a live sub-monitor.")

def _infer_proj_from_id(aid):
    m = re.search(r"(PROJ-\d+)", aid or "")
    if m: return m.group(1)
    m = re.search(r"(?:sub-?monitor|submonitor)[-_]?(?:p|proj)?(\d+)", aid or "", re.I)
    if m: return f"PROJ-{int(m.group(1)):04d}"
    return ""

def _had_handoff(rd, agent_id):
    """True if the sub-monitor filed a graceful-handoff report (its last report mentions handoff/retire).
    Absence on a DEAD sub-monitor => it died WITHOUT handing off (context-overflow case)."""
    import json as _json
    p = os.path.join(rd, "reports", f"{agent_id}.jsonl")
    if not os.path.exists(p): return False
    try:
        lines = [l for l in open(p) if l.strip()]
        for l in reversed(lines[-5:]):
            r = _json.loads(l)
            blob = " ".join(str(r.get(k,"")) for k in ("done","doing","next")).lower()
            if "hand" in blob and "off" in blob or "handed off" in blob or "retire" in blob:
                return True
    except Exception:
        pass
    return False

def _retire_pct(cfg):
    """GLOBAL retire-and-respawn context threshold for all long-running/standby agents (orchestrator,
    sub-monitor, gpu_coordinator). Prefer globals.retire_at_context_pct; fall back to
    research.sub_monitor.retire_at_context_pct; default 35."""
    g = (cfg.get("globals") or {}).get("retire_at_context_pct")
    if g is not None: return int(g)
    sm = ((cfg.get("research") or {}).get("sub_monitor") or {}).get("retire_at_context_pct")
    return int(sm) if sm is not None else 35

def cmd_lanes(args):
    """★ ANTI-SPRAWL (DESIGN_anti-sprawl_single-poller.md): emit the per-lane work plan for the SINGLE
    multi-lane POLLER that REPLACES N self-spawning sub-monitor sessions. READ-ONLY (computes a plan, does
    NOT spawn/mutate). For every ACTIVE (non-converged) project, reports the lane state + the ONE next
    action keyword the poller should act on:
      HOLD    = below floor, no open work -> NO-OP (correct; not a coverage gap; no make-work, no commit).
      AWAIT   = a researcher is running OR committee is pending -> just heartbeat this cycle.
      FORWARD = a researcher finished candidate-grade evidence (claim evidence_ready + no pending queue
                item) -> poller should `ros queue submit` it (observe+forward only; never judge).
      RESEED? = no live researcher AND the claim has open work (not terminal/not green) -> flag for the
                ORCHESTRATOR (the lane NEVER auto-respawns; orchestrator owns reseed via reaper notifs).
    Exit non-zero if any lane is FORWARD or RESEED? (so the poller wrapper can branch). One poller heartbeat
    replaces N sub-monitor heartbeats; idle (HOLD/AWAIT) lanes do nothing -> kills the commit-churn idleness."""
    import datetime as _dt
    root = inst_root(args); rd = runtime_dir(root); cfg = _cfg(root)
    liv = cfg.get("liveness", {}) or {}
    grace = int(liv.get("patient_grace_minutes", 45))
    now = _dt.datetime.now(_dt.timezone.utc)
    all_projects = sorted(glob.glob(os.path.join(root, "projects", "PROJ-*")))
    converged = set(os.path.basename(p) for p in all_projects if _is_converged_project(p))
    active = [os.path.basename(p) for p in all_projects if os.path.basename(p) not in converged]
    # index LIVE researchers by project
    researchers_by_proj = {}
    for fn in glob.glob(os.path.join(rd, "agents", "*.yaml")):
        a = load_yaml(fn, {}) or {}
        aid = a.get("agent_id", ""); role = a.get("role", "")
        if not (role == "researcher" or "researcher" in aid): continue
        pid = a.get("project_id", "") or _infer_proj_from_id(aid)
        try:
            last = _dt.datetime.strptime(a.get("last_heartbeat",""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
            age = (now - last).total_seconds()/60.0
        except Exception:
            age = 1e9
        st = a.get("status","?")
        alive = st not in ("completed","retired","failed","dead","superseded","done","dropped") and age <= grace
        cur = researchers_by_proj.setdefault(pid, {"alive": [], "any": []})
        cur["any"].append((aid, st, round(age,1)))
        if alive: cur["alive"].append((aid, st, round(age,1)))
    # pending committee-queue submissions by claim
    pending_claims = set()
    try:
        if Channel is not None:
            for it in _committee_channel(root).list(include_acked=False):
                if it.get("claim_id"): pending_claims.add(it["claim_id"])
    except Exception:
        pass
    # in-flight claims by project (lifecycle from registry)
    claims_by_proj = {}
    for fn in glob.glob(reg_dir(root,"claims","**","CLAIM-*.yaml"),recursive=True):
        c = load_yaml(fn, {}) or {}
        claims_by_proj.setdefault(c.get("project_id",""), []).append(c)
    actionable = []
    print(f"LANES — single-poller work plan ({len(active)} active project lane(s)"
          + (f"; {len(converged)} converged (no lane): {', '.join(sorted(converged))}" if converged else "") + "):")
    TERMINAL_LS = ("done",)
    for pid in active:
        cs = claims_by_proj.get(pid, [])
        live = researchers_by_proj.get(pid, {}).get("alive", [])
        # the lane's "open work" = any non-terminal, non-green in-flight claim
        open_claims = [c for c in cs if c.get("lifecycle_state") not in TERMINAL_LS and c.get("status") != "green"]
        evidence_ready = [c for c in cs if c.get("lifecycle_state") == "evidence_ready"
                          and c.get("claim_id") not in pending_claims]
        # ★ a verdict_recorded (committee already ruled: yellow / needs-more-evidence) is NOT the same as a
        # fresh drafted claim. RESEED? (spawn a new L0 researcher) is for claims with NO experiment yet;
        # a verdict_recorded claim needs an ORCHESTRATOR ADVANCE decision (dispatch a TARGETED follow-up
        # addressing the verdict's required_evidence, or accept the yellow as converged) — blindly
        # reseeding it re-runs work the committee already saw. Classify them separately so the poller/
        # orchestrator doesn't churn on yellows.
        verdict_open = [c for c in open_claims if c.get("lifecycle_state") == "verdict_recorded"]
        reseed_open  = [c for c in open_claims if c.get("lifecycle_state") != "verdict_recorded"]
        if evidence_ready:
            action = "FORWARD"
            detail = f"evidence_ready (not yet queued): {', '.join(c.get('claim_id','?') for c in evidence_ready)}"
        elif live:
            action = "AWAIT"
            detail = f"researcher(s) live: {', '.join(a for a,_,_ in live)}"
        elif any(c.get('claim_id') in pending_claims for c in cs):
            action = "AWAIT"
            detail = "committee submission pending in queue"
        elif reseed_open:
            action = "RESEED?"
            detail = ("no live researcher + un-experimented open work: "
                      + ', '.join(f"{c.get('claim_id','?')}[{c.get('lifecycle_state','?')}]" for c in reseed_open)
                      + " — ORCHESTRATOR decides reseed (lane does NOT auto-respawn)")
        elif verdict_open:
            action = "ADVANCE?"
            detail = ("verdict recorded, awaiting ORCHESTRATOR decision: "
                      + ', '.join(f"{c.get('claim_id','?')}[{(c.get('verdict_history') or [{}])[-1].get('result','?')}]" for c in verdict_open)
                      + " — dispatch TARGETED follow-up for required_evidence OR accept/converge (NOT a blind reseed)")
        else:
            action = "HOLD"
            detail = "below floor, no open work (correct; not a gap)"
        mark = {"FORWARD":"📤","AWAIT":"⏳","RESEED?":"⚠️","ADVANCE?":"🔬","HOLD":"·"}[action]
        print(f"  {mark} {pid}: {action} — {detail}")
        if action in ("FORWARD","RESEED?","ADVANCE?"): actionable.append((pid, action, detail))
    if actionable:
        print(f"\n{len(actionable)} actionable lane(s) for the poller:")
        for pid, act, det in actionable: print(f"   - {pid}: {act}")
        sys.exit(3)
    print("\n✅ all lanes HOLD/AWAIT — poller heartbeats, no action, no commit (idle is healthy).")


def cmd_coordinators(args):
    """GPU-COORDINATOR DISCOVERY + health (ORCHESTRATOR duty). Mirror of `ros submonitors` but for the
    role==gpu_coordinator one-per-GPU-backend invariant: enumerate every config compute_node with kind==gpu
    and verify each has a LIVE coordinator (matched by gpu_type via the agent's `gpu` tag or _infer). Any
    GPU whose coordinator is MISSING / DEAD / retired-without-successor must get one (re)spawned by the
    ORCHESTRATOR — a dead coordinator cannot respawn itself (esp. context-overflow before handoff). Exit
    non-zero if any GPU needs action, so a wrapper/cron can branch on it."""
    import datetime as _dt
    root = inst_root(args); rd = runtime_dir(root); cfg = _cfg(root)
    liv = cfg.get("liveness", {}) or {}
    kick = int(liv.get("kick_interval_minutes", 15)); grace = int(liv.get("patient_grace_minutes", 45))
    now = _dt.datetime.now(_dt.timezone.utc)
    gpus = [n for n in (cfg.get("compute_nodes") or []) if n.get("kind") == "gpu"]
    # index gpu_coordinator agents by gpu_type (role==gpu_coordinator or 'gpu_coordinator'/'coordinator' in id)
    coord_by_gpu = {}
    for fn in glob.glob(os.path.join(rd, "agents", "*.yaml")):
        a = load_yaml(fn, {}) or {}
        aid = a.get("agent_id", ""); role = a.get("role", "")
        if not (role == "gpu_coordinator" or "gpu_coordinator" in aid or "gpu-coordinator" in aid):
            continue
        try:
            last = _dt.datetime.strptime(a.get("last_heartbeat",""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
            age = (now - last).total_seconds() / 60.0
        except Exception:
            age = 1e9
        # key on the agent's gpu_type tag; fall back to inferring from node or id
        gpu = a.get("gpu", "") or a.get("node", "") or _infer_gpu_from_id(aid)
        # BUG-41 fix (mirror of submonitors): prefer a LIVE coordinator over a retired/terminal one.
        st = a.get("status", "?")
        alive = st not in ("completed", "retired", "failed", "dead", "superseded", "done", "dropped")
        cand = {"id": aid, "status": st, "age": round(age, 1), "alive": alive}
        cur = coord_by_gpu.get(gpu)
        if cur is None or (cand["alive"] != cur.get("alive", True) and cand["alive"]) or \
           (cand["alive"] == cur.get("alive", True) and age < cur["age"]):
            coord_by_gpu[gpu] = cand
    need = []
    print(f"GPU-COORDINATOR DISCOVERY (kick={kick}m, grace={grace}m) — {len(gpus)} GPU backend(s):")
    for n in gpus:
        gt = n.get("gpu_type", ""); node = n.get("name", "")
        # a coordinator matches this GPU by gpu_type tag OR by the node name (tolerant)
        c = coord_by_gpu.get(gt) or coord_by_gpu.get(node)
        label = f"{node}/{gt}"
        if not c:
            print(f"  ❌ {label}: NO gpu_coordinator — orchestrator must spawn one."); need.append((label,"missing")); continue
        st = c["status"]; age = c["age"]
        if st in ("completed","retired"):
            print(f"  🏁 {label}: {c['id']} retired (last {age}m) — verify a successor exists; if not, respawn.")
            need.append((label,"retired-no-successor"))
        elif st == "failed" or age > grace:
            handed = _had_handoff(rd, c["id"])
            tag = "DEAD" if age > grace else "failed"
            extra = "" if handed else " (NO handoff audit — likely context overflow; orchestrator owns respawn)"
            print(f"  ☠️ {label}: {c['id']} {tag} last={age}m{extra} — orchestrator MUST spawn a replacement.")
            need.append((label,"dead"))
        elif age > kick*1.5:
            print(f"  ⏳ {label}: {c['id']} STALE last={age}m (within grace) — watch; respawn if it crosses {grace}m.")
        else:
            print(f"  💓 {label}: {c['id']} alive last={age}m.")
    if need:
        print(f"\n⚠️ {len(need)} GPU(s) need a gpu_coordinator (re)spawn by the ORCHESTRATOR:")
        for label, why in need: print(f"   - {label}: {why}")
        sys.exit(3)
    print("\n✅ every GPU backend has a live gpu_coordinator.")

def _infer_gpu_from_id(aid):
    for gt in ("H100","MI350X","A100","H200","MI300X"):
        if gt.lower() in (aid or "").lower(): return gt
    return ""

def cmd_exp_gc(args):
    """BUG-23: retire orphan pending experiments (registered but never completed) + clear their dangling
    active_experiments back-links on claims. Reports what it would do; --apply to act."""
    root=inst_root(args)
    import time as _t
    orphans=[]; skipped=[]
    stale_min = args.stale_min if getattr(args,"stale_min",None) is not None else 30
    for ef in glob.glob(os.path.join(root,"experiments","**","experiment.yaml"),recursive=True):
        e=load_yaml(ef,{}) or {}
        if e.get("status")!="pending" or e.get("result_effect"): continue
        d=os.path.dirname(ef)
        # BUG-24 SAFETY: never retire a pending exp that has ANY artifacts (active researcher mid-run)
        arts=[p for p in glob.glob(os.path.join(d,"**","*"),recursive=True)
              if os.path.isfile(p) and os.path.basename(p)!="experiment.yaml"]
        if arts: skipped.append((e.get("exp_id"),"has artifacts (active?)")); continue
        # never retire one modified within the stale window (recent = possibly mid-flight)
        age_min=(_t.time()-os.path.getmtime(ef))/60.0
        if age_min < stale_min: skipped.append((e.get("exp_id"),f"modified {age_min:.0f}m ago < {stale_min}m")); continue
        orphans.append((e.get("exp_id"),e.get("claim_id"),d))
    for eid,why in skipped: print(f"  ⏭  skip {eid}: {why}")
    if not orphans: print("✅ no orphan pending experiments"); return
    print(f"{'RETIRING' if args.apply else 'WOULD RETIRE'} {len(orphans)} orphan pending experiment(s):")
    for eid,cid,d in orphans:
        print(f"  {eid} (claim {cid})")
        if args.apply:
            # clear from claim.active_experiments
            if cid:
                cf=find_obj(root,"claims",cid); c=load_yaml(cf) if cf else None
                if c and eid in (c.get("active_experiments") or []):
                    c["active_experiments"].remove(eid); c["last_updated"]=NOW(); dump_yaml(cf,c)
            # mark the experiment retired (keep the dir for audit; don't delete)
            ef=os.path.join(d,"experiment.yaml"); e=load_yaml(ef); e["status"]="retired"
            e["result_summary"]="retired by ros exp gc (orphan pending, never completed)"; dump_yaml(ef,e)
    if not args.apply: print("   (dry-run; re-run with --apply to retire)")
    else: print("   retired + cleared back-links")

def _gpu_queue_path(root):
    return os.path.join(runtime_dir(root), "gpu_queue.yaml")

def cmd_gpu_queue(args):
    """Enqueue a COMMITTEE-APPROVED GPU experiment. Pull-based scheduler dispatches it when a node is free."""
    root=inst_root(args)
    hits=glob.glob(os.path.join(root,"experiments","**",args.exp,"experiment.yaml"),recursive=True)
    ef=hits[0] if hits else None; exp=load_yaml(ef) if ef else None
    if not exp: sys.exit(f"❌ {args.exp} not found.")
    _win,_until=_gpu_window_open(inst_root(args))
    if not (exp.get("committee_approved") or args.force or _win):
        sys.exit(f"❌ {args.exp} is not committee_approved — only committee-greenlit experiments enter the GPU queue (--force, or ros gpu-approve-window --hours N).")
    if _win and not exp.get("committee_approved"): print(f"   ⏱️ GPU auto-approve window OPEN (until {_until}) — committee gate waived for {args.exp}.")
    floor=(exp.get("resource_budget") or {}).get("host_mem_floor_gb",0) or 0
    q=load_yaml(_gpu_queue_path(root),{"queue":[],"leases":{}}) or {"queue":[],"leases":{}}
    if any(i.get("exp_id")==args.exp for i in q["queue"]): print(f"already queued: {args.exp}"); return
    q["queue"].append({"exp_id":args.exp,"claim_id":exp.get("claim_id"),"gpu_type":args.gpu_type or "any",
                       "host_mem_floor_gb":floor,"queued_at":NOW(),"priority":args.priority or 0})
    dump_yaml(_gpu_queue_path(root),q)
    print(f"✅ queued {args.exp} (gpu={args.gpu_type or 'any'}, floor={floor}GB). Scheduler pulls it when a node frees.")

def cmd_gpu_status(args):
    root=inst_root(args); q=load_yaml(_gpu_queue_path(root),{"queue":[],"leases":{}}) or {"queue":[],"leases":{}}
    cfg=_cfg(root); nodes=cfg.get("compute_nodes",[]) or []
    print("GPU NODES (standby):")
    for n in nodes:
        lease=q.get("leases",{}).get(n["name"])
        print(f"  {n['name']} ({n.get('gpu_type','?')}, fragile={bool(n.get('fragile'))}): "
              + (f"BUSY -> {lease}" if lease else "FREE"))
    print(f"QUEUE ({len(q.get('queue',[]))} pending):")
    for i in sorted(q.get("queue",[]),key=lambda x:-x.get("priority",0)):
        print(f"  {i['exp_id']} (claim {i.get('claim_id')}, gpu={i.get('gpu_type')}, floor={i.get('host_mem_floor_gb')}GB)")
    # BUG-53 fix: gpu status was blind to the v2 gpu-task CHANNEL (the path orchestrators actually use now),
    # so it falsely reported an empty queue while tasks waited. Surface the channel's pending count too.
    if Channel is not None:
        try:
            tasks = _gpu_task_channel(root).list(include_acked=False)
            if tasks:
                print(f"TASK CHANNEL ({len(tasks)} pending — gpu-task, the v2 path):")
                for i in sorted(tasks, key=lambda x: -x.get("priority", 0)):
                    print(f"  {i.get('id')} exp={i.get('exp_id')} gpu={i.get('gpu_type')} "
                          f"floor={i.get('host_mem_floor_gb')}GB (claim {i.get('claim_id')})")
        except Exception:
            pass

def cmd_gpu_poll(args):
    """PULL-BASED scheduler tick for a STANDBY node. Probes the node; if FREE and lease-free, pulls the next
    queued exp (matching gpu_type, watchdog-safe) and dispatches it. Run this on a cadence per node."""
    import subprocess
    root=inst_root(args); cfg=_cfg(root)
    node=next((n for n in (cfg.get("compute_nodes") or []) if n.get("name")==args.node),None)
    if not node: sys.exit(f"❌ node {args.node} not in config")
    q=load_yaml(_gpu_queue_path(root),{"queue":[],"leases":{}}) or {"queue":[],"leases":{}}
    q.setdefault("leases",{})
    # already leased? report and exit (don't double-dispatch)
    if q["leases"].get(args.node):
        print(f"{args.node} BUSY (lease {q['leases'][args.node]}); no pull."); return
    # probe node free (config probe_cmd must succeed)
    # BUG-26 fix: probe_cmd is a heartbeat-file freshness check; nothing refreshes that file when the node is
    # idle, so it goes stale (>15m) and a healthy free node is wrongly declared unreachable, stalling the pull
    # scheduler. When probe_cmd fails, fall back to the live _old_probe (ssh nvidia-smi/rocminfo) before giving
    # up; if the live probe succeeds, refresh the heartbeat file so subsequent ticks pass cleanly.
    if node.get("probe_cmd"):
        r=subprocess.run(node["probe_cmd"],shell=True,capture_output=True,text=True,timeout=30)
        if r.returncode!=0:
            live=node.get("_old_probe")
            ok=False
            if live:
                try:
                    r2=subprocess.run(live,shell=True,capture_output=True,text=True,timeout=30)
                    ok=(r2.returncode==0)
                except Exception:
                    ok=False
            if not ok:
                print(f"{args.node} unreachable (heartbeat stale AND live probe failed); no pull."); return
            # live probe confirms node alive -> refresh stale heartbeat so the scheduler unblocks
            try:
                hb=os.path.join(root,"runtime","gpu_heartbeat",args.node)
                os.makedirs(os.path.dirname(hb),exist_ok=True)
                with open(hb,"a"): os.utime(hb,None)
                print(f"   (heartbeat for {args.node} was stale; live probe OK, refreshed heartbeat)")
            except Exception as _e:
                print(f"   (warn: live probe OK but could not refresh heartbeat: {str(_e)[:120]})")
    # pick next queued exp matching this node's gpu_type (or 'any'), highest priority, watchdog-safe on fragile
    cand=None
    for i in sorted(q["queue"],key=lambda x:-x.get("priority",0)):
        gt=i.get("gpu_type","any")
        if gt not in ("any", node.get("gpu_type")): continue
        if node.get("fragile") and (i.get("host_mem_floor_gb",0) or 0)<=0: continue  # SAFETY: never on fragile w/o floor
        cand=i; break
    if not cand: print(f"{args.node} FREE but no matching queued exp."); return
    # claim the lease + mark dispatched
    q["queue"]=[i for i in q["queue"] if i["exp_id"]!=cand["exp_id"]]
    q["leases"][args.node]=cand["exp_id"]; dump_yaml(_gpu_queue_path(root),q)
    ef=glob.glob(os.path.join(root,"experiments","**",cand["exp_id"],"experiment.yaml"),recursive=True)[0]
    e=load_yaml(ef); e["status"]="running"; e["started_at"]=NOW(); e["dispatched_by"]="gpu-scheduler"
    e["node_lease"]=f"{args.node}:{cand['exp_id']}"; e["hardware"]=node.get("gpu_type",""); dump_yaml(ef,e)
    print(f"🚀 PULLED {cand['exp_id']} -> {args.node} ({node.get('gpu_type')}, fragile={bool(node.get('fragile'))}, floor={cand.get('host_mem_floor_gb')}GB)")
    print(f"   run it with host-RAM watchdog on alloc AND teardown (os._exit). On completion: `ros gpu release --node {args.node}` then ros exp complete.")

def cmd_gpu_release(args):
    root=inst_root(args); q=load_yaml(_gpu_queue_path(root),{"queue":[],"leases":{}}) or {"queue":[],"leases":{}}
    rel=q.get("leases",{}).pop(args.node,None); dump_yaml(_gpu_queue_path(root),q)
    print(f"released {args.node}" + (f" (was {rel})" if rel else " (no lease)"))


# ============================ v2 STAGE B: backend registry + gpu task/result channels ============================
# A backend (cpu|gpu) self-registers like an agent (per-file under runtime/registry/) + carries runtime
# {status,current_task,lease,last_heartbeat}. The gpu TASK channel is the queue producers push to; the
# registry is the table of who can PULL from it. Generic over kind; gpu_coordinators (Stage C) are the
# consumers. All additive — the legacy `ros gpu queue/poll/release` path is untouched.
def _backend_path(root, bid):
    return os.path.join(runtime_dir(root), "registry", "backends", f"{bid}.yaml")

def _gpu_tasks_path(root):
    return os.path.join(runtime_dir(root), "orchestrator", "gpu_tasks.yaml")

def _gpu_results_path(root):
    return os.path.join(runtime_dir(root), "orchestrator", "gpu_results.yaml")

def _gpu_task_channel(root):
    return Channel(_gpu_tasks_path(root), list_key="tasks", id_prefix="GT")

def _gpu_result_channel(root):
    return Channel(_gpu_results_path(root), list_key="results", id_prefix="GR")

def cmd_backend_register(args):
    """A compute backend (cpu|gpu) self-registers into the runtime registry (per-file, mirrors agents/).
    gpu_coordinators register their ONE GPU backend on boot; the gpu task channel routes work to it."""
    root = inst_root(args); rd = runtime_dir(root)
    d = os.path.join(rd, "registry", "backends"); os.makedirs(d, exist_ok=True)
    p = _backend_path(root, args.id)
    rec = load_yaml(p, {}) or {}
    rec.update({"backend_id": args.id, "kind": args.kind, "gpu_type": args.gpu_type or "",
                "node": args.node or "", "status": rec.get("status", "idle"),
                "current_task": rec.get("current_task", ""), "lease": rec.get("lease", ""),
                "host_mem_floor_gb": int(args.host_mem_floor or 0),
                "last_heartbeat": NOW(), "spawned_at": rec.get("spawned_at", NOW()),
                "heartbeat_count": int(rec.get("heartbeat_count", 0))})
    dump_yaml(p, rec)
    print(f"✅ backend {args.id} registered (kind={args.kind}, gpu_type={args.gpu_type or '—'}, node={args.node or '—'}, "
          f"floor={rec['host_mem_floor_gb']}GB). Heartbeat: `ros backend heartbeat --id {args.id}`")

def cmd_backend_heartbeat(args):
    """Bump a backend's freshness + optionally update status/current_task/lease. Tolerant of
    heartbeat-before-register (auto-creates a minimal record), mirroring cmd_heartbeat."""
    root = inst_root(args); rd = runtime_dir(root)
    p = _backend_path(root, args.id)
    rec = load_yaml(p, {}) or {}
    if not rec.get("backend_id"):
        rec.setdefault("backend_id", args.id); rec.setdefault("spawned_at", NOW())
        rec.setdefault("heartbeat_count", 0); rec.setdefault("kind", "")
    rec["last_heartbeat"] = NOW()
    rec["heartbeat_count"] = int(rec.get("heartbeat_count", 0)) + 1
    if args.status: rec["status"] = args.status
    if args.task is not None: rec["current_task"] = args.task
    if args.lease is not None: rec["lease"] = args.lease
    dump_yaml(p, rec)
    print(f"💓 backend {args.id} heartbeat #{rec['heartbeat_count']} ({rec.get('status','?')})"
          + (f" task={rec.get('current_task')}" if rec.get("current_task") else ""))

def cmd_backend_list(args):
    """List registered backends with freshness, reusing the config liveness kick/grace age logic
    (same patient-grace semantics as cmd_liveness — never declare dead before the grace window)."""
    import datetime as _dt
    root = inst_root(args); rd = runtime_dir(root); cfg = _cfg(root)
    liv = cfg.get("liveness", {}) or {}
    kick = int(liv.get("kick_interval_minutes", 15)); grace = int(liv.get("patient_grace_minutes", 45))
    now = _dt.datetime.now(_dt.timezone.utc)
    rows = []
    for fn in sorted(glob.glob(os.path.join(rd, "registry", "backends", "*.yaml"))):
        b = load_yaml(fn, {}) or {}
        try:
            last = _dt.datetime.strptime(b.get("last_heartbeat",""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
            age = (now - last).total_seconds() / 60.0
        except Exception:
            age = 1e9
        if b.get("status") in ("retired",):
            state = "retired"
        else:
            state = "fresh" if age <= kick*1.5 else ("stale" if age <= grace else "DEAD")
        rows.append((b.get("backend_id","?"), b.get("kind","?"), b.get("gpu_type","") or "—",
                     b.get("node","") or "—", b.get("status","?"), round(age,1), state, b.get("current_task","") or ""))
    if not rows: print("(no backends registered)"); return
    print(f"BACKENDS (kick={kick}m, patient grace={grace}m):")
    for bid, kind, gt, node, st, age, state, task in rows:
        mark = {"fresh":"💓","stale":"⏳","DEAD":"☠️","retired":"🏁"}[state]
        print(f"  {mark} {bid:18} {kind:4} {gt:8} {node:12} {st:8} last={age}m -> {state}"
              + (f"  task={task}" if task else ""))
    dead = [r[0] for r in rows if r[6]=="DEAD"]
    if dead: print(f"  ⚠️ DEAD backends (past {grace}m grace): {', '.join(dead)}")

def cmd_gpu_pending(args):
    """TASK_COORDINATOR self-feed discovery: list experiments that are READY to run on a GPU but not yet
    on the task channel — so the coordinator pulls its own work instead of waiting for the orchestrator to
    submit. An exp qualifies when: needs_gpu==true AND status==pending AND result not yet recorded AND
    (committee_approved OR a gpu-approve-window is open) AND it is not already queued/acked on the task
    channel. Optionally filter by --gpu-type (matches the exp's hardware/gpu_type or 'any').
    The science gate is UNCHANGED: committee_approved (or an explicit operator window) is still required —
    discovery never invents approval, it only surfaces already-approved work to the runner."""
    root = inst_root(args)
    if Channel is None: sys.exit("❌ channeling unavailable.")
    _win,_until = _gpu_window_open(root)
    # already-on-channel exp ids (pending or acked) — don't re-surface
    on_channel = set()
    try:
        for it in _gpu_task_channel(root).list(include_acked=True):
            if it.get("exp_id"): on_channel.add(it["exp_id"])
    except Exception:
        pass
    want_gt = (args.gpu_type or "").strip().lower()
    rows = []
    for ef in glob.glob(os.path.join(root, "experiments", "**", "experiment.yaml"), recursive=True):
        exp = load_yaml(ef, {}) or {}
        eid = exp.get("exp_id")
        if not eid or eid in on_channel: continue
        if not exp.get("needs_gpu"): continue
        if (exp.get("status") or "").lower() not in ("pending", "designed", "ready", ""): continue
        if (exp.get("result_effect") or "").strip(): continue   # already has a recorded effect
        approved = bool(exp.get("committee_approved"))
        if not (approved or _win): continue                      # science gate (or open window) required
        budget = (exp.get("resource_budget") or {})
        floor = int(budget.get("host_mem_floor_gb", 0) or 0)
        # gpu_type: prefer explicit field, else parse hardware string, else 'any'
        gt = (exp.get("gpu_type") or "").strip()
        if not gt:
            hw = (exp.get("hardware") or "").upper()
            gt = "H100" if "H100" in hw else ("MI350X" if "MI350X" in hw or "MI300" in hw else "any")
        if want_gt and gt.lower() != want_gt and gt.lower() != "any": continue
        rows.append({"exp": eid, "claim": exp.get("claim_id") or "", "gpu": gt,
                     "floor": floor, "approved": approved, "via": "committee" if approved else f"window(until {_until})"})
    if not rows:
        print("(no GPU-ready experiments awaiting a coordinator — self-feed queue empty)"); return
    print(f"GPU-PENDING (coordinator self-feed) — {len(rows)} ready, not yet on task channel:")
    for r in rows:
        print(f"  exp={r['exp']} claim={r['claim']} gpu={r['gpu']} floor={r['floor']}GB approved_via={r['via']}")
    print("→ task_coordinator: `ros gpu-task submit --exp <e> --gpu-type <T> [--host-mem-floor <GB>] "
          "--by <coord-id>` then pull+run+report; the orchestrator no longer submits.")

def cmd_gpu_task_submit(args):
    """ORCHESTRATOR pushes a committee-approved GPU experiment onto the gpu TASK channel. A gpu_coordinator
    pulls the next matching task for its GPU. Mirrors the cmd_gpu_queue safety gates (committee + fragile)."""
    root = inst_root(args); cfg = _cfg(root)
    if Channel is None: sys.exit("❌ channeling unavailable — cannot use gpu task channel.")
    hits = glob.glob(os.path.join(root, "experiments", "**", args.exp, "experiment.yaml"), recursive=True)
    ef = hits[0] if hits else None; exp = load_yaml(ef) if ef else None
    if not exp: sys.exit(f"❌ {args.exp} not found.")
    _win,_until=_gpu_window_open(inst_root(args))
    if not (exp.get("committee_approved") or args.force or _win):
        sys.exit(f"❌ {args.exp} is not committee_approved — only committee-greenlit experiments enter the GPU "
                 f"task channel (--force, or open a window: ros gpu-approve-window --hours N).")
    if _win and not exp.get("committee_approved"): print(f"   ⏱️ GPU auto-approve window OPEN (until {_until}) — committee gate waived for {args.exp}.")
    floor = int(args.host_mem_floor if args.host_mem_floor is not None
                else (exp.get("resource_budget") or {}).get("host_mem_floor_gb", 0) or 0)
    gt = args.gpu_type or "any"
    # SAFETY (mirror cmd_gpu_queue/dispatch): a task targeting a fragile compute_node with no watchdog floor
    # is REFUSED — the coordinator would otherwise risk crashing the fragile node (MI350X postmortems).
    node = next((n for n in (cfg.get("compute_nodes") or [])
                 if n.get("gpu_type") == gt and n.get("fragile")), None)
    if node is not None and floor <= 0:
        sys.exit(f"❌ SAFETY: gpu_type '{gt}' maps to fragile node '{node.get('name')}' and this task has no "
                 f"host_mem_floor_gb. Set --host-mem-floor (watchdog) before submitting. (See learning/ postmortems.)")
    payload = {"exp_id": args.exp, "claim_id": exp.get("claim_id") or "", "gpu_type": gt,
               "host_mem_floor_gb": floor, "priority": args.priority or 0,
               "by": args.by or "", "summary": args.summary or ""}
    r = _gpu_task_channel(root).submit(payload, dedup_keys=["exp_id"])
    if r.get("dup"):
        print(f"↩︎ already queued as {r['id']} (exp {args.exp}) — not duplicating."); return
    print(f"✅ {r['id']} gpu-task submitted (exp {args.exp}, gpu={gt}, floor={floor}GB, prio={payload['priority']}). "
          f"A gpu_coordinator for {gt} pulls it when its GPU is idle.")

def cmd_gpu_task_list(args):
    root = inst_root(args)
    if Channel is None: sys.exit("❌ channeling unavailable.")
    items = _gpu_task_channel(root).list(include_acked=args.all)
    if not items: print("(gpu task channel empty — no pending tasks)"); return
    print(f"GPU TASK CHANNEL — {len(items)} {'total' if args.all else 'pending'}:")
    for i in sorted(items, key=lambda x: -x.get("priority", 0)):
        tag = "✓acked" if i.get("acked") else "⏳PENDING"
        print(f"  [{tag}] {i.get('id')} {i.get('submitted_at','')} exp={i.get('exp_id')} "
              f"gpu={i.get('gpu_type')} floor={i.get('host_mem_floor_gb')}GB prio={i.get('priority',0)}")
        if i.get("claim_id"): print(f"          claim: {i['claim_id']}")
        if i.get("summary"):  print(f"          summary: {i['summary']}")
        if i.get("acked") and i.get("answer"): print(f"          answer:  {i['answer']}")

def cmd_gpu_task_ack(args):
    root = inst_root(args)
    if Channel is None: sys.exit("❌ channeling unavailable.")
    n = _gpu_task_channel(root).ack(item_id=args.id, all_items=bool(args.all),
                                    by=args.by or "", answer=args.answer or "")
    print(f"✅ acked {n} gpu-task(s)" + (f": {args.answer}" if args.answer else ""))

def cmd_gpu_result_submit(args):
    """gpu_coordinator loops a completed (or faulted) GPU run's artifact back to the orchestrator via the
    RESULT channel. The orchestrator drains these (`ros gpu-result list`) → `ros exp complete` → verdict."""
    root = inst_root(args)
    if Channel is None: sys.exit("❌ channeling unavailable.")
    payload = {"exp_id": args.exp, "task_id": args.task or "", "effect": args.effect,
               "summary": args.summary or "", "artifacts_path": args.artifacts_path or "",
               "by": args.by or "", "fault": bool(args.fault)}
    r = _gpu_result_channel(root).submit(payload, dedup_keys=["exp_id", "task_id"])
    if r.get("dup"):
        print(f"↩︎ result already submitted as {r['id']} (exp {args.exp}, task {args.task}) — not duplicating."); return
    # BUG-55 fix: a FAULTED run (crash/OOM/host-mem) leaves the node leased forever — the orchestrator
    # then sees the (esp. fragile) node perpetually BUSY and never re-dispatches. On fault, AUTO-RELEASE
    # the node lease this exp held + mark the exp 'faulted' (not stuck 'running') so it can be re-dispatched.
    if args.fault:
        hits = glob.glob(os.path.join(root, "experiments", "**", args.exp, "experiment.yaml"), recursive=True)
        ef = hits[0] if hits else None; exp = load_yaml(ef) if ef else None
        if exp:
            lease = exp.get("node_lease", "")
            if lease and ":" in lease:
                lnode = lease.split(":", 1)[0]
                q = load_yaml(_gpu_queue_path(root), {"queue": [], "leases": {}}) or {"queue": [], "leases": {}}
                if q.get("leases", {}).get(lnode) == exp.get("exp_id"):
                    q["leases"].pop(lnode, None); dump_yaml(_gpu_queue_path(root), q)
                    print(f"   ⚠️ FAULT: auto-released node {lnode} lease (was {exp.get('exp_id')}).")
            if exp.get("status") == "running":
                exp["status"] = "faulted"; exp["result_summary"] = (args.summary or "")[:200]
                exp["node_lease"] = ""; dump_yaml(ef, exp)
    print(f"✅ {r['id']} gpu-result submitted (exp {args.exp}, effect={args.effect}"
          + (", FAULT" if args.fault else "") + "). Orchestrator drains it via `ros gpu-result list`.")

def cmd_gpu_result_list(args):
    root = inst_root(args)
    if Channel is None: sys.exit("❌ channeling unavailable.")
    items = _gpu_result_channel(root).list(include_acked=args.all)
    if not items: print("(gpu result channel empty — no pending results)"); return
    print(f"GPU RESULT CHANNEL — {len(items)} {'total' if args.all else 'pending'}:")
    for i in items:
        tag = "✓acked" if i.get("acked") else "⏳PENDING"
        fault = " ⚠️FAULT" if i.get("fault") else ""
        print(f"  [{tag}] {i.get('id')} {i.get('submitted_at','')} exp={i.get('exp_id')} "
              f"effect={i.get('effect')}{fault} (task {i.get('task_id') or '—'}, by {i.get('by') or '?'})")
        if i.get("summary"):        print(f"          summary: {i['summary']}")
        if i.get("artifacts_path"): print(f"          artifacts: {i['artifacts_path']}")
        if i.get("acked") and i.get("answer"): print(f"          answer:  {i['answer']}")

def cmd_gpu_result_ack(args):
    root = inst_root(args)
    if Channel is None: sys.exit("❌ channeling unavailable.")
    n = _gpu_result_channel(root).ack(item_id=args.id, all_items=bool(args.all),
                                      by=args.by or "", answer=args.answer or "")
    print(f"✅ acked {n} gpu-result(s)" + (f": {args.answer}" if args.answer else ""))

def cmd_verdict_write(args):
    """Write a committee VERDICT into verdicts/<PROJ>/<date>/, REQUIRING linked experiment ids that exist.
    Enforces config green_rule (BUG-3 fix): green/promote require full committee parity.
    Back-links the verdict into the claim's verdict_history + each experiment's linked_verdicts."""
    root=inst_root(args)
    cf=find_obj(root,"claims",args.claim)
    if not cf: sys.exit(f"❌ claim {args.claim} not found.")
    claim=load_yaml(cf); pid=claim.get("project_id","PROJ-0000")
    # validate experiment links (must exist under experiments/**/<EXP>/)
    exp_ids=[e.strip() for e in (args.experiments or "").split(",") if e.strip()]
    if not exp_ids:
        sys.exit("❌ a verdict MUST cite the experiment(s) that informed it: --experiments EXP-xxxx[,EXP-yyyy]")
    exp_paths=[]
    for eid in exp_ids:
        hits=glob.glob(os.path.join(root,"experiments","**",eid,"experiment.yaml"),recursive=True)
        if not hits: sys.exit(f"❌ experiment {eid} not found under experiments/ — cannot file a verdict citing it.")
        exp_paths.append(os.path.relpath(os.path.dirname(hits[0]),root))
    votes=[v.strip() for v in (args.votes or "").split(",") if v.strip()]  # role:vote pairs
    parsed=[dict(zip(["role","vote"],v.split(":",1))) for v in votes]
    # BUG-3 FIX: enforce committee green_rule for green/promote verdicts
    cfg=_cfg(root); comm=cfg.get("committee",{}) or {}
    members=comm.get("members",[]) or []; rule=comm.get("green_rule","unanimous")
    if args.final in ("green","promote") and not args.override_rule:
        # ★ BUG-56/57 FIX (scientific-integrity gate): a green/promote requires EVERY configured committee
        # MEMBER (by role name) to be present and green — NOT merely len(votes)>=N. The old count-only
        # check let a vote slate (a) duplicate one role to pad the count, (b) OMIT area_chair=FINAL_VERDICT,
        # or (c) use bogus/typo'd role names — all silently sailing through as "6/6 green". The gate now
        # validates the EXACT configured member set is covered, each role appears once, each votes green,
        # and no unknown roles are present. The configured member list is the source of truth.
        member_roles=[ (m.get("role") or "").strip() for m in members if (m.get("role") or "").strip() ]
        if not member_roles: member_roles=["novelty_killer","systems_reviewer","evaluation_prosecutor",
                                           "theory_skeptic","product_realist","area_chair"]
        nmembers=len(member_roles)
        # normalize parsed roles/votes
        vote_by_role={}
        dup_roles=[]
        for p in parsed:
            r=(p.get("role") or "").strip()
            v=(p.get("vote") or "").strip().lower()
            if not r:
                sys.exit(f"❌ green_rule={rule}: a vote is missing its role (need role:vote). got slate {votes}.")
            if r in vote_by_role: dup_roles.append(r)
            vote_by_role[r]=v
        if dup_roles:
            sys.exit(f"❌ green_rule={rule}: duplicate vote(s) for role(s) {sorted(set(dup_roles))} — each "
                     f"committee member votes exactly once. Cannot pad a {args.final} with repeated roles.")
        missing=[r for r in member_roles if r not in vote_by_role]
        if missing:
            sys.exit(f"❌ green_rule={rule}: {args.final} needs a vote from EVERY committee member; "
                     f"missing {missing}. (configured members: {member_roles}; "
                     f"use --override-rule only with explicit justification.)")
        unknown=[r for r in vote_by_role if r not in member_roles]
        if unknown:
            sys.exit(f"❌ green_rule={rule}: unknown role(s) {unknown} are not configured committee members "
                     f"{member_roles} — a {args.final} verdict must come from the real committee, not "
                     f"arbitrary roles. (use --override-rule only with explicit justification.)")
        if rule=="unanimous":
            non_green=[f"{r}:{vote_by_role[r]}" for r in member_roles if vote_by_role[r]!="green"]
            if non_green:
                sys.exit(f"❌ green_rule=unanimous: every member must vote green for a {args.final} verdict; "
                         f"non-green: {non_green}.")
    # BUG-18 fix: idempotency — if an identical verdict (same claim+experiments+final) already exists,
    # do NOT create a duplicate (guards against harness/transport retries of the same tool call).
    if not getattr(args, "allow_dup", False):
        for vf in glob.glob(reg_dir(root,"verdicts","**","VERDICT-*.yaml"),recursive=True):
            ev=load_yaml(vf,{}) or {}
            if (ev.get("claim_id")==args.claim and ev.get("final_verdict")==args.final
                    and sorted(ev.get("experiment_ids") or [])==sorted(exp_ids)):
                print(f"↩︎ idempotent: {ev.get('verdict_id')} already records {args.final} for {args.claim} "
                      f"citing {exp_ids} — not creating a duplicate. (use --allow-dup to force.)")
                return
    vid=next_id(root,"verdicts","VERDICT")
    import datetime as _d
    date=_valid_date(args.date)
    obj={"verdict_id":vid,"claim_id":args.claim,"project_id":pid,"date":date,
         "experiment_ids":exp_ids,"experiment_paths":exp_paths,
         "committee_version":args.committee_version or comm.get("rubric_version","v001"),"prompt_versions":{},
         "reviewer_votes":parsed,"green_rule":rule,
         "final_verdict":args.final,
         "fatal_objections":[x.strip() for x in (args.fatal or "").split(";") if x.strip()],
         "required_evidence":[x.strip() for x in (args.required or "").split(";") if x.strip()],
         "map_delta_proposals":[x.strip() for x in (args.map_delta or "").split(";") if x.strip()],
         "baseline_requirements":[x.strip() for x in (args.baselines or "").split(";") if x.strip()],
         "created_at":NOW()}
    d=obj_dir(root,"verdicts",pid,date); path=os.path.join(d,f"{vid}.yaml"); dump_yaml(path,obj)
    # back-link claim + experiments
    claim.setdefault("verdict_history",[]).append({"verdict_id":vid,"date":date,"result":args.final})
    if args.final in ("promote","kill"):
        claim["lifecycle_state"]="done"; claim["next_action"]=f"{args.final}ed by {vid}"
        # ★ BUG-58b: mirror the claim STATUS on promote/kill so it matches the exp_complete path (which
        # sets status promoted/dead). Without this, a promote verdict left status='seed' on a top result,
        # and the BUG-25 promoted-claim demote-guard (which keys on status=='promoted') never engaged.
        if args.final=="promote": claim["status"]="promoted"
        else: claim["status"]="dead"
    elif args.final=="green":
        # ★ BUG-58 FIX: a GREEN verdict PASSED committee (real 6/6). The old code reused the yellow/red
        # message ("address required_evidence to advance"), which made every green claim linger in `ros
        # resume` telling the orchestrator to fix evidence it had already cleared — nonsensical for a pass.
        # A green is the success milestone but NOT auto-terminal (green-lift / promote may follow), so we
        # mark a green-specific lifecycle + status + an honest next_action, WITHOUT forcing 'done'.
        claim["lifecycle_state"]="verdict_recorded"
        claim["status"]="green"
        claim["next_action"]=(f"{vid}=GREEN (6/6 committee): promote (ros verdict --final promote) "
                              f"or close; any green-lift/follow-on is optional, not required")
    else:
        claim["lifecycle_state"]="verdict_recorded"
        claim["next_action"]=f"{vid}={args.final}: address required_evidence to advance"
    claim["last_updated"]=NOW(); dump_yaml(cf,claim)
    for eid in exp_ids:
        ep=glob.glob(os.path.join(root,"experiments","**",eid,"experiment.yaml"),recursive=True)[0]
        e=load_yaml(ep); e.setdefault("linked_verdicts",[]).append(vid); dump_yaml(ep,e)
    # committee can greenlight a FOLLOW-UP GPU experiment id(s) to enter the queue (autonomous, no human gate)
    for aeid in [x.strip() for x in (args.approves_exp or "").split(",") if x.strip()]:
        ah=glob.glob(os.path.join(root,"experiments","**",aeid,"experiment.yaml"),recursive=True)
        if ah:
            ae=load_yaml(ah[0]); ae["committee_approved"]=vid; dump_yaml(ah[0],ae)
            print(f"   committee-approved {aeid} for GPU queue (via {vid})")
    print(f"✅ {vid} ({args.final}) -> {os.path.relpath(path,root)}")
    print(f"   votes: {len(parsed)}/{len(members) or 6}  cites: {', '.join(exp_ids)}  | claim {args.claim} updated")


VALID_LIFECYCLE={"drafted","prior_art_pending","experiment_designing","experiment_running",
  "evidence_ready","committee_pending","verdict_recorded","blocked","done"}

def cmd_claim_advance(args):
    """Explicitly set a claim's lifecycle_state + next_action (resume pointer). For phases the
    auto-transitions don't cover (e.g. committee_pending, blocked)."""
    root=inst_root(args); cf=find_obj(root,"claims",args.claim)
    if not cf: sys.exit(f"❌ claim {args.claim} not found.")
    if args.state not in VALID_LIFECYCLE:
        sys.exit(f"❌ state must be one of {sorted(VALID_LIFECYCLE)}")
    c=load_yaml(cf); old=c.get("lifecycle_state","?")
    c["lifecycle_state"]=args.state
    if args.next is not None: c["next_action"]=args.next
    if args.blocking is not None: c["blocking_on"]=args.blocking
    if args.state!="blocked": c["blocking_on"]=c.get("blocking_on","") if args.blocking else ""
    c["last_updated"]=NOW(); dump_yaml(cf,c)
    print(f"✅ {args.claim}: {old} -> {args.state}")
    if c.get("next_action"): print(f"   next: {c['next_action']}")

def cmd_resume(args):
    """RESUME DASHBOARD: every claim not in a terminal state, with its lifecycle phase + next_action.
    A fresh/rebooted orchestrator runs this FIRST to pick up paused work from exactly where it stopped."""
    root=inst_root(args)
    rows=[]
    for fn in glob.glob(reg_dir(root,"claims","**","CLAIM-*.yaml"),recursive=True):
        c=load_yaml(fn,{}) or {}
        ls=c.get("lifecycle_state","drafted")
        if ls=="done": continue
        rows.append((c.get("claim_id","?"),c.get("project_id","?"),ls,c.get("status","?"),
                     c.get("active_experiments",[]),c.get("next_action",""),c.get("blocking_on","")))
    if not rows: print("✅ nothing in-flight — all claims terminal (done). Open a new seed."); return
    order={s:i for i,s in enumerate(["blocked","committee_pending","evidence_ready","experiment_running",
        "experiment_designing","prior_art_pending","verdict_recorded","drafted"])}
    rows.sort(key=lambda r: order.get(r[2],99))
    print(f"IN-FLIGHT CLAIMS ({len(rows)}) — resume from here:")
    for cid,pid,ls,st,act,nxt,blk in rows:
        flag=" ⛔" if ls=="blocked" else ""
        print(f"  [{ls:18}] {cid} ({pid}) status={st}{flag}")
        if act: print(f"      active_exp: {', '.join(act)}")
        if blk: print(f"      blocked_on: {blk}")
        if nxt: print(f"      NEXT: {nxt}")


# ============================ committee-submission queue (sub-monitor -> orchestrator) ============================
# DECOUPLING (v2 routing): researchers report to their PER-PROJECT SUB-MONITOR (not the orchestrator).
# A sub-monitor that judges a researcher's proposal committee-ready pushes a SUBMISSION REQUEST onto this
# queue. The orchestrator CONSUMES the queue (`ros queue list`) and convenes the honest committee — it no
# longer manages/debugs researchers directly. This keeps the orchestrator off the researcher-debug hot path.
def _queue_path(root):
    return os.path.join(runtime_dir(root), "orchestrator", "committee_queue.yaml")

def _committee_channel(root):
    return Channel(_queue_path(root), list_key="queue", id_prefix="Q")

def cmd_queue_submit(args):
    """SUB-MONITOR pushes a committee-submission request for a researcher proposal that is ready for review.
    The orchestrator pulls these (`ros queue list`) and convenes the committee. Decouples researcher mgmt
    (sub-monitor) from committee orchestration (orchestrator)."""
    root = inst_root(args); rd = runtime_dir(root)
    os.makedirs(os.path.join(rd, "orchestrator"), exist_ok=True)
    # claim must exist (a committee reviews a claim's evidence)
    if args.claim and args.claim != "none":
        if not find_obj(root, "claims", args.claim):
            sys.exit(f"❌ claim {args.claim} not found — submit a real claim id (or seed it first).")
    exp_ids = [e.strip() for e in (args.exp or "").split(",") if e.strip()]
    for eid in exp_ids:
        if not glob.glob(os.path.join(root, "experiments", "**", eid, "experiment.yaml"), recursive=True):
            sys.exit(f"❌ experiment {eid} not found — a submission must cite real EXP evidence.")
    payload = {"claim_id": args.claim, "project_id": args.project or "",
               "experiment_ids": exp_ids, "submitted_by": args.by or "", "researcher": args.researcher or "",
               "kind": args.kind or "committee", "summary": args.summary or "", "priority": args.priority or 0}
    if Channel is not None:
        ch = _committee_channel(root)
        # ★ BUG-59: mirror queue_id INSIDE the channel lock (was a separate unlocked read-modify-write
        # that could clobber a concurrent submit). submit() now writes queue_id atomically.
        r = ch.submit(payload, dedup_keys=["claim_id", "experiment_ids"], mirror_id_key="queue_id")
        if r.get("dup"):
            print(f"↩︎ already queued as {r['id']} (claim {args.claim}, exps {exp_ids}) — not duplicating."); return
        qid = r["id"]
    else:
        q = load_yaml(_queue_path(root), {"queue": []}) or {"queue": []}
        qid = f"Q-{len(q['queue'])+1:04d}"
        for it in q["queue"]:
            if (not it.get("acked") and it.get("claim_id") == args.claim
                    and sorted(it.get("experiment_ids") or []) == sorted(exp_ids)):
                print(f"↩︎ already queued as {it.get('queue_id')} (claim {args.claim}, exps {exp_ids}) — not duplicating.")
                return
        rec = {"queue_id": qid, **payload, "submitted_at": NOW(), "acked": False, "acked_by": "", "acked_at": "", "answer": ""}
        q["queue"].append(rec); dump_yaml(_queue_path(root), q)
    print(f"✅ {qid} submitted to orchestrator committee-queue (claim {args.claim}, kind={payload['kind']}, "
          f"exps={exp_ids or '—'}, by={payload['submitted_by'] or '?'})")

def cmd_queue_list(args):
    """ORCHESTRATOR pulls pending committee-submission requests from the sub-monitors. This is the
    decoupled replacement for poking researchers: the orchestrator acts only on queued, vetted submissions."""
    root = inst_root(args)
    if Channel is not None:
        items = _committee_channel(root).list(include_acked=args.all)
    else:
        q = load_yaml(_queue_path(root), {"queue": []}) or {"queue": []}
        items = [i for i in q["queue"] if (args.all or not i.get("acked"))]
    if not items: print("(committee-queue empty — no pending submissions)"); return
    print(f"COMMITTEE QUEUE — {len(items)} {'total' if args.all else 'pending'}:")
    for i in sorted(items, key=lambda x: -x.get("priority", 0)):
        qid = i.get("queue_id") or i.get("id")
        tag = "✓acked" if i.get("acked") else "⏳PENDING"
        print(f"  [{tag}] {qid} {i.get('submitted_at','')} claim={i.get('claim_id')} "
              f"({i.get('project_id') or '?'}) kind={i.get('kind')}")
        if i.get("researcher"): print(f"          researcher: {i['researcher']}  (via {i.get('submitted_by','?')})")
        if i.get("experiment_ids"): print(f"          cites: {', '.join(i['experiment_ids'])}")
        if i.get("summary"):  print(f"          summary: {i['summary']}")
        if i.get("acked") and i.get("answer"): print(f"          answer:  {i['answer']}")

def cmd_queue_ack(args):
    """ORCHESTRATOR marks a submission consumed (committee convened / deferred / rejected, with a reason)."""
    root = inst_root(args); p = _queue_path(root)
    if Channel is not None:
        ch = _committee_channel(root)
        # Channel.ack keys on native id; queue_id == id for v2-created items. mirror_key='queue_id'
        # (★ BUG-59) handles legacy items inside the lock — no separate unlocked read-modify-write.
        n = ch.ack(item_id=args.id, all_items=bool(args.all), by=args.by or "orchestrator",
                   answer=args.answer or "", mirror_key="queue_id")
    else:
        q = load_yaml(p, {"queue": []}) or {"queue": []}
        n = 0
        for i in q["queue"]:
            if not i.get("acked") and (args.all or i.get("queue_id") == args.id):
                i["acked"] = True; i["acked_at"] = NOW(); i["acked_by"] = args.by or "orchestrator"
                i["answer"] = args.answer or ""; n += 1
        dump_yaml(p, q)
    print(f"✅ acked {n} queue item(s)" + (f": {args.answer}" if args.answer else ""))


def cmd_commit(args):
    """Commit + push ALL durable instance state to GitHub. The CORE persistence feature: uncommitted local
    work is LOST if a session dies/retires. Every agent should call this frequently (each cycle, and ALWAYS
    at spawn/retire boundaries). Uses HTTPS (SSH port 22 is blocked on the control Mac — see meta GIT_PUSH.md);
    auto-fixes an SSH remote to HTTPS so push never silently fails. No-op (clean exit) if nothing to commit."""
    import subprocess
    root = inst_root(args)
    def _git(*a, **k):
        return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=k.get("t", 60))
    # nothing staged/dirty/untracked? -> still push in case of unpushed commits, else exit clean
    st = _git("status", "--porcelain")
    dirty = bool(st.stdout.strip())
    # ensure HTTPS remote (SSH is blocked on this Mac)
    url = _git("remote", "get-url", "origin").stdout.strip()
    if url.startswith("git@github.com:"):
        https = "https://github.com/" + url.split("git@github.com:", 1)[1]
        _git("remote", "set-url", "origin", https)
        print(f"   (fixed SSH->HTTPS remote: {https})")
    if dirty:
        _git("add", "-A")
        msg = args.message or f"ros commit (autosave {NOW()})"
        c = _git("commit", "-m", msg)
        if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
            print(f"⚠️ commit issue: {(c.stdout + c.stderr).strip()[:300]}")
    else:
        print("   (working tree clean — checking for unpushed commits)")
    p = _git("push", "origin", "HEAD", t=90)
    out = (p.stdout + p.stderr).strip()
    if p.returncode == 0:
        head = _git("rev-parse", "--short", "HEAD").stdout.strip()
        print(f"✅ ros commit: pushed (HEAD {head}). {'committed + ' if dirty else ''}up to date with origin.")
    else:
        print(f"❌ push FAILED: {out[:400]}")
        sys.exit(1)


# ---------------- SUPERVISION TREE + TASK LEDGER + REAPER (BUG-31..34) ----------------
def _sup_helpers():
    """Bundle ros.py helpers for engine/supervise.py (avoids circular import)."""
    return {"load_yaml": load_yaml, "dump_yaml": dump_yaml, "NOW": NOW, "next_id": next_id,
            "reg_dir": reg_dir, "runtime_dir": runtime_dir, "_cfg": _cfg, "Channel": Channel}

def _sup():
    import importlib, supervise
    importlib.reload(supervise) if False else None
    return supervise

def cmd_task_open(args):
    import supervise
    root = inst_root(args); H = _sup_helpers()
    tid, path = supervise.task_open(H, root, kind=args.kind, parent=args.parent, assignee=args.assignee,
                                    project=args.project or "", claim=args.claim or "", exp=args.exp or "",
                                    session=args.session or "", summary=args.summary or "")
    print(f"✅ {tid} opened (kind={args.kind}, assignee={args.assignee or '-'}, parent={args.parent or '-'}) -> {os.path.relpath(path, root)}")

def cmd_task_update(args):
    import supervise
    root = inst_root(args); H = _sup_helpers()
    rec = supervise.task_update(H, root, args.id, assignee=args.assignee, status=args.status,
                                session=args.session, evidence_delta=args.evidence_delta, by=args.by or "", note=args.note or "")
    print(f"✅ {args.id} -> status={rec.get('status')} assignee={rec.get('assignee','-')}"
          + (f" evidence_delta={rec['evidence_delta'][:60]}" if rec.get('evidence_delta') else ""))

def cmd_task_list(args):
    import supervise
    root = inst_root(args); H = _sup_helpers()
    rows = supervise.task_list(H, root, only_open=args.open, only_orphans=args.orphans, project=args.project)
    if not rows: print("(no tasks)"); return
    print(f"TASKS ({len(rows)}):")
    for r in rows:
        mark = {"open":"○","active":"▶","orphaned":"⚠️","done":"✓","dropped":"✗"}.get(r.get("status"),"?")
        print(f"  {mark} {r['task_id']} [{r.get('status')}] {r.get('kind','')} "
              f"assignee={r.get('assignee','-')} parent={r.get('parent','-')} {r.get('project_id','')} "
              f"{r.get('claim_id','')}".rstrip())
        if r.get("summary"): print(f"        {r['summary'][:100]}")
        if r.get("evidence_delta"): print(f"        Δ {r['evidence_delta'][:100]}")

def cmd_reap(args):
    import supervise
    root = inst_root(args); H = _sup_helpers(); cfg = _cfg(root)
    grace = int((cfg.get("liveness", {}) or {}).get("patient_grace_minutes", 45))
    conv = [os.path.basename(p) for p in glob.glob(os.path.join(root, "projects", "PROJ-*")) if _is_converged_project(p)]
    changes, notifs = supervise.reap(H, root, apply=args.apply, grace_min=grace, converged_pids=conv)
    if not changes: print("✅ no lingering agents (all alive within grace or already terminal)."); return
    verb = "REAPED" if args.apply else "WOULD REAP (dry-run; pass --apply)"
    n_dead = sum(1 for c in changes if c[2] == "dead")
    n_sup = sum(1 for c in changes if c[2] == "superseded")
    n_ret = sum(1 for c in changes if c[2] == "retired")
    print(f"{verb} — {len(changes)} lingering agent(s): {n_sup} superseded, {n_ret} retired(converged), {n_dead} DEAD(active-gap):")
    for aid, old, new, proj, role, note in changes:
        flag = " ❗" if new == "dead" else ""
        print(f"  {aid:28} {old} -> {new}{flag}  ({proj or '-'}/{role}) {note}")
    if notifs:
        print(f"\n📨 {len(notifs)} orchestrator notification(s): " + ", ".join(f"{k}:{s}" for k,s,_ in notifs))
    if not args.apply: sys.exit(3)

def cmd_handoff(args):
    import supervise
    root = inst_root(args); H = _sup_helpers()
    rec = supervise.handoff(H, root, frm=args.frm, to=args.to, project=args.project or "",
                            researcher_state=args.researcher_state or "", seeder_state=args.seeder_state or "",
                            open_work=args.open_work or "", evidence_delta=args.evidence_delta or "",
                            reseed_needed=args.reseed_needed, by=args.by or "", task=args.task or "")
    print(f"✅ handoff {args.frm} -> {args.to} recorded"
          + (f" (task {args.task} transferred)" if args.task else "")
          + (" ⚠️ RESEED_NEEDED notified to orchestrator" if args.reseed_needed else ""))

def cmd_tree(args):
    import supervise, datetime as _dt
    root = inst_root(args); H = _sup_helpers(); cfg = _cfg(root)
    grace = int((cfg.get("liveness", {}) or {}).get("patient_grace_minutes", 45))
    agents = supervise.tree(H, root, grace_min=grace)
    show_all = args.all
    live = [a for a in agents if a.get("status") in ("running","active","open") and a["_age"] <= grace]
    byid = {a.get("agent_id"): a for a in agents}
    children = {}
    for a in agents:
        children.setdefault(a.get("parent","") or "(root)", []).append(a)
    def _mark(a):
        st = a.get("status","?")
        if st in ("retired","completed","done"): return "🏁"
        if st in ("dead","failed"): return "☠️"
        if st == "superseded": return "♻️"
        if a["_age"] > grace: return "☠️"   # past-grace but still flagged running = lingering (reap candidate)
        return "💓" if a["_age"] <= grace*1.0 else "⏳"
    def _emit(pid, depth):
        for a in sorted(children.get(pid, []), key=lambda x: x.get("agent_id","")):
            st = a.get("status","?")
            if not show_all and st in ("retired","superseded","dead","failed","completed","done","dropped"):
                continue
            print("  "*depth + f"{_mark(a)} {a.get('agent_id')} [{st}] {a.get('role','')} "
                  f"{a.get('project_id','')} last={round(a['_age'],1)}m".rstrip())
            _emit(a.get("agent_id"), depth+1)
    print(f"SUPERVISION TREE ({len(live)} live / {len(agents)} total; grace={grace}m)"
          + ("" if show_all else " — live only, pass --all for full") + ":")
    _emit("(root)", 0)
    # also surface any non-rooted live agents
    rooted = set()
    def _collect(pid):
        for a in children.get(pid, []):
            rooted.add(a.get("agent_id")); _collect(a.get("agent_id"))
    _collect("(root)")
    orphans = [a for a in live if a.get("agent_id") not in rooted]
    if orphans:
        print("  (live agents with no parent edge:)")
        for a in orphans: print(f"    {_mark(a)} {a.get('agent_id')} [{a.get('status')}] {a.get('role','')}")

def cmd_retire(args):
    import supervise
    root = inst_root(args); H = _sup_helpers(); cfg = _cfg(root)
    grace = int((cfg.get("liveness", {}) or {}).get("patient_grace_minutes", 45))
    rep = supervise.retire(H, root, frm=args.frm, to=args.to, project=args.project or "",
                           researcher_state=args.researcher_state or "", seeder_state=args.seeder_state or "",
                           open_work=args.open_work or "", evidence_delta=args.evidence_delta or "",
                           reseed_needed=args.reseed_needed, by=args.by or "", grace_min=grace)
    print(f"✅ RETIRED {rep['from']} -> {rep['to']} (atomic)")
    print(f"   successor inherits parent={rep['inherited_parent'] or '(root)'}")
    print(f"   tasks moved: {', '.join(rep['moved_tasks']) or '(none)'}")
    print(f"   supervised agents re-parented: {', '.join(rep['moved_agents']) or '(none)'}")
    if rep.get("reseed_conflict"):
        print(f"   ⚠️ RESEED_CONFLICT: live researcher(s) {', '.join(rep['reseed_conflict'])} exist — "
              f"orchestrator must VERIFY before spawning (do NOT double-spawn)")
    elif rep["reseed_needed"]:
        print(f"   ⚠️ RESEED_NEEDED notified to orchestrator (dead seeder + open work)")

def cmd_project_tree(args):
    import supervise
    root = inst_root(args); H = _sup_helpers(); cfg = _cfg(root)
    grace = int((cfg.get("liveness", {}) or {}).get("patient_grace_minutes", 45))
    conv = [os.path.basename(p) for p in glob.glob(os.path.join(root, "projects", "PROJ-*")) if _is_converged_project(p)]
    tr = supervise.project_tree(H, root, grace_min=grace, converged_pids=conv)
    print("PROJECT MEMORY TREE (project -> live sub-monitor -> claim-seeders; committed registry):")
    for pid in sorted(tr):
        v = tr[pid]
        if v["converged"]:
            print(f"  🏁 {pid} CONVERGED  (claims: {', '.join(c['claim_id'] for c in v['claims']) or '-'})")
            continue
        sm = v["live_sub_monitor"]
        smflag = sm if sm else "❌ NONE (orchestrator must spawn)"
        print(f"  📁 {pid}  sub-monitor={smflag}  (gens={v['sub_monitor_generations']})")
        for c in v["claims"]:
            print(f"        claim {c['claim_id']} [{c['status']}/{c['stage']}]")
        if v["live_researchers"]:
            print(f"        live researchers: {', '.join(v['live_researchers'])}")
        elif not v["converged"]:
            print(f"        live researchers: (none — below floor or work-gated)")

# ============================ v3 MONITOR EYES (read-only) ============================
# Two net-new READ-ONLY commands the v3 deterministic monitor cron uses. Neither spawns nor mutates
# anything — they compute and print state, exit non-zero when there is something to escalate.
#
#  ros cron-health  (monitor job #1) — every v3 cron drops runtime/cron/<job>.alive on a successful run.
#                    This reads those stamps and flags any cron whose stamp is missing or stale (> N x its
#                    configured interval). A dead/stalled cron => escalate to the troubleshooter (main navi).
#
#  ros progress     (monitor job #4, "work isn't landing") — time-since-last-REAL-progress per in-flight
#                    claim, NOT just liveness. Distinguishes the v3 stall taxonomy's "work not landing":
#                       local-but-uncommitted  : claim's registry file changed on disk but not committed
#                       committee-done-no-verdict : committee_run _status==ALL_COMMITTEE_DONE but no verdict
#                       gpu-result-not-resubmitted : a GPU result landed (channel) but not acked/advanced
#                    Threshold: > stall_min (default 20) global; GPU stage > gpu_stall_min (default 30).
#                    Over threshold => notify the ORCHESTRATOR to troubleshoot (exit 3).

def _cron_table(root):
    """v3 cron table from config (crons:). Each entry: interval_min + alive_stale_x. Falls back to the
    design defaults if the key is absent (so the command works on any instance)."""
    cfg = _cfg(root)
    crons = cfg.get("crons") or {}
    defaults = {
        "monitor":          {"interval_min": 5, "alive_stale_x": 3},
        "coordinator":      {"interval_min": 2, "alive_stale_x": 3},
        "committee_health": {"interval_min": 1, "alive_stale_x": 3},
        "proj_monitor":     {"interval_min": 5, "alive_stale_x": 3},
    }
    out = {}
    for name, d in (crons or defaults).items():
        d = d or {}
        out[name] = {"interval_min": float(d.get("interval_min", defaults.get(name, {}).get("interval_min", 5))),
                     "alive_stale_x": float(d.get("alive_stale_x", 3))}
    return out

def _alive_age_min(path, now):
    """Age in minutes of a .alive stamp (mtime). Returns None if the stamp is missing."""
    if not os.path.exists(path): return None
    try:
        import datetime as _dt
        mt = _dt.datetime.fromtimestamp(os.path.getmtime(path), _dt.timezone.utc)
        return (now - mt).total_seconds() / 60.0
    except OSError:
        return None

def cmd_cron_health(args):
    """★ v3 MONITOR JOB #1 (read-only): check every cron's runtime/cron/<job>.alive stamp. A stamp that
    is MISSING or older than alive_stale_x x its interval => that cron is dead/stalled => escalate to the
    TROUBLESHOOTER (main navi). Exits 3 if any cron is dead/stalled/never-started, 0 if all fresh."""
    import datetime as _dt
    root = inst_root(args); rd = runtime_dir(root)
    cron_dir = os.path.join(rd, "cron")
    now = _dt.datetime.now(_dt.timezone.utc)
    table = _cron_table(root)
    # optionally only check a subset (--cron monitor) — default = all configured crons
    only = getattr(args, "cron", None)
    names = [only] if only else sorted(table.keys())
    rows = []; bad = []
    for name in names:
        spec = table.get(name, {"interval_min": 5, "alive_stale_x": 3})
        interval = spec["interval_min"]; stale_x = spec["alive_stale_x"]
        budget = interval * stale_x
        stamp = os.path.join(cron_dir, f"{name}.alive")
        age = _alive_age_min(stamp, now)
        if age is None:
            state = "NEVER_STARTED"; bad.append(name)
        elif age > budget:
            state = "STALE"; bad.append(name)
        else:
            state = "FRESH"
        rows.append((name, state, age, interval, stale_x, budget))
    print(f"CRON HEALTH — {len(names)} cron(s), runtime/cron/*.alive stamps:")
    for name, state, age, interval, stale_x, budget in rows:
        mark = {"FRESH": "💓", "STALE": "🔴", "NEVER_STARTED": "⚪"}[state]
        agestr = f"{age:.1f}m" if age is not None else "—"
        print(f"  {mark} {name:18} {state:13} age={agestr:>7}  (interval={interval:g}m, "
              f"stale>{stale_x:g}x={budget:g}m)")
    if bad:
        print(f"\n🔴 {len(bad)} cron(s) DEAD/STALLED -> ESCALATE TO TROUBLESHOOTER (main navi): {', '.join(bad)}")
        sys.exit(3)
    print("\n✅ all crons fresh (every .alive within budget).")

def _git_committed_clean(root, relpath):
    """True if relpath has NO uncommitted changes (staged or unstaged) in the instance git repo.
    Used to detect a 'local-but-uncommitted' claim (registry file changed on disk, not yet committed)."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", root, "status", "--porcelain", "--", relpath],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0 and r.stdout.strip() == ""
    except Exception:
        return True  # can't tell -> don't false-flag

def cmd_progress(args):
    """★ v3 MONITOR JOB #4 (read-only, the important one — 'work isn't landing'): time-since-last-REAL
    progress per in-flight claim. Distinguishes the v3 'work not landing' stalls from mere liveness:
       local-but-uncommitted       — claim registry file modified on disk but not git-committed
       committee-done-no-verdict    — committee_run _status==ALL_COMMITTEE_DONE but no verdict for the claim
       gpu-result-not-resubmitted   — a GPU result landed in the channel (unacked) for the claim's exp
    Threshold from config: progress.stall_min (default 20) global; progress.gpu_stall_min (default 30) for
    the GPU stage. Over threshold => notify the ORCHESTRATOR (exit 3). A claim making normal forward motion
    or fully committed/dispositioned is silent."""
    import datetime as _dt
    root = inst_root(args); rd = runtime_dir(root); cfg = _cfg(root)
    pcfg = cfg.get("progress") or {}
    stall_min = float(pcfg.get("stall_min", 20))
    gpu_stall_min = float(pcfg.get("gpu_stall_min", 30))
    now = _dt.datetime.now(_dt.timezone.utc)
    TERMINAL_LS = ("done",)

    # index pending committee-queue submissions (claim already forwarded) so we don't double-flag
    pending_q = set()
    try:
        if Channel is not None:
            for it in _committee_channel(root).list(include_acked=False):
                if it.get("claim_id"): pending_q.add(it["claim_id"])
    except Exception:
        pass

    # index unacked GPU results by exp_id (gpu-result-not-resubmitted)
    gpu_results_by_exp = {}
    try:
        if Channel is not None:
            for it in _gpu_result_channel(root).list(include_acked=False):
                exp = it.get("exp_id")
                if not exp: continue
                try:
                    sub_t = _dt.datetime.strptime(it.get("submitted_at",""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
                    age = (now - sub_t).total_seconds()/60.0
                except Exception:
                    age = 1e9
                # keep the OLDEST unacked result per exp (worst-case stall)
                if exp not in gpu_results_by_exp or age > gpu_results_by_exp[exp]["age"]:
                    gpu_results_by_exp[exp] = {"age": age, "id": it.get("id"), "fault": it.get("fault")}
    except Exception:
        pass

    # index committee_run dirs that are ALL_COMMITTEE_DONE, keyed by any CLAIM-id in the dir name
    committee_done = {}  # claim_id -> {age, dir}
    for sd in glob.glob(os.path.join(rd, "committee_run_*")):
        if not os.path.isdir(sd): continue
        status_f = os.path.join(sd, "_status.txt")
        try:
            status = open(status_f).read().strip()
        except Exception:
            continue
        if status != "ALL_COMMITTEE_DONE": continue
        m = re.search(r"(CLAIM-\d+)", os.path.basename(sd))
        if not m: continue
        cid = m.group(1)
        try:
            age = (now - _dt.datetime.fromtimestamp(os.path.getmtime(status_f), _dt.timezone.utc)).total_seconds()/60.0
        except OSError:
            age = 1e9
        if cid not in committee_done or age < committee_done[cid]["age"]:
            committee_done[cid] = {"age": age, "dir": os.path.basename(sd)}

    # index existing verdicts by claim, tracking the NEWEST verdict-file mtime (for two-pass: a verdict
    # from committee pass #1 must NOT mask a pass #2 run that finished later with no new verdict).
    verdicts_by_claim = {}  # claim_id -> newest verdict file mtime (epoch)
    for fn in glob.glob(reg_dir(root, "verdicts", "**", "VERDICT-*.yaml"), recursive=True):
        v = load_yaml(fn, {}) or {}
        cid = v.get("claim_id")
        if not cid: continue
        try:
            mt = os.path.getmtime(fn)
        except OSError:
            mt = 0
        if cid not in verdicts_by_claim or mt > verdicts_by_claim[cid]:
            verdicts_by_claim[cid] = mt

    stalls = []
    inflight = 0
    for fn in glob.glob(reg_dir(root, "claims", "**", "CLAIM-*.yaml"), recursive=True):
        c = load_yaml(fn, {}) or {}
        ls = c.get("lifecycle_state", "drafted")
        if ls in TERMINAL_LS or c.get("status") == "green": continue
        cid = c.get("claim_id", "?"); pid = c.get("project_id", "?")
        inflight += 1
        relpath = os.path.relpath(fn, root)

        # (a) local-but-uncommitted: claim file dirty in git + dirty for > stall_min
        if not _git_committed_clean(root, relpath):
            try:
                age = (now - _dt.datetime.fromtimestamp(os.path.getmtime(fn), _dt.timezone.utc)).total_seconds()/60.0
            except OSError:
                age = 1e9
            if age > stall_min:
                stalls.append((cid, pid, "local-but-uncommitted", age, stall_min,
                               f"{relpath} modified on disk, not committed"))

        # (b) committee-done-no-verdict: ALL_COMMITTEE_DONE run exists, but no verdict written SINCE it
        #     finished (two-pass aware: compare committee-run finish mtime vs the newest verdict mtime).
        cd = committee_done.get(cid)
        if cd:
            try:
                done_mt = os.path.getmtime(os.path.join(rd, cd["dir"], "_status.txt"))
            except OSError:
                done_mt = 0
            verdict_mt = verdicts_by_claim.get(cid, 0)
            resolved = verdict_mt >= done_mt  # a verdict written at/after this committee run resolves it
            if cd["age"] > stall_min and not resolved:
                stalls.append((cid, pid, "committee-done-no-verdict", cd["age"], stall_min,
                               f"{cd['dir']} ALL_COMMITTEE_DONE, no verdict written since"))

        # (c) gpu-result-not-resubmitted: any of the claim's active/cited exps has an unacked GPU result
        exps = list(c.get("active_experiments", []) or [])
        for se in (c.get("supporting_evidence", []) or []):
            if se.get("exp_id"): exps.append(se["exp_id"])
        for exp in set(exps):
            gr = gpu_results_by_exp.get(exp)
            if gr and gr["age"] > gpu_stall_min:
                faulttag = " (FAULT)" if gr.get("fault") else ""
                stalls.append((cid, pid, "gpu-result-not-resubmitted", gr["age"], gpu_stall_min,
                               f"{exp} result {gr['id']} unacked{faulttag}"))

    print(f"PROGRESS — time-since-last-real-landing across {inflight} in-flight claim(s) "
          f"(stall>{stall_min:g}m, GPU>{gpu_stall_min:g}m):")
    if not stalls:
        print("✅ no work-not-landing stalls (every in-flight claim is committed / progressing / dispositioned).")
        return
    # worst first
    stalls.sort(key=lambda s: s[3], reverse=True)
    for cid, pid, kind, age, thr, detail in stalls:
        print(f"  🔴 {cid} ({pid}) {kind}: {age:.1f}m > {thr:g}m — {detail}")
    print(f"\n🔴 {len(stalls)} stall(s) -> NOTIFY ORCHESTRATOR to troubleshoot (work not landing).")
    sys.exit(3)


# ============================ v3 TWO-TIER LEARNING (warm-start / daily / distill) ============================
# DESIGN_v3_lean_architecture.md "LEARNING SYSTEM (two tiers)". Replaces the v2 flat learning/ graveyard
# (per-agent handoff+buglog files re-read on every boot -> context bloat -> self-kill death spiral).
#   TIER 1  learning/roles/<role>.md        — curated role-family brain, SMALL. A fresh/successor agent
#                                             reads THIS on boot (cheap warm-start), NOT raw history.
#   TIER 2  learning/<role>/YYYY-MM-DD.md    — append-only per-role per-date daily log.
# ON RETIRE@350k: distill the most valuable learnings -> append to BOTH Tier-1 brain + Tier-2 dated.
# A "role" here is the role-FAMILY (orchestrator / researcher / committee), not the per-generation id.

_ROLE_FAMILIES = ("orchestrator", "researcher", "committee", "monitor", "coordinator",
                  "committee_health", "proj_monitor")
# The 6 committee member roles all fold into the 'committee' role-family (one shared reviewer brain).
_COMMITTEE_MEMBER_ROLES = ("novelty_killer", "systems_reviewer", "evaluation_prosecutor",
                           "theory_skeptic", "product_realist", "area_chair")

def _role_family(role):
    """Normalize an agent id/role to its role-family (the Tier-1 brain key). e.g. orchestrator-r15-001
    -> orchestrator; researcher-0026-lift-r8 -> researcher; sub-monitor-0013-r17 -> proj_monitor;
    novelty_killer/area_chair/... -> committee."""
    r = (role or "").lower()
    if "sub-monitor" in r or "submonitor" in r or "proj" in r: return "proj_monitor"
    if "orchestrat" in r: return "orchestrator"
    if "research" in r or "seeder" in r: return "researcher"
    if "committee_health" in r or "committee-health" in r: return "committee_health"
    if "committee" in r or "reviewer" in r: return "committee"
    if any(m in r for m in _COMMITTEE_MEMBER_ROLES): return "committee"
    if "coordinat" in r: return "coordinator"
    if "monitor" in r: return "monitor"
    for fam in _ROLE_FAMILIES:
        if r == fam: return fam
    return r or "unknown"

def _tier1_path(root, role):
    return os.path.join(root, "learning", "roles", f"{_role_family(role)}.md")

def _tier2_path(root, role, date=None):
    return os.path.join(root, "learning", _role_family(role), f"{_valid_date(date)}.md")

def cmd_learn_warm(args):
    """v3 WARM-START (read-only): print the Tier-1 role-family brain a fresh/successor agent reads on boot.
    Cheap (small curated file), NOT raw history. Optionally also tails today's Tier-2 daily log."""
    root = inst_root(args); fam = _role_family(args.role)
    t1 = _tier1_path(root, args.role)
    if os.path.exists(t1):
        print(f"===== TIER-1 ROLE BRAIN: {fam} ({os.path.relpath(t1, root)}) =====")
        sys.stdout.write(open(t1).read())
        if not open(t1).read().endswith("\n"): print()
    else:
        print(f"(no Tier-1 brain yet for role '{fam}' — first agent of this family; "
              f"distill learnings at retire to seed {os.path.relpath(t1, root)})")
    if getattr(args, "with_today", False):
        t2 = _tier2_path(root, args.role)
        if os.path.exists(t2):
            print(f"\n===== TIER-2 TODAY: {fam} ({os.path.relpath(t2, root)}) =====")
            sys.stdout.write(open(t2).read())

def _append_md(path, text, header=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a") as f:
        if new and header: f.write(header + "\n")
        f.write(text if text.endswith("\n") else text + "\n")

def cmd_learn_note(args):
    """v3 TIER-2 daily append: append a timestamped note to learning/<role>/YYYY-MM-DD.md (append-only).
    --text inline or --file <path> to append a file's contents. --by tags the author agent id."""
    root = inst_root(args); fam = _role_family(args.role)
    text = args.text or ""
    if getattr(args, "file", None):
        try: text = (text + "\n" if text else "") + open(args.file).read()
        except OSError as e: sys.exit(f"❌ cannot read --file {args.file}: {e}")
    if not text.strip(): sys.exit("❌ nothing to append (need --text or --file)")
    by = f" [{args.by}]" if getattr(args, "by", None) else ""
    entry = f"\n## {NOW()}{by}\n{text.rstrip()}\n"
    t2 = _tier2_path(root, args.role)
    _append_md(t2, entry, header=f"# TIER-2 daily log — role-family: {fam}")
    print(f"✅ appended to Tier-2 {os.path.relpath(t2, root)} (role-family {fam})")

def cmd_learn_distill(args):
    """v3 RETIRE@350k DISTILL: append the most-valuable distilled learnings to BOTH the Tier-1 role brain
    AND the Tier-2 dated log. Called by an ever-run agent when it hits the token ceiling, just before it
    hands to its ONE successor (so the successor warm-starts from an updated Tier-1 brain)."""
    root = inst_root(args); fam = _role_family(args.role)
    text = args.text or ""
    if getattr(args, "file", None):
        try: text = (text + "\n" if text else "") + open(args.file).read()
        except OSError as e: sys.exit(f"❌ cannot read --file {args.file}: {e}")
    if not text.strip(): sys.exit("❌ nothing to distill (need --text or --file)")
    by = f" [{args.by}]" if getattr(args, "by", None) else ""
    t1 = _tier1_path(root, args.role); t2 = _tier2_path(root, args.role)
    block = f"\n## DISTILLED {NOW()}{by}\n{text.rstrip()}\n"
    _append_md(t1, block, header=f"# TIER-1 ROLE BRAIN — {fam}\n# Curated warm-start. A fresh/successor agent reads THIS on boot (NOT raw history).")
    _append_md(t2, block, header=f"# TIER-2 daily log — role-family: {fam}")
    print(f"✅ distilled to BOTH tiers:")
    print(f"   Tier-1 brain: {os.path.relpath(t1, root)}")
    print(f"   Tier-2 dated: {os.path.relpath(t2, root)}")
    # nudge: keep the Tier-1 brain SMALL (warn if it's growing past a soft cap)
    try:
        sz = os.path.getsize(t1)
        if sz > 16000:
            print(f"   ⚠️ Tier-1 brain is {sz} bytes (>16KB) — prune/curate it; warm-start must stay cheap.")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(prog="ros", description="research-os engine CLI")
    ap.add_argument("--instance", help="instance repo root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("projects").set_defaults(fn=cmd_projects)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    gp=sub.add_parser("gpu"); gps=gp.add_subparsers(dest="sub",required=True)
    gq=gps.add_parser("queue"); gq.add_argument("--exp",required=True); gq.add_argument("--gpu-type",dest="gpu_type")
    gq.add_argument("--priority",type=int); gq.add_argument("--force",action="store_true"); gq.set_defaults(fn=cmd_gpu_queue)
    gps.add_parser("status").set_defaults(fn=cmd_gpu_status)
    gpp=gps.add_parser("poll"); gpp.add_argument("--node",required=True); gpp.set_defaults(fn=cmd_gpu_poll)
    gpr=gps.add_parser("release"); gpr.add_argument("--node",required=True); gpr.set_defaults(fn=cmd_gpu_release)
    # v2 STAGE B: backend registry
    bk=sub.add_parser("backend"); bks=bk.add_subparsers(dest="sub",required=True)
    bkr=bks.add_parser("register")
    bkr.add_argument("--id",required=True); bkr.add_argument("--kind",required=True,choices=["cpu","gpu"])
    bkr.add_argument("--gpu-type",dest="gpu_type"); bkr.add_argument("--node")
    bkr.add_argument("--host-mem-floor",dest="host_mem_floor",type=int,default=0)
    bkr.set_defaults(fn=cmd_backend_register)
    bkh=bks.add_parser("heartbeat"); bkh.add_argument("--id",required=True)
    bkh.add_argument("--status"); bkh.add_argument("--task"); bkh.add_argument("--lease")
    bkh.set_defaults(fn=cmd_backend_heartbeat)
    bks.add_parser("list").set_defaults(fn=cmd_backend_list)
    # v2 STAGE B: gpu task channel (orchestrator -> gpu_coordinator)
    gt=sub.add_parser("gpu-task"); gts=gt.add_subparsers(dest="sub",required=True)
    # v2: task_coordinator self-feed discovery (replaces orchestrator-driven gpu-task submission)
    gpd=sub.add_parser("gpu-pending"); gpd.add_argument("--gpu-type",dest="gpu_type",help="H100|MI350X (filter)")
    gpd.set_defaults(fn=cmd_gpu_pending)
    gtsub=gts.add_parser("submit")
    gtsub.add_argument("--exp",required=True); gtsub.add_argument("--gpu-type",dest="gpu_type",help="H100|MI350X|any")
    gtsub.add_argument("--host-mem-floor",dest="host_mem_floor",type=int)
    gtsub.add_argument("--priority",type=int); gtsub.add_argument("--by"); gtsub.add_argument("--summary")
    gtsub.add_argument("--force",action="store_true",help="override committee-approval requirement (explicit)")
    gtsub.set_defaults(fn=cmd_gpu_task_submit)
    gtl=gts.add_parser("list"); gtl.add_argument("--all",action="store_true",help="include acked")
    gtl.set_defaults(fn=cmd_gpu_task_list)
    gta=gts.add_parser("ack"); gta.add_argument("--id"); gta.add_argument("--all",action="store_true")
    gta.add_argument("--by"); gta.add_argument("--answer"); gta.set_defaults(fn=cmd_gpu_task_ack)
    # v2 STAGE B: gpu result channel (gpu_coordinator -> orchestrator)
    gr=sub.add_parser("gpu-result"); grs=gr.add_subparsers(dest="sub",required=True)
    grsub=grs.add_parser("submit")
    grsub.add_argument("--exp",required=True); grsub.add_argument("--task",help="GT-id this result is for")
    grsub.add_argument("--effect",required=True,help="kill|weaken|keep-exploring|promote|archive|support")
    grsub.add_argument("--summary"); grsub.add_argument("--artifacts-path",dest="artifacts_path")
    grsub.add_argument("--by"); grsub.add_argument("--fault",action="store_true",help="run faulted (crash/host-mem)")
    grsub.set_defaults(fn=cmd_gpu_result_submit)
    grl=grs.add_parser("list"); grl.add_argument("--all",action="store_true",help="include acked")
    grl.set_defaults(fn=cmd_gpu_result_list)
    gra=grs.add_parser("ack"); gra.add_argument("--id"); gra.add_argument("--all",action="store_true")
    gra.add_argument("--by"); gra.add_argument("--answer"); gra.set_defaults(fn=cmd_gpu_result_ack)
    ca=sub.add_parser("claim"); cas=ca.add_subparsers(dest="sub",required=True)
    cav=cas.add_parser("advance")
    cav.add_argument("--claim",required=True); cav.add_argument("--state",required=True,help="lifecycle_state")
    cav.add_argument("--next",help="next_action resume pointer"); cav.add_argument("--blocking",help="if blocked: what it waits on")
    cav.set_defaults(fn=cmd_claim_advance)
    vw=sub.add_parser("verdict"); vws=vw.add_subparsers(dest="sub",required=True)
    vwr=vws.add_parser("write")
    vwr.add_argument("--claim",required=True); vwr.add_argument("--final",required=True,help="green|yellow|red|kill|promote|needs-more-evidence")
    vwr.add_argument("--experiments",required=True,help="REQUIRED EXP-xxxx[,EXP-yyyy] that informed the verdict")
    vwr.add_argument("--votes",help="role:vote comma list, e.g. novelty_killer:yellow,area_chair:yellow")
    vwr.add_argument("--committee-version",dest="committee_version"); vwr.add_argument("--date")
    vwr.add_argument("--override-rule",dest="override_rule",action="store_true",help="bypass green_rule vote-count check (needs justification)")
    vwr.add_argument("--fatal",help="fatal_objections, ';'-separated")
    vwr.add_argument("--required",help="required_evidence to advance, ';'-separated")
    vwr.add_argument("--map-delta",dest="map_delta",help="academic-map delta proposals, ';'-separated")
    vwr.add_argument("--baselines",help="baseline_requirements, ';'-separated")
    vwr.add_argument("--allow-dup",dest="allow_dup",action="store_true",help="bypass idempotency dedup (force a duplicate verdict)")
    vwr.add_argument("--approves-exp",dest="approves_exp",help="EXP-id(s) the committee greenlights for the GPU queue (autonomous dispatch)")
    vwr.set_defaults(fn=cmd_verdict_write)
    # env discovery
    ev = sub.add_parser("env"); evs = ev.add_subparsers(dest="sub", required=True)
    evd = evs.add_parser("discover"); evd.add_argument("--probe", action="store_true", help="run config probe_cmd per node")
    evd.set_defaults(fn=cmd_env_discover)
    # agent registry + heartbeat + liveness
    ag = sub.add_parser("agent"); ags = ag.add_subparsers(dest="sub", required=True)
    agr = ags.add_parser("register")
    agr.add_argument("--id", required=True); agr.add_argument("--role", required=True)
    agr.add_argument("--backend"); agr.add_argument("--node"); agr.add_argument("--claim"); agr.add_argument("--exp")
    agr.add_argument("--project", help="project this agent owns (sub-monitors/researchers)"); agr.add_argument("--session", help="this agent's own session id (for revival/handoff)")
    agr.add_argument("--parent", help="supervising agent id (orchestrator for sub-monitors; sub-monitor for researchers) — builds the supervision tree")
    agr.add_argument("--gpu", help="gpu_type this agent coordinates (gpu_coordinator one-per-GPU keying, e.g. H100)")
    agr.set_defaults(fn=cmd_agent_register)
    hb = sub.add_parser("heartbeat")
    hb.add_argument("--agent", required=True); hb.add_argument("--status"); hb.add_argument("--note")
    hb.add_argument("--role"); hb.add_argument("--claim"); hb.add_argument("--exp")
    hb.add_argument("--project"); hb.add_argument("--session"); hb.add_argument("--gpu")
    hb.set_defaults(fn=cmd_heartbeat)
    sub.add_parser("liveness").set_defaults(fn=cmd_liveness)
    sub.add_parser("submonitors").set_defaults(fn=cmd_submonitors)
    sub.add_parser("lanes", help="ANTI-SPRAWL single-poller per-lane work plan (FORWARD|AWAIT|RESEED?|HOLD); read-only").set_defaults(fn=cmd_lanes)
    sub.add_parser("coordinators").set_defaults(fn=cmd_coordinators)
    cm = sub.add_parser("commit")
    cm.add_argument("--message", "-m", help="commit message (default: ros commit autosave <ts>)")
    cm.set_defaults(fn=cmd_commit)
    gaw = sub.add_parser("gpu-approve-window")
    gaw.add_argument("--hours", type=float, help="open the auto-approve window for N hours (default 24)")
    gaw.add_argument("--clear", action="store_true", help="close the window now")
    gaw.add_argument("--by"); gaw.add_argument("--note")
    gaw.set_defaults(fn=cmd_gpu_approve_window)
    rp = sub.add_parser("report")
    rp.add_argument("--agent", required=True); rp.add_argument("--role")
    rp.add_argument("--done"); rp.add_argument("--doing"); rp.add_argument("--blocked")
    rp.add_argument("--need"); rp.add_argument("--next"); rp.add_argument("--claim"); rp.add_argument("--exp")
    rp.add_argument("--ping-orchestrator", dest="ping_orchestrator", action="store_true")
    rp.set_defaults(fn=cmd_report)
    ib = sub.add_parser("inbox"); ibs = ib.add_subparsers(dest="sub")
    ib.add_argument("--action-only", dest="action_only", action="store_true"); ib.set_defaults(fn=cmd_inbox)
    iba = ibs.add_parser("ack"); iba.add_argument("--agent"); iba.add_argument("--all", action="store_true")
    iba.add_argument("--answer"); iba.add_argument("--action-only", dest="action_only", action="store_true")
    iba.set_defaults(fn=cmd_inbox_ack)
    ra = sub.add_parser("reports-age"); ra.add_argument("--window-min", dest="window_min", type=int, default=10)
    ra.set_defaults(fn=cmd_reports_age)
    # committee-submission queue (sub-monitor -> orchestrator decoupling)
    qp = sub.add_parser("queue"); qps = qp.add_subparsers(dest="sub", required=True)
    qsub = qps.add_parser("submit")
    qsub.add_argument("--claim", required=True, help="CLAIM-id the proposal is ready to take to committee")
    qsub.add_argument("--exp", help="EXP-id(s) cited as evidence, comma-separated")
    qsub.add_argument("--project"); qsub.add_argument("--by", help="sub-monitor agent id")
    qsub.add_argument("--researcher", help="researcher agent id whose proposal this is")
    qsub.add_argument("--kind", help="committee|verdict|review (default committee)")
    qsub.add_argument("--summary"); qsub.add_argument("--priority", type=int)
    qsub.set_defaults(fn=cmd_queue_submit)
    qls = qps.add_parser("list"); qls.add_argument("--all", action="store_true", help="include acked items")
    qls.set_defaults(fn=cmd_queue_list)
    qak = qps.add_parser("ack"); qak.add_argument("--id", help="queue id (Q-xxxx)")
    qak.add_argument("--all", action="store_true"); qak.add_argument("--answer"); qak.add_argument("--by")
    qak.set_defaults(fn=cmd_queue_ack)
    sp = sub.add_parser("seed"); ssub = sp.add_subparsers(dest="sub", required=True)
    sn = ssub.add_parser("new"); sn.add_argument("--claim", required=True); sn.add_argument("--why")
    sn.add_argument("--project"); sn.add_argument("--owner"); sn.add_argument("--prompt-version", dest="prompt_version")
    sn.add_argument("--force-revive", dest="force_revive", help="new evidence string to revive a cemetery dup")
    sn.add_argument("--ack-dup", dest="ack_dup", action="store_true", help="acknowledge a SOFT cemetery match and proceed")
    sn.set_defaults(fn=cmd_seed_new)
    ep = sub.add_parser("exp"); esub = ep.add_subparsers(dest="sub", required=True)
    er = esub.add_parser("register")
    er.add_argument("--claim", required=True, help="CLAIM-id or 'none' for pure infra probe")
    er.add_argument("--level", type=int, default=0, choices=[0,1,2,3])
    er.add_argument("--project"); er.add_argument("--session"); er.add_argument("--hardware")
    er.add_argument("--max-minutes", dest="max_minutes", type=int, default=15)
    er.add_argument("--max-gpu-hours", dest="max_gpu_hours", type=float, default=0.0)
    er.add_argument("--gpu", action="store_true", help="this experiment needs GPU (else CPU-only by default)")
    er.add_argument("--max-mem-gb", dest="max_mem_gb", type=int, default=0)
    er.add_argument("--host-mem-floor", dest="host_mem_floor", type=int, default=0)
    er.add_argument("--prompt-version", dest="prompt_version")
    er.set_defaults(fn=cmd_exp_register)
    ec = esub.add_parser("complete")
    ec.add_argument("--exp", required=True); ec.add_argument("--effect", required=True)
    ec.add_argument("--summary", required=True); ec.add_argument("--revival")
    ec.add_argument("--by", help="researcher agent id that ran this exp — auto-marks it 'completed' on EXP-terminal (BUG-60: avoids reaper false-DEAD of a finished researcher)")
    ec.add_argument("--commit", action="store_true", help="git-commit durable state after completing")
    ec.add_argument("--force-demote", dest="force_demote", action="store_true", help="explicitly allow killing/weakening a PROMOTED claim (BUG-25 guard override)")
    ec.set_defaults(fn=cmd_exp_complete)
    eg = esub.add_parser("gc"); eg.add_argument("--apply", action="store_true", help="actually retire (default dry-run)")
    eg.add_argument("--stale-min", dest="stale_min", type=int, default=30, help="min age (min) before a pending exp is gc-eligible")
    eg.set_defaults(fn=cmd_exp_gc)
    ed = esub.add_parser("dispatch")
    ed.add_argument("--exp", required=True); ed.add_argument("--node", required=True)
    ed.add_argument("--by", help="orchestrator id"); ed.add_argument("--approve", action="store_true")
    ed.add_argument("--force", action="store_true", help="override committee-approval requirement (explicit)")
    ed.set_defaults(fn=cmd_exp_dispatch)
    # ---- supervision tree + task ledger + reaper (BUG-31..34) ----
    tk = sub.add_parser("task"); tks = tk.add_subparsers(dest="sub", required=True)
    tko = tks.add_parser("open")
    tko.add_argument("--kind", required=True, help="monitor|research|design|committee|gpu (task category)")
    tko.add_argument("--parent", help="parent agent id (e.g. orchestrator-r15-001)")
    tko.add_argument("--assignee", help="agent id that owns this task")
    tko.add_argument("--project"); tko.add_argument("--claim"); tko.add_argument("--exp")
    tko.add_argument("--session"); tko.add_argument("--summary")
    tko.set_defaults(fn=cmd_task_open)
    tku = tks.add_parser("update"); tku.add_argument("--id", required=True)
    tku.add_argument("--assignee"); tku.add_argument("--status", help="open|active|orphaned|done|dropped")
    tku.add_argument("--session"); tku.add_argument("--evidence-delta", dest="evidence_delta")
    tku.add_argument("--by"); tku.add_argument("--note")
    tku.set_defaults(fn=cmd_task_update)
    tkl = tks.add_parser("list"); tkl.add_argument("--open", action="store_true", help="only un-closed tasks")
    tkl.add_argument("--orphans", action="store_true", help="only orphaned tasks")
    tkl.add_argument("--project")
    tkl.set_defaults(fn=cmd_task_list)
    rpe = sub.add_parser("reap"); rpe.add_argument("--apply", action="store_true", help="actually flip statuses (default dry-run)")
    rpe.set_defaults(fn=cmd_reap)
    ho = sub.add_parser("handoff")
    ho.add_argument("--from", dest="frm", required=True); ho.add_argument("--to", required=True)
    ho.add_argument("--project"); ho.add_argument("--task")
    ho.add_argument("--researcher-state", dest="researcher_state", help="alive|dead|completed|none + detail")
    ho.add_argument("--seeder-state", dest="seeder_state", help="state of the claim-seeder researcher")
    ho.add_argument("--open-work", dest="open_work"); ho.add_argument("--evidence-delta", dest="evidence_delta")
    ho.add_argument("--reseed-needed", dest="reseed_needed", action="store_true",
                    help="monitored researcher/seeder is dead+work-open -> orchestrator must dispatch a fresh one")
    ho.add_argument("--by")
    ho.set_defaults(fn=cmd_handoff)
    tr = sub.add_parser("tree"); tr.add_argument("--all", action="store_true", help="include terminal/retired agents")
    tr.set_defaults(fn=cmd_tree)
    rt = sub.add_parser("retire", help="ATOMIC retire frm->to: register successor, re-parent tasks+supervised agents, flip frm retired, structured handoff")
    rt.add_argument("--from", dest="frm", required=True); rt.add_argument("--to", required=True)
    rt.add_argument("--project")
    rt.add_argument("--researcher-state", dest="researcher_state", help="alive|dead|completed + detail of the monitored researcher")
    rt.add_argument("--seeder-state", dest="seeder_state", help="state of the claim-seeder")
    rt.add_argument("--open-work", dest="open_work"); rt.add_argument("--evidence-delta", dest="evidence_delta")
    rt.add_argument("--reseed-needed", dest="reseed_needed", action="store_true",
                    help="monitored researcher/seeder dead + work open -> notify orchestrator to dispatch fresh")
    rt.add_argument("--by")
    rt.set_defaults(fn=cmd_retire)
    pt = sub.add_parser("project-tree", help="hierarchical project->sub-monitor->claim-seeder memory view (committed registry; no runtime overflow)")
    pt.set_defaults(fn=cmd_project_tree)
    # ---- v3 MONITOR EYES (read-only) ----
    ch = sub.add_parser("cron-health", help="v3 monitor #1 (read-only): check runtime/cron/<job>.alive stamps; stale > alive_stale_x x interval = dead -> escalate troubleshooter")
    ch.add_argument("--cron", help="check only this cron (default: all configured crons)")
    ch.set_defaults(fn=cmd_cron_health)
    pg = sub.add_parser("progress", help="v3 monitor #4 (read-only): time-since-last-real-landing per in-flight claim (local-but-uncommitted / committee-done-no-verdict / gpu-result-not-resubmitted); > stall_min(20)/gpu_stall_min(30) -> notify orchestrator")
    pg.set_defaults(fn=cmd_progress)
    # ---- v3 TWO-TIER LEARNING (warm-start / daily / distill) ----
    ln = sub.add_parser("learn"); lns = ln.add_subparsers(dest="sub", required=True)
    lw = lns.add_parser("warm", help="read-only Tier-1 role brain warm-start (what a fresh/successor agent reads on boot)")
    lw.add_argument("--role", required=True, help="role-family or agent id (orchestrator|researcher|committee|monitor|...)")
    lw.add_argument("--with-today", dest="with_today", action="store_true", help="also tail today's Tier-2 daily log")
    lw.set_defaults(fn=cmd_learn_warm)
    lnn = lns.add_parser("note", help="append a timestamped note to Tier-2 learning/<role>/YYYY-MM-DD.md")
    lnn.add_argument("--role", required=True); lnn.add_argument("--text"); lnn.add_argument("--file")
    lnn.add_argument("--by", help="author agent id")
    lnn.set_defaults(fn=cmd_learn_note)
    lnd = lns.add_parser("distill", help="retire@350k: append distilled learnings to BOTH Tier-1 brain AND Tier-2 dated log")
    lnd.add_argument("--role", required=True); lnd.add_argument("--text"); lnd.add_argument("--file")
    lnd.add_argument("--by", help="retiring agent id")
    lnd.set_defaults(fn=cmd_learn_distill)

    args = ap.parse_args(); args.fn(args)

if __name__ == "__main__": main()
