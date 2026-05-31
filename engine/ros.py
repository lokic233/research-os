#!/usr/bin/env python3
"""
ros.py — research-os engine CLI. Platform-agnostic. Runs IN an instance repo (cwd = instance root,
or pass --instance). Holds the two hard invariants:
  (1) no experiment runs unregistered  -> `ros exp register` is the only door; it mints EXP-id.
  (2) no cemetery idea resurrects       -> `ros seed new` refuses near-duplicates of DEAD-* entries.

Dependencies: pyyaml only. Python 3.9+.
"""
import argparse, os, sys, re, datetime, glob, hashlib

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
    date = date or _d.datetime.now(_d.timezone.utc).strftime("%Y-%m-%d")
    d = os.path.join(reg_dir(root, kind), project_id or "PROJ-0000", date)
    os.makedirs(d, exist_ok=True); return d

def next_id(root, kind, prefix):
    """Next zero-padded id. Experiments live as experiments/<date>/<EXP-id>/ (dir names);
    other objects as registry/<kind>/**/PREFIX-*.yaml. Scan the RIGHT place to avoid id reuse."""
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
    return f"{prefix}-{mx+1:04d}"

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
    # propagate to claim ledger
    cid = exp.get("claim_id")
    note = []
    if cid:
        cf = find_obj(root, "claims", cid); claim = load_yaml(cf) if cf else None
        if claim:
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

def cmd_agent_register(args):
    root = inst_root(args); rd = runtime_dir(root)
    d = os.path.join(rd, "agents"); os.makedirs(d, exist_ok=True)
    rec = {"agent_id": args.id, "role": args.role, "backend": args.backend or "",
           "node": args.node or "", "status": "running", "spawned_at": NOW(),
           "last_heartbeat": NOW(), "current_claim_id": args.claim or "", "current_exp_id": args.exp or "",
           "note": "", "heartbeat_count": 0}
    dump_yaml(os.path.join(d, f"{args.id}.yaml"), rec)
    print(f"✅ agent {args.id} registered (role={args.role}). Heartbeat: `ros heartbeat --agent {args.id}` every ~{(_cfg(root).get('liveness') or {}).get('kick_interval_minutes',15)}m")

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
    if exp.get("status") not in ("pending",): sys.exit(f"❌ {args.exp} is '{exp.get('status')}', not dispatchable.")
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
    bcfg = (cfg.get("budgets") or {}).get(f"level{lvl}", {})
    if bcfg.get("requires_orchestrator_approval") and not args.approve:
        sys.exit(f"❌ level {lvl} requires orchestrator approval: re-run with --approve.")
    if bcfg.get("requires_explicit_human_approval") and not args.human_approved:
        sys.exit(f"❌ level {lvl} (dangerous) requires explicit human approval: re-run with --human-approved.")
    exp["status"] = "running"; exp["started_at"] = NOW()
    exp["dispatched_by"] = args.by or "orchestrator"
    exp["node_lease"] = f"{args.node}:{exp.get('exp_id')}"
    dump_yaml(ef, exp)
    print(f"🚀 dispatched {args.exp} -> {args.node} ({node.get('gpu_type','?')}, fragile={bool(node.get('fragile'))})")
    print(f"   host_mem_floor={floor}GB  budget={budget}  by={exp['dispatched_by']}")
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
    inbox = load_yaml(os.path.join(rd, "orchestrator", "inbox.yaml"), {"items": []}) or {"items": []}
    needs_action = bool(args.blocked or args.need)
    inbox["items"].append({**rec, "acked": False, "needs_action": needs_action})
    dump_yaml(os.path.join(rd, "orchestrator", "inbox.yaml"), inbox)
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
    inbox = load_yaml(os.path.join(rd, "orchestrator", "inbox.yaml"), {"items": []}) or {"items": []}
    items = [i for i in inbox["items"] if not i.get("acked")]
    if args.action_only: items = [i for i in items if i.get("needs_action")]
    if not items: print("(inbox empty — no pending reports)"); return
    print(f"ORCHESTRATOR INBOX — {len(items)} pending:")
    for i in items:
        tag = "❗ACTION" if i.get("needs_action") else "  info "
        print(f"  [{tag}] {i['ts']} {i['agent']} [{i.get('role','')}]")
        if i.get("done"):    print(f"          done:    {i['done']}")
        if i.get("doing"):   print(f"          doing:   {i['doing']}")
        if i.get("blocked"): print(f"          BLOCKED: {i['blocked']}")
        if i.get("need"):    print(f"          NEEDS:   {i['need']}")
        if i.get("next"):    print(f"          next:    {i['next']}")

def cmd_inbox_ack(args):
    root = inst_root(args); rd = runtime_dir(root)
    p = os.path.join(rd, "orchestrator", "inbox.yaml")
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


def cmd_projects(args):
    root=inst_root(args)
    pdir=os.path.join(root,"projects")
    found=sorted(glob.glob(os.path.join(pdir,"PROJ-*")))
    if not found: print("(no projects — create projects/<PROJ-id>/project_overview.md)"); return
    print(f"projects ({len(found)}):")
    for d in found:
        pid=os.path.basename(d)
        ov=os.path.join(d,"project_overview.md")
        title=""
        if os.path.exists(ov):
            for ln in open(ov):
                if ln.startswith("# "): title=ln[2:].strip(); break
        reports=sorted(glob.glob(os.path.join(d,"*","progress_report.md")))
        last=os.path.basename(os.path.dirname(reports[-1])) if reports else "no report"
        print(f"  {pid}: {title}  (latest progress: {last})")


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
        nmembers=len(members) or 6
        vals=[ (p.get("vote") or "").lower() for p in parsed ]
        if len(parsed) < nmembers:
            sys.exit(f"❌ green_rule={rule}: {args.final} needs all {nmembers} committee votes via --votes; "
                     f"got {len(parsed)}. (use --override-rule only with explicit justification.)")
        if rule=="unanimous" and any(v!="green" for v in vals):
            sys.exit(f"❌ green_rule=unanimous: every vote must be green for a {args.final} verdict; got {vals}.")
    vid=next_id(root,"verdicts","VERDICT")
    import datetime as _d
    date=args.date or _d.datetime.now(_d.timezone.utc).strftime("%Y-%m-%d")
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
    else:
        claim["lifecycle_state"]="verdict_recorded"
        claim["next_action"]=f"{vid}={args.final}: address required_evidence to advance"
    claim["last_updated"]=NOW(); dump_yaml(cf,claim)
    for eid in exp_ids:
        ep=glob.glob(os.path.join(root,"experiments","**",eid,"experiment.yaml"),recursive=True)[0]
        e=load_yaml(ep); e.setdefault("linked_verdicts",[]).append(vid); dump_yaml(ep,e)
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


def main():
    ap = argparse.ArgumentParser(prog="ros", description="research-os engine CLI")
    ap.add_argument("--instance", help="instance repo root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("projects").set_defaults(fn=cmd_projects)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
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
    agr.set_defaults(fn=cmd_agent_register)
    hb = sub.add_parser("heartbeat")
    hb.add_argument("--agent", required=True); hb.add_argument("--status"); hb.add_argument("--note")
    hb.add_argument("--role"); hb.add_argument("--claim"); hb.add_argument("--exp")
    hb.set_defaults(fn=cmd_heartbeat)
    sub.add_parser("liveness").set_defaults(fn=cmd_liveness)
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
    ec.set_defaults(fn=cmd_exp_complete)
    ed = esub.add_parser("dispatch")
    ed.add_argument("--exp", required=True); ed.add_argument("--node", required=True)
    ed.add_argument("--by", help="orchestrator id"); ed.add_argument("--approve", action="store_true")
    ed.add_argument("--human-approved", dest="human_approved", action="store_true")
    ed.set_defaults(fn=cmd_exp_dispatch)
    args = ap.parse_args(); args.fn(args)

if __name__ == "__main__": main()
