#!/usr/bin/env python3
"""Data-driven orchestrator monitor. Reads runtime/ + registry/, computes 10-min deltas,
flags stalls, surfaces researcher Q&A. Writes runtime/monitor/report-<ts>.md + prints summary.
Usage: monitor_report.py --instance <root> [--window-min 10]"""
import os, sys, glob, json, datetime, argparse
try: import yaml
except ImportError: sys.exit("need pyyaml (/usr/bin/python3)")

NOW = datetime.datetime.now(datetime.timezone.utc)
def parse_ts(s):
    try: return datetime.datetime.strptime(s,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except: return None
def load(p,d=None):
    try:
        with open(p) as f: return yaml.safe_load(f) or d
    except: return d
def age_min(ts):
    t=parse_ts(ts); return None if not t else round((NOW-t).total_seconds()/60,1)

ap=argparse.ArgumentParser(); ap.add_argument("--instance",required=True); ap.add_argument("--window-min",type=int,default=10)
a=ap.parse_args(); root=a.instance; W=a.window_min
cfg=load(os.path.join(root,"research-os.config.yaml"),{}) or {}
liv=cfg.get("liveness",{}) or {}; kick=int(liv.get("kick_interval_minutes",15)); grace=int(liv.get("patient_grace_minutes",45))
rd=os.path.join(root,"runtime")

def reg_count(kind,pre): return len(glob.glob(os.path.join(root,"registry",kind,f"{pre}-*.yaml")))
def reg_recent(kind,pre,field):
    out=[]
    for fn in glob.glob(os.path.join(root,"registry",kind,f"{pre}-*.yaml")):
        o=load(fn,{}); am=age_min(o.get(field,""))
        if am is not None and am<=W: out.append(o)
    return out

# --- agents / liveness ---
agents=[load(fn,{}) for fn in glob.glob(os.path.join(rd,"agents","*.yaml"))]
def state(a_):
    am=age_min(a_.get("last_heartbeat",""))
    if am is None: return "UNKNOWN",am
    return ("alive" if am<=kick*1.5 else "stale" if am<=grace else "DEAD"),am
roster=[]
for ag in agents:
    st,am=state(ag); roster.append((ag.get("agent_id","?"),ag.get("role","?"),ag.get("status","?"),am,st,ag.get("note","")))
researchers=[r for r in roster if r[1]=="researcher" or "researcher" in r[0]]
committee=[r for r in roster if "committee" in r[0] or r[1] in ("novelty_killer","systems_reviewer","evaluation_prosecutor","theory_skeptic","product_realist","area_chair")]
dead=[r for r in roster if r[4]=="DEAD"]; stale=[r for r in roster if r[4]=="stale"]

# --- 10-min deltas ---
new_claims=reg_recent("claims","CLAIM","created_at")
new_exps=reg_recent("experiments","EXP","started_at") + [o for o in (load(fn,{}) for fn in glob.glob(os.path.join(root,"registry","experiments","EXP-*.yaml"))) if (age_min(o.get("completed_at","")) or 1e9)<=W]
done_exps=[o for o in (load(fn,{}) for fn in glob.glob(os.path.join(root,"registry","experiments","EXP-*.yaml"))) if (age_min(o.get("completed_at","")) or 1e9)<=W]
new_verdicts=reg_recent("verdicts","VERDICT","created_at")

# --- committee run(s) ---
comm_runs=[]
for d in sorted(glob.glob(os.path.join(rd,"committee_run_*"))):
    status="DONE" if os.path.exists(os.path.join(d,"_status.txt")) else "running"
    votes={}
    for of in glob.glob(os.path.join(d,"*.out")):
        role=os.path.basename(of)[:-4]; txt=open(of,errors="ignore").read()
        import re
        m=re.search(r"(?:VOTE|FINAL_VERDICT):\s*([a-zA-Z-]+)",txt)
        votes[role]= (m.group(1).lower() if m else "—")
    comm_runs.append((os.path.basename(d),status,votes))

# --- Q&A surface: researcher notes that look like questions / blocks ---
qa=[]
for ag in agents:
    note=(ag.get("note","") or ""); 
    if any(k in note.lower() for k in ["?","blocked","need ","question","waiting","approve","help"]):
        qa.append((ag.get("agent_id","?"),note))

# --- progress reports in window + silent researchers + pending inbox ---
import datetime as _dt2
reports_in_window=0; reporters=set()
for fn in glob.glob(os.path.join(rd,"reports","*.jsonl")):
    for ln in open(fn,errors="ignore"):
        try: r=json.loads(ln)
        except: continue
        am=age_min(r.get("ts","")); 
        if am is not None and am<=W:
            reports_in_window+=1; reporters.add(r.get("agent",""))
silent_researchers=[]
for ag in agents:
    aid=ag.get("agent_id",""); role=ag.get("role","")
    if not (role=="researcher" or "researcher" in aid): continue
    lr=ag.get("last_report_ts") or ag.get("last_heartbeat","")
    am=age_min(lr)
    if am is None or am>W: silent_researchers.append((aid,role,am))
inbox=load(os.path.join(rd,"orchestrator","inbox.yaml"),{"items":[]}) or {"items":[]}
pending=[i for i in inbox["items"] if not i.get("acked")]
pending_action=[i for i in pending if i.get("needs_action")]

# --- STUCK-STATE detector (idle-while-waiting backstop) ---
stuck=[]
for fn in glob.glob(os.path.join(root,"registry","experiments","EXP-*.yaml")):
    e=load(fn,{}) or {}
    if e.get("status")=="completed" and e.get("claim_id"):
        cf=os.path.join(root,"registry","claims",f"{e['claim_id']}.yaml"); c=load(cf,{}) or {}
        # completed exp but claim still seed/active AND no verdict referencing it -> orchestrator didn't process
        vfound=any((load(vf,{}) or {}).get("claim_id")==e["claim_id"] for vf in glob.glob(os.path.join(root,"registry","verdicts","VERDICT-*.yaml")))
        if c.get("status") in ("seed","active") and not vfound and e.get("result_effect") in ("support","promote","keep-exploring"):
            stuck.append(f"{e['exp_id']} done(effect={e.get('result_effect')}) but {e['claim_id']} still {c.get('status')}, no verdict")
for d in glob.glob(os.path.join(rd,"committee_run_*")):
    if os.path.exists(os.path.join(d,"_status.txt")):
        run=os.path.basename(d)
        # committee done — is there a verdict newer than it?
        if not glob.glob(os.path.join(root,"registry","verdicts","VERDICT-*.yaml")):
            stuck.append(f"{run} committee DONE but no VERDICT written")

# --- STUCK-STATE detector (idle-while-waiting backstop) ---
stuck=[]
for fn in glob.glob(os.path.join(root,"registry","experiments","EXP-*.yaml")):
    e=load(fn,{}) or {}
    if e.get("status")=="completed" and e.get("claim_id"):
        cf=os.path.join(root,"registry","claims",f"{e['claim_id']}.yaml"); c=load(cf,{}) or {}
        # completed exp but claim still seed/active AND no verdict referencing it -> orchestrator didn't process
        vfound=any((load(vf,{}) or {}).get("claim_id")==e["claim_id"] for vf in glob.glob(os.path.join(root,"registry","verdicts","VERDICT-*.yaml")))
        if c.get("status") in ("seed","active") and not vfound and e.get("result_effect") in ("support","promote","keep-exploring"):
            stuck.append(f"{e['exp_id']} done(effect={e.get('result_effect')}) but {e['claim_id']} still {c.get('status')}, no verdict")
for d in glob.glob(os.path.join(rd,"committee_run_*")):
    if os.path.exists(os.path.join(d,"_status.txt")):
        run=os.path.basename(d)
        # committee done — is there a verdict newer than it?
        if not glob.glob(os.path.join(root,"registry","verdicts","VERDICT-*.yaml")):
            stuck.append(f"{run} committee DONE but no VERDICT written")

# --- orchestrator self-report freshness (it should PUSH every ~10m) ---
orch_report_age=None
for fn in glob.glob(os.path.join(rd,"reports","*.jsonl")):
    base=os.path.basename(fn)
    if "orchestrator" in base:
        last=None
        for ln in open(fn,errors="ignore"):
            try: last=json.loads(ln)
            except: pass
        if last: orch_report_age=age_min(last.get("ts",""))
orch_silent = (orch_report_age is None) or (orch_report_age > W)

# --- write report ---
os.makedirs(os.path.join(rd,"monitor"),exist_ok=True)
ts=NOW.strftime("%Y%m%d-%H%M%S")
lines=[]
def P(x): lines.append(x); print(x)
P(f"# Monitor Report — {NOW.strftime('%H:%M UTC')} (window={W}m)")
P(f"orchestrator self-report: {'SILENT >'+str(W)+'m (hub should push!)' if orch_silent else str(orch_report_age)+'m ago'}")
P(f"orchestrator: {next((f'{r[4]} ({r[3]}m)' for r in roster if r[1]=='orchestrator'),'NOT FOUND')}")
P(f"## Δ last {W}m: +{len(new_claims)} claims, +{len(done_exps)} experiments completed, +{len(new_verdicts)} verdicts")
P(f"## Progress reports last {W}m: {reports_in_window} from {len(reporters)} researcher(s)")
if silent_researchers: P(f"## ⚠️ SILENT researchers (>{W}m no report — orchestrator may sit idle): " + ", ".join(f"{a}[{r}]" for a,r,_ in silent_researchers))
P(f"## Orchestrator inbox: {len(pending)} pending ({len(pending_action)} need action)")
for i in pending_action: P(f"   ❗ {i.get('agent')}: BLOCKED={i.get('blocked','')[:50]} NEED={i.get('need','')[:40]}")
P(f"## Researchers ({len(researchers)}): " + (", ".join(f"{r[1]}={r[4]}" for r in researchers) or "none"))
for r in researchers: P(f"   - {r[0]} [{r[1]}] {r[4]} last={r[3]}m :: {r[5][:60]}")
P(f"## Committee ({len(committee)} alive-registered):")
for name,st,votes in comm_runs:
    P(f"   {name}: {st} votes={votes}")
if not comm_runs: P("   (no committee run yet)")
P(f"## Researcher Q&A / blocks ({len(qa)}):")
for aid,note in qa: P(f"   - {aid}: {note[:80]}")
if not qa: P("   (none)")
if stale: P(f"## ⏳ STALE (within grace, patient-wait): {', '.join(r[0] for r in stale)}")
if stuck:
    P(f"## 🔁 STUCK STATE (completed-but-unprocessed — AUTO-REVIVE orchestrator):")
    for x in stuck: P(f"   - {x}")
if stuck:
    P(f"## 🔁 STUCK STATE (completed-but-unprocessed — AUTO-REVIVE orchestrator):")
    for x in sorted(set(stuck)): P(f"   - {x}")
if dead: P(f"## ☠️ DEAD (past {grace}m grace — RESPAWN NEEDED): {', '.join(r[0] for r in dead)}")
else: P(f"## ✅ no dead agents")
# machine-readable sidecar
side={"ts":ts,"window_min":W,"delta":{"claims":len(new_claims),"experiments":len(done_exps),"verdicts":len(new_verdicts)},
      "researchers":[{"id":r[0],"role":r[1],"state":r[4],"age_min":r[3]} for r in researchers],
      "committee_runs":[{"run":n,"status":s,"votes":v} for n,s,v in comm_runs],
      "reports_in_window":reports_in_window,"silent_researchers":[a for a,_,_ in silent_researchers],"inbox_pending":len(pending),"inbox_need_action":len(pending_action),"qa":[{"id":a_,"note":n} for a_,n in qa],"dead":[r[0] for r in dead],"stale":[r[0] for r in stale],"stuck":stuck,"orchestrator_self_report_age_min":orch_report_age,"orchestrator_silent":orch_silent}
open(os.path.join(rd,"monitor",f"report-{ts}.md"),"w").write("\n".join(lines))
open(os.path.join(rd,"monitor",f"report-{ts}.json"),"w").write(json.dumps(side,indent=2))
print(f"\n(report saved: runtime/monitor/report-{ts}.md)")
