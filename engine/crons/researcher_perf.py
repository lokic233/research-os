#!/usr/bin/env python3
# Researcher performance / stall watch for v3. Read-only. Flags LIVE researchers whose
# heartbeat is stale (>kick) OR whose active duration far exceeds baseline w/o landing.
# Baseline (2026-06-03, n=48 completed): design-phase ~0-1m (hbc=1); real-compute 8-103m
# (max=103m = researcher-0051 L2 real-model MATH-500 300xk32x3 on H100, legit). tokens<=22k.
import glob, datetime, re, sys, os
ROOT="/Users/dengcchi/autonomous-research-v3"
os.chdir(ROOT)
KICK_MIN=15.0           # engine liveness kick
GRACE_MIN=45.0          # patient grace
SLOW_NONGPU_MIN=40.0    # non-GPU design/sim run far past baseline -> suspicious
SLOW_GPU_MIN=150.0      # GPU run past ~1.5x the legit max(103m) -> suspicious
def parse(f):
    d={}
    for line in open(f):
        if ':' in line and not line.strip().startswith('#'):
            k,_,v=line.partition(':'); d[k.strip()]=v.strip().strip("'\"")
    return d
now=datetime.datetime.now(datetime.timezone.utc)
def dt(ts):
    try: return datetime.datetime.strptime(ts,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except: return None
flags=[]; live=[]
for f in glob.glob("runtime/agents/researcher-*.yaml"):
    d=parse(f); st=d.get('status','')
    if st in ('completed','retired','dead','superseded','killed'): continue
    sp=dt(d.get('spawned_at','')); hb=dt(d.get('last_heartbeat',''))
    dur=(hb-sp).total_seconds()/60 if (sp and hb) else None
    hbage=(now-hb).total_seconds()/60 if hb else None
    exp=d.get('current_exp_id',''); note=d.get('note','')[:60]
    aid=d.get('agent_id',''); gpu=bool(d.get('gpu','')) or 'gpu' in aid
    live.append((aid,st,dur,hbage,exp,note,gpu,d.get('heartbeat_count','?'),d.get('tokens_used','?')))
    if hbage is not None and hbage>KICK_MIN:
        flags.append(f"STALE-HB {aid}: last hb {hbage:.0f}m ago (>{KICK_MIN:.0f}m kick) exp={exp} note='{note}'")
    lim=SLOW_GPU_MIN if gpu else SLOW_NONGPU_MIN
    if dur is not None and dur>lim:
        flags.append(f"SLOW {aid}: active {dur:.0f}m (> {lim:.0f}m {'GPU' if gpu else 'nonGPU'} baseline) exp={exp} note='{note}' -> ROOT-CAUSE")
print("LIVE researchers:", len(live))
for r in live:
    dur=f"{r[2]:.0f}m" if r[2] is not None else "?"; hba=f"{r[3]:.0f}m" if r[3] is not None else "?"
    print(f"  {r[0]:<18} {r[1]:<9} active={dur:<6} hb_age={hba:<6} hbc={r[7]} tok={r[8]} exp={r[4]} | {r[5]}")
if flags:
    print("\n*** PERF FLAGS (root-cause needed) ***")
    for fl in flags: print("  "+fl)
else:
    print("\nperf: OK (no stale-hb, no slow-runner over baseline)")
