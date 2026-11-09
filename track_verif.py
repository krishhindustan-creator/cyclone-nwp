#!/usr/bin/env python3
"""
track_verif.py — verification of the cyclone forecasts (IMD MME, its member models, ECMWF ENS mean and HRES)
against the observed track (JTWC best track, b-deck from UCAR RAL).

For every archived run it computes, per model:
  • track (direct position) error at +0 … +120 h (km)
  • MSW and central-pressure error (forecast − observed)
  • landfall point error (km) and landfall time error (h, + = forecast later than observed)
Forecast tracks are copied into the permanent verification archive (VERIF_ARCHIVE/trackverif/tracks/), so a storm can
still be verified after its runs have left the 15-day web archive. Results: SITE/trackverif/summary.json + figures.
Every run now holds forecasts for BOTH basins (files prefixed ars_ / bob_ by nwp_runner.py). They are archived per basin
(tracks/<run>/ars/, tracks/<run>/bob/) and each storm is verified with the forecast set that is about that storm.
Runs archived before the two-basin version (tracks/<run>/*.txt) are still read.
Environment: SITE (default ./site), VERIF_ARCHIVE (default ./archive).
"""
import datetime as dt, glob, io, json, math, os, re, shutil, urllib.request, zipfile

SITE = os.path.abspath(os.environ.get("SITE", "site"))
ARCH = os.path.abspath(os.environ.get("VERIF_ARCHIVE", "archive"))
OUT = os.path.join(SITE, "trackverif"); TRK = os.path.join(ARCH, "trackverif", "tracks")
os.makedirs(OUT, exist_ok=True); os.makedirs(TRK, exist_ok=True)
LEADS = [0, 12, 24, 36, 48, 72, 96, 120]
BAS = ("ars", "bob")                                # basin prefixes used by nwp_runner.py (Arabian Sea / Bay of Bengal)
# member files inside the MME package -> name shown on the page
MME_MEMBERS = {"MMM_track.txt": "IMD MME", "IMD_GFS_track.txt": "IMD GFS", "NCUM_track.txt": "NCUM-G", "NCUMR_track.txt": "NCUM-R",
               "NCEP_GFS_track.txt": "NCEP GFS", "CMC_GDPS_track.txt": "CMC GDPS", "NGX_track.txt": "NAVGEM"}
ENS_FILES = {"ECMWF_ENSMEAN_track.txt": "ECMWF ENS mean", "ECMWF_HRES_track.txt": "ECMWF HRES"}
ORDER = ["IMD MME", "ECMWF ENS mean", "ECMWF HRES", "IMD GFS", "NCUM-G", "NCUM-R", "NCEP GFS", "CMC GDPS", "NAVGEM"]
COLOR = {"IMD MME": "#000000", "ECMWF ENS mean": "#1f77b4", "ECMWF HRES": "#17becf", "IMD GFS": "#2ca02c", "NCUM-G": "#8c564b",
         "NCUM-R": "#bcbd22", "NCEP GFS": "#d62728", "CMC GDPS": "#9467bd", "NAVGEM": "#7f7f7f"}
T = lambda s: dt.datetime.strptime(s, "%Y%m%d%H")

def km(a, b, c, d):
    p = math.pi / 180; x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(min(1, x)))

def read_track(path):
    """[(time, lat, lon, msw, mslp)] from any of the track formats used on the site."""
    out = []
    for l in open(path, encoding="utf-8", errors="replace"):
        if l.startswith(("#", "%")): continue
        p = l.split()
        i = next((k for k, x in enumerate(p) if re.fullmatch(r"\d{10}", x)), None)
        if i is None or len(p) < i + 3: continue
        f = lambda x: float("nan") if x.lower() == "nan" else float(x)
        try:
            out.append((T(p[i]), f(p[i + 1]), f(p[i + 2]), f(p[i + 3]) if len(p) > i + 3 else float("nan"),
                        f(p[i + 4]) if len(p) > i + 4 else float("nan")))
        except ValueError: pass
    return out

# ---------- 1. keep the forecast tracks of every run in the permanent archive
def split_tag(fname):
    """'bob_MMM_track.txt' -> ('bob', 'MMM_track.txt'); files of runs made before the two-basin version have no prefix."""
    for t in BAS:
        if fname.startswith(t + "_"): return t, fname[len(t) + 1:]
    return None, fname
def store(ic, name, data, tag=None):
    d = os.path.join(TRK, ic, tag) if tag else os.path.join(TRK, ic); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, re.sub(r"[^A-Za-z0-9]+", "_", name) + ".txt"), "wb").write(data)
for d in glob.glob(os.path.join(SITE, "mme", "*")):
    ic = os.path.basename(d)
    if not re.fullmatch(r"\d{10}", ic): continue
    for p in glob.glob(os.path.join(d, "*")):
        tag, rest = split_tag(os.path.basename(p))
        if rest == "MMM_track.txt": store(ic, "IMD MME", open(p, "rb").read(), tag)
        elif rest.endswith(".zip"):
            try:
                with zipfile.ZipFile(p) as zf:
                    for n in zf.namelist():
                        b = os.path.basename(n)
                        if "adeck/" in n: continue                      # the a-deck copies are not MME members
                        if b in MME_MEMBERS and b != "MMM_track.txt": store(ic, MME_MEMBERS[b], zf.read(n), tag)
            except Exception as e: print(f"{ic}: cannot read package ({e})")
for d in glob.glob(os.path.join(SITE, "ens", "*")):
    ic = os.path.basename(d)
    if not re.fullmatch(r"\d{10}", ic): continue
    for p in glob.glob(os.path.join(d, "*")):
        tag, rest = split_tag(os.path.basename(p))
        if rest in ENS_FILES: store(ic, ENS_FILES[rest], open(p, "rb").read(), tag)
NAME_OF = {re.sub(r"[^A-Za-z0-9]+", "_", n): n for n in ORDER}
FC = {}                                             # ic -> [ {model: track}, ... ]  one set per basin (older runs: one set)
for d in sorted(glob.glob(os.path.join(TRK, "*"))):
    ic = os.path.basename(d)
    for sub in [d] + [os.path.join(d, t) for t in BAS]:
        if not os.path.isdir(sub): continue
        S_ = {}
        for f in glob.glob(os.path.join(sub, "*.txt")):
            n = NAME_OF.get(os.path.basename(f)[:-4]); tr = read_track(f)
            if n and len(tr) >= 2: S_[n] = tr
        if S_: FC.setdefault(ic, []).append(S_)
print(f"forecast runs with tracks: {len(FC)}")

# ---------- 2. observed tracks (JTWC best track) for this year's North Indian Ocean systems
years = sorted({T(ic).year for ic in FC} or {dt.datetime.utcnow().year})
STORMS = {}
for y in years:
    for n in range(1, 31):
        sid = f"io{n:02d}{y}"; url = f"https://hurricanes.ral.ucar.edu/repository/data/bdecks_open/{y}/b{sid}.dat"
        try: txt = urllib.request.urlopen(url, timeout=40).read().decode("utf-8", "replace")
        except Exception: continue
        obs, name = {}, ""
        for l in txt.splitlines():
            p = [x.strip() for x in l.split(",")]
            if len(p) < 10 or not re.fullmatch(r"\d{10}", p[2]): continue
            t = T(p[2])
            if t in obs: continue
            la = int(p[6][:-1]) / 10 * (1 if p[6][-1] == "N" else -1); lo = int(p[7][:-1]) / 10 * (1 if p[7][-1] == "E" else -1)
            obs[t] = (la, lo, float(p[8] or "nan"), float(p[9] or "nan") if p[9] not in ("", "0") else float("nan"))
            if len(p) > 27 and p[27] and not p[27].isdigit(): name = p[27]
        if len(obs) >= 3:
            NUMW = {"ONE","TWO","THREE","FOUR","FIVE","SIX","SEVEN","EIGHT","NINE","TEN","ELEVEN","TWELVE","INVEST",""}
            STORMS[sid.upper()] = {"name": "" if name.upper() in NUMW else name.upper(), "obs": dict(sorted(obs.items()))}
            print(f"best track {sid.upper()} {name}: {len(obs)} positions {min(obs):%d %b %HZ}–{max(obs):%d %b %HZ}")

def obs_at(st, t):
    """Observed (lat, lon, msw, mslp) at time t, interpolated linearly between fixes at most 18 h apart."""
    o = st["obs"]
    if t in o: return o[t]
    before = [k for k in o if k < t]; after = [k for k in o if k > t]
    if not before or not after: return None
    a, b = max(before), min(after)
    if (b - a).total_seconds() > 18 * 3600: return None
    w = (t - a).total_seconds() / (b - a).total_seconds()
    return tuple(o[a][i] + w * (o[b][i] - o[a][i]) for i in range(4))

# ---------- 3. landfall (first crossing from sea to land) on the Natural Earth coastline
LAND = None
try:
    import cartopy.io.shapereader as shp
    from shapely.geometry import Point, LineString
    from shapely.ops import unary_union
    from shapely.prepared import prep
    from shapely.geometry import box
    geoms = [g for g in shp.Reader(shp.natural_earth("10m", "physical", "land")).geometries() if g.intersects(box(35, -5, 105, 40))]
    LAND_G = unary_union(geoms).intersection(box(35, -5, 105, 40)); LAND = prep(LAND_G)
except Exception as e:
    print("coastline not available — landfall verification skipped:", e)

def landfall(track):
    """(time, lat, lon) of the first sea→land crossing, interpolated along the 6-hourly segment, or None."""
    if LAND is None: return None
    pts = [(t, la, lo) for t, la, lo, *_ in track if not math.isnan(la)]
    for (t0, a0, b0), (t1, a1, b1) in zip(pts, pts[1:]):
        if not LAND.contains(Point(b0, a0)) and LAND.contains(Point(b1, a1)):
            lo_, hi_ = 0.0, 1.0
            for _ in range(25):                       # bisection for the crossing point
                m_ = (lo_ + hi_) / 2
                if LAND.contains(Point(b0 + m_ * (b1 - b0), a0 + m_ * (a1 - a0))): hi_ = m_
                else: lo_ = m_
            return (t0 + (t1 - t0) * hi_, a0 + hi_ * (a1 - a0), b0 + hi_ * (b1 - b0))
    return None

PLACES = {"Chennai": (13.08, 80.29), "Nellore": (14.44, 80.0), "Ongole": (15.5, 80.05), "Bapatla": (15.9, 80.47), "Machilipatnam": (16.17, 81.13),
          "Narsapur": (16.43, 81.70), "Yanam": (16.73, 82.21), "Kakinada": (16.93, 82.22), "Visakhapatnam": (17.69, 83.22), "Kalingapatnam": (18.33, 84.12),
          "Gopalpur": (19.26, 84.90), "Puri": (19.81, 85.83), "Paradip": (20.26, 86.67), "Balasore": (21.49, 86.93), "Digha": (21.63, 87.51),
          "Sagar Island": (21.65, 88.05), "Khepupara": (21.98, 90.22), "Chittagong": (22.33, 91.83), "Cox's Bazar": (21.43, 92.0), "Sittwe": (20.15, 92.9),
          "Puducherry": (11.93, 79.83), "Karaikal": (10.92, 79.84), "Mumbai": (18.94, 72.83), "Veraval": (20.9, 70.37), "Porbandar": (21.64, 69.6),
          "Dwarka": (22.24, 68.97), "Karachi": (24.86, 67.01), "Muscat": (23.59, 58.41), "Salalah": (17.02, 54.09), "Kochi": (9.97, 76.27), "Goa": (15.49, 73.83)}
def near(la, lo):
    n, d = min(((k, km(la, lo, *v)) for k, v in PLACES.items()), key=lambda x: x[1])
    return f"near {n}" if d < 15 else f"{d:.0f} km from {n}"

# ---------- 4. errors per run and model
SEL = {}                                            # (storm id, run) -> the forecast set (one basin) used for that storm
summary = {"updated": (dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)).strftime("%d %b %Y %H:%M IST"),
           "leads": LEADS, "models": ORDER, "storms": []}
for sid, st in sorted(STORMS.items()):
    obs_lf = landfall([(t, *v) for t, v in st["obs"].items()])
    runs = []
    for ic in sorted(FC):
        t0 = T(ic); o0 = obs_at(st, t0)
        if not o0: continue
        S = None
        for cand in FC[ic]:                         # each basin's forecast set: keep the one that is about this storm
            ref = cand.get("IMD MME") or next(iter(cand.values()))
            f0 = next(((la, lo) for t, la, lo, *_ in ref if t == t0), (ref[0][1], ref[0][2]))
            if km(o0[0], o0[1], *f0) <= 400: S = cand; break
        if S is None: continue                      # no forecast set of this run is about this storm
        SEL[(sid, ic)] = S
        row = {"ic": ic, "label": f"{t0:%H} UTC {t0.day} {t0:%b}", "models": {}}
        for mdl in ORDER:
            tr = S.get(mdl)
            if not tr: continue
            fc = {t: (la, lo, w, p) for t, la, lo, w, p in tr}
            r = {"dpe": {}, "dv": {}, "dp": {}}
            for L in LEADS:
                t = t0 + dt.timedelta(hours=L); o = obs_at(st, t); f = fc.get(t)
                if not o or not f or math.isnan(f[0]): continue
                r["dpe"][L] = round(km(o[0], o[1], f[0], f[1]))
                if not math.isnan(f[2]) and not math.isnan(o[2]): r["dv"][L] = round(f[2] - o[2])
                if not math.isnan(f[3]) and not math.isnan(o[3]): r["dp"][L] = round(f[3] - o[3], 1)
            starts_on_land = LAND is not None and LAND.contains(Point(tr[0][2], tr[0][1]))
            lf = None if (starts_on_land or (obs_lf and t0 >= obs_lf[0])) else landfall(tr)   # only runs started before landfall, over sea
            if lf:
                r["lf"] = {"t": f"{lf[0]:%d %b %H:%MZ}", "lat": round(lf[1], 2), "lon": round(lf[2], 2), "place": near(lf[1], lf[2])}
                if obs_lf:
                    r["lf"]["dist_km"] = round(km(obs_lf[1], obs_lf[2], lf[1], lf[2]))
                    r["lf"]["dt_h"] = round((lf[0] - obs_lf[0]).total_seconds() / 3600, 1)
            row["models"][mdl] = r
        if row["models"]: runs.append(row)
    if not runs: continue
    stats = {}
    for mdl in ORDER:
        s = {"dpe": {}, "dv": {}, "n": {}, "lf_dist": None, "lf_dt": None, "lf_n": 0}
        for L in LEADS:
            v = [r["models"][mdl]["dpe"][L] for r in runs if mdl in r["models"] and L in r["models"][mdl]["dpe"]]
            if v: s["dpe"][L] = round(sum(v) / len(v)); s["n"][L] = len(v)
            w_ = [r["models"][mdl]["dv"][L] for r in runs if mdl in r["models"] and L in r["models"][mdl]["dv"]]
            if w_: s["dv"][L] = round(sum(w_) / len(w_))
        lf_ = [r["models"][mdl]["lf"] for r in runs if mdl in r["models"] and "dist_km" in r["models"][mdl].get("lf", {})]
        if lf_:
            s["lf_dist"] = round(sum(x["dist_km"] for x in lf_) / len(lf_)); s["lf_dt"] = round(sum(abs(x["dt_h"]) for x in lf_) / len(lf_), 1); s["lf_n"] = len(lf_)
        if s["dpe"]: stats[mdl] = s
    o = st["obs"]
    basin = "A" if sum(v[1] for v in o.values()) / len(o) < 78 else "B"
    label_ = (f"{st['name']} ({sid[2:4]}{basin})" if st["name"] else f"System {sid[2:4]}{basin}")
    storm = {"id": sid, "name": st["name"], "title": label_ + f", {min(o):%d %b}–{max(o):%d %b %Y}",
             "obs": [[f"{t:%Y%m%d%H}", v[0], v[1], v[2], v[3]] for t, v in o.items()],
             "obs_peak": max((v[2] for v in o.values() if not math.isnan(v[2])), default=None),
             "obs_landfall": ({"t": f"{obs_lf[0]:%d %b %H:%MZ}", "lat": round(obs_lf[1], 2), "lon": round(obs_lf[2], 2), "place": near(obs_lf[1], obs_lf[2])} if obs_lf else None),
             "runs": runs, "stats": stats, "figures": []}

    # ---------- 5. figures
    try:
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
        sfx = sid.lower()
        # (a) mean track error by lead time
        fig, ax = plt.subplots(figsize=(10, 5.6), dpi=130)
        for mdl in ORDER:
            s = stats.get(mdl)
            if not s: continue
            Ls = [L for L in LEADS if L in s["dpe"]]
            ax.plot(Ls, [s["dpe"][L] for L in Ls], "-o", color=COLOR[mdl], lw=3.2 if mdl == "IMD MME" else 1.6, ms=5, label=mdl)
        ax.set_xticks(LEADS); ax.set_xlabel("Forecast lead time (h)"); ax.set_ylabel("Mean track error (km)"); ax.grid(alpha=.4)
        nr = max((max(s_["n"].values()) for s_ in stats.values() if s_["n"]), default=0)
        ax.set_title(f"{storm['title']} — mean track error ({nr} runs at most per lead time)", fontsize=12); ax.legend(fontsize=9, ncol=3)
        Lmax = max((L for s_ in stats.values() for L in s_["dpe"]), default=120); ax.set_xlim(-3, Lmax + 6)
        fn = f"track_error_{sfx}.png"; fig.tight_layout(); fig.savefig(os.path.join(OUT, fn)); plt.close(fig)
        storm["figures"].append({"t": "Mean track error by lead time", "f": f"trackverif/{fn}"})
        # (b) observed track and the MME / ENS-mean forecasts of every run, with landfall points
        import cartopy.crs as ccrs, cartopy.feature as cf
        la_ = [v[0] for v in o.values()] + [p[1] for r in runs for m_ in ("IMD MME", "ECMWF ENS mean") for p in SEL[(sid, r["ic"])].get(m_, [])]
        lo_ = [v[1] for v in o.values()] + [p[2] for r in runs for m_ in ("IMD MME", "ECMWF ENS mean") for p in SEL[(sid, r["ic"])].get(m_, [])]
        ext = [max(35, min(lo_) - 2), min(105, max(lo_) + 2), max(-5, min(la_) - 1.5), min(40, max(la_) + 1.5)]
        fig = plt.figure(figsize=(12.5, 6.4), dpi=130)
        for k, mdl in enumerate(("IMD MME", "ECMWF ENS mean")):
            ax = fig.add_subplot(1, 2, k + 1, projection=ccrs.PlateCarree()); ax.set_extent(ext, ccrs.PlateCarree())
            ax.add_feature(cf.LAND.with_scale("50m"), facecolor="#f3efe6"); ax.add_feature(cf.OCEAN.with_scale("50m"), facecolor="#dceaf5")
            ax.add_feature(cf.COASTLINE.with_scale("50m"), lw=.7); ax.add_feature(cf.BORDERS.with_scale("50m"), lw=.4, color="gray")
            cm = plt.get_cmap("viridis", max(2, len(runs)))
            for i, r in enumerate(runs):
                tr = SEL[(sid, r["ic"])].get(mdl)
                if not tr: continue
                ax.plot([p[2] for p in tr], [p[1] for p in tr], color=cm(i), lw=1.3, alpha=.85, transform=ccrs.PlateCarree(),
                        label=r["label"] if (i % max(1, len(runs) // 6) == 0) else None)
                lf = r["models"].get(mdl, {}).get("lf")
                if lf: ax.plot(lf["lon"], lf["lat"], "x", color=cm(i), ms=7, mew=2, transform=ccrs.PlateCarree())
            ax.plot([v[1] for v in o.values()], [v[0] for v in o.values()], "-", color="#d62728", lw=3.5, transform=ccrs.PlateCarree(), label="Observed (best track)")
            if obs_lf: ax.plot(obs_lf[2], obs_lf[1], "*", color="gold", ms=18, mec="k", transform=ccrs.PlateCarree(), label="Observed landfall")
            ax.set_title(f"{mdl} forecasts (× = forecast landfall)", fontsize=11); ax.legend(fontsize=7, loc="lower left")
            gl = ax.gridlines(draw_labels=True, lw=.3, alpha=.5); gl.top_labels = gl.right_labels = False
        fig.suptitle(f"{storm['title']}: forecast tracks of every run vs observed track", fontsize=13, fontweight="bold")
        fn = f"tracks_{sfx}.png"; fig.tight_layout(); fig.savefig(os.path.join(OUT, fn)); plt.close(fig)
        storm["figures"].append({"t": "Forecast tracks vs observed track", "f": f"trackverif/{fn}"})
        # (c) intensity: MME and ENS-mean MSW of every run vs observed
        fig, axs = plt.subplots(1, 2, figsize=(12.5, 5), dpi=130, sharey=True)
        ot = list(o.keys())
        for ax, mdl in zip(axs, ("IMD MME", "ECMWF ENS mean")):
            cm = plt.get_cmap("viridis", max(2, len(runs)))
            for i, r in enumerate(runs):
                tr = SEL[(sid, r["ic"])].get(mdl)
                if tr: ax.plot([p[0] for p in tr], [p[3] for p in tr], color=cm(i), lw=1.2, alpha=.85)
            ax.plot(ot, [o[t][2] for t in ot], "-o", color="#d62728", lw=3, ms=4, label="Observed (JTWC, 1-min wind)")
            ax.set_title(f"{mdl}: maximum sustained wind of every run", fontsize=11); ax.grid(alpha=.4); ax.legend(fontsize=8)
            ax.set_xlim(min(ot) - dt.timedelta(hours=6), max(ot) + dt.timedelta(hours=24)); ax.tick_params(axis="x", labelsize=8, rotation=30)
        axs[0].set_ylabel("MSW (kt)")
        fig.suptitle(f"{storm['title']}: forecast vs observed intensity", fontsize=13, fontweight="bold")
        fn = f"intensity_{sfx}.png"; fig.tight_layout(); fig.savefig(os.path.join(OUT, fn)); plt.close(fig)
        storm["figures"].append({"t": "Forecast vs observed intensity", "f": f"trackverif/{fn}"})
    except Exception as e:
        print(f"{sid}: figures failed ({e})")
    summary["storms"].append(storm)
    print(f"{sid}: {len(runs)} runs verified; observed landfall: {storm['obs_landfall']}")

summary["storms"].sort(key=lambda s: s["obs"][-1][0], reverse=True)       # newest storm first
json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), separators=(",", ":"))
print(f"track verification: {len(summary['storms'])} storm(s) written to {OUT}")
