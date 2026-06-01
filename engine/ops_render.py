"""ops_render.py - render a pure-CSS (no JS, no SVG, no CDN) dashboard from metrics rows.
Imported by ops_metrics.py. Server-side rendered so it always displays in any sandbox/iframe.
"""
COL=['#58a6ff','#3fb950','#f0883e','#bc8cff','#f85149','#56d4dd']

def _bar(label,val,mx,color,sub=""):
    pct=0 if not mx else round(val/mx*100)
    return ('<div class=row><div class=lab>%s</div><div class=track>'
            '<div class=fill style="width:%d%%;background:%s"></div></div>'
            '<div class=val>%s%s</div></div>')%(label,pct,color,val,sub)

def _panel(title,items):
    mx=max([v for _,v,_ in items]+[1])
    return '<div class=card><p class=ct>%s</p>%s</div>'%(title,''.join(_bar(l,v,mx,c) for l,v,c in items))

def render(inst_series, proj_series):
    inst=inst_series[-1] if inst_series else {}
    pids=sorted(proj_series.keys())
    def pv(p,f): s=proj_series.get(p,[]); return (s[-1].get(f,0) if s else 0) or 0
    g=lambda k: inst.get(k,0) or 0
    H=['''<!doctype html><html><head><meta charset=utf-8><title>research-os ops</title><style>
body{font-family:ui-sans-serif,system-ui,Arial;margin:0;background:#0b0e14;color:#e6edf3}
h1{font-size:17px;padding:13px 18px;margin:0;border-bottom:1px solid #1f2530;background:#11151d}
h2{font-size:12px;color:#8b949e;margin:16px 16px 6px;text-transform:uppercase;letter-spacing:.6px}
.kpis{display:flex;flex-wrap:wrap;gap:10px;padding:12px 16px}
.kpi{background:#11151d;border:1px solid #1f2530;border-radius:10px;padding:9px 13px;min-width:104px}
.kpi b{font-size:22px;display:block}.kpi span{color:#8b949e;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;padding:6px 16px}
.card{background:#11151d;border:1px solid #1f2530;border-radius:10px;padding:14px}
.ct{font-size:12px;color:#8b949e;margin:0 0 12px;font-weight:600}
.row{display:flex;align-items:center;gap:10px;margin:7px 0}
.lab{width:88px;font-size:12px;color:#c9d1d9;text-align:right;flex:none}
.track{flex:1;height:14px;background:#0d1117;border:1px solid #1f2530;border-radius:7px;overflow:hidden}
.fill{height:100%;border-radius:7px 0 0 7px;min-width:2px}
.val{width:46px;font-size:12px;font-weight:600;flex:none}
.warn{color:#f0883e}.bad{color:#f85149}.ok{color:#3fb950}
#bn div{padding:6px 0;border-bottom:1px solid #1f2530;font-size:13px}
</style></head><body>''']
    H.append('<h1>research-os — operational dashboard <span style="color:#8b949e;font-size:12px">· %s · %d snapshots · %d projects</span></h1>'%(inst.get('ts','(no data)'),len(inst_series),len(pids)))
    def kpi(l,v,c=''):return '<div class=kpi><b class="%s">%s</b><span>%s</span></div>'%(c,v,l)
    gi='warn' if g('gpu_leases_active')==0 else 'ok'
    H.append('<div class=kpis>'+''.join([
        kpi('claims',g('claims')),kpi('verdicts',g('verdicts')),kpi('GREEN',g('verdict_green'),'ok'),
        kpi('cemetery',g('cemetery')),kpi('experiments',g('experiments_total')),
        kpi('CPU / GPU exp','%s / %s'%(g('cpu_exp'),g('gpu_exp'))),
        kpi('committee done',g('committee_done'),'ok'),
        kpi('committee incomplete',g('committee_incomplete'),'bad' if g('committee_incomplete')>0 else ''),
        kpi('GPU leases',g('gpu_leases_active'),gi),kpi('GPU queue',g('gpu_queue_len'))])+'</div>')
    H.append('<h2>Instance</h2><div class=grid>')
    H.append(_panel('Production',[('claims',g('claims'),COL[0]),('verdicts',g('verdicts'),COL[1]),('GREEN',g('verdict_green'),COL[3]),('cemetery',g('cemetery'),'#8b949e')]))
    H.append(_panel('Experiments',[('CPU',g('cpu_exp'),COL[0]),('GPU',g('gpu_exp'),COL[2]),('completed',g('exp_completed'),COL[1])]))
    H.append(_panel('Committee',[('done',g('committee_done'),COL[1]),('incomplete',g('committee_incomplete'),COL[4])]))
    H.append(_panel('GPU',[('leases',g('gpu_leases_active'),COL[2]),('queue',g('gpu_queue_len'),COL[0]),('gpu-exp',g('gpu_exp'),COL[1])]))
    H.append('</div><h2>Per-project</h2><div class=grid>')
    for field,title in [('claims','Claims per project'),('verdicts','Verdicts per project'),('experiments_total','Experiments per project'),('verdict_green','GREEN verdicts per project')]:
        H.append(_panel(title,[(p.replace('PROJ-','P'),pv(p,field),COL[i%len(COL)]) for i,p in enumerate(pids)]))
    H.append('</div>')
    bn=[]
    if g('gpu_leases_active')==0 and g('gpu_queue_len')==0: bn.append(('warn','GPU IDLE: 0 leases + empty queue — H100 capacity unused.'))
    if g('committee_incomplete')>0: bn.append(('bad','COMMITTEE_INCOMPLETE=%s — re-run, never count.'%g('committee_incomplete')))
    ratio=round(g('gpu_exp')/g('experiments_total')*100) if g('experiments_total') else 0
    gr=round(g('verdict_green')/g('verdicts')*100) if g('verdicts') else 0
    bn.append(('ok','GPU-exp share %d%% · GREEN rate %d%% (%s/%s). CPU research is cheap+parallel; reserve GPU for serving cells.'%(ratio,gr,g('verdict_green'),g('verdicts'))))
    if pids:
        cl=[pv(p,'claims') for p in pids]
        if max(cl)-min(cl)>=4:
            mxp=pids[cl.index(max(cl))]; mnp=pids[cl.index(min(cl))]
            bn.append(('warn','PROJECT IMBALANCE: %s %d claims vs %s %d — rebalance lanes toward %s.'%(mxp,max(cl),mnp,min(cl),mnp)))
    H.append('<div class=card style="margin:6px 16px 18px"><p class=ct>Bottleneck signals</p><div id=bn>'+''.join('<div class="%s">%s</div>'%(c,t) for c,t in bn)+'</div></div></body></html>')
    return '\n'.join(H)

def _mdbar(v, mx, width=20):
    n = 0 if not mx else round(v/mx*width)
    return "`"+("\u2588"*n)+("\u00b7"*(width-n))+"`"

def render_md(inst_series, proj_series):
    """GitHub-native Markdown report (auto-rendered in the repo). No HTML/JS needed."""
    inst = inst_series[-1] if inst_series else {}
    pids = sorted(proj_series.keys())
    def pv(p,f):
        s=proj_series.get(p,[]); return (s[-1].get(f,0) if s else 0) or 0
    g=lambda k: inst.get(k,0) or 0
    L=[]
    L.append("# research-os — operational report")
    L.append("")
    L.append("_auto-generated by `engine/ops_metrics.py` · UTC %s · %d snapshots · %d projects_"%(inst.get("ts","(no data)"),len(inst_series),len(pids)))
    L.append("")
    L.append("> View the interactive HTML dashboard (GitHub won't render HTML in-repo): open `operational/dashboard.html` via the **Raw** button, or prefix the raw URL with `https://raw.githack.com/` to render it live.")
    L.append("")
    L.append("## Instance KPIs")
    L.append("")
    L.append("| metric | value |")
    L.append("|---|---|")
    rows=[("claims",g("claims")),("verdicts",g("verdicts")),("GREEN verdicts",g("verdict_green")),
          ("cemetery",g("cemetery")),("experiments",g("experiments_total")),
          ("CPU exp",g("cpu_exp")),("GPU exp",g("gpu_exp")),("completed exp",g("exp_completed")),
          ("committee done",g("committee_done")),("committee incomplete",g("committee_incomplete")),
          ("GPU leases active",g("gpu_leases_active")),("GPU queue",g("gpu_queue_len"))]
    for k,v in rows: L.append("| %s | **%s** |"%(k,v))
    L.append("")
    L.append("## Per-project")
    L.append("")
    L.append("| project | claims | verdicts | GREEN | CPU exp | GPU exp | experiments |")
    L.append("|---|---|---|---|---|---|---|")
    for p in pids:
        L.append("| %s | %s | %s | %s | %s | %s | %s |"%(p,pv(p,"claims"),pv(p,"verdicts"),
            pv(p,"verdict_green"),pv(p,"cpu_exp"),pv(p,"gpu_exp"),pv(p,"experiments_total")))
    L.append("")
    # claims bar
    mxc=max([pv(p,"claims") for p in pids]+[1])
    L.append("**Claims per project**")
    L.append("")
    for p in pids: L.append("- %s %s %d"%(p, _mdbar(pv(p,"claims"),mxc), pv(p,"claims")))
    L.append("")
    L.append("## Bottleneck signals")
    L.append("")
    sig=[]
    if g("gpu_leases_active")==0 and g("gpu_queue_len")==0: sig.append("⚠️ **GPU IDLE** — 0 leases + empty queue; H100 capacity unused.")
    if g("committee_incomplete")>0: sig.append("🛑 **COMMITTEE_INCOMPLETE=%s** — re-run, never count."%g("committee_incomplete"))
    ratio=round(g("gpu_exp")/g("experiments_total")*100) if g("experiments_total") else 0
    gr=round(g("verdict_green")/g("verdicts")*100) if g("verdicts") else 0
    sig.append("✅ GPU-exp share **%d%%** · GREEN rate **%d%%** (%s/%s). CPU research is cheap+parallel; reserve GPU for serving cells."%(ratio,gr,g("verdict_green"),g("verdicts")))
    if pids:
        cl=[pv(p,"claims") for p in pids]
        if max(cl)-min(cl)>=4:
            mxp=pids[cl.index(max(cl))]; mnp=pids[cl.index(min(cl))]
            sig.append("⚠️ **PROJECT IMBALANCE** — %s %d claims vs %s %d; rebalance lanes toward %s."%(mxp,max(cl),mnp,min(cl),mnp))
    for s in sig: L.append("- "+s)
    L.append("")
    return "\n".join(L)
