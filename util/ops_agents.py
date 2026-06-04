#!/usr/bin/env python3
"""ops_agents.py — agent-liveness + progress alerting for a research-os instance.

The COMPANION to ops_metrics.py (which tracks claim/verdict/exp KPIs but NOT agent health).
This closes the blind spot that let the orchestrator + GPU coordinators die silently for ~10h:
ops_metrics has no agent layer, and a `liveness | grep alive` only SEES the survivors.

Read-only over <instance>/runtime/agents/*.yaml + the cron .alive stamps + git HEAD.
Stdlib only. Emits a JSON verdict + human lines; exit 0 = OK, exit 1 = ALERT (>=1 critical flag).
Writes a timeseries to <instance>/operational/<date>/_agents.jsonl (instance repo only).

ALERTS (critical) — fire at GRACE (the threshold the engine's reaper itself acts on), NOT at KICK:
  - ORCH_DOWN     : no orchestrator status=running with hb < GRACE (a truly dead brain)
  - COORD_DOWN    : a gpu_coordinator status=running but hb past GRACE (dead-lease risk the reaper would reap)
  - NO_PROGRESS   : newest verdict/claim/exp mtime older than STALL_MIN AND below-invest-target
  - DRIVER_DOWN   : cron driver pid not alive (deterministic loop stopped)
WARN (non-critical): orch/coord hb in (KICK, GRACE) — late but within grace ; researcher slow (researcher_perf.py)

★ FALSE-POSITIVE GUARD: the 3 LLM agents (orchestrator + 2 GPU coords) all heartbeat TOGETHER every ~10m
(driven by the single self-check job), so their hb-age sawtooths 0->10m in lockstep. A naive KICK(15m)
snapshot sampled late in the interval false-trips on all three at once (observed twice). Two fixes:
(1) critical alerts fire at GRACE(45m) = exactly when the reaper would act, not at KICK; KICK..GRACE is a WARN.
(2) RE-READ-after-sleep: before emitting any past-grace alert, sleep briefly and re-read the record — a
heartbeat that landed since the first snapshot CLEARS it (mirrors the reaper's BUG-113 in-lock liveness
re-check). A genuinely dead agent never refreshes, so real ORCH_DOWN/COORD_DOWN still fires.

Usage: python3 util/ops_agents.py --instance <ROOT>
"""
import os,sys,glob,json,re,datetime,subprocess,time
KICK_MIN=15.0; GRACE_MIN=45.0; STALL_MIN=60.0; RECHECK_SLEEP_S=8.0
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
    def hb_age_of(path):
        # live re-read of one agent's hb-age in minutes (None if unreadable)
        d2=yread(path); h=dt(d2.get("last_heartbeat","")); 
        return ((utc_now()-h).total_seconds()/60 if h else None), d2
    def confirm_stale(path, thresh):
        # RE-READ-after-sleep guard: a heartbeat that lands between the first snapshot and now CLEARS a
        # provisional past-threshold flag (lockstep self-check beat). A truly dead agent never refreshes.
        time.sleep(RECHECK_SLEEP_S)
        a2,_=hb_age_of(path)
        return (a2 is not None and a2>=thresh), a2
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
                elif age is not None and age<GRACE_MIN:
                    warns.append(f"ORCH_LATE {rec['agent']}: hb {age:.0f}m (>{KICK_MIN:.0f} kick, <{GRACE_MIN:.0f} grace — within grace, not yet reapable)")
                    orch_ok=True  # within grace = still considered live (the reaper won't touch it yet)
                else:
                    # provisionally past grace -> CONFIRM with a re-read before alerting
                    still, a2 = confirm_stale(f, GRACE_MIN)
                    if still: alerts.append(f"ORCH_DOWN {rec['agent']}: hb {a2:.0f}m ago, CONFIRMED past {GRACE_MIN:.0f}m grace after re-read (DEAD brain) tokens={rec['tokens']}")
                    else:
                        orch_ok=True
                        warns.append(f"ORCH_LATE {rec['agent']}: snapshot {age:.0f}m but a fresh heartbeat landed on re-read (lockstep beat) — live")
            elif role=="gpu_coordinator":
                if age is not None and age>=GRACE_MIN:
                    still, a2 = confirm_stale(f, GRACE_MIN)
                    if still: alerts.append(f"COORD_DOWN {rec['agent']}: hb {a2:.0f}m ago, CONFIRMED past {GRACE_MIN:.0f}m grace after re-read -> dead-lease risk (reaper will reap)")
                    else: warns.append(f"COORD_LATE {rec['agent']}: snapshot {age:.0f}m but fresh heartbeat on re-read — live")
                elif age is not None and age>=KICK_MIN:
                    warns.append(f"COORD_LATE {rec['agent']}: hb {age:.0f}m (>{KICK_MIN:.0f} kick, <{GRACE_MIN:.0f} grace — late but not yet reapable)")
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
