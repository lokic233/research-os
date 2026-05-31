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
    with open(path) as f: return YAML.safe_load(f) or default

def dump_yaml(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f: YAML.safe_dump(obj, f, sort_keys=False, default_flow_style=False)

def next_id(root, kind, prefix):
    """Scan registry/<kind>/ for PREFIX-NNNN.yaml, return next zero-padded id."""
    d = reg_dir(root, kind)
    os.makedirs(d, exist_ok=True)
    mx = 0
    for fn in glob.glob(os.path.join(d, f"{prefix}-*.yaml")):
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
    for fn in glob.glob(reg_dir(root, "cemetery", "DEAD-*.yaml")):
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
        "created_at": NOW(), "last_updated": NOW(),
    }
    if conflict and args.force_revive:
        obj["revival_conditions"] = f"REVIVED from {conflict['dead_id']} with new evidence: {args.force_revive}"
    path = reg_dir(root, "claims", f"{cid}.yaml")
    dump_yaml(path, obj)
    print(f"✅ {cid} created -> {os.path.relpath(path, root)}")
    if conflict: print(f"   ⚠️ force-revived past {conflict['dead_id']}")

# ---------------- INVARIANT 1: experiment registration ----------------
def cmd_exp_register(args):
    root = inst_root(args)
    # claim must exist (unless explicitly infra probe)
    if args.claim and args.claim != "none":
        cf = reg_dir(root, "claims", f"{args.claim}.yaml")
        if not os.path.exists(cf):
            sys.exit(f"❌ claim {args.claim} not found. Register the claim/seed first.")
    eid = next_id(root, "experiments", "EXP")
    obj = {
        "exp_id": eid, "project_id": args.project or "PROJ-0000",
        "claim_id": (None if args.claim=="none" else args.claim),
        "session_id": args.session or "", "level": args.level, "hardware": args.hardware or "",
        "resource_budget": {"max_wall_clock_minutes": args.max_minutes, "max_gpu_hours": args.max_gpu_hours,
                            "max_memory_gb": args.max_mem_gb, "host_mem_floor_gb": args.host_mem_floor},
        "status": "pending", "result_summary": "", "result_effect": "",
        "artifacts_path": f"experiments/{eid}/", "reproducibility": "", "linked_verdicts": [],
        "started_at": "", "completed_at": "", "prompt_version": args.prompt_version or "",
    }
    dump_yaml(reg_dir(root, "experiments", f"{eid}.yaml"), obj)
    os.makedirs(os.path.join(root, "experiments", eid, "results"), exist_ok=True)
    os.makedirs(os.path.join(root, "experiments", eid, "logs"), exist_ok=True)
    if args.level >= 2:
        print(f"✅ {eid} registered (level {args.level}) — ⚠️ level>=2 requires orchestrator approval before running.")
    else:
        print(f"✅ {eid} registered (level {args.level}). Artifact dir: experiments/{eid}/")
    print(f"   budget: {obj['resource_budget']}")

def cmd_exp_complete(args):
    root = inst_root(args)
    ef = reg_dir(root, "experiments", f"{args.exp}.yaml")
    exp = load_yaml(ef)
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
        cf = reg_dir(root, "claims", f"{cid}.yaml"); claim = load_yaml(cf)
        if claim:
            entry = {"exp_id": args.exp, "summary": args.summary, "data_path": exp["artifacts_path"]}
            if args.effect in ("kill","weaken"): claim["negative_evidence"].append(entry)
            else: claim["supporting_evidence"].append(entry)
            if args.effect == "kill": claim["status"] = "killed"
            elif args.effect == "weaken": claim["status"] = "weakened"
            elif args.effect == "promote": claim["status"] = "promoted"
            claim["last_updated"] = NOW()
            dump_yaml(cf, claim)
            note.append(f"claim {cid} -> {claim['status']}")
            # auto-cemetery on kill
            if args.effect == "kill":
                did = next_id(root, "cemetery", "DEAD")
                dump_yaml(reg_dir(root, "cemetery", f"{did}.yaml"), {
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
    def count(kind, pre): return len(glob.glob(reg_dir(root, kind, f"{pre}-*.yaml")))
    print(f"research-os instance: {root}")
    print(f"  claims:      {count('claims','CLAIM')}")
    print(f"  experiments: {count('experiments','EXP')}")
    print(f"  verdicts:    {count('verdicts','VERDICT')}")
    print(f"  cemetery:    {count('cemetery','DEAD')}")
    # claim status breakdown
    by = {}
    for fn in glob.glob(reg_dir(root,'claims','CLAIM-*.yaml')):
        c = load_yaml(fn,{}); by[c.get('status','?')] = by.get(c.get('status','?'),0)+1
    if by: print("  claim status:", ", ".join(f"{k}={v}" for k,v in sorted(by.items())))

def cmd_init(args):
    root = inst_root(args)
    for d in ["registry/claims","registry/experiments","registry/verdicts","registry/cemetery",
              "experiments","prior_art","sessions","learning","projects"]:
        os.makedirs(os.path.join(root,d), exist_ok=True)
        open(os.path.join(root,d,".gitkeep"),"a").close()
    for f,seed in [("registry/academic_map.yaml",{"nodes":[]}),("registry/baselines.yaml",{"families":[]}),
                   ("registry/projects.yaml",{"projects":[]})]:
        p=os.path.join(root,f)
        if not os.path.exists(p): dump_yaml(p, seed)
    print(f"✅ initialized research-os instance at {root}")
    print("   next: create research-os.config.yaml (see engine repo schemas/config.schema.yaml)")

def main():
    ap = argparse.ArgumentParser(prog="ros", description="research-os engine CLI")
    ap.add_argument("--instance", help="instance repo root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("status").set_defaults(fn=cmd_status)
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
    er.add_argument("--max-gpu-hours", dest="max_gpu_hours", type=float, default=0.25)
    er.add_argument("--max-mem-gb", dest="max_mem_gb", type=int, default=0)
    er.add_argument("--host-mem-floor", dest="host_mem_floor", type=int, default=0)
    er.add_argument("--prompt-version", dest="prompt_version")
    er.set_defaults(fn=cmd_exp_register)
    ec = esub.add_parser("complete")
    ec.add_argument("--exp", required=True); ec.add_argument("--effect", required=True)
    ec.add_argument("--summary", required=True); ec.add_argument("--revival")
    ec.set_defaults(fn=cmd_exp_complete)
    args = ap.parse_args(); args.fn(args)

if __name__ == "__main__": main()
