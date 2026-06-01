#!/usr/bin/env python3
"""ops_metrics.py - hourly operational snapshot for a research-os instance.

A UTILITY (lives in the engine repo's util/, not engine/ core). All observability DATA is written
into the INSTANCE repo only, PER PROJECT:
  <instance>/operational/<UTC-date>/<PROJ>/metrics.jsonl   (per-project hourly timeseries)
  <instance>/operational/<PROJ>/metrics_alltime.jsonl      (per-project all-time series)
  <instance>/operational/<UTC-date>/dashboard.html         (rollup dashboard, all projects)
  <instance>/operational/<UTC-date>/README.md              (GitHub-rendered markdown report)
  <instance>/operational/dashboard.html                    (latest rollup)
  <instance>/operational/README.md                         (latest markdown report)
Stdlib only; read-only over registry/experiments. Never writes into the engine repo.
Usage: python3 util/ops_metrics.py --instance <ROOT>
"""
import os, sys, glob, json, datetime, re

def utc_now(): return datetime.datetime.now(datetime.timezone.utc)
def parse_args():
    a=sys.argv[1:]
    for i,x in enumerate(a):
        if x=="--instance" and i+1<len(a): return a[i+1]
    sys.exit("need --instance <ROOT>")
def yread(path):
    d={}
    try:
        for ln in open(path,encoding="utf-8",errors="replace"):
            m=re.match(r'^([a-z_]+):\s*(.*)$', ln)
            if m:
                k,v=m.group(1),m.group(2).strip().strip("'\"")
                if k not in d: d[k]=v
    except Exception: pass
    return d
def proj_of(path, default="UNKNOWN"):
    return yread(path).get("project_id", default) or default

def blank(): return {"claims":0,"verdicts":0,"cemetery":0,"experiments_total":0,"cpu_exp":0,
    "gpu_exp":0,"exp_completed":0,"exp_running":0,
    "verdict_green":0,"verdict_yellow":0,"verdict_other":0,
    "claim_submissions":0,"submissions_pending":0,"submissions_acked":0}

def main():
    root=parse_args(); now=utc_now(); date=now.strftime("%Y-%m-%d"); ts=now.strftime("%Y-%m-%dT%H:%M:%SZ")
    projects=sorted(os.path.basename(p) for p in glob.glob(os.path.join(root,"projects","PROJ-*")))
    per={p:blank() for p in projects}
    def slot(pid):
        if pid not in per: per[pid]=blank()
        return per[pid]

    for f in glob.glob(os.path.join(root,"registry","claims","**","CLAIM-*.yaml"),recursive=True):
        slot(proj_of(f))["claims"]+=1
    for f in glob.glob(os.path.join(root,"registry","cemetery","**","DEAD-*.yaml"),recursive=True):
        slot(proj_of(f))["cemetery"]+=1
    for f in glob.glob(os.path.join(root,"registry","verdicts","**","VERDICT-*.yaml"),recursive=True):
        d=yread(f); s=slot(d.get("project_id","UNKNOWN")); s["verdicts"]+=1
        fv=d.get("final_verdict","?")
        s["verdict_green"]+= 1 if fv=="green" else 0
        s["verdict_yellow"]+= 1 if fv=="yellow" else 0
        s["verdict_other"]+= 1 if fv not in ("green","yellow") else 0
    for f in glob.glob(os.path.join(root,"experiments","**","experiment.yaml"),recursive=True):
        d=yread(f); s=slot(d.get("project_id","UNKNOWN")); s["experiments_total"]+=1
        if str(d.get("needs_gpu","")).lower() in ("true","1","yes"): s["gpu_exp"]+=1
        else: s["cpu_exp"]+=1
        st=d.get("status","")
        if st=="completed": s["exp_completed"]+=1
        elif st=="running": s["exp_running"]+=1

    # claim-submission queue (sub-monitor -> orchestrator): document committee submissions per project.
    # Each entry: queue_id/claim_id/project_id/acked. Count per project + pending/acked split (instance-global too).
    sub_total=sub_pending=sub_acked=0
    qf=os.path.join(root,"runtime","orchestrator","committee_queue.yaml")
    if os.path.exists(qf):
        try:
            import importlib.util as _yu
            _yspec=_yu.find_spec("yaml")
        except Exception:
            _yspec=None
        items=[]
        if _yspec:
            import yaml as _y
            qd=_y.safe_load(open(qf)) or {}
            items=qd.get("queue",[]) or []
        else:
            # stdlib fallback: split on queue_id markers
            blocks=re.split(r'(?m)^\s*-\s*queue_id:', open(qf).read())[1:]
            for b in blocks:
                pid=re.search(r'project_id:\s*(\S+)',b)
                ack=re.search(r'acked:\s*(\S+)',b)
                items.append({"project_id":(pid.group(1).strip("'\"") if pid else "UNKNOWN"),
                              "acked":(ack and ack.group(1).strip().lower() in ("true","1","yes"))})
        for it in items:
            pid=it.get("project_id") or "UNKNOWN"
            s=slot(pid); s["claim_submissions"]+=1; sub_total+=1
            if it.get("acked"): s["submissions_acked"]+=1; sub_acked+=1
            else: s["submissions_pending"]+=1; sub_pending+=1

    # committee runs: instance-global (dir doesn't carry project); attribute by claim_id->project if packet names it, else global bucket
    cdone=cinc=0
    for c in glob.glob(os.path.join(root,"runtime","committee_run_*")):
        sp=os.path.join(c,"_status.txt"); s=""
        if os.path.exists(sp):
            try: s=open(sp).read()
            except Exception: pass
        if "ALL_COMMITTEE_DONE" in s: cdone+=1
        elif "INCOMPLETE" in s: cinc+=1

    # GPU is instance-global (shared nodes)
    qlen=leases=0
    qp=os.path.join(root,"runtime","gpu_queue.yaml")
    if os.path.exists(qp):
        txt=open(qp).read(); qlen=len(re.findall(r'exp_id:',txt))
        leases=0 if re.search(r'leases:\s*\{\}',txt) else len(re.findall(r'^\s{2,}\S+:\s',txt,re.M))
    nodes={}
    for n in glob.glob(os.path.join(root,"runtime","gpu_heartbeat","*")):
        nodes[os.path.basename(n)]=round((now.timestamp()-os.path.getmtime(n))/60.0,1)

    oproot=os.path.join(root,"operational"); os.makedirs(os.path.join(oproot,date),exist_ok=True)
    # PER-PROJECT rows
    for pid,m in per.items():
        row=dict(ts=ts,date=date,project=pid,**m)
        dd=os.path.join(oproot,date,pid); os.makedirs(dd,exist_ok=True)
        open(os.path.join(dd,"metrics.jsonl"),"a").write(json.dumps(row)+"\n")
        pa=os.path.join(oproot,pid); os.makedirs(pa,exist_ok=True)
        open(os.path.join(pa,"metrics_alltime.jsonl"),"a").write(json.dumps(row)+"\n")
    # INSTANCE-global row (committee+gpu shared) for the rollup
    g=dict(ts=ts,date=date,project="_INSTANCE",
        committee_done=cdone,committee_incomplete=cinc,
        gpu_queue_len=qlen,gpu_leases_active=leases,gpu_nodes=nodes,
        **{k:sum(per[p][k] for p in per) for k in blank()})
    open(os.path.join(oproot,date,"_instance.jsonl"),"a").write(json.dumps(g)+"\n")
    open(os.path.join(oproot,"_instance_alltime.jsonl"),"a").write(json.dumps(g)+"\n")

    # rollup dashboard reads per-project all-time series + instance series
    html=render(oproot, projects)
    open(os.path.join(oproot,date,"dashboard.html"),"w").write(html)
    open(os.path.join(oproot,"dashboard.html"),"w").write(html)
    # GitHub-native Markdown report (auto-renders in the repo)
    md=render_md_rollup(oproot, projects)
    open(os.path.join(oproot,date,"README.md"),"w").write(md)
    open(os.path.join(oproot,"README.md"),"w").write(md)
    print(json.dumps({"date":date,"projects":per,"instance":{"committee_done":cdone,
        "committee_incomplete":cinc,"gpu_leases":leases,"gpu_queue":qlen,"gpu_nodes":nodes,
        "claim_submissions":sub_total,"submissions_pending":sub_pending,"submissions_acked":sub_acked}}))

def _series(path):
    rows=[]
    try:
        for ln in open(path):
            ln=ln.strip()
            if ln: rows.append(json.loads(ln))
    except Exception: pass
    return rows

def render(oproot, projects):
    series_proj={p:_series(os.path.join(oproot,p,"metrics_alltime.jsonl")) for p in projects}
    inst=_series(os.path.join(oproot,"_instance_alltime.jsonl"))
    return _opsrender().render(inst, series_proj)

def render_md_rollup(oproot, projects):
    series_proj={p:_series(os.path.join(oproot,p,"metrics_alltime.jsonl")) for p in projects}
    inst=_series(os.path.join(oproot,"_instance_alltime.jsonl"))
    return _opsrender().render_md(inst, series_proj)

def _opsrender():
    import importlib.util as _u
    _p=os.path.join(os.path.dirname(__file__),"ops_render.py")
    _s=_u.spec_from_file_location("ops_render",_p); _m=_u.module_from_spec(_s); _s.loader.exec_module(_m)
    return _m

if __name__=="__main__": main()
