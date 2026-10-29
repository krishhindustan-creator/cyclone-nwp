#!/usr/bin/env python3
"""
rsrw_tphigrams.py  -  Batch T-phi grams + thermodynamic indices for all IMD RS/RW stations.

For one date and hour (00 or 12 UTC) it:
  1. gets every station's File-3 Standard Summary (_sum.txt), or its File-4 zip (_other.zip) when File-3
     is missing (online from clims.imd.gov.in, or from a local folder),
  2. reads the sounding table (Time, P(hPa), T(C), U(%), Dew(C), Geo(gpm) ... Wdir, Wspd),
  3. draws a T-phi gram + 39-card thermodynamic indices sheet per station,
  4. writes <out>/<YYYYMMDD>_<HH>Z/<index>_<STATION>.png and an index.html gallery,
  5. lists stations that were missing or failed.

Requirements:  Python 3.9+,  pip install numpy matplotlib requests

USAGE
  Daily automatic run (what the Windows schedule calls): fill in rsrw_config.json, then
      python rsrw_tphigrams.py --auto
    -> downloads every new ascent of the last 36 h, plots it, and updates Tphigrams/index.html (archive)

  Offline (files you already downloaded into one folder; browser names like 'rsrw_download (3).zip' are fine):
      python rsrw_tphigrams.py --date 2026-10-19 --hour 00 --zips ./downloads

  Online (uses your logged-in browser session cookie):
      python rsrw_tphigrams.py --date 2026-10-19 --hour 00 \
             --list-url "https://clims.imd.gov.in/<list page>?date={date}" \
             --cookie "PHPSESSID=xxxxxxxx"
    * {date} is replaced by YYYY-MM-DD, {hour} by 00/12.
    * Or save the list page from the browser (Ctrl+S) and pass --list-html saved_page.html
    * The cookie can also go in rsrw_config.json: {"cookie": "PHPSESSID=...", "list_url": "..."}
"""
import argparse, datetime as dt, html, io, json, os, re, sys, time, traceback, zipfile
from urllib.parse import urljoin, parse_qs, urlparse
_HERE=os.path.dirname(os.path.abspath(__file__))
if __name__=='__main__' and '--log' in sys.argv:          # scheduled run: everything (even crashes) goes to rsrw_log.txt
    sys.stdout=sys.stderr=open(os.path.join(_HERE,'rsrw_log.txt'),'a',encoding='utf-8',buffering=1)
# Anaconda started by Task Scheduler is not 'activated': add its DLL folders so numpy/matplotlib can load
for _d in (r'Library\mingw-w64\bin', r'Library\usr\bin', r'Library\bin', 'Scripts', 'bin', ''):
    _p=os.path.join(sys.prefix,_d)
    if os.path.isdir(_p):
        os.environ['PATH']=_p+os.pathsep+os.environ.get('PATH','')
        try: os.add_dll_directory(_p)
        except Exception: pass
try:
    import numpy as np
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    from matplotlib.collections import LineCollection
except Exception:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] could not load numpy/matplotlib with {sys.executable}:")
    traceback.print_exc(); sys.exit(1)
import warnings; warnings.filterwarnings('ignore', category=RuntimeWarning)

BASE = "https://clims.imd.gov.in/"
# File-1 = <idx>_<date>_<hh>_rsr.txt, File-2 = _int.txt (integration), File-3 = _sum.txt (Standard Summary),
# File-4 = _other.zip. adt4 is a per-ascent id, so links are read from the list page, not built.
PREFER = ("sum", "other", "rsr")   # File-3 full-resolution summary, then File-4 zip, then File-1 (significant levels only)

STATIONS = {  # index -> name (from the RS/RW list page); unknown indices fall back to "STATION <idx>"
 "42027":"SRINAGAR","42079":"SUNDERNAGAR","42101":"PATIALA","42111":"DEHRADUN","42123":"SRIGANGANAGAR",
 "42182":"NEW DELHI","42314":"MOHANBARI","42348":"JAIPUR","42369":"LUCKNOW","42379":"GORAKHPUR",
 "42399":"JALPAIGURI","42410":"GUWAHATI","42492":"PATNA","42647":"AHMEDABAD","42667":"BHOPAL",
 "42675":"JABALPUR","42701":"RANCHI","42724":"AGARTALA","42875":"RAIPUR","42886":"JHARSUGUDA",
 "42971":"BHUBANESWAR","43003":"MUMBAI (SANTACRUZ)","43110":"RATNAGIRI","43128":"HYDERABAD",
 "43150":"VISAKHAPATNAM","43185":"MACHILIPATNAM","43192":"GOA","43243":"KAVALI","43279":"CHENNAI",
 "43285":"MANGALORE","43295":"BENGALURU","43311":"AMINIDIVI","43346":"KARAIKAL","43353":"KOCHI","42867":"NAGPUR","42809":"KOLKATA","43371":"THIRUVANANTHAPURAM",
}

LEARNED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stations_learned.json')
def clean_name(name):
    """Station name in capitals, or None if it does not look like a place name (HTML leftovers, numbers ...)."""
    name=html.unescape(name or '')
    name=name.split('>')[-1]                                   # drop any leftover tag text such as  style="...">
    name=re.sub(r'^\s*RSRW[\s_-]*','',name,flags=re.I).replace('_',' ')
    name=re.sub(r'\s+',' ',name).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z .()'/-]{2,40}",name) or re.search(r'STYLE|FONT|CLASS|WIDTH|COLOR|DOWNLOAD|UTC|STATION$',name):
        return None
    return name

def _load_learned():
    try: d=json.load(open(LEARNED_FILE, encoding='utf-8'))
    except Exception: return
    good={k:clean_name(v) for k,v in d.items()}
    good={k:v for k,v in good.items() if v}
    STATIONS.update(good)
    if good!=d:                                                # repair a file with bad names from older versions
        try: json.dump(good,open(LEARNED_FILE,'w',encoding='utf-8'),indent=1,sort_keys=True)
        except Exception: pass
_load_learned()

def learn(idx, name, authoritative=False):
    """Remember a station name so charts and the archive show it. Names from the IMD table win over built-ins."""
    name=clean_name(name)
    if not idx or not name or STATIONS.get(idx)==name: return
    if not authoritative and idx in STATIONS and not STATIONS[idx].startswith('STATION'): return
    STATIONS[idx]=name
    try:
        old=json.load(open(LEARNED_FILE,encoding='utf-8')) if os.path.exists(LEARNED_FILE) else {}
        old[idx]=name; json.dump(old,open(LEARNED_FILE,'w',encoding='utf-8'),indent=1,sort_keys=True)
    except Exception: pass

def name_from_file(data):
    """Station name written inside a File-4 zip (…-42111-RSRW_DEHRADUN-…) or a File-1 record (Station Name : RSRW NAGPUR)."""
    try:
        if data[:2]==b'PK':
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for n in z.namelist():
                    m=re.search(r'-(\d{5})-RSRW[_ ]([A-Za-z _().-]+?)-',n)
                    if m: return m[1], m[2]
                    if n.lower().endswith('.txt') and 'rsr' in n.lower():
                        t=z.read(n)[:1500].decode('utf-8','ignore')
                        a=re.search(r'Station Name\s*:\s*(.+)',t); b=re.search(r'Station Number\s*:\s*(\d{5})',t)
                        if a and b: return b[1], a[1].strip()
    except Exception: pass
    return None, None

# ------------------------------------------------------------------ thermodynamics
Rd,Cp,Lv,eps,g,K0 = 287.04,1005.7,2.501e6,0.622,9.80665,273.15; kap = Rd/Cp
es = lambda t: 6.112*np.exp(17.67*t/(t+243.5))
def wsat(t,p): e=es(t); return eps*e/(p-e)
def theta(t,p): return (t+K0)*(1000/p)**kap
def tv(t,p,w): return (t+K0)*(1+w/eps)/(1+w)
def thetae(t,td,p):
    w=wsat(td,p); Tk=t+K0; Tdk=td+K0
    Tl=1/(1/(Tdk-56)+np.log(Tk/Tdk)/800)+56
    return Tk*(1000/p)**(0.2854*(1-0.28*w))*np.exp((3.376/Tl-0.00254)*1000*w*(1+0.81*w))
def lcl(t,td,p):
    Tk,Tdk=t+K0,td+K0
    Tl=1/(1/(Tdk-56)+np.log(Tk/Tdk)/800)+56
    return p*(Tl/Tk)**(1/kap),Tl-K0
def mstep(tk,p):
    r=wsat(tk-K0,p); return (Rd*tk+Lv*r)/(p*(Cp+Lv**2*r*eps/(Rd*tk**2)))
def moist(t0,pgrid):
    out=[t0]; t=t0+K0
    for i in range(1,len(pgrid)):
        p=pgrid[i-1]; dp=pgrid[i]-p
        k1=mstep(t,p);k2=mstep(t+.5*dp*k1,p+.5*dp);k3=mstep(t+.5*dp*k2,p+.5*dp);k4=mstep(t+dp*k3,p+dp)
        t+=dp*(k1+2*k2+2*k3+k4)/6; out.append(t-K0)
    return np.array(out)
def wetbulb(t,td,p):
    pl,tl=lcl(t,td,p)
    if pl>=p: return t
    return moist(tl,np.linspace(pl,p,60))[-1]
def parcel(S,p0,t0,td0,virtual=True):
    P=S['p']; m=P<=p0; pp=np.r_[p0,P[m]]
    pl,tl=lcl(t0,td0,p0); w0=wsat(td0,p0)
    tp=np.empty_like(pp); dry=pp>=pl
    tp[dry]=(t0+K0)*(pp[dry]/p0)**kap-K0
    if (~dry).any(): tp[~dry]=moist(tl,np.r_[pl,pp[~dry]])[1:]
    te=np.interp(np.log(pp),np.log(P[::-1]),S['t'][::-1])
    tde=np.interp(np.log(pp),np.log(P[::-1]),S['td'][::-1])
    if virtual:
        wp=np.where(dry,w0,wsat(tp,pp)); bp=tv(tp,pp,wp); be=tv(te,pp,wsat(tde,pp))
    else: bp=tp+K0; be=te+K0
    return pp,tp,bp-be,pl,tl
def cape_cin(pp,dT):
    dl=-np.diff(np.log(pp)); dm=.5*(dT[1:]+dT[:-1])
    pos=np.where(dm>0)[0]
    if not len(pos): return 0.,0.,np.nan,np.nan
    groups=np.split(pos,np.where(np.diff(pos)>1)[0]+1)
    areas=[Rd*np.sum(dm[gp]*dl[gp]) for gp in groups]
    lfc_i=groups[int(np.argmax(areas))][0]; el_i=groups[-1][-1]+1
    idx=np.arange(len(dm))
    return (Rd*np.sum((dm*dl)[(dm>0)&(idx>=lfc_i)]), Rd*np.sum((dm*dl)[(dm<0)&(idx<lfc_i)]),
            pp[lfc_i], pp[el_i])

# ------------------------------------------------------------------ sounding file parsing
def _tables(text):
    """Yield (columns, rows) for every comma table whose header has a pressure column."""
    lines=text.splitlines(); i=0
    while i<len(lines):
        cols=[c.strip().lower() for c in lines[i].split(',')]
        if any(re.match(r'p\s*\(',c) for c in cols) and len(cols)>=4:
            rows=[]; j=i+1
            while j<len(lines):
                c=[x.strip() for x in lines[j].split(',')]
                try: float(c[1] if re.match(r'time',cols[0]) else c[0]); rows.append(c); j+=1
                except (ValueError,IndexError):
                    if lines[j].strip()=='' and not rows: j+=1; continue
                    break
            yield cols,rows; i=j
        else: i+=1

def _num(rows,j):
    out=[]
    for r in rows:
        try: v=float(r[j]); out.append(np.nan if v<=-998 else v)
        except (ValueError,IndexError): out.append(np.nan)
    return np.array(out)

def _find(cols,*pats,exclude=None):
    for j,c in enumerate(cols):
        if any(re.match(pt,c) for pt in pats) and not (exclude and re.search(exclude,c)): return j
    return None

QC_NOTE=['']

def parse_sounding(text):
    """Read the PTU table (+ wind table if separate) from a Standard Summary / PTU Summary / rsr record.
    Handles header spellings like P(hPa)/p(hPa), T(C)/T(Deg.C), Dew(C)/Dew(Deg.C) or Dew Diff(C)."""
    ptu=wind=None
    for cols,rows in _tables(text):
        if not rows: continue
        jp=_find(cols,r'p\s*\('); jt=_find(cols,r't\s*\(')
        jd=_find(cols,r'dew\s*\(',exclude='diff'); jdd=_find(cols,r'dew\s*diff'); ju=_find(cols,r'u\s*\(')
        jz=_find(cols,r'geo\s*\(\s*gpm'); jwd=_find(cols,r'wdir'); jws=_find(cols,r'wspd')
        P=_num(rows,jp)
        if jt is not None and (jd is not None or jdd is not None) and ptu is None and len(rows)>=10:
            T=_num(rows,jt); Td=_num(rows,jd) if jd is not None else T-_num(rows,jdd)
            U=_num(rows,ju) if ju is not None else np.full(len(P),np.nan)
            Z=_num(rows,jz) if jz is not None else np.full(len(P),np.nan)
            W=(_num(rows,jwd),_num(rows,jws)) if jwd is not None and jws is not None else None
            ptu=dict(p=P,t=T,td=Td,rh=U,z=Z,w=W)
        elif ptu is not None and ptu['w'] is None and wind is None and jwd is not None and jws is not None and jt is None and len(rows)>=5:
            wind=dict(p=P,d=_num(rows,jwd),s=_num(rows,jws),z=_num(rows,jz) if jz is not None else None)
    if ptu is None: raise ValueError("no PTU table (P, T, Dew) found")
    P,T,Td,U,Z=ptu['p'],ptu['t'],ptu['td'],ptu['rh'],ptu['z']
    ok=np.isfinite(P)&np.isfinite(T)&np.isfinite(Td)&np.isfinite(Z)
    P,T,Td,U,Z=P[ok],T[ok],Td[ok],U[ok],Z[ok]
    if ptu['w'] is not None:
        D,Sp=ptu['w'][0][ok],ptu['w'][1][ok]
    else:
        if wind is None: raise ValueError("PTU table found but no wind data")
        wk=np.isfinite(wind['p'])&np.isfinite(wind['d'])&np.isfinite(wind['s'])
        wp,wd,ws=wind['p'][wk],wind['d'][wk],wind['s'][wk]
        # merge PTU and wind levels on one log-p grid (significant-level files)
        allp=np.unique(np.r_[P,wp][(np.r_[P,wp]<=P.max())&(np.r_[P,wp]>=P.min())])[::-1]
        ip=lambda x,y: np.interp(np.log(allp),np.log(x[::-1]),y[::-1])
        o=np.argsort(-P); P,T,Td,U,Z=P[o],T[o],Td[o],U[o],Z[o]
        o=np.argsort(-wp); wp,wd,ws=wp[o],wd[o],ws[o]
        wu,wv=-ws*np.sin(np.radians(wd)),-ws*np.cos(np.radians(wd))
        uu,vv=ip(wp,wu),ip(wp,wv)
        T,Td,U,Z=ip(P,T),ip(P,Td),ip(P,U),ip(P,Z); P=allp
        Sp=np.hypot(uu,vv); D=np.degrees(np.arctan2(-uu,-vv))%360
    if np.isnan(U).all(): U=100*es(Td)/es(T)
    D=np.where(np.isfinite(D),D,0.); Sp=np.where(np.isfinite(Sp),Sp,0.)
    R=np.c_[P,T,U,Td,Z,D,Sp]; keep=[0]
    for i in range(1,len(R)):
        if R[i,0]<R[keep[-1],0]: keep.append(i)
    R=R[keep]
    if len(R)<10: raise ValueError(f"only {len(R)} usable levels")
    # --- quality control: remove impossible temperatures and isolated spikes (partial sensor failure)
    P_,T_=R[:,0],R[:,1]
    bad=(T_>50)|(T_<-95)|((P_>=300)&(T_<-60))|((P_>=500)&(T_<-40))
    n=len(T_); w=7
    if n>2*w+1:                                               # spike test against a running median of good points
        Tm=np.where(bad,np.nan,T_)
        pad=np.r_[np.full(w,np.nan),Tm,np.full(w,np.nan)]
        med=np.nanmedian(np.lib.stride_tricks.sliding_window_view(pad,2*w+1),axis=1)
        bad|=np.abs(T_-med)>8
    low=P_>=300; frac=bad[low].mean() if low.any() else bad.mean()
    if frac>0.2:
        raise ValueError(f"temperature sensor failure in this sounding ({frac*100:.0f}% of levels below 300 hPa "
                         f"have impossible temperatures, e.g. {np.median(T_[low&bad]) if (low&bad).any() else np.nan:.0f} °C) - not plotted")
    if bad.any():
        R=R[~bad]
    R[:,3]=np.minimum(R[:,3],R[:,1])                          # dew point never above temperature
    QC_NOTE[0]=f"{int(bad.sum())} bad temperature level(s) removed by quality control" if bad.any() else ''
    if len(R)<10: raise ValueError(f"only {len(R)} usable levels after quality control")
    return dict(p=R[:,0],t=R[:,1],rh=R[:,2],td=R[:,3],z=R[:,4],d=R[:,5],s=R[:,6])

def sounding_from_zip(data):
    """Return (parsed sounding, member name) from zip bytes; tries every text member."""
    errs=[]
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        rank=lambda n: (0 if re.search(r"standard[ _-]*summary|_sum\.txt$",n,re.I) else
                        1 if n.lower().endswith('.zip') else 2 if n.lower().endswith('.txt') else 9, n)
        names=sorted(z.namelist(), key=rank)
        for n in names:
            if n.endswith('/') or rank(n)[0]==9: continue
            raw=z.read(n)
            if n.lower().endswith('.zip'):
                try: return sounding_from_zip(raw)
                except Exception as e: errs.append(f"{n}: {e}"); continue
            try: return parse_sounding(raw.decode('utf-8','ignore')), n
            except Exception as e: errs.append(f"{n}: {e}")
    sf=[e for e in errs if 'sensor failure' in e]
    if sf: raise ValueError(sf[0].split(': ',1)[1])
    raise ValueError("no usable sounding table in zip -> " + " | ".join(errs[:6]))

# ------------------------------------------------------------------ indices + figure
def make_chart(S, station, idx, when, out):
    P,T,Td,Z,RH,D,Sp=S['p'],S['t'],S['td'],S['z'],S['rh'],S['d'],S['s']
    H=Z-Z[0]; u=-Sp*np.sin(np.radians(D)); v=-Sp*np.cos(np.radians(D))
    at=lambda arr,L: np.interp(np.log(L),np.log(P[::-1]),arr[::-1])
    ath=lambda arr,h: np.interp(h,H,arr)
    def safe(fn):
        try: return float(fn())
        except Exception: return np.nan
    p0,t0,td0=P[0],T[0],Td[0]
    sfc_note=''
    lay=(P<P[0])&(P>=P[0]-10)
    if lay.sum()>=3 and td0-np.mean(Td[lay])>5:          # surface dew point much moister than the air just above
        sfc_note=(f"surface Td {td0:.1f} °C vs {np.mean(Td[lay]):.1f} °C in lowest 10 hPa - "
                  "SBCAPE/MUCAPE may be overstated, see MLCAPE")
    pp_sb,tp_sb,b_sb,pl_sb,_=parcel(S,p0,t0,td0)
    SB,CIN,LFC,EL=cape_cin(pp_sb,b_sb)
    m=P>=p0-100; th=np.mean(theta(T[m],P[m])); wml=np.mean(wsat(Td[m],P[m]))
    tml=th*(p0/1000)**kap-K0; e=wml*p0/(eps+wml); a=np.log(e/6.112); tdml=243.5*a/(17.67-a)
    r=parcel(S,p0,tml,tdml); ML=cape_cin(r[0],r[2])[0]
    Tdx=Td
    m=P>=p0-300; i=np.argmax(thetae(T[m],Tdx[m],P[m])); pmu,tmu,tdmu=P[m][i],T[m][i],Tdx[m][i]
    r=parcel(S,pmu,tmu,tdmu); MU=cape_cin(r[0],r[2])[0]
    def lifted500(p,t,td):
        r=parcel(S,p,t,td,False); return at(T,500)-np.interp(np.log(500),np.log(r[0][::-1]),r[1][::-1])
    LI=safe(lambda: lifted500(p0,t0,td0)); SI=safe(lambda: lifted500(850,at(T,850),at(Td,850)))
    T8,T7,T5,D8,D7,D5=at(T,850),at(T,700),at(T,500),at(Td,850),at(Td,700),at(Td,500)
    KI=T8-T5+D8-(T7-D7); TT=T8+D8-2*T5
    RHmid=RH[(P<=700)&(P>=500)].mean(); RHlow=RH[P>=700].mean()
    w=wsat(Td,P); PW=-np.trapezoid(w,P*100)/g
    FZL=safe(lambda: Z[np.where(T<=0)[0][0]])
    def dcape():
        m=P>=400; te=thetae(T[m],Td[m],P[m]); i=np.argmin(te); ps=P[m][i]
        tw=wetbulb(T[m][i],Td[m][i],ps); grid=np.r_[ps,P[P>ps][::-1]]
        bb=moist(tw,grid)-at(T,grid); dl=np.diff(np.log(grid))
        return -Rd*np.sum(.5*(bb[1:]+bb[:-1])*dl), thetae(t0,td0,p0)-te.min()
    try: DCAPE,TEDEF=dcape()
    except Exception: DCAPE=TEDEF=np.nan
    VMAX=np.sqrt(DCAPE) * np.sqrt(2) if np.isfinite(DCAPE) and DCAPE>0 else np.nan
    LR06=safe(lambda:(t0-ath(T,6000))/6); LR75=(T7-T5)/(at(Z,500)-at(Z,700))*1000
    T67,D67=at(T,670),at(Td,670); LR8_67=(T8-T67)/(at(Z,670)-at(Z,850))*1000
    MWPI=SB/100+LR8_67/5+((T8-D8)-(T67-D67))/5
    DMI=LR75+(T7-D7)-(T5-D5)
    dT=np.diff(T); invb=invt=invs=np.nan; i=0
    while i<len(dT):
        if dT[i]>0:
            j=i
            while j<len(dT) and dT[j]>=0: j+=1
            if T[j]-T[i]>=0.5: invb,invt,invs=H[i],H[j],T[j]-T[i]; break
            i=j
        else: i+=1
    gr=np.diff(T)/np.maximum(np.diff(H),1e-3)*1000; GMAX=safe(lambda: np.nanmax(np.where(H[1:]<3000,gr,np.nan)))
    thv=theta(T,P)*(1+0.61*w)
    Ri=g/thv[0]*(thv-thv[0])*H/np.maximum((u-u[0])**2+(v-v[0])**2,0.1)
    BLH=safe(lambda: H[np.where((Ri>0.25)&(H>50))[0][0]])
    bl=H<=(BLH if np.isfinite(BLH) else 0)+1e-6; BLW=np.mean(Sp[bl]); VC=BLH*BLW; BLQ=np.mean(w[bl])*1000
    u6,v6=ath(u,6000),ath(v,6000); SH06=np.hypot(u6-u[0],v6-v[0])
    u3,v3=ath(u,3000),ath(v,3000); SH03=np.hypot(u3-u[0],v3-v[0])
    mm=H<=6000; um,vm=u[mm].mean(),v[mm].mean(); m5=H<=500; u5,v5=u[m5].mean(),v[m5].mean()
    sh=np.array([u6-u5,v6-v5]); sh/=max(np.linalg.norm(sh),1e-6); ur,vr=um+7.5*sh[1],vm-7.5*sh[0]
    def srh(top):
        k=H<=top; uu,vv=u[k],v[k]; return np.sum((uu[1:]-ur)*(vv[:-1]-vr)-(uu[:-1]-ur)*(vv[1:]-vr))
    SRH3,SRH1=srh(3000),srh(1000)
    BRN=safe(lambda: SB/(0.5*np.hypot(um-u5,vm-v5)**2))
    VGP=np.sqrt(SB)*SH03/3000
    LCLH=np.interp(np.log(pl_sb),np.log(P[::-1]),H[::-1])
    SCP=(MU/1000)*(SRH3/50)*(0 if SH06<10 else min(SH06,20)/20)
    STP=(SB/1500)*min(max((2000-LCLH)/1000,0),1)*(SRH1/150)*(0 if SH06<12.5 else min(SH06,30)/20)
    wmu=wsat(tdmu,pmu)*1000; SHIP=MU*wmu*LR75*(-T5)*min(max(SH06,7),27)/42e6
    if MU<1300: SHIP*=MU/1300
    if LR75<5.8: SHIP*=LR75/5.8
    if np.isfinite(FZL) and FZL<2400: SHIP*=FZL/2400
    kt=1.94384; f8,f5=at(Sp,850)*kt,at(Sp,500)*kt; d8,d5=at(D,850),at(D,500)
    SWEAT=12*max(D8,0)+20*max(TT-49,0)+2*f8+f5
    if 130<=d8<=250 and 210<=d5<=310 and d5-d8>0 and f8>=15 and f5>=15: SWEAT+=125*(np.sin(np.radians(d5-d8))+0.2)
    ix=np.arange(0,len(P),5); twb=np.array([wetbulb(T[k],Td[k],P[k]) for k in ix])
    WBZ=safe(lambda: H[ix][np.where(twb<=0)[0][0]])
    def windex():
        HM=(FZL-Z[0])/1000; QL=np.mean(w[H<=1000])*1000; pm=np.interp(FZL,Z,P); QM=wsat(at(Td,pm),pm)*1000
        GAM=t0/HM; return 5*np.sqrt(max(HM*min(QL/12,1)*(GAM**2-30+QL-2*QM),0))
    WINDEX=safe(windex)

    issues=[x for x in (QC_NOTE[0], sfc_note) if x]
    if not (2<=PW<=90): issues.append(f"PW {PW:.1f} mm")
    if SB>7000 or MU>7000: issues.append(f"CAPE {max(SB,MU):.0f} J/kg")
    if not (-40<=KI<=60): issues.append(f"K Index {KI:.0f}")
    if P.min()>300: issues.append(f"ascent ends at {P.min():.0f} hPa")
    if np.any((np.abs(np.diff(T))>8)&(np.abs(np.diff(P))<20)): issues.append("temperature jump >8 °C within 20 hPa")
    fig=plt.figure(figsize=(10.5,19.5),facecolor='white')
    fig.text(0.03,0.985,'T-phi gram and Thermo-dynamic Indices Analysis',fontsize=21,weight='bold',va='top')
    fig.text(0.03,0.968,f'{station.title()} ({idx}) | {when}',fontsize=13,va='top')
    if issues: fig.text(0.97,0.968,'⚠ data check: '+'; '.join(issues),fontsize=9,color='#b00020',ha='right',va='top')
    ax=fig.add_axes([0.16,0.585,0.58,0.355]); C=35.
    X=lambda t,p: t+C*np.log(1000/p)
    ax.set_yscale('log'); ax.set_ylim(1000,100); ax.set_xlim(-30,50)
    pt=[1000,900,800,700,600,500,400,300,200,100]
    ax.set_yticks(pt); ax.set_yticklabels([str(x) for x in pt]); ax.minorticks_off(); ax.tick_params(labelsize=8)
    pg=np.geomspace(1050,90,100)
    for t in range(-120,60,10): ax.plot(X(t,pg),pg,color='0.75',lw=.6)
    for th_ in range(-40,200,10): ax.plot(X((th_+K0)*(pg/1000)**kap-K0,pg),pg,color='0.6',lw=.6,alpha=.6)
    mg=np.r_[1000,np.geomspace(990,100,80)]
    for t0_ in range(-10,36,5): ax.plot(X(moist(t0_,mg),mg),mg,color='0.6',lw=.5,ls='--',alpha=.5)
    for p in pt: ax.axhline(p,color='0.85',lw=.6)
    ax.axhline(p0,color='0.5',lw=.8,ls='--')
    k=P>=100
    ax.plot(X(T[k],P[k]),P[k],color='red',lw=1.2); ax.plot(X(Td[k],P[k]),P[k],color='green',lw=1.2)
    kp=pp_sb>=100; tpar=tp_sb[kp]; ppar=pp_sb[kp]; ax.plot(X(tpar,ppar),ppar,color='k',lw=1.4)
    te_=at(T,ppar)
    ax.fill_betweenx(ppar,X(te_,ppar),X(tpar,ppar),where=(tpar>te_)&(ppar<=LFC)&(ppar>=EL),color='red',alpha=.3,lw=0)
    ax.fill_betweenx(ppar,X(te_,ppar),X(tpar,ppar),where=(tpar<te_)&(ppar>=LFC),color='blue',alpha=.15,lw=0)
    ax.set_xlabel('degree_Celsius',fontsize=8); ax.set_ylabel('hPa',fontsize=8)
    f0=lambda x,fmt: (fmt.format(x) if np.isfinite(x) else 'n/a')
    box=(f"CAPE: {SB:.0f} J/kg\nCIN: {CIN:.0f} J/kg\nLI: {f0(LI,'{:.1f}')}\nKI: {KI:.1f}\nTTI: {TT:.1f}\n"
         f"SWEAT: {SWEAT:.0f}\nSRH: {SRH3:.0f} m²/s²\nSCP: {SCP:.1f}\nSTP: {STP:.2f}\nWBZ: {f0(WBZ,'{:.0f}')} m")
    ax.text(0.015,0.985,box,transform=ax.transAxes,fontsize=7,va='top',bbox=dict(fc='white',ec='0.5',lw=.6))
    bp=np.r_[np.arange(950,100,-50),100]; bp=bp[bp<=p0]
    ax.barbs(np.full(len(bp),50),bp,at(u,bp)*kt,at(v,bp)*kt,length=6,lw=.7,clip_on=False)
    hx=fig.add_axes([0.505,0.84,0.18,0.10]); hk=H<=25000
    for rr in (10,20,30,40,50,60): hx.add_patch(plt.Circle((0,0),rr,fill=False,ls='--',color='0.6',lw=.5))
    hx.axhline(0,color='0.6',lw=.5,ls='--'); hx.axvline(0,color='0.6',lw=.5,ls='--')
    pts=np.c_[u[hk],v[hk]].reshape(-1,1,2); seg=np.concatenate([pts[:-1],pts[1:]],axis=1)
    lc=LineCollection(seg,cmap='viridis',norm=plt.Normalize(0,25)); lc.set_array(H[hk][:-1]/1000); lc.set_linewidth(1.3)
    hx.add_collection(lc); hx.set_xlim(-60,60); hx.set_ylim(-60,60); hx.set_aspect('equal'); hx.tick_params(labelsize=6)
    hx.set_xticks([-60,-30,0,30,60]); hx.set_yticks([-60,-30,0,30,60]); hx.set_title('Hodograph (m/s)',fontsize=7,pad=2)
    cx=fig.add_axes([0.83,0.84,0.012,0.09]); cb=fig.colorbar(lc,cax=cx); cb.set_label('Height (km)',fontsize=7); cb.ax.tick_params(labelsize=6)
    fig.add_artist(plt.Line2D([0.03,0.97],[0.548,0.548],color='k',lw=1.2,transform=fig.transFigure))
    fig.text(0.03,0.54,'Thermo-dynamic Indices',fontsize=16,weight='bold',va='top')
    cards=[("Dry Mid-Levels RH (%)",RHmid),("Low-Levels RH (%)",RHlow),("Freezing Level (m)",FZL),
    ("Total Precipitable Water (mm)",PW),("Surface-Based Convective\nAvailable Potential Energy",SB),("Mixed-Layer Convective\nAvailable Potential Energy",ML),
    ("Most-Unstable Convective\nAvailable Potential Energy",MU),("Convective Inhibition (J/kg)",CIN),("Lifted Condensation Level\nHeight AGL (m)",LCLH),
    ("Downdraft Convective Available\nPotential Energy (J/kg)",DCAPE),("Maximum Downdraft Velocity\nPotential (m/s)",VMAX),("Lifted Index (°C)",LI),
    ("Showalter Index (°C)",SI),("Total Totals Index",TT),("K Index",KI),
    ("ThetaE Deficit (K)",TEDEF),("Microburst Windspeed Potential\nIndex",MWPI),("Dry Microburst Index",DMI),
    ("Wind Index, WINDEX (kt)",WINDEX),("Wet-Bulb Zero Height AGL (m)",WBZ),("SWEAT Index",SWEAT),
    ("Level of Free Convection (hPa)",LFC),("0 to 6 km Environmental Lapse\nRate (°C/km)",LR06),("700 to 500 hPa Lapse Rate\n(°C/km)",LR75),
    ("Inversion Base Height AGL (m)",invb),("Inversion Top Height AGL (m)",invt),("Inversion Strength (°C)",invs),
    ("Maximum Inversion Gradient\n(°C/km)",GMAX),("Boundary Layer Height (m)",BLH+Z[0]),("Boundary Layer Height AGL (m)",BLH),
    ("Mean Boundary Layer Wind Speed\n(m/s)",BLW),("Ventilation Coefficient (m²/s)",VC),("Boundary Layer Mixing Ratio\n(g/kg)",BLQ),
    ("Bulk Shear (0 to 6 km, m/s)",SH06),("Bulk Richardson Number",BRN),("Vorticity Generation Parameter",VGP),
    ("Storm-Relative Helicity\n0–3 km (m²/s²)",SRH3),("Significant Hail Parameter",SHIP),("Equilibrium Level (hPa)",EL)]
    cw,ch,top=0.30,0.0335,0.522
    for n,(lab,val) in enumerate(cards):
        r,c=divmod(n,3); x=0.03+c*(cw+0.02); y=top-r*(ch+0.0055)-ch
        fig.add_artist(FancyBboxPatch((x,y),cw,ch,boxstyle="round,pad=0,rounding_size=0.006",transform=fig.transFigure,fc='white',ec='0.3',lw=.8))
        fig.text(x+0.01,y+ch-0.004,lab,fontsize=8.5,va='top',linespacing=1.1)
        fig.text(x+0.01,y+0.004,(f"{float(val):.2f}" if np.isfinite(val) else "n/a"),fontsize=11.5,weight='bold',va='bottom')
    fig.text(0.03,0.012,f'Source: Computed from IMD RS/RW ascent, {station.title()} ({idx}), {when}',fontsize=9)
    fig.savefig(out,dpi=130); plt.close(fig)
    return dict(SBCAPE=SB,MUCAPE=MU,CIN=CIN,LI=LI,KI=KI,PW=PW,suspect='; '.join(issues))

# ------------------------------------------------------------------ getting the zips
def links_from_html(text, date, hour, base=BASE):
    """rsrw_download links on the list page for this date/hour -> {index: url of best file}."""
    found={}
    for href in re.findall(r'href\s*=\s*["\']([^"\']*rsrw_download\.php[^"\']*)["\']', text, re.I):
        url=urljoin(base, html.unescape(href)); q=parse_qs(urlparse(url).query)
        get=lambda k: (q.get(k) or [''])[0]
        if get('adt2')!=date or get('adt3').zfill(2)!=hour: continue
        m=re.search(r'_(rsr|int|sum|other)\.(txt|zip)$', get('filnm'), re.I)
        if m: found.setdefault(get('adt1'),{})[m.group(1).lower()]=url
    for row in re.split(r'<tr\b',text,flags=re.I)[1:]:                  # station names from the table rows
        m=re.search(r'adt1=(\d{5})',row)
        if not m or m[1] not in found: continue
        cells=[re.sub(r'\s+',' ',re.sub(r'<[^>]*>?',' ',c)).strip() for c in re.split(r'</t[dh]\s*>',row,flags=re.I)]
        nm=next((n for n in (clean_name(c) for c in cells) if n),None)
        if nm: learn(m[1],nm,authoritative=True)
    return {i:next(d[k] for k in PREFER if k in d) for i,d in found.items() if any(k in d for k in PREFER)}

def as_zip(name, data):
    """Wrap a plain text file as a one-member zip so every source goes through sounding_from_zip."""
    if data[:2]==b'PK': return data
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z: z.writestr(name, data)
    return buf.getvalue()

def get_page(s, url, method='get', data=None, depth=0):
    """Fetch a page; if it is just a frameset / iframe / meta-refresh wrapper, follow it to the real page."""
    r=(s.post(url,data=data,timeout=60) if method=='post' else s.get(url,timeout=60)); r.raise_for_status()
    if depth<3 and not re.search(r'<input\b|rsrw_download',r.text,re.I):
        m=(re.search(r'<i?frame\b[^>]*\bsrc\s*=\s*["\']?([^"\'\s>]+)',r.text,re.I) or
           re.search(r'http-equiv\s*=\s*["\']?refresh[^>]*url\s*=\s*([^"\'>\s]+)',r.text,re.I))
        if m: return get_page(s, urljoin(r.url, html.unescape(m.group(1))), depth=depth+1)
    return r

def make_session(cfg, cookie=None):
    """requests session logged in to CLIMS: browser cookie, or username/password from rsrw_config.json."""
    import requests
    s=requests.Session(); s.headers['User-Agent']='Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    cookie=cookie or cfg.get('cookie')
    if cookie:
        for part in cookie.split(';'):
            if '=' in part: k,v=part.strip().split('=',1); s.cookies.set(k,v,domain='clims.imd.gov.in')
        return s
    user,pw=cfg.get('username'),cfg.get('password')
    if not (user and pw): return s
    login_url=cfg.get('login_url') or BASE
    r=get_page(s,login_url)
    # forms may be unclosed: take each <form ...> up to the next <form or end of page
    parts=re.split(r'(?=<form\b)',r.text,flags=re.I)
    forms=[(m.group(1),m.group(2)) for p_ in parts for m in [re.match(r'<form\b([^>]*)>(.*)',p_,re.I|re.S)] if m]
    form=next(((fa,fb) for fa,fb in forms if re.search(r'type\s*=\s*["\']?password',fb,re.I)),None)
    if not form and re.search(r'type\s*=\s*["\']?password',r.text,re.I): form=('',r.text)   # password box outside a <form>
    if not form:
        open('login_page_debug.html','w',encoding='utf-8').write(f"<!-- fetched from {r.url} status {r.status_code} -->\n"+r.text)
        raise RuntimeError(f"no login box found at {r.url}. The page received was saved as login_page_debug.html - "
                           "send that file to Claude, or use a browser cookie instead (see rsrw_config.json)")
    fa,fb=form
    action=re.search(r'action\s*=\s*["\']([^"\']*)',fa,re.I); action=urljoin(r.url,html.unescape(action.group(1)) if action else r.url)
    fields={}; userf=pwf=None
    for inp in re.findall(r'<input\b[^>]*>',fb,re.I):
        nm=re.search(r'name\s*=\s*["\']([^"\']+)',inp,re.I)
        if not nm: continue
        nm=nm.group(1); typ=(re.search(r'type\s*=\s*["\']?(\w+)',inp,re.I) or [None,'text'])[1].lower()
        val=re.search(r'value\s*=\s*["\']([^"\']*)',inp,re.I); val=html.unescape(val.group(1)) if val else ''
        if typ=='password': pwf=nm
        elif typ in ('text','email') and userf is None: userf=nm; continue
        elif typ in ('checkbox','radio') and 'checked' not in inp.lower(): continue
        fields[nm]=val
    # username chosen from a dropdown (<select>): match the option whose text or value equals the username
    norm=lambda x: re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]+>','',x))).strip().lower()
    for sname,sbody in re.findall(r'<select\b[^>]*name\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</select>',fb,re.I|re.S):
        opts=re.findall(r'<option\b([^>]*)>(.*?)(?=<option\b|</select>|$)',sbody+'</select>',re.I|re.S)
        hit=None
        for oa,ot in opts:
            v=re.search(r'value\s*=\s*["\']([^"\']*)',oa,re.I); v=html.unescape(v.group(1)) if v else norm(ot)
            if norm(ot)==norm(user) or v.strip().lower()==norm(user): hit=v; break
        if hit is not None: fields[sname]=hit; userf=None if userf in fields else userf; selected_user=sname; break
        sel=next((re.search(r'value\s*=\s*["\']([^"\']*)',oa,re.I) for oa,_ in opts if 'selected' in oa.lower()),None)
        fields.setdefault(sname, sel.group(1) if sel else '')
    else:
        selected_user=None
    if selected_user is None:
        if userf is None and not cfg.get('username_field'):
            names=[norm(t) for _,b in re.findall(r'<select\b[^>]*name\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</select>',fb,re.I|re.S)
                   for _,t in re.findall(r'<option\b([^>]*)>(.*?)(?=<option\b|$)',b,re.I|re.S)]
            raise RuntimeError(f"username '{user}' not found in the login dropdown. Options seen: {names[:60]}")
        fields[cfg.get('username_field',userf)]=user
    fields[cfg.get('password_field',pwf)]=pw
    for k,v in (cfg.get('extra_login_fields') or {}).items(): fields[k]=v
    r=get_page(s,action,'post',fields)
    if re.search(r'type\s*=\s*["\']?password',r.text,re.I) and 'logout' not in r.text.lower():
        open('login_result_debug.html','w',encoding='utf-8').write(f"<!-- {r.url} -->\n"+r.text)
        raise RuntimeError("login failed (still on the login page) - check username/password in rsrw_config.json "
                           "(page saved as login_result_debug.html)")
    s.home=(r.text,r.url)            # first page after login: used to find the RS/RW table and its date box
    return s

def fill_date(text, d, hour):
    """Put the run date into a URL/field: {date} {dmy} {mdy} {hour} placeholders, or any date already written in it
    (2026-10-19, {2026-10-19}, 19-10-2026, 19/10/2026, 10/19/2026, 2026%2F10%2F19 ...) is swapped for this run's date."""
    rep={'date':d.isoformat(),'dmy':d.strftime('%d-%m-%Y'),'mdy':d.strftime('%m/%d/%Y'),'hour':hour}
    t=re.sub(r'\{(date|dmy|mdy|hour)\}',lambda m:rep[m[1]],text)
    t=re.sub(r'\{([^{}]*\d[^{}]*)\}',r'\1',t)                                     # {2026-10-19} -> 2026-10-19
    t=re.sub(r'(?<!\d)(\d{4})([-/]|%2F)(\d{1,2})\2(\d{1,2})(?!\d)',lambda m:f"{d.year}{m[2]}{d.month:02d}{m[2]}{d.day:02d}",t)
    t=re.sub(r'(?<!\d)(\d{1,2})([-/.]|%2F)(\d{1,2})\2(\d{4})(?!\d)',
             lambda m:(f"{d.month:02d}{m[2]}{d.day:02d}{m[2]}{d.year}" if int(m[1])<=12 and int(m[3])>12 else f"{d.day:02d}{m[2]}{d.month:02d}{m[2]}{d.year}"),t)
    return t

def _form_fields(fb):
    """Default values of a form's inputs/selects (hidden values, checked boxes, selected options)."""
    f={}
    for inp in re.findall(r'<input\b[^>]*>',fb,re.I):
        nm=re.search(r'name\s*=\s*["\']?([^"\'\s>]+)',inp,re.I)
        if not nm: continue
        typ=(re.search(r'type\s*=\s*["\']?(\w+)',inp,re.I) or [None,'text'])[1].lower()
        if typ in ('checkbox','radio') and 'checked' not in inp.lower(): continue
        if typ in ('button','image','reset','file'): continue
        val=re.search(r'value\s*=\s*["\']([^"\']*)',inp,re.I); f[nm.group(1)]=html.unescape(val.group(1)) if val else ''
    for nm,body in re.findall(r'<select\b[^>]*name\s*=\s*["\']?([^"\'\s>]+)[^>]*>(.*?)</select>',fb,re.I|re.S):
        sel=re.search(r'<option\b[^>]*selected[^>]*value\s*=\s*["\']([^"\']*)|<option\b[^>]*value\s*=\s*["\']([^"\']*)["\'][^>]*selected',body,re.I)
        first=re.search(r'<option\b[^>]*value\s*=\s*["\']([^"\']*)',body,re.I)
        f[nm]=(sel.group(1) or sel.group(2)) if sel else (first.group(1) if first else '')
    return f

def _date_box(fb):
    """Name and type of the date input in a form (type=date, or a name/id that looks like a date)."""
    for inp in re.findall(r'<input\b[^>]*>',fb,re.I):
        nm=re.search(r'name\s*=\s*["\']?([^"\'\s>]+)',inp,re.I)
        if not nm: continue
        typ=(re.search(r'type\s*=\s*["\']?(\w+)',inp,re.I) or [None,'text'])[1].lower()
        if typ=='date' or re.search(r'date|dt|adt|day',nm.group(1)+(re.search(r'id\s*=\s*["\']?([^"\'\s>]+)',inp,re.I) or [None,''])[1],re.I) and typ in ('text','date','hidden'):
            val=re.search(r'value\s*=\s*["\']([^"\']*)',inp,re.I)
            return nm.group(1), typ, (val.group(1) if val else '')
    return None

def _fmt_like(example, d):
    """Write date d in the same style as an example value (2026-10-19, 19-10-2026, 19/10/2026, 10/19/2026)."""
    if re.fullmatch(r'\d{1,2}[-/.]\d{1,2}[-/.]\d{4}',example or ''): return fill_date(example,d,'00')
    return d.isoformat()

def _pages_to_search(s, start_text, start_url):
    """The page after login plus every same-site page it frames or links to whose name mentions rsrw/data/download."""
    seen={start_url}; out=[(start_text,start_url)]
    for m in re.finditer(r'(?:<i?frame\b[^>]*\bsrc|<a\b[^>]*\bhref)\s*=\s*["\']?([^"\'\s>#]+)',start_text,re.I):
        u=urljoin(start_url,html.unescape(m.group(1)))
        if u in seen or not u.startswith(BASE) or re.search(r'logout|rsrw_download|\.(zip|txt|pdf|png|jpg)$',u,re.I): continue
        if not re.search(r'frame',m.group(0),re.I) and not re.search(r'rsrw|data|download|all|station',u,re.I): continue
        seen.add(u)
        try: r=get_page(s,u); out.append((r.text,r.url))
        except Exception: pass
        if len(out)>12: break
    return out

def fetch_list_page(s, cfg, date, hour, args=None):
    if args and args.list_html: return open(args.list_html,encoding='utf-8',errors='ignore').read(), BASE
    d=dt.date.fromisoformat(date)
    url=((args and args.list_url) or cfg.get('list_url') or '').strip()
    if url and '{' not in url and 'PASTE' not in url.upper() and re.search(r'\d{4}[-/]\d|\d[-/]\d{4}|date',url,re.I):
        r=get_page(s,fill_date(url,d,hour))                       # explicit URL that carries the date
        if r.status_code==200 and 'rsrw_download' in r.text: return r.text, r.url
    url=fill_date(url,d,hour) if url and 'PASTE' not in url.upper() else ''
    if url and '{' not in url:
        try:
            r=get_page(s,url)
            if 'rsrw_download' in r.text and links_from_html(r.text,date,hour,r.url): return r.text, r.url
        except Exception: pass
    # automatic: find the page with the RS/RW table / date box, fill in the date, submit the form
    home=getattr(s,'home',None) or (lambda r:(r.text,r.url))(get_page(s,cfg.get('login_url') or BASE+'dp-rsrw/'))
    tried=[]
    for text,purl in _pages_to_search(s,*home):
        if links_from_html(text,date,hour,purl): return text,purl
        parts=re.split(r'(?=<form\b)',text,flags=re.I)
        for p_ in parts:
            m=re.match(r'<form\b([^>]*)>(.*)',p_,re.I|re.S)
            if not m: continue
            fa,fb=m.groups(); box=_date_box(fb)
            if not box: continue
            name,typ,example=box
            fields=_form_fields(fb); fields[name]=_fmt_like(example,d) if typ!='date' else d.isoformat()
            for k in list(fields):                                  # hour selector, if the form has one
                if re.search(r'hour|time|hh|utc',k,re.I): fields[k]=hour
            act=re.search(r'action\s*=\s*["\']?([^"\'\s>]*)',fa,re.I); act=urljoin(purl,html.unescape(act.group(1))) if act and act.group(1) else purl
            meth='post' if re.search(r'method\s*=\s*["\']?post',fa,re.I) else 'get'
            try:
                if meth=='post': r=get_page(s,act,'post',fields)
                else:
                    from urllib.parse import urlencode
                    r=get_page(s,act+('&' if '?' in act else '?')+urlencode(fields))
                tried.append(f"{meth.upper()} {act} {fields}")
                if 'rsrw_download' in r.text: return r.text, r.url
            except Exception as e: tried.append(f"{meth.upper()} {act}: {e}")
    with open('list_page_debug.html','w',encoding='utf-8') as f:
        f.write(f"<!-- tried: {tried} -->\n<!-- after login: {home[1]} -->\n"+home[0])
    return home[0], home[1]

def online_zips(s, cfg, date, hour, rawdir, skip=(), args=None):
    """Download every station's best file for date/hour (saved in rawdir) -> yields (index, zipbytes, error)."""
    page,purl=fetch_list_page(s,cfg,date,hour,args)
    links=links_from_html(page,date,hour,purl)
    if not links:
        if 'rsrw_download' not in page: print(f"  {date} {hour}Z: could not find the RS/RW table - page saved as list_page_debug.html, send it to Claude")
        else: print(f"  {date} {hour}Z: no files listed yet")
        return
    todo={k:v for k,v in links.items() if k not in skip}
    print(f"  {date} {hour}Z: {len(links)} stations listed, {len(todo)} new")
    os.makedirs(rawdir,exist_ok=True)
    def fetch(item):
        idx,url=item
        try:
            name=parse_qs(urlparse(url).query).get('filnm',[f'{idx}.dat'])[0]
            for attempt in range(3):
                try: r=s.get(url,timeout=180); r.raise_for_status(); break
                except Exception:
                    if attempt==2: raise
                    time.sleep(5)
            data=r.content
            if data.lstrip()[:15].lower().startswith((b'<!doctype',b'<html')): raise ValueError("got a web page, not data (session expired?)")
            open(os.path.join(rawdir,name),'wb').write(data)
            return idx, as_zip(name, data), None
        except Exception as e: return idx, None, str(e)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:                 # 4 stations downloading at once
        for res in ex.map(fetch, sorted(todo.items())):
            yield res

def identify(data):
    """(station index, 'YYYY_MM_DD_HH' or None) read from inside a downloaded zip / txt, whatever its file name."""
    idx=tag=None; texts=[]
    if data[:2]==b'PK':
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                m=re.search(r'(\d{4})(\d\d)(\d\d)-(\d\d)-',n)
                if m and not tag: tag=f"{m[1]}_{m[2]}_{m[3]}_{m[4]}"
                m=re.search(r'-(\d{5})-RSRW',n,re.I)
                if m and not idx: idx=m[1]
                if not idx and re.search(r'standard[ _-]*summary',n,re.I): texts.append(z.read(n)[:600].decode('utf-8','ignore'))
    else: texts.append(data[:2000].decode('utf-8','ignore'))
    for t in texts:
        m=re.search(r'(?:Station\s*(?:ID|Number)|Index)\s*:\s*(\d{5})',t,re.I)
        if m and not idx: idx=m[1]
    return idx, tag

def local_zips(args):
    """Folder mode: every .zip/.txt in the folder (names like 'rsrw_download (3).zip' are fine).
    Station and date/hour are read from inside each file; per station the best type wins (sum > zip > rsr)."""
    want=args.date.replace('-','_')+'_'+args.hour; best={}
    for fn in sorted(os.listdir(args.zips)):
        if not fn.lower().endswith(('.zip','.txt')): continue
        data=open(os.path.join(args.zips,fn),'rb').read()
        try: idx,tag=identify(data)
        except Exception as e: print(f"  ? {fn}: unreadable ({e})"); continue
        m=re.match(r'(\d{5})_(\d{4}_\d\d_\d\d_\d\d)',fn)          # original IMD names still work
        if m: idx=idx or m[1]; tag=tag or m[2]
        if not idx: print(f"  ? {fn}: station number not found inside, skipped"); continue
        if tag and tag!=want: continue                            # a different date/hour
        kind='other' if data[:2]==b'PK' else ('rsr' if b'[PTU summary]' in data[:5000] or b'Radiosounding' in data[:300] else 'sum')
        r=PREFER.index(kind)
        if idx not in best or r<best[idx][0]: best[idx]=(r,fn,data)
    if not best: print(f"No files for {args.date} {args.hour} UTC found in {args.zips}")
    for k,(_,fn,data) in sorted(best.items()):
        yield k, as_zip(fn, data), None

# ------------------------------------------------------------------ gallery page
def write_gallery(folder, items, when):
    cards="\n".join(f'<a class="c" href="{fn}" target="_blank"><img loading="lazy" src="{fn}" alt="{nm}"><span>{nm} · {ix}</span></a>'
                    for ix,nm,fn in items)
    open(os.path.join(folder,'index.html'),'w',encoding='utf-8').write(f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>RS/RW T-phi grams {when}</title>
<style>body{{font:14px system-ui,sans-serif;margin:0;padding:20px 16px;background:#f6f8fa;color:#14202b}}
h1{{font-size:20px;margin:0 0 14px}}.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}
.c{{background:#fff;border:1px solid #d5dde5;border-radius:8px;padding:8px;text-decoration:none;color:inherit}}
.c img{{width:100%;height:260px;object-fit:cover;object-position:top;border-radius:4px}}.c span{{display:block;margin-top:6px;font-weight:600}}</style>
<h1>RS/RW T-phi grams · {when} · {len(items)} stations</h1><div class="g">{cards}</div>""")

# ------------------------------------------------------------------ archive page (all dates)
ARCHIVE_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>IMD RS/RW T-phi gram Archive</title>
<style>
:root{--bg:#f4f6f8;--card:#fff;--fg:#14202b;--mut:#5a6b7a;--line:#d5dde5;--acc:#1f5fa8;--accfg:#fff;--chip:#e8eef4}
@media (prefers-color-scheme:dark){:root{--bg:#0f151b;--card:#18212a;--fg:#e4ebf1;--mut:#93a4b3;--line:#2b3946;--acc:#7fb1ea;--accfg:#0f151b;--chip:#1f2b36}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px "Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center}
h1{font-size:18px;margin:0 12px 0 0}select,input,button{font:inherit;color:var(--fg);background:var(--chip);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
button{cursor:pointer}button[aria-pressed=true]{background:var(--acc);color:var(--accfg);border-color:var(--acc)}button:disabled{opacity:.35;cursor:default}
.seg{display:flex;gap:4px}.mut{color:var(--mut);font-size:12px}main{padding:16px;max-width:1500px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.c{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;cursor:pointer;text-align:left;color:inherit}
.c img{width:100%;height:250px;object-fit:cover;object-position:top;border-radius:4px;background:#fff;display:block}
.c b{display:block;margin-top:6px}.c span{font-size:12px;color:var(--mut)}
#viewer{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;z-index:10;overflow:auto;padding:12px}
#viewer.on{display:block}#viewer .bar{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-bottom:10px;position:sticky;top:0}
#viewer img{display:block;margin:0 auto;max-width:min(1000px,100%);background:#fff;border-radius:4px}
.empty{padding:40px;text-align:center;color:var(--mut)}
.tabs{display:flex;gap:4px;flex-wrap:wrap}.tabs button{font-weight:600}
.nwpbar{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:0 0 14px}
.nwpgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.nwpgrid .c img{height:220px;object-fit:contain;object-position:center}
.files{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 0}.files a{color:var(--acc);text-decoration:none;border:1px solid var(--line);border-radius:6px;padding:6px 10px;background:var(--card)}
.note{margin:0 0 12px;padding:8px 10px;border-left:3px solid var(--acc);background:var(--card);border-radius:4px;font-size:13px}
.cmpwrap{overflow-x:auto;margin:0 0 16px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
.cmp{border-collapse:collapse;font-size:12.5px;min-width:100%}.cmp th,.cmp td{border:1px solid var(--line);padding:6px 8px;text-align:center;vertical-align:middle}
.cmp th{background:var(--acc);color:var(--accfg);white-space:nowrap}.cmp td:first-child{text-align:left;font-weight:700;white-space:nowrap;background:var(--chip)}
.cmp td.sel{background:rgba(255,200,0,.18);font-weight:700}.cmpt{font-weight:700;margin:0 0 6px}
#nv{position:fixed;inset:0;background:rgba(0,0,0,.88);display:none;z-index:20;overflow:auto;padding:12px}
#nv.on{display:block}#nv .bar{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-bottom:10px;position:sticky;top:0}
#nv img{display:block;margin:0 auto;max-width:min(1400px,100%);background:#fff;border-radius:4px}
#nv .cap{color:#fff;text-align:center;margin:0 0 8px;font-size:13px}
.xlink{font-weight:700;font-size:16px;color:var(--acc);text-decoration:none;border:2px solid var(--acc);border-radius:6px;padding:5px 12px}.xlink:hover{background:var(--acc);color:var(--accfg)}
</style></head><body>
<header><h1 id="ttl">IMD RS/RW T-phi grams</h1>
<nav class="tabs"><button id="t_tphi">T-phi grams</button><button id="t_ens">ECMWF ENS</button><button id="t_mme">IMD MME</button><button id="t_adeck">A-Deck</button></nav>
<a class="xlink" href="https://krishhindustan-creator.github.io/cyclone-nwp/#mme">Cyclone NWP ➡</a>
<span id="tphictl" style="display:contents">
<label>View <select id="mode"><option value="day">All stations, one ascent</option><option value="stn">One station, all dates</option></select></label>
<span id="dayctl"><select id="date"></select> <span class="seg" style="display:inline-flex"><button id="h00">00 UTC</button><button id="h12">12 UTC</button></span></span>
<span id="stnctl" hidden><select id="stn"></select></span>
<input id="q" placeholder="Filter station…" size="14"><span class="mut" id="info"></span></span></header>
<main id="tphi"><div class="grid" id="grid"></div></main>
<main id="nwp" hidden><div class="nwpbar"><b id="nwptitle"></b><select id="nwprun"></select><span class="mut" id="nwpinfo"></span></div>
<div id="nwpnote"></div><div id="nwpcmp"></div><div class="nwpgrid" id="nwpgrid"></div><div class="files" id="nwpfiles"></div></main>
<div id="nv"><div class="bar"><button id="nvprev">◀ Prev</button><button id="nvclose">Close</button><button id="nvnext">Next ▶</button></div><div class="cap" id="nvcap"></div><img id="nvimg" alt=""></div>
<div id="viewer"><div class="bar"><button id="prev">◀ Prev</button><button id="close">Close</button><button id="next">Next ▶</button></div><img id="big" alt=""></div>
<script src="archive.js"></script>
<script>
const A=window.ARCHIVE||{}, keys=Object.keys(A).sort().reverse(), dates=[...new Set(keys.map(k=>k.slice(0,8)))];
const M=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], fmt=d=>`${+d.slice(6)} ${M[+d.slice(4,6)-1]} ${d.slice(0,4)}`;
const $=id=>document.getElementById(id); let hour='00', list=[], cur=0;
const stations={}; keys.forEach(k=>A[k].forEach(([ix,nm])=>stations[ix]=nm));
$('date').innerHTML=dates.map(d=>`<option value="${d}">${fmt(d)}</option>`).join('');
$('stn').innerHTML=Object.keys(stations).sort((a,b)=>stations[a].localeCompare(stations[b])).map(i=>`<option value="${i}">${stations[i]} (${i})</option>`).join('');
function render(){
  const day=$('mode').value==='day', q=$('q').value.trim().toLowerCase();
  $('dayctl').hidden=!day; $('stnctl').hidden=day;
  if(day){const d=$('date').value; ['00','12'].forEach(h=>{const b=$('h'+h);b.disabled=!A[d+'_'+h];b.setAttribute('aria-pressed',h===hour)});
    if(!A[d+'_'+hour]) hour=A[d+'_00']?'00':'12';
    ['00','12'].forEach(h=>$('h'+h).setAttribute('aria-pressed',h===hour));
    list=(A[d+'_'+hour]||[]).map(([ix,nm,f])=>({ix,nm,f,sub:`${hour} UTC · ${fmt(d)}`}));}
  else{const s=$('stn').value; list=keys.filter(k=>A[k].some(r=>r[0]===s)).map(k=>{const r=A[k].find(r=>r[0]===s);return{ix:s,nm:r[1],f:r[2],sub:`${k.slice(9)} UTC · ${fmt(k.slice(0,8))}`}});}
  if(q) list=list.filter(o=>(o.nm+' '+o.ix).toLowerCase().includes(q));
  $('info').textContent=`${list.length} chart${list.length===1?'':'s'} · ${keys.length} ascents archived`;
  $('grid').innerHTML=list.length?list.map((o,i)=>`<button class="c" data-i="${i}"><img loading="lazy" src="${o.f}" alt="${o.nm}"><b>${o.nm}</b><span>${o.ix} · ${o.sub}</span></button>`).join(''):'<div class="empty">No charts yet.</div>';
  try{localStorage.setItem('rsrw',JSON.stringify({m:$('mode').value,d:$('date').value,h:hour,s:$('stn').value}))}catch(e){}
}
function show(i){cur=(i+list.length)%list.length;$('big').src=list[cur].f;$('viewer').classList.add('on')}
$('grid').onclick=e=>{const c=e.target.closest('.c');if(c)show(+c.dataset.i)};
$('prev').onclick=()=>show(cur-1);$('next').onclick=()=>show(cur+1);$('close').onclick=()=>$('viewer').classList.remove('on');
document.onkeydown=e=>{if(!$('viewer').classList.contains('on'))return;if(e.key==='ArrowLeft')show(cur-1);if(e.key==='ArrowRight')show(cur+1);if(e.key==='Escape')$('close').onclick()};
['mode','date','stn'].forEach(id=>$(id).onchange=render);$('q').oninput=render;
$('h00').onclick=()=>{hour='00';render()};$('h12').onclick=()=>{hour='12';render()};
try{const s=JSON.parse(localStorage.getItem('rsrw')||'{}');if(s.m)$('mode').value=s.m;if(s.s&&stations[s.s])$('stn').value=s.s}catch(e){}
if(keys.length){$('date').value=keys[0].slice(0,8);hour=keys[0].slice(9)}
render();
</script>
<script>
(function(){
  const BASE=window.NWP_BASE||'https://krishhindustan-creator.github.io/cyclone-nwp/', g=id=>document.getElementById(id);
  const NAMES={ens:'ECMWF Ensemble',mme:'IMD MME',adeck:'A-Deck (JTWC model tracks)'};
  let MAN=null, kind=null, items=[], cur=0;
  function load(){ if(MAN) return Promise.resolve(MAN);
    return fetch(BASE+'manifest.json?t='+Date.now()).then(r=>r.json()).then(m=>MAN=m)
      .catch(()=>{MAN={ens:[],mme:[],adeck:[]};return MAN}); }
  function draw(){
    const runs=(MAN&&MAN[kind])||[]; g('nwptitle').textContent=NAMES[kind];
    if(!runs.length){g('nwprun').innerHTML='';g('nwpgrid').innerHTML='<div class="empty">'+(kind==='adeck'?'No a-deck yet — it appears once JTWC has numbered the system.':'No runs yet — the first one appears after the next scheduled update.')+'</div>';
      g('nwpfiles').innerHTML='';g('nwpnote').innerHTML='';g('nwpinfo').textContent='';return}
    const sel=g('nwprun'), keep=sel.value&&runs.some(r=>r.ic===sel.value)?sel.value:runs[0].ic;
    sel.innerHTML=runs.map(r=>`<option value="${r.ic}">${r.label}</option>`).join(''); sel.value=keep;
    const run=runs.find(r=>r.ic===keep); items=run.items.map(o=>({...o,f:BASE+o.f}));
    g('nwpinfo').textContent=`${runs.length} run${runs.length===1?'':'s'} kept (3 days)`+(MAN.updated?` · updated ${MAN.updated}`:'');
    g('nwpnote').innerHTML=run.note?`<div class="note">${run.note}</div>`:'';
    g('nwpcmp').innerHTML=kind==='ens'?cmp(runs,keep):'';
    g('nwpgrid').innerHTML=items.map((o,i)=>`<button class="c" data-n="${i}"><img loading="lazy" src="${o.f}" alt="${o.t}"><b>${o.t}</b></button>`).join('');
    g('nwpfiles').innerHTML=(run.files||[]).map(o=>`<a href="${BASE+o.f}" download>⬇ ${o.t}</a>`).join('');
  }
  function stats(note){const o={};(note||'').split('<br>').forEach(l=>{const i=l.indexOf(': ');if(i>0)o[l.slice(0,i).trim()]=l.slice(i+2).trim()});return o}
  function cmp(runs,sel){ if(runs.length<2) return '';
    const cols=runs.slice().reverse(), S=cols.map(r=>stats(r.note)), keys=[];
    S.forEach(o=>Object.keys(o).forEach(k=>{if(!keys.includes(k))keys.push(k)}));
    if(!keys.length) return '';
    let h='<div class="cmpt">Comparison of the last '+cols.length+' runs (oldest → newest)</div><div class="cmpwrap"><table class="cmp"><tr><th></th>'+
      cols.map(r=>'<th>'+r.label.replace(/ 20\d\d$/,'')+'</th>').join('')+'</tr>';
    keys.forEach(k=>{h+='<tr><td>'+k+'</td>'+S.map((o,i)=>'<td'+(cols[i].ic===sel?' class="sel"':'')+'>'+(o[k]||'–')+'</td>').join('')+'</tr>'});
    h+='</table></div>'; if(MAN.ens_compare) h+='<div class="files" style="margin:-6px 0 16px"><a href="'+BASE+MAN.ens_compare+'?t='+Date.now()+'" download>⬇ Comparison table (PNG, for WhatsApp)</a></div>';
    return h }
  function show(i){cur=(i+items.length)%items.length;g('nvimg').src=items[cur].f;g('nvcap').textContent=items[cur].t;g('nv').classList.add('on')}
  g('nwpgrid').onclick=e=>{const c=e.target.closest('.c');if(c)show(+c.dataset.n)};
  g('nvprev').onclick=()=>show(cur-1);g('nvnext').onclick=()=>show(cur+1);g('nvclose').onclick=()=>g('nv').classList.remove('on');
  document.addEventListener('keydown',e=>{if(!g('nv').classList.contains('on'))return;
    if(e.key==='ArrowLeft')show(cur-1);if(e.key==='ArrowRight')show(cur+1);if(e.key==='Escape')g('nvclose').onclick()});
  g('nwprun').onchange=draw;
  window.NWP_SHOW=function(k){kind=k;load().then(draw)};
})();
(function(){
  const g=id=>document.getElementById(id), T={tphi:'IMD RS/RW T-phi grams',ens:'ECMWF Ensemble',mme:'IMD MME',adeck:'A-Deck (JTWC model tracks)'};
  function tab(t){ if(!T[t]) t='tphi';
    ['tphi','ens','mme','adeck'].forEach(k=>g('t_'+k).setAttribute('aria-pressed',k===t));
    g('tphi').hidden=t!=='tphi'; g('tphictl').style.display=t==='tphi'?'contents':'none'; g('nwp').hidden=t==='tphi';
    g('ttl').textContent=T[t]; document.title=t==='tphi'?'IMD RS/RW T-phi gram Archive':T[t];
    if(t!=='tphi') window.NWP_SHOW(t);
    if(location.hash.slice(1)!==t) history.replaceState(null,'','#'+t);
    try{localStorage.setItem('rsrwtab',t)}catch(e){} }
  ['tphi','ens','mme','adeck'].forEach(k=>g('t_'+k).onclick=()=>tab(k));
  window.addEventListener('hashchange',()=>tab(location.hash.slice(1)));
  let t0=location.hash.slice(1); if(!t0){try{t0=localStorage.getItem('rsrwtab')||'tphi'}catch(e){t0='tphi'}}
  tab(t0);
})();
</script></body></html>"""

def write_archive(out):
    """Rebuild <out>/index.html + archive.js from every <YYYYMMDD>_<HH>Z folder."""
    arch={}
    for d in sorted(os.listdir(out)):
        m=re.fullmatch(r'(\d{8})_(\d\d)Z',d)
        if not m or not os.path.isdir(os.path.join(out,d)): continue
        rows=[]
        for fn in sorted(os.listdir(os.path.join(out,d))):
            mm=re.match(r'(\d{5})_(.+)\.png$',fn)
            if mm: rows.append([mm[1],STATIONS.get(mm[1],mm[2].replace('_',' ')),f"{d}/{fn}"])
        if rows: arch[f"{m[1]}_{m[2]}"]=sorted(rows,key=lambda r:r[1])
    open(os.path.join(out,'archive.js'),'w',encoding='utf-8').write("window.ARCHIVE="+json.dumps(arch,separators=(',',':'))+";")
    open(os.path.join(out,'index.html'),'w',encoding='utf-8').write(ARCHIVE_HTML)
    return len(arch)

def process(src, date, hour, out):
    d=dt.date.fromisoformat(date); when=f"{hour} UTC, {d.day} {d.strftime('%B %Y')}"
    folder=os.path.join(out,f"{d:%Y%m%d}_{hour}Z"); os.makedirs(folder,exist_ok=True)
    done,failed=[],[]
    for idx,data,err in src:
        nm=STATIONS.get(idx,f"STATION {idx}")
        if err: failed.append((idx,nm,err)); print(f"  ✗ {idx} {nm}: {err}"); continue
        try:
            fi,fname=name_from_file(data)
            if fi==idx: learn(idx,fname)
            nm=STATIONS.get(idx,nm)
            S,member=sounding_from_zip(data)
            fn=f"{idx}_{re.sub(r'[^A-Z0-9]+','_',nm.upper()).strip('_')}.png"
            r=make_chart(S,nm,idx,when,os.path.join(folder,fn))
            done.append((idx,nm,fn))
            line=f"{idx},{nm},"+",".join(f"{r[k]:.1f}" for k in ('SBCAPE','MUCAPE','CIN','LI','KI','PW'))
            csv=os.path.join(folder,'summary.csv'); new=not os.path.exists(csv)
            old=[] if new else [l for l in open(csv).read().splitlines()[1:] if not l.startswith(idx+',')]
            open(csv,'w').write("index,station,SBCAPE,MUCAPE,CIN,LI,KI,PW\n"+"\n".join(sorted(old+[line]))+"\n")
            print(f"  {'⚠' if r.get('suspect') else '✓'} {idx} {nm:<20} SBCAPE {r['SBCAPE']:6.0f}  MUCAPE {r['MUCAPE']:6.0f}  KI {r['KI']:5.1f}  PW {r['PW']:5.1f}"
                  + (f"   <- check data: {r['suspect']}" if r.get('suspect') else ''))
        except Exception as e:
            failed.append((idx,nm,str(e))); print(f"  ✗ {idx} {nm}: {e}")
            if os.environ.get('RSRW_DEBUG'): traceback.print_exc()
    allpng=[(m[1],STATIONS.get(m[1],m[2]),f) for f in sorted(os.listdir(folder)) for m in [re.match(r'(\d{5})_(.+)\.png$',f)] if m]
    write_gallery(folder,allpng,when)
    return done,failed,folder

STATE_FILE='rsrw_state.json'
def catchup_hours():
    """36 h normally; longer if the last successful run was earlier (PC off for a while), max 10 days."""
    try:
        last=dt.datetime.fromisoformat(json.load(open(STATE_FILE))['last_ok'])
        gap=(dt.datetime.now(dt.timezone.utc)-last).total_seconds()/3600
        return int(min(max(36,gap+24),240))
    except Exception: return 36

def recent_slots(n_hours=36):
    """Synoptic ascents (date, hour) from the last n_hours whose launch time has passed, newest first."""
    now=dt.datetime.now(dt.timezone.utc); t=now.replace(minute=0,second=0,microsecond=0)
    t=t.replace(hour=12 if t.hour>=12 else 0); out=[]
    while now-t<=dt.timedelta(hours=n_hours):
        if now-t>=dt.timedelta(minutes=30): out.append((t.date().isoformat(),f"{t.hour:02d}"))
        t-=dt.timedelta(hours=12)
    return out

# ------------------------------------------------------------------ main
# ------------------------------------------------------------------ public web link (GitHub Pages)
GH_API="https://api.github.com"

class GitHub:
    def __init__(self, token, owner, repo):
        import requests
        self.s=requests.Session(); self.owner=owner; self.repo=repo
        self.s.headers.update({'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json',
                               'X-GitHub-Api-Version':'2022-11-28','User-Agent':'rsrw-tphigrams'})
    def call(self, method, path, ok=(200,201), **kw):
        for attempt in range(4):                       # retry dropped connections / GitHub hiccups
            try:
                r=self.s.request(method, GH_API+path, timeout=120, **kw)
                if r.status_code in (500,502,503,504) and attempt<3: raise ConnectionError(f"HTTP {r.status_code}")
                break
            except Exception as e:
                if attempt==3: raise
                time.sleep(10*(attempt+1))
        if r.status_code not in ok:
            raise RuntimeError(f"GitHub {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.text else {}
    def rp(self, tail=''): return f"/repos/{self.owner}/{self.repo}{tail}"

def _web_image(png, jpg):
    """Web copy of a chart: 1000 px wide JPEG (~200 KB instead of ~650 KB)."""
    from PIL import Image
    if os.path.exists(jpg) and os.path.getmtime(jpg)>=os.path.getmtime(png): return
    os.makedirs(os.path.dirname(jpg),exist_ok=True)
    im=Image.open(png).convert('RGB'); w=1000
    im.resize((w,int(im.size[1]*w/im.size[0])),Image.LANCZOS).save(jpg,'JPEG',quality=78,optimize=True)

def publish_web(out, cfg, setup=False):
    """Mirror the archive (last keep_days days) to a GitHub Pages site. Returns the public link."""
    import base64
    g=cfg.get('github') or {}
    token,user=g.get('token',''),g.get('user','')
    repo=g.get('repo') or 'rsrw-tphigrams'; keep=int(g.get('keep_days',30))
    if not token or 'PASTE' in token.upper() or not user or 'YOUR' in user.upper():
        if setup: sys.exit('Fill in "github": {"user": ..., "token": ...} in rsrw_config.json first.')
        return None
    gh=GitHub(token,user,repo); link=f"https://{user.lower()}.github.io/{repo}/"
    # repository + Pages (first time only)
    try: info=gh.call('GET',gh.rp())
    except RuntimeError as e:
        if ' 404 ' not in str(e): raise
        print(f"  creating GitHub repository {user}/{repo} ...")
        info=gh.call('POST','/user/repos',json={'name':repo,'auto_init':True,'private':False,
                     'description':'IMD RS/RW T-phi grams and thermodynamic indices (auto-updated)'})
        time.sleep(3)
    branch=info.get('default_branch','main')
    try: gh.call('GET',gh.rp('/pages'))
    except RuntimeError:
        try: gh.call('POST',gh.rp('/pages'),json={'source':{'branch':branch,'path':'/'}}); print("  GitHub Pages switched on")
        except RuntimeError as e: print(f"  could not switch on Pages automatically ({e}); do it once in the repo Settings -> Pages")
    # what the site should contain
    cutoff=(dt.date.today()-dt.timedelta(days=keep)).strftime('%Y%m%d') if keep>0 else '00000000'
    webdir=os.path.join('web_cache'); arch={}; want={}
    for d in sorted(os.listdir(out)):
        m=re.fullmatch(r'(\d{8})_(\d\d)Z',d)
        if not m or m[1]<cutoff or not os.path.isdir(os.path.join(out,d)): continue
        rows=[]
        for fn in sorted(os.listdir(os.path.join(out,d))):
            mm=re.match(r'(\d{5})_(.+)\.png$',fn)
            if not mm: continue
            jpg=os.path.join(webdir,d,fn[:-4]+'.jpg'); _web_image(os.path.join(out,d,fn),jpg)
            path=f"charts/{d}/{fn[:-4]}.jpg"; want[path]=jpg
            rows.append([mm[1],STATIONS.get(mm[1],mm[2].replace('_',' ')),path])
        if rows: arch[f"{m[1]}_{m[2]}"]=sorted(rows,key=lambda r:r[1])
    # GitHub Actions (RSRW_KEEP_REMOTE=1): the computer starts empty every time, so keep the charts that are
    # already on the web page (within keep_days) and list them in the archive, instead of deleting them
    KEEP=set()
    if os.environ.get('RSRW_KEEP_REMOTE'):
        r0=gh.call('GET',gh.rp(f'/git/ref/heads/{branch}'))
        t0=gh.call('GET',gh.rp(f"/git/commits/{r0['object']['sha']}"))['tree']['sha']
        for t in gh.call('GET',gh.rp(f'/git/trees/{t0}?recursive=1')).get('tree',[]):
            mm=re.fullmatch(r'charts/(\d{8})_(\d\d)Z/(\d{5})_(.+)\.jpg',t['path'])
            if t['type']!='blob' or not mm or mm[1]<cutoff or t['path'] in want: continue
            KEEP.add(t['path'])
            arch.setdefault(f"{mm[1]}_{mm[2]}",[]).append([mm[3],STATIONS.get(mm[3],mm[4].replace('_',' ')),t['path']])
        for k_ in arch: arch[k_]=sorted(arch[k_],key=lambda r:r[1])
        arch=dict(sorted(arch.items()))
        print(f"  keeping {len(KEEP)} chart(s) already on the web page")
    newest=max((os.path.getmtime(f) for f in want.values()),default=time.time())   # time of the newest chart
    stamp=dt.datetime.fromtimestamp(newest).strftime('%d %b %Y %H:%M IST')
    page=ARCHIVE_HTML.replace('<span class="mut" id="info"></span>',f'<span class="mut" id="info"></span><span class="mut">updated {stamp}</span>')
    archjs="window.ARCHIVE="+json.dumps(arch,separators=(',',':'))+";"
    import hashlib as _h; ver=_h.md5(archjs.encode()).hexdigest()[:10]
    page=page.replace('<script src="archive.js"></script>',f'<script src="archive.js?v={ver}"></script>')   # browsers always fetch the newest list
    page=page.replace('<meta charset="utf-8">','<meta charset="utf-8"><meta http-equiv="Cache-Control" content="no-cache"><meta http-equiv="Pragma" content="no-cache">',1)
    texts={'index.html':page,'archive.js':archjs,
           '.nojekyll':'','README.md':f"# IMD RS/RW T-phi grams\n\nLive page: {link}\n\nUpdated automatically by rsrw_tphigrams.py.\n"}
    # current site contents
    ref=gh.call('GET',gh.rp(f'/git/ref/heads/{branch}'))
    head=ref['object']['sha']; tree_sha=gh.call('GET',gh.rp(f'/git/commits/{head}'))['tree']['sha']
    have={t['path']:t['sha'] for t in gh.call('GET',gh.rp(f'/git/trees/{tree_sha}?recursive=1')).get('tree',[]) if t['type']=='blob'}
    import hashlib
    def gitsha(b): return hashlib.sha1(b'blob %d\0'%len(b)+b).hexdigest()
    entries=[]; new=0
    gone=[p for p in have if p not in want and p not in texts and p not in KEEP]
    order=list(want.items())+[(k,None) for k in texts if k!='index.html']+[('index.html',None)]   # page last
    for path,src in order:
        b=open(src,'rb').read() if src else texts[path].encode('utf-8')
        if have.get(path)==gitsha(b): continue           # unchanged: GitHub already has it
        sha=gh.call('POST',gh.rp('/git/blobs'),json={'content':base64.b64encode(b).decode(),'encoding':'base64'})['sha']; new+=1
        entries.append({'path':path,'mode':'100644','type':'blob','sha':sha})
    entries+=[{'path':p,'mode':'100644','type':'blob','sha':None} for p in gone]   # old charts to remove
    removed=len(gone)
    if not entries:
        print(f"  web page already up to date: {link}"); return link
    # build the new tree on top of the current one, in small batches (big requests time out on GitHub)
    tree=tree_sha
    for i in range(0,len(entries),150):
        tree=gh.call('POST',gh.rp('/git/trees'),json={'base_tree':tree,'tree':entries[i:i+150]})['sha']
    # single commit with no history, so old charts do not pile up in the repository
    c=gh.call('POST',gh.rp('/git/commits'),json={'message':f'Update {stamp}','tree':tree,'parents':[]})['sha']
    gh.call('PATCH',gh.rp(f'/git/refs/heads/{branch}'),json={'sha':c,'force':True})
    print(f"  web page updated ({new} file(s) uploaded, {removed} old removed): {link}")
    return link

RUN_WINDOWS = (('07:30',3), ('19:30',3))   # IST start, hours: check every 15 min 07:30-10:30 and 19:30-22:30
SCHEDULE_TIMES = tuple(w for w,_ in RUN_WINDOWS)

def load_cfg(path):
    """rsrw_config.json, forgiving trailing commas."""
    if not os.path.exists(path): return {}
    t=open(path,encoding='utf-8-sig').read()
    return json.loads(re.sub(r',\s*([}\]])',r'\1',t))

def schedule(remove=False):
    """Windows Task Scheduler task 'RSRW Tphigrams':
       every 15 min in the RUN_WINDOWS, 3 min after logon, 2 min after unlocking the PC, and any missed run
       as soon as the PC is on again. Each run only does what is still missing, so frequent checks are cheap."""
    import subprocess
    here=os.path.dirname(os.path.abspath(__file__)); script=os.path.join(here,os.path.basename(__file__))
    exe=sys.executable
    if exe.lower().endswith('python.exe') and os.path.exists(exe[:-10]+'pythonw.exe'): exe=exe[:-10]+'pythonw.exe'
    for old in ('0730','0900','1930','2100'):                 # tasks made by earlier versions
        subprocess.run(['schtasks','/delete','/f','/tn',f"RSRW Tphigrams {old}"],capture_output=True)
    if remove:
        subprocess.run(['schtasks','/delete','/f','/tn',"RSRW Tphigrams"],capture_output=True)
        print("Daily RSRW runs removed."); return
    q=lambda x: x.replace("'","''")
    def ps(logon, unlock):
        win="".join(f"$d=New-ScheduledTaskTrigger -Daily -At '{w}';"
                    f"$r=New-ScheduledTaskTrigger -Once -At '{w}' -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours {h});"
                    "$d.Repetition=$r.Repetition;$t+=$d;" for w,h in RUN_WINDOWS)
        lg="$l=New-ScheduledTaskTrigger -AtLogOn -User \"$env:USERDOMAIN\\$env:USERNAME\";$l.Delay='PT3M';$t+=$l;" if logon else ""
        ul=("$c=Get-CimClass -Namespace ROOT\\Microsoft\\Windows\\TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger;"
            "$u=New-CimInstance -CimClass $c -ClientOnly;$u.StateChange=8;$u.Delay='PT2M';$u.Enabled=$true;"
            "$u.UserId=\"$env:USERDOMAIN\\$env:USERNAME\";$t+=$u;") if unlock else ""
        return (f"$ErrorActionPreference='Stop';$t=@();"
                f"$a=New-ScheduledTaskAction -Execute '{q(exe)}' -Argument '\"{q(script)}\" --auto --log' -WorkingDirectory '{q(here)}';"
                f"{win}{lg}{ul}"
                "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                "-MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3);"
                "Register-ScheduledTask -TaskName 'RSRW Tphigrams' -Action $a -Trigger $t -Settings $s -Force | Out-Null;'OK'")
    for logon,unlock in ((True,True),(True,False),(False,False)):
        r=subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-Command',ps(logon,unlock)],capture_output=True,text=True)
        if r.returncode==0 and 'OK' in r.stdout: break
    if r.returncode!=0 or 'OK' not in r.stdout:
        print("FAILED to create the schedule:\n"+(r.stderr or r.stdout)[-800:]); return
    extra=[x for x,f in (("3 min after you log in",logon),("2 min after you unlock the PC",unlock)) if f]
    print("OK - task 'RSRW Tphigrams' created.")
    print("Checks every 15 minutes from "+" and ".join(f"{w} to {int(w[:2])+h:02d}{w[2:]}" for w,h in RUN_WINDOWS)+" IST"
          +(", and "+", ".join(extra) if extra else "")+".")
    print("If the PC is switched on late (e.g. 08:43), a run starts within minutes and fills in everything missed")
    print("since the last successful run (up to 10 days back). Stations already drawn are skipped.")

def status():
    """Show when the scheduled task last ran, its result, the next run, and the end of rsrw_log.txt."""
    import subprocess
    here=os.path.dirname(os.path.abspath(__file__))
    r=subprocess.run(['schtasks','/query','/tn','RSRW Tphigrams','/v','/fo','LIST'],capture_output=True,text=True)
    if r.returncode!=0: print("Scheduled task 'RSRW Tphigrams' not found - run:  python rsrw_tphigrams.py --install-schedule")
    else:
        seen=set()
        for line in r.stdout.splitlines():
            k=line.split(':')[0].strip()
            if k in ('Status','Last Run Time','Last Result','Next Run Time','Task To Run') and k not in seen:
                seen.add(k); print(line.strip())
        print("(Last Result 0 = success; 267009 = running now; 267011 = not run yet)")
    try: print("Last successful download run:", json.load(open(os.path.join(here,STATE_FILE)))['last_ok'][:16].replace('T',' '),"UTC")
    except Exception: print("Last successful download run: none recorded yet")
    lg=os.path.join(here,'rsrw_log.txt')
    if os.path.exists(lg):
        print("\n--- last lines of rsrw_log.txt ---"); print("".join(open(lg,encoding='utf-8',errors='ignore').readlines()[-25:]))
    else: print("rsrw_log.txt does not exist yet (the scheduled task has not run, or failed before starting).")

def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--auto',action='store_true',help='download + plot every ascent not done yet: last 36 h, or since the last successful run (max 10 days)')
    ap.add_argument('--date',default=dt.date.today().isoformat(),help='YYYY-MM-DD (default today)')
    ap.add_argument('--hour',default='00',choices=['00','12'])
    ap.add_argument('--zips',help='folder of files you downloaded by hand (offline mode)')
    ap.add_argument('--list-url'); ap.add_argument('--list-html'); ap.add_argument('--cookie')
    ap.add_argument('--out',default='Tphigrams'); ap.add_argument('--config',default='rsrw_config.json')
    ap.add_argument('--log',action='store_true',help=argparse.SUPPRESS)
    ap.add_argument('--install-schedule',action='store_true',help='Windows: run --auto every 15 min 07:30-10:30 and 19:30-22:30 IST, at logon/unlock, and after missed runs')
    ap.add_argument('--status',action='store_true',help='show the last scheduled run, its result and the end of the log')
    ap.add_argument('--setup-web',action='store_true',help='create/update the public web page (GitHub Pages) now')
    ap.add_argument('--remove-schedule',action='store_true',help='Windows: remove that daily schedule')
    args=ap.parse_args()
    if args.status: return status()
    if args.install_schedule or args.remove_schedule: return schedule(args.remove_schedule)
    if args.setup_web:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
        cfg=load_cfg(args.config)
        write_archive(args.out); link=publish_web(args.out,cfg,setup=True)
        print(f"\nYour shareable link: {link}\n(GitHub needs 1-2 minutes to show it the very first time.)"); return
    here=os.path.dirname(os.path.abspath(__file__)); os.chdir(here)       # works from Task Scheduler too
    cfg=load_cfg(args.config)
    os.makedirs(args.out,exist_ok=True)
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] RS/RW T-phi grams")
    if args.zips:
        done,failed,folder=process(local_zips(args),args.date,args.hour,args.out)
        print(f"\n{len(done)} charts in {folder}")
        known=set(STATIONS)-{i for i,_,_ in done}-{i for i,_,_ in failed}
        if known: print("No file in the folder for: "+", ".join(sorted(STATIONS[k] for k in known)))
    else:
        for attempt in range(6):                              # the network may not be up yet right after switch-on
            try: s=make_session(cfg,args.cookie); break
            except Exception as e:
                if attempt==5 or 'login failed' in str(e) or 'not found in the login dropdown' in str(e): raise
                print(f"  cannot reach CLIMS yet ({e.__class__.__name__}); retrying in 60 s"); time.sleep(60)
        hrs=catchup_hours() if args.auto else 0
        if args.auto and hrs>36: print(f"  catching up on the last {hrs} h (PC was off since the last run)")
        slots=recent_slots(hrs) if args.auto else [(args.date,args.hour)]
        for date,hour in slots:
            folder=os.path.join(args.out,f"{date.replace('-','')}_{hour}Z")
            have={f[:5] for f in os.listdir(folder) if f.endswith('.png')} if os.path.isdir(folder) else set()
            raw=os.path.join('data',f"{date.replace('-','')}_{hour}Z")
            done,failed,_=process(online_zips(s,cfg,date,hour,raw,skip=have if args.auto else (),args=args),date,hour,args.out)
            if done or failed: print(f"  -> {len(done)} new chart(s), {len(failed)} failed")
            if done:                                          # put this ascent online straight away
                write_archive(args.out)
                try: publish_web(args.out,cfg)
                except Exception as e: print(f"  web upload failed: {e}")
    if args.auto and not args.zips:
        json.dump({'last_ok':dt.datetime.now(dt.timezone.utc).isoformat()},open(STATE_FILE,'w'))
    n=write_archive(args.out)
    print(f"Archive: {os.path.abspath(os.path.join(args.out,'index.html'))}  ({n} ascents)")
    try:
        link=publish_web(args.out,cfg)
        if link: print(f"Web link: {link}")
    except Exception as e: print(f"  web upload failed: {e}")

if __name__=='__main__':
    try: main()
    except SystemExit: raise
    except Exception:
        print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] RUN FAILED:"); traceback.print_exc(); sys.exit(1)
