#!/usr/bin/env python3
"""
rsrw_delhi.py - 'Delhi Past 7 Days' tab for the IMD RS/RW T-phi page.

What it does (all hooked into rsrw_tphigrams.py by rsrw_delhi.install(globals()), see the 3 inserted lines there):
  1. Every time the New Delhi (42182) chart is drawn, the 39 index values of the ascent are saved in
     <out>/delhi_indices.json  (one record per date_hour, e.g. 20261110_00).
  2. When the web page is published, that history is merged with the one already on the web page
     (the GitHub Actions computer starts empty each time) and uploaded as delhi_indices.json.
  3. The page gets a new tab 'Delhi Past 7 Days': one small line chart per index, 00 UTC and 12 UTC as two lines.
  4. Optional filling of past days (GitHub Actions 'Run workflow' inputs):
       RSRW_BACKFILL_DAYS=7  -> also process the ascents of the last 7 days
       RSRW_ONLY=42182       -> only that station (fast; other stations' charts already on the page are kept)
If anything here fails, the normal T-phi pipeline carries on without the tab.
"""
import base64, datetime as dt, json, math, os, re

IDX = '42182'                      # New Delhi
KEEP_DAYS = 30                     # history kept in delhi_indices.json (the tab shows the last 7 days)
HITS_BADGE = True                  # keep the hits.sh 'Page views' badge on the generated page (it is on the live page now)

GROUP_ORDER = ["Convective energy", "Moisture", "Stability indices", "Heights and levels",
               "Boundary layer and inversion", "Shear and severe weather"]
# (key, group) of the 39 index cards, in the SAME ORDER as the cards list in make_chart()
CARD_KEYS = [("RHmid", "Moisture"), ("RHlow", "Moisture"), ("FZL", "Heights and levels"), ("PW", "Moisture"),
             ("SBCAPE", "Convective energy"), ("MLCAPE", "Convective energy"), ("MUCAPE", "Convective energy"),
             ("CIN", "Convective energy"), ("LCLH", "Heights and levels"), ("DCAPE", "Convective energy"),
             ("VMAX", "Convective energy"), ("LI", "Stability indices"), ("SI", "Stability indices"),
             ("TT", "Stability indices"), ("KI", "Stability indices"), ("TEDEF", "Moisture"),
             ("MWPI", "Stability indices"), ("DMI", "Stability indices"), ("WINDEX", "Stability indices"),
             ("WBZ", "Heights and levels"), ("SWEAT", "Stability indices"), ("LFC", "Heights and levels"),
             ("LR06", "Stability indices"), ("LR75", "Stability indices"),
             ("INVB", "Boundary layer and inversion"), ("INVT", "Boundary layer and inversion"),
             ("INVS", "Boundary layer and inversion"), ("GMAX", "Boundary layer and inversion"),
             ("BLH", "Boundary layer and inversion"), ("BLHAGL", "Boundary layer and inversion"),
             ("BLW", "Boundary layer and inversion"), ("VC", "Boundary layer and inversion"), ("BLQ", "Moisture"),
             ("SH06", "Shear and severe weather"), ("BRN", "Shear and severe weather"),
             ("VGP", "Shear and severe weather"), ("SRH3", "Shear and severe weather"),
             ("SHIP", "Shear and severe weather"), ("EL", "Heights and levels")]

# ------------------------------------------------------------------ history file
def _path(out):
    return os.path.join(out, 'delhi_indices.json')

def load(path):
    try:
        d = json.load(open(path, encoding='utf-8'))
    except Exception:
        d = {}
    if not isinstance(d.get('records'), dict): d['records'] = {}
    return d

def _meta(labels):
    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    items = sorted(CARD_KEYS, key=lambda kg: order.get(kg[1], 99))          # stable: keeps card order inside a group
    return [{'k': k, 't': labels[k], 'g': g} for k, g in items]

def save(out, date, hour, cards, flag=''):
    """Store the index values of one New Delhi ascent. cards = [(label, value), ...] from make_chart()."""
    if len(cards) != len(CARD_KEYS):
        raise ValueError(f"{len(cards)} index cards but {len(CARD_KEYS)} expected - update CARD_KEYS in rsrw_delhi.py")
    vals, labels = {}, {}
    for (k, _), (lab, val) in zip(CARD_KEYS, cards):
        labels[k] = lab.replace('\n', ' ')
        try: f = float(val)
        except Exception: f = None
        vals[k] = round(f, 2) if f is not None and math.isfinite(f) else None
    p = _path(out); d = load(p)
    d['meta'] = _meta(labels)
    d['records'][f"{date.replace('-', '')}_{hour}"] = {'v': vals, 'flag': flag or ''}
    d['records'] = dict(sorted(d['records'].items()))
    json.dump(d, open(p, 'w', encoding='utf-8'), separators=(',', ':'))

def merged_json(out, gh, branch):
    """This computer's history + the one already on the web page -> JSON text to upload (also saved locally).
    A failed read of the web page's copy raises, so a good history is never overwritten by a short one."""
    local = load(_path(out)); remote = {}
    try:
        r = gh.call('GET', gh.rp('/contents/delhi_indices.json'), params={'ref': branch})
        remote = json.loads(base64.b64decode(r['content']).decode('utf-8'))
    except RuntimeError as e:
        if ' 404 ' not in str(e): raise                       # 404 = no history yet; anything else = try again later
    recs = dict(remote.get('records', {})); recs.update(local.get('records', {}))
    cut = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).strftime('%Y%m%d')
    recs = {k: v for k, v in sorted(recs.items()) if k[:8] >= cut}
    d = {'station': IDX, 'name': 'NEW DELHI', 'meta': local.get('meta') or remote.get('meta') or [], 'records': recs}
    json.dump(d, open(_path(out), 'w', encoding='utf-8'), separators=(',', ':'))
    return json.dumps(d, separators=(',', ':'))

# ------------------------------------------------------------------ page: tab, charts
CSS = r"""
.dlbar{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin:0 0 6px}
.dlleg{display:flex;gap:14px;align-items:center;font-size:12.5px}.dlleg span{display:inline-flex;align-items:center}
.dlleg i{display:inline-block;width:18px;height:3px;border-radius:2px;margin-right:5px}
.dlh{font-size:15px;font-weight:700;margin:20px 0 8px}
.dlgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.dlc{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.dlc b{display:block;font-size:12.5px;line-height:1.25;min-height:2.5em}
.dlc .now{font-size:12px;color:var(--mut);margin:2px 0 4px}
.dlc svg{width:100%;height:auto;display:block}
.dlc .ax{stroke:var(--mut);stroke-width:1}.dlc .gr{stroke:var(--line);stroke-width:.6;stroke-dasharray:3 3}
.dlc text{fill:var(--mut);font-size:9.5px}
.dl00{stroke:var(--acc);fill:var(--acc)}.dl12{stroke:#d9822b;fill:#d9822b}
.dlc path{fill:none;stroke-width:1.8}.dlc circle.fl{stroke:#c0392b;stroke-width:1.8;fill:var(--card)}
"""

MAIN = r"""<main id="delhi" hidden><div class="dlbar"><b>New Delhi (42182) &mdash; thermodynamic indices, last 7 days</b>
<span class="dlleg"><span><i style="background:var(--acc)"></i>00 UTC</span><span><i style="background:#d9822b"></i>12 UTC</span><span><i style="width:8px;height:8px;border:2px solid #c0392b;border-radius:50%"></i>data-check flag</span></span></div>
<div class="mut" id="dlinfo" style="margin-bottom:6px"></div><div id="dlbody"></div></main>
"""

JS = r"""<script>
(function(){
  const g=id=>document.getElementById(id), MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'], DAY=864e5;
  let D=null;
  const tm=k=>Date.UTC(+k.slice(0,4),+k.slice(4,6)-1,+k.slice(6,8),+k.slice(9,11));
  const fv=v=>v==null?'n/a':(Math.abs(v)>=100?v.toFixed(0):Math.abs(v)>=10?v.toFixed(1):v.toFixed(2));
  const dm=t=>{const d=new Date(t);return d.getUTCDate()+' '+MON[d.getUTCMonth()]};
  const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  function chart(m,use,recs,W,E){
    const w=300,h=150,ml=44,mr=8,mt=8,mb=22,px=x=>ml+(x-W)/(E-W)*(w-ml-mr);
    const S={'00':[],'12':[]}; let lo=Infinity,hi=-Infinity;
    use.forEach(k=>{const v=recs[k].v[m.k],hh=k.slice(9,11); if(v==null||!S[hh]) return;
      S[hh].push({t:tm(k),v:v,f:!!recs[k].flag}); lo=Math.min(lo,v); hi=Math.max(hi,v)});
    if(!isFinite(lo)) return '<div class="now">no value in this period</div>';
    let a=lo,z=hi; const rng=(z-a)||Math.abs(z)*0.2||1;
    if(a>=0&&a<0.4*z) a=0; else a-=rng*0.1;
    if(z<=0&&z>0.4*a) z=0; else z+=rng*0.1;
    const py=v=>mt+(z-v)/(z-a)*(h-mt-mb);
    let s='<svg viewBox="0 0 '+w+' '+h+'" role="img" aria-label="'+esc(m.t)+'">';
    [a,(a+z)/2,z].forEach(v=>{s+='<line class="gr" x1="'+ml+'" x2="'+(w-mr)+'" y1="'+py(v).toFixed(1)+'" y2="'+py(v).toFixed(1)+'"/><text x="'+(ml-4)+'" y="'+(py(v)+3).toFixed(1)+'" text-anchor="end">'+fv(v)+'</text>'});
    for(let d=W;d<E;d+=DAY){const x=px(d).toFixed(1); s+='<line class="gr" x1="'+x+'" x2="'+x+'" y1="'+mt+'" y2="'+(h-mb)+'"/><text x="'+x+'" y="'+(h-7)+'" text-anchor="middle">'+dm(d)+'</text>'}
    s+='<line class="ax" x1="'+ml+'" x2="'+ml+'" y1="'+mt+'" y2="'+(h-mb)+'"/><line class="ax" x1="'+ml+'" x2="'+(w-mr)+'" y1="'+(h-mb)+'" y2="'+(h-mb)+'"/>';
    ['00','12'].forEach(hh=>{
      const P=S[hh]; let d='',prev=null;
      P.forEach(p=>{d+=(prev&&p.t-prev.t<=30*36e5?'L':'M')+px(p.t).toFixed(1)+' '+py(p.v).toFixed(1)+' '; prev=p});
      if(d) s+='<path class="dl'+hh+'" d="'+d+'"/>';
      P.forEach(p=>{s+='<circle class="dl'+hh+(p.f?' fl':'')+'" cx="'+px(p.t).toFixed(1)+'" cy="'+py(p.v).toFixed(1)+'" r="3"><title>'+dm(p.t)+' '+hh+' UTC: '+fv(p.v)+(p.f?' (data-check flag)':'')+'</title></circle>'});
    });
    return s+'</svg>';
  }
  function draw(){
    const body=g('dlbody'), recs=(D&&D.records)||{}, keys=Object.keys(recs).sort(), meta=(D&&D.meta)||[];
    if(!keys.length||!meta.length){body.innerHTML='<div class="empty">No New Delhi (42182) ascents saved yet. The history fills in with each scheduled update.</div>'; g('dlinfo').textContent=''; return}
    const last=keys[keys.length-1], L=tm(last), d0=L-L%DAY, W=d0-6*DAY, E=d0+DAY, use=keys.filter(k=>{const t=tm(k);return t>=W&&t<E});
    g('dlinfo').textContent=use.length+' ascents, '+dm(W)+' to '+dm(d0)+' · latest '+last.slice(9,11)+' UTC '+dm(L)+' · red ring = data-check flag on that ascent (see its T-phi chart)';
    const groups=[]; meta.forEach(m=>{let G=groups.find(x=>x.n===m.g); if(!G){G={n:m.g,items:[]};groups.push(G)} G.items.push(m)});
    body.innerHTML=groups.map(G=>'<div class="dlh">'+esc(G.n)+'</div><div class="dlgrid">'+G.items.map(m=>{
      const k=[...use].reverse().find(q=>recs[q].v[m.k]!=null);
      const now=k?'Latest '+fv(recs[k].v[m.k])+' ('+dm(tm(k))+' '+k.slice(9,11)+' UTC)':'no value';
      return '<div class="dlc"><b>'+esc(m.t)+'</b><div class="now">'+now+'</div>'+chart(m,use,recs,W,E)+'</div>'}).join('')+'</div>').join('');
  }
  window.DELHI_SHOW=function(){
    if(D) return draw();
    fetch('delhi_indices.json?t='+Date.now()).then(r=>{if(!r.ok) throw 0; return r.json()})
      .then(j=>{D=j;draw()}).catch(()=>{D={records:{},meta:[]};draw();D=null});
  };
})();
</script>
"""

BADGE = ('<a class="xlink" href="https://hits.sh/krishhindustan-creator.github.io/rsrw-tphigrams.svg" target="_blank" rel="noopener" '
         'style="margin-left:auto"><img alt="Page views" src="https://hits.sh/krishhindustan-creator.github.io/rsrw-tphigrams.svg'
         '?style=flat-square&label=Page%20views&color=0b5cad&labelColor=14202b"></a>')

def patch_page(html):
    """Add the Delhi tab to the generated page. All anchors must be found, otherwise the page is left as it was."""
    # (old text, new text); every 'old' must occur exactly once
    edits = [
        ('</style></head><body>', CSS + '</style></head><body>'),
        ('<button id="t_tphi">T-phi grams</button>', '<button id="t_tphi">T-phi grams</button><button id="t_delhi">Delhi Past 7 Days</button>'),
        ('<main id="nwp" hidden>', MAIN + '<main id="nwp" hidden>'),
        ('<script src="archive.js"></script>', '<script src="archive.js"></script>\n' + JS),
        ("T={tphi:'IMD RS/RW T-phi grams',ens:", "T={tphi:'IMD RS/RW T-phi grams',delhi:'Delhi Past 7 Days',ens:"),
        ("['tphi','ens','mme','adeck'].forEach(k=>g('t_'+k).setAttribute('aria-pressed',k===t));",
         "['tphi','delhi','ens','mme','adeck'].forEach(k=>g('t_'+k).setAttribute('aria-pressed',k===t));"),
        ("g('nwp').hidden=t==='tphi';", "g('nwp').hidden=!(t==='ens'||t==='mme'||t==='adeck'); g('delhi').hidden=t!=='delhi';"),
        ("if(t!=='tphi') window.NWP_SHOW(t);", "if(t==='delhi') window.DELHI_SHOW(); else if(t!=='tphi') window.NWP_SHOW(t);"),
        ("['tphi','ens','mme','adeck'].forEach(k=>g('t_'+k).onclick=()=>tab(k));",
         "['tphi','delhi','ens','mme','adeck'].forEach(k=>g('t_'+k).onclick=()=>tab(k));"),
    ]
    if HITS_BADGE and 'hits.sh' not in html:
        edits.append(('<span class="mut" id="info"></span></span></header>', '<span class="mut" id="info"></span></span>' + BADGE + '</header>'))
    bad = [o[:60] for o, _ in edits if html.count(o) != 1]
    if bad:
        print("  Delhi tab NOT added - the page template changed; these anchors were not found once:", bad)
        return html
    for o, n in edits: html = html.replace(o, n)
    return html

# ------------------------------------------------------------------ hook into rsrw_tphigrams.py
def install(R):
    """R = globals() of rsrw_tphigrams.py (called from its last lines)."""
    ctx = {}
    try:
        R['ARCHIVE_HTML'] = patch_page(R['ARCHIVE_HTML'])
    except Exception as e:
        print("  Delhi tab: page not patched:", e)

    # remember which date/hour/output folder is being processed
    orig_process = R['process']
    def process(src, date, hour, out):
        ctx.update(date=date, hour=hour, out=out)
        return orig_process(src, date, hour, out)
    R['process'] = process

    # save the 39 values whenever the New Delhi chart is drawn
    orig_chart = R['make_chart']
    def make_chart(S, station, idx, when, out_png):
        res = orig_chart(S, station, idx, when, out_png)
        if idx == IDX:
            try:
                save(ctx['out'], ctx['date'], ctx['hour'], R['LAST_CARDS'], res.get('suspect', ''))
            except Exception as e:
                print(f"  Delhi history not saved: {e}")
        return res
    R['make_chart'] = make_chart

    # upload the merged history together with the page (it must be in the list of files, otherwise it would be deleted)
    R['EXTRA_TEXTS'] = lambda out, gh, branch: {'delhi_indices.json': merged_json(out, gh, branch)}

    # optional: only some stations, and/or fill in the last N days
    only = {x for x in re.split(r'[,\s]+', os.environ.get('RSRW_ONLY', '').strip()) if x}
    if only:
        orig_links = R['links_from_html']
        R['links_from_html'] = lambda *a, **k: {i: u for i, u in orig_links(*a, **k).items() if i in only}
        print("  only these stations:", ", ".join(sorted(only)))
    try: bf = int(os.environ.get('RSRW_BACKFILL_DAYS') or 0)
    except ValueError: bf = 0
    if bf > 0:
        orig_cu = R['catchup_hours']
        R['catchup_hours'] = lambda: int(min(max(orig_cu(), bf * 24 + 12), 240))
        print(f"  filling in the last {bf} day(s)")
