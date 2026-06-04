#!/usr/bin/env python3
"""ops_agents.py — agent-liveness + progress alerting for a research-os instance.

The COMPANION to ops_metrics.py (which tracks claim/verdict/exp KPIs but NOT agent health).
This closes the blind spot that let the orchestrator + GPU coordinators die silently for ~10h:
ops_metrics has no agent layer, and a `liveness | grep alive` only SEES the survivors.

Read-only over <instance>/runtime/agents/*.yaml + the cron .alive stamps + git HEAD.
Stdlib only. Emits a JSON verdict + human lines; exit 0 = OK, exit 1 = ALERT (>=1 critical flag).
Writes a timeseries to <instance>/operational/<date>/_agents.jsonl (instance repo only).

ALERTS (critical):
  - ORCH_DOWN     : no orchestrator with role=orchestrator+status=running whose hb < KICK
  - COORD_DOWN    : a gpu_coordinator status=running but hb past KICK (dead lease risk)
  - NO_PROGRESS   : newest verdict/claim/exp mtime older than STALL_MIN AND below-invest-target
  - DRIVER_DOWN   : cron driver pid not alive (deterministic loop stopped)
WARN (non-critical): orchestrator hb in (KICK, GRACE) ; researcher slow (handled by researcher_perf.py)

Usage: python3 util/ops_agents.py --instance <ROOT>
"""
import os,sys,glob,json,re,datetime,subprocess
KICK_MIN=15.0; GRACE_MIN=45.0; STALL_MIN=60.0
def utc_now(): return datetime.datetime.now(datetime.timezone.utc)
def arg():
    a=sys.argv[1:]
    for i,x in enumerate(a):
        if x=="--instance" and i+1<len(a): return a[i+1]
    sys.exit("need --instance <ROOT>")
def yread(p):
    d={}
    try:
        for ln in open(p,encoding="utf-8",errors="replace"):
            m=re.match(r'^([a-z_]+):\s*(.*)$',ln)
            if m and m.group(1) not in d: d[m.group(1)]=m.group(2).strip().strip("'\"")
    except Exception: pass
    return d
def dt(s):
    try: return datetime.datetime.strptime(s,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except Exception: return None

def main():
    root=arg(); now=utc_now(); ts=now.strftime("%Y-%m-%dT%H:%M:%SZ")
    alerts=[]; warns=[]; agents=[]
    orch_ok=False; live_orch=[]
    for f in glob.glob(os.path.join(root,"runtime","agents","*.yaml")):
        d=yread(f); role=d.get("role",""); st=d.get("status","")
        hb=dt(d.get("last_heartbeat","")); age=(now-hb).total_seconds()/60 if hb else None
        rec=dict(agent=d.get("agent_id",os.path.basename(f)[:-5]),role=role,status=st,
                 hb_age_min=(round(age,1) if age is not None else None),
                 tokens=d.get("tokens_used",""))
        # only RUNNING agents are liveness-relevant; terminal states are expected-quiet
        if st=="running":
            agents.append(rec)
            if role=="orchestrator":
                if age is not None and age<KICK_MIN: orch_ok=True; live_orch.append(rec)
                elif age is not None and age<GRACE_MIN: warns.append(f"ORCH_LATE {rec['agent']}: hb {age:.0f}m (>{KICK_MIN:.0f} kick, <{GRACE_MIN:.0f} grace)")
                else: alerts.append(f"ORCH_DOWN {rec['agent']}: hb {age:.0f}m ago (DEAD past {GRACE_MIN:.0f}m grace) tokens={rec['tokens']}")
            elif role=="gpu_coordinator":
                if age is not None and age>=KICK_MIN: alerts.append(f"COORD_DOWN {rec['agent']}: hb {age:.0f}m ago (status=running but past {KICK_MIN:.0f}m kick) -> dead-lease risk")
    # no running orchestrator under kick at all
    if not orch_ok:
        # was there ANY running orchestrator record? if yes it's covered above; if none, flag explicitly
        if not any(a["role"]=="orchestrator" for a in agents):
            alerts.append("ORCH_DOWN: no orchestrator in status=running (research loop is not being driven)")
    # driver
    pidf=os.path.join(root,"runtime","cron",".driver.pid"); drv_alive=False; drvpid=None
    if os.path.exists(pidf):
        try:
            drvpid=int(open(pidf).read().strip()); os.kill(drvpid,0); drv_alive=True
        except Exception: drv_alive=False
    if not drv_alive: alerts.append(f"DRIVER_DOWN: cron driver pid {drvpid} not alive (deterministic loop stopped)")
    # progress: newest registry mtime
    newest=0.0; newest_path=""
    for pat in ("registry/verdicts/**/VERDICT-*.yaml","registry/claims/**/CLAIM-*.yaml","experiments/**/experiment.yaml"):
        for f in glob.glob(os.path.join(root,pat),recursive=True):
            m=os.path.getmtime(f)
            if m>newest: newest=m; newest_path=f
    prog_age=(now.timestamp()-newest)/60 if newest else None
    # below invest target?
    cfg=yread(os.path.join(root,"research-os.config.yaml"))
    below=False
    try:
        # cheap: ask ros projects for the banner; fallback to config target
        out=subprocess.run(["/usr/bin/python3",os.path.join(os.path.dirname(__file__),"..","engine","ros.py"),
                            "--instance",root,"projects"],capture_output=True,text=True,timeout=30).stdout
        if "BELOW TARGET" in out: below=True
    except Exception: pass
    if prog_age is not None and prog_age>STALL_MIN and below:
        alerts.append(f"NO_PROGRESS: newest registry write {prog_age:.0f}m ago (>{STALL_MIN:.0f}m) AND below invest target -> orchestrator not seeding")
    elif prog_age is not None and prog_age>STALL_MIN:
        warns.append(f"quiet: newest registry write {prog_age:.0f}m ago (>{STALL_MIN:.0f}m) but at/above invest target (may be legit converged-pause)")

    status="ALERT" if alerts else ("WARN" if warns else "OK")
    rec=dict(ts=ts,status=status,orch_ok=orch_ok,driver_alive=drv_alive,
             progress_age_min=(round(prog_age,1) if prog_age is not None else None),
             below_target=below,alerts=alerts,warns=warns,live_orchestrators=[a["agent"] for a in live_orch],
             running_agents=len(agents))
    # timeseries into instance repo only
    try:
        dd=os.path.join(root,"operational",now.strftime("%Y-%m-%d")); os.makedirs(dd,exist_ok=True)
        open(os.path.join(dd,"_agents.jsonl"),"a").write(json.dumps(rec)+"\n")
    except Exception: pass
    print(json.dumps(rec,indent=2))
    for a in alerts: print("  !! ALERT:",a,file=sys.stderr)
    for w in warns: print("  ~ warn:",w,file=sys.stderr)
    sys.exit(1 if alerts else 0)

if __name__=="__main__": main()
