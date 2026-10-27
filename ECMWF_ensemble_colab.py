# ================================================================
#  ECMWF ENSEMBLE (51 members) + HRES cyclone tracks — Google Colab, one cell
#  Output (zip): E1 spaghetti, E2 strike probability, E3 MSW plume, E4 MSLP plume,
#                landfall statistics (table + WhatsApp-size PNG), ensemble-mean / HRES track files
# ================================================================
!pip install -q eccodes cartopy

# ======================= USER SETTINGS =======================
DATE  = "20261025"           # run date YYYYMMDD
RUN   = "00"                 # "00" or "12"  (00 UTC is out ~13:15 IST, 12 UTC ~01:15 IST)
BASIN = "B"                  # B = Bay of Bengal, A = Arabian Sea
REF_LAT, REF_LON = None, None   # None = automatic (start of the ECMWF track)
# =============================================================
TAG = "ECMWF_ENS"

import os, re, glob, shutil, traceback, numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg")
try:
    from google.colab import files; IN_COLAB = True
except ImportError:
    IN_COLAB = False
IC = f"{DATE}{RUN}"; IC_T = pd.to_datetime(IC, format="%Y%m%d%H"); IC_TEXT = f"{RUN} UTC {IC_T:%d %b %Y}"
WORK = (f"/content/{TAG}_{IC}" if IN_COLAB else os.path.abspath(f"{TAG}_{IC}")); os.makedirs(WORK, exist_ok=True); os.chdir(WORK)
PKG = f"{TAG}_package_{IC}"; os.makedirs(PKG, exist_ok=True); STATUS = {}
def setv(src, name, value):
    return re.compile(rf"^{re.escape(name)}\s*=.*$", re.M).sub(f"{name} = {value!r}", src, count=1)
def setref(src):
    return re.sub(r"^REF_LAT, REF_LON\s*=.*$", f"REF_LAT, REF_LON = {REF_LAT}, {REF_LON}", src, count=1, flags=re.M)
def run_part(name, src):
    print("\n" + "=" * 70 + f"\n  {name}\n" + "=" * 70)
    g = {"__name__": f"part_{name}"}; import time as _t; t0_ = _t.time()
    try:
        exec(compile(src, f"<{name}>", "exec"), g); STATUS[name] = "OK"
    except SystemExit as e:
        STATUS[name] = f"stopped: {e}"; print(f"[{name}] stopped: {e}")
    except Exception as e:
        STATUS[name] = f"FAILED: {type(e).__name__}: {e}"; traceback.print_exc(limit=2)
    STATUS[name] += f"   ({(_t.time() - t0_) / 60:.1f} min)"
    return g
def _auto_ref():
    """Start position of the system at this run's time, from ECMWF HRES tracks.
    Uses this run's ECMWF track file; if it is not published yet (or too old), falls back to the
    previous ECMWF runs (up to 24 h earlier) and takes their forecast position at this run's time."""
    import urllib.request, urllib.error, re as _re, eccodes
    box = {"B": (5, 23, 78, 100), "A": (5, 25, 50, 78)}.get(BASIN, (0, 30, 40, 100))
    t_run = pd.to_datetime(f"{DATE}{RUN}", format="%Y%m%d%H")
    for back in (0, 12, 24):
        t_ec = t_run - pd.Timedelta(hours=back)
        if t_ec.hour not in (0, 12): continue
        d = f"https://data.ecmwf.int/forecasts/{t_ec:%Y%m%d}/{t_ec:%H}z/ifs/0p25/oper/"
        try:
            m = _re.findall(r'href="([^"]*-tf\.bufr)"', urllib.request.urlopen(d, timeout=60).read().decode())
        except Exception as e:
            print(f"auto REF: ECMWF {t_ec:%d %b %H}Z not available ({e})"); continue
        if not m:
            print(f"auto REF: ECMWF {t_ec:%d %b %H}Z has no track file"); continue
        urllib.request.urlretrieve("https://data.ecmwf.int" + m[0], "ref_oper.bufr")
        best = None
        with open("ref_oper.bufr", "rb") as f:
            while True:
                h = eccodes.codes_bufr_new_from_file(f)
                if h is None: break
                eccodes.codes_set(h, "unpack", 1)
                sid = eccodes.codes_get(h, "stormIdentifier").strip()
                if sid.endswith(BASIN) and eccodes.codes_is_defined(h, "timePeriod"):
                    tp = [0] + list(eccodes.codes_get_array(h, "timePeriod"))
                    la = [eccodes.codes_get_array(h, f"#{2*(k+1)}#latitude")[0] for k in range(len(tp))]
                    lo = [eccodes.codes_get_array(h, f"#{2*(k+1)}#longitude")[0] for k in range(len(tp))]
                    w = [x for x in eccodes.codes_get_array(h, "windSpeedAt10M") if abs(x) < 1e90]
                    ok = [k for k in range(len(tp)) if abs(la[k]) < 90]
                    k0 = next((k for k in ok if tp[k] >= back), None)          # position at this run's time
                    if k0 is not None and box[0] <= la[k0] <= box[1] and box[2] <= lo[k0] <= box[3]:
                        wm = max(w) if w else 0
                        if best is None or wm > best[0]: best = (wm, la[k0], lo[k0], sid)
                eccodes.codes_release(h)
        if best:
            src = "this run" if back == 0 else f"ECMWF {t_ec:%d %b %H}Z run, +{back} h"
            print(f"auto REF: storm {best[3]} at {best[1]:.1f}N {best[2]:.1f}E ({src})")
            return best
    return None


STORM_SID = None
if REF_LAT is None or REF_LON is None:
    b = _auto_ref()
    if b is None: raise SystemExit("No ECMWF track found for this run or the previous 24 h (wrong DATE/RUN, run older than ~3 days, or no system). Check DATE/RUN, or set REF_LAT, REF_LON yourself.")
    REF_LAT, REF_LON = round(b[1], 1), round(b[2], 1)
    STORM_SID = b[3]
print(f"Run {IC_TEXT} | basin {BASIN} | start position {REF_LAT}N {REF_LON}E")

SRC_ECM = '# ================================================================\n#  ECMWF ENS (51 members) + HRES tropical-cyclone tracks — Google Colab\n#  Source: ECMWF open data (CC BY 4.0), https://data.ecmwf.int/forecasts\n#  Products: tracks (type = tf, BUFR) for 00 and 12 UTC runs.\n#  The site keeps only the last few days, so fetch each run soon after it is issued.\n# ================================================================\n\n# ---------- USER SETTINGS ----------\nDATE   = "20261023"        # run date YYYYMMDD\nRUN    = "00"              # "00" or "12"\nBASIN  = "B"               # B = Bay of Bengal, A = Arabian Sea\nREF_LAT, REF_LON = 15.5, 89.0   # rough position of YOUR system (pick it on any chart)\nREF_RADIUS_DEG   = 4.0          # a member\'s track is accepted if it passes within this of REF ...\nREF_HOURS        = 72           # ... during the first REF_HOURS of the forecast\nMIN_MEMBERS      = 10           # ensemble mean only where at least this many members still track it\nSTRIKE_KM        = 120          # strike-probability radius (ECMWF uses 120 km)\nMAP_EXT = [78, 95, 8, 27]       # [lon0, lon1, lat0, lat1]\n\n# ---------- downloads ----------\nimport os, re, subprocess, urllib.request\nimport numpy as np, pandas as pd, matplotlib.pyplot as plt, matplotlib.dates as mdates\nimport eccodes\nimport cartopy.crs as ccrs\nfrom cartopy.io.shapereader import Reader\n\nos.makedirs("ne", exist_ok=True); os.makedirs("ecmwf", exist_ok=True); os.makedirs("ecmwf_plots", exist_ok=True)\nfor f in ["cultural/ne_10m_admin_0_countries_ind", "physical/ne_10m_coastline",\n          "physical/ne_10m_land", "cultural/ne_10m_admin_1_states_provinces"]:\n    z = "ne/" + f.split("/")[1] + ".zip"\n    if not os.path.exists(z.replace(".zip", ".shp")):\n        subprocess.run(["wget", "-q", "-O", z, f"https://naciscdn.org/naturalearth/10m/{f}.zip"])\n        subprocess.run(["unzip", "-oq", z, "-d", "ne"])\n\nROOT = "https://data.ecmwf.int"\ndef fetch(stream):\n    """Find the *-tf.bufr file in the run directory (file name changes with forecast length) and download it."""\n    d = f"{ROOT}/forecasts/{DATE}/{RUN}z/ifs/0p25/{stream}/"\n    try:\n        html = urllib.request.urlopen(d, timeout=60).read().decode()\n    except Exception as e:\n        print(f"[{stream}] cannot list {d}: {e}"); return None\n    m = re.findall(r\'href="([^"]*-tf\\.bufr)"\', html)\n    if not m:\n        print(f"[{stream}] no track file for {DATE} {RUN}Z (no TC tracked, run not yet out, or too old)"); return None\n    out = f"ecmwf/{stream}_{DATE}{RUN}.bufr"\n    urllib.request.urlretrieve(ROOT + m[0], out)\n    print(f"[{stream}] downloaded {m[0].split(\'/\')[-1]}  ({os.path.getsize(out)/1e6:.2f} MB)")\n    return out\n\nens_file  = fetch("enfo")   # 50 perturbed members + control (member 51)\nhres_file = fetch("oper")   # deterministic HRES\n\n# ---------- BUFR decoding ----------\nMISS = 1e90\ndef _arr(h, key, nsub, comp):\n    if comp or nsub == 1:\n        a = np.atleast_1d(eccodes.codes_get_array(h, key)).astype(float)\n        return np.repeat(a, nsub) if len(a) == 1 else a\n    return np.array([eccodes.codes_get(h, f"/subsetNumber={s}/{key}") for s in range(1, nsub + 1)], float)\n\ndef decode(path, label):\n    """Return list of tracks: dict(sid, member, kind, df[t, lat, lon, w(kt), p(hPa)])."""\n    tracks = []\n    with open(path, "rb") as f:\n        while True:\n            h = eccodes.codes_bufr_new_from_file(f)\n            if h is None: break\n            eccodes.codes_set(h, "unpack", 1)\n            sid = eccodes.codes_get(h, "stormIdentifier").strip()\n            if not sid.endswith(BASIN) or not eccodes.codes_is_defined(h, "timePeriod"):\n                eccodes.codes_release(h); continue          # other basins / analysis-only messages\n            nsub = eccodes.codes_get(h, "numberOfSubsets")\n            comp = eccodes.codes_get(h, "compressedData") == 1\n            t0 = pd.Timestamp(year=eccodes.codes_get(h, "year"), month=eccodes.codes_get(h, "month"),\n                              day=eccodes.codes_get(h, "day"), hour=eccodes.codes_get(h, "hour"))\n            tp = [0] + list(np.atleast_1d(eccodes.codes_get_array(h, "timePeriod")).astype(int))\n            nt = len(tp)\n            mem = _arr(h, "ensembleMemberNumber", nsub, comp) if label == "ENS" else np.zeros(nsub)\n            lat = np.array([_arr(h, f"#{2*(k+1)}#latitude",  nsub, comp) for k in range(nt)])   # storm centre\n            lon = np.array([_arr(h, f"#{2*(k+1)}#longitude", nsub, comp) for k in range(nt)])\n            p   = np.array([_arr(h, f"#{k+1}#pressureReducedToMeanSeaLevel", nsub, comp) for k in range(nt)])\n            w   = np.array([_arr(h, f"#{k+1}#windSpeedAt10M", nsub, comp) for k in range(nt)])\n            eccodes.codes_release(h)\n            for s in range(nsub):\n                df = pd.DataFrame(dict(t=[t0 + pd.Timedelta(hours=x) for x in tp],\n                                       lat=lat[:, s], lon=lon[:, s], w=w[:, s] * 1.94384, p=p[:, s] / 100))\n                df.loc[(df.lat.abs() > MISS) | (df.lon.abs() > MISS), ["lat", "lon"]] = np.nan\n                df.loc[df.w.abs() > MISS, "w"] = np.nan; df.loc[df.p.abs() > MISS, "p"] = np.nan\n                df = df.dropna(subset=["lat"]).reset_index(drop=True)\n                if df.empty: continue\n                m = int(mem[s])\n                kind = "HRES" if label == "HRES" else ("Control" if m == 51 else "Member")\n                tracks.append(dict(sid=sid, member=m, kind=kind, df=df))\n    return tracks\n\ndef pick(tracks):\n    """For each member keep the basin track that comes closest to REF in the first REF_HOURS\n       (ECMWF numbers new storms 70B, 71B… independently in each member)."""\n    best = {}\n    for tr in tracks:\n        if not tr["sid"].endswith(BASIN): continue\n        d = tr["df"]; d = d[d.t <= d.t.iloc[0].normalize() + pd.Timedelta(hours=REF_HOURS + 24)]\n        if d.empty: continue\n        dist = np.hypot(d.lat - REF_LAT, (d.lon - REF_LON) * np.cos(np.radians(REF_LAT))).min()\n        if dist <= REF_RADIUS_DEG and (tr["member"] not in best or dist < best[tr["member"]][0]):\n            best[tr["member"]] = (dist, tr)\n    return [v[1] for k, v in sorted(best.items())]\n\nENS  = pick(decode(ens_file, "ENS")) if ens_file else []\nHRES = pick(decode(hres_file, "HRES")) if hres_file else []\nHRES = HRES[0] if HRES else None\nCTRL = next((t for t in ENS if t["kind"] == "Control"), None)\nCTRL_RAW = CTRL\nHLAB = "HRES"\nif HRES and CTRL and len(HRES["df"]) == len(CTRL["df"]) and np.allclose(HRES["df"][["lat", "lon"]], CTRL["df"][["lat", "lon"]]):\n    CTRL = None; HLAB = "HRES (= ENS control)"   # since IFS 49r1 the control runs at HRES resolution\nprint(f"Members tracking the system: {sum(t[\'kind\']==\'Member\' for t in ENS)}/50"\n      f"{\' + control\' if any(t[\'kind\']==\'Control\' for t in ENS) else \'\'};  HRES: {\'yes (\'+HRES[\'sid\']+\')\' if HRES else \'no\'}")\nif not ENS: raise SystemExit("No ENS member matched — check REF_LAT/REF_LON/BASIN.")\n\n# ---------- ensemble statistics ----------\nallm = pd.concat([t["df"].assign(member=t["member"]) for t in ENS])\ng = allm.groupby("t")\nstat = pd.DataFrame(dict(n=g.size(), lat=g.lat.mean(), lon=g.lon.mean(),\n                         w=g.w.mean(), w10=g.w.quantile(.1), w90=g.w.quantile(.9), wmax=g.w.max(),\n                         p=g.p.mean(), p10=g.p.quantile(.1), p90=g.p.quantile(.9), pmin=g.p.min())).reset_index()\nmean = stat[stat.n >= MIN_MEMBERS]\nIC = f"{RUN} UTC {pd.Timestamp(DATE):%d %b %Y}"\n\n# ---------- text files in the same format as the other *_track.txt files ----------\ndef write_track(df, fn, note=None):\n    with open(fn, "w") as f:\n        if note: f.write(f"# {note}\\n")              # \'#\' lines are skipped by the plotting script\n        for r in df.itertuples():\n            f.write(f"1 {r.t:%Y%m%d%H} {r.lat:.1f} {r.lon:.1f} {r.w:.0f} {r.p:.0f}\\n")\n# Always exactly two track files:\n#   ECMWF_ENSMEAN_track.txt  – ensemble mean\n#   ECMWF_HRES_track.txt     – HRES; if HRES has no track for this system, the ENS control is used instead\nTXT_FILES = ["ECMWF_ENSMEAN_track.txt", "ECMWF_HRES_track.txt"]\nwrite_track(mean, "ecmwf_plots/ECMWF_ENSMEAN_track.txt",\n            f"ECMWF ENS mean, IC {DATE} {RUN}Z, positions where >= {MIN_MEMBERS} members track the system")\nif HRES:\n    write_track(HRES["df"], "ecmwf_plots/ECMWF_HRES_track.txt", f"ECMWF {HLAB}, IC {DATE} {RUN}Z")\nelif CTRL_RAW:\n    write_track(CTRL_RAW["df"], "ecmwf_plots/ECMWF_HRES_track.txt",\n                f"ECMWF ENS CONTROL (HRES track not available), IC {DATE} {RUN}Z")\n    print("HRES has no track for this system — ECMWF_HRES_track.txt contains the ENS control run.")\nelse:\n    print("WARNING: neither HRES nor control tracks this system — only the ensemble-mean file was written.")\n    TXT_FILES = TXT_FILES[:1]\nallm.to_csv("ecmwf_plots/ECMWF_ENS_all_members.csv", index=False)\n\n# ---------- plotting helpers ----------\ndef basemap(ax, ext):\n    ax.set_extent(ext, ccrs.PlateCarree())\n    ax.add_geometries(Reader("ne/ne_10m_land.shp").geometries(), ccrs.PlateCarree(), fc="#f3efe4", ec="none", zorder=0)\n    ax.set_facecolor("#dcebf5")\n    ax.add_geometries(Reader("ne/ne_10m_admin_1_states_provinces.shp").geometries(), ccrs.PlateCarree(), fc="none", ec="#aaaaaa", lw=0.4, zorder=1)\n    ax.add_geometries(Reader("ne/ne_10m_admin_0_countries_ind.shp").geometries(), ccrs.PlateCarree(), fc="none", ec="#444", lw=0.8, zorder=1)\n    ax.add_geometries(Reader("ne/ne_10m_coastline.shp").geometries(), ccrs.PlateCarree(), fc="none", ec="#333", lw=0.7, zorder=1)\n    gl = ax.gridlines(draw_labels=True, lw=0.4, color="gray", alpha=0.5, ls="--"); gl.top_labels = gl.right_labels = False\n    for c, (x, y) in {"Kolkata": (88.36, 22.57), "Bhubaneswar": (85.82, 20.30), "Visakhapatnam": (83.22, 17.69),\n                      "Chennai": (80.27, 13.08), "Machilipatnam": (81.13, 16.17), "Gopalpur": (84.90, 19.26)}.items():\n        if ext[0] < x < ext[1] and ext[2] < y < ext[3]:\n            ax.plot(x, y, "ks", ms=3, zorder=6); ax.text(x + 0.15, y + 0.1, c, fontsize=7.5, zorder=6)\ncats = [(17, 27, "D", "#e8f4e8"), (28, 33, "DD", "#fff8d6"), (34, 47, "CS", "#ffe4c4"),\n        (48, 63, "SCS", "#ffcccc"), (64, 89, "VSCS", "#f2c4e6"), (90, 119, "ESCS", "#d9b3ff"), (120, 200, "SuCS", "#c2c2f0")]\ndef tfmt(a):\n    a.xaxis.set_major_locator(mdates.HourLocator(byhour=[0])); a.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))\n    a.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\\n00Z")); a.grid(alpha=0.4)\n\nnmem = len(ENS)\n\n# ---------- E1. spaghetti map ----------\nfig = plt.figure(figsize=(10, 10)); ax = plt.axes(projection=ccrs.PlateCarree()); basemap(ax, MAP_EXT)\nfor t in ENS:\n    if t["kind"] == "Member": ax.plot(t["df"].lon, t["df"].lat, "-", color="#4a7fb5", lw=0.7, alpha=0.55, zorder=2)\nax.plot([], [], "-", color="#4a7fb5", lw=1, label=f"ENS members ({sum(t[\'kind\']==\'Member\' for t in ENS)}/50)")\nif CTRL: ax.plot(CTRL["df"].lon, CTRL["df"].lat, "--", color="#2ca02c", lw=2, label="Control", zorder=3)\nif HRES: ax.plot(HRES["df"].lon, HRES["df"].lat, "-", color="#d62728", lw=2.2, label=HLAB, zorder=3)\nax.plot(mean.lon, mean.lat, "-", color="k", lw=3, label=f"Ensemble mean (≥{MIN_MEMBERS} members)", zorder=4)\nfor r in mean[mean.t.dt.hour == 0].itertuples():\n    ax.plot(r.lon, r.lat, "o", color="k", ms=6, zorder=5)\n    ax.annotate(f"{r.t:%d/%H}", (r.lon, r.lat), xytext=(6, -10), textcoords="offset points", fontsize=8, fontweight="bold",\n                zorder=6, bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75))\nax.legend(loc="upper right", fontsize=9, framealpha=0.95)\nax.set_title(f"ECMWF ENS Tropical Cyclone Tracks — IC: {IC}", fontsize=13)\nplt.savefig("ecmwf_plots/E1_ENS_spaghetti.png", dpi=200, bbox_inches="tight"); plt.close()\n\n# ---------- E2. strike probability ----------\nlons = np.arange(MAP_EXT[0], MAP_EXT[1] + 0.01, 0.25); lats = np.arange(MAP_EXT[2], MAP_EXT[3] + 0.01, 0.25)\nLO, LA = np.meshgrid(lons, lats); hit = np.zeros(LO.shape)\nfor t in ENS:\n    d = t["df"].set_index("t")[["lat", "lon"]].resample("1h").interpolate().dropna()\n    m = np.zeros(LO.shape, bool)\n    for la, lo in zip(d.lat, d.lon):\n        m |= np.hypot((LA - la) * 111.2, (LO - lo) * 111.2 * np.cos(np.radians(la))) <= STRIKE_KM\n    hit += m\nprob = 100 * hit / 51\nfig = plt.figure(figsize=(10, 10)); ax = plt.axes(projection=ccrs.PlateCarree()); basemap(ax, MAP_EXT)\nlev = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]\ncs = ax.contourf(LO, LA, prob, levels=lev, cmap="YlOrRd", alpha=0.8, zorder=2, transform=ccrs.PlateCarree())\nax.contour(LO, LA, prob, levels=[10, 50, 90], colors="k", linewidths=0.6, zorder=3)\nax.plot(mean.lon, mean.lat, "-", color="k", lw=2, zorder=4, label="Ensemble mean")\nax.legend(loc="upper right", fontsize=9)\nplt.colorbar(cs, ax=ax, shrink=0.7, label=f"Probability (%) of centre passing within {STRIKE_KM} km")\nax.set_title(f"ECMWF ENS Strike Probability (whole forecast) — IC: {IC}", fontsize=13)\nplt.savefig("ecmwf_plots/E2_ENS_strike_probability.png", dpi=200, bbox_inches="tight"); plt.close()\n\n# ---------- E3 / E4. MSW and MSLP plumes ----------\ndef plume(var, fn, ylab, title):\n    fig, (ax, an) = plt.subplots(2, 1, figsize=(12, 7.5), sharex=True, gridspec_kw=dict(height_ratios=[5, 1], hspace=0.08))\n    if var == "w":\n        for lo, hi, n, c in cats:\n            top = np.nanmax(stat.wmax) + 8; ax.set_ylim(10, top)\n            if lo <= top:\n                ax.axhspan(lo, min(hi + 1, top), color=c, zorder=0)\n                ax.text(1.005, (lo + min(hi + 1, top)) / 2, n, transform=ax.get_yaxis_transform(), va="center", fontsize=9, fontweight="bold")\n    for t in ENS:\n        if t["kind"] == "Member": ax.plot(t["df"].t, t["df"][var], "-", color="#4a7fb5", lw=0.6, alpha=0.45)\n    ax.fill_between(mean.t, mean[var + "10"], mean[var + "90"], color="#4a7fb5", alpha=0.25, label="10–90 percentile")\n    ax.plot(mean.t, mean[var], "-", color="k", lw=2.8, label="Ensemble mean")\n    if CTRL: ax.plot(CTRL["df"].t, CTRL["df"][var], "--", color="#2ca02c", lw=2, label="Control")\n    if HRES: ax.plot(HRES["df"].t, HRES["df"][var], "-", color="#d62728", lw=2.2, label=HLAB)\n    if var == "p": ax.invert_yaxis()\n    ax.set_ylabel(ylab); ax.legend(loc="upper left", fontsize=9, ncol=2); tfmt(ax)\n    ax.set_title(f"{title} — IC: {IC}", fontsize=13)\n    an.bar(stat.t, stat.n, width=0.2, color="#888"); an.set_ylabel("Members"); an.set_ylim(0, 52); tfmt(an)\n    plt.savefig(fn, dpi=200, bbox_inches="tight"); plt.close()\nplume("w", "ecmwf_plots/E3_ENS_MSW_plume.png", "Maximum sustained wind (kt)", "ECMWF ENS Maximum Sustained Wind")\nplume("p", "ecmwf_plots/E4_ENS_MSLP_plume.png", "Central MSLP (hPa)", "ECMWF ENS Central Pressure")\n\n'
g = run_part("ECMWF ENS + HRES", setref(setv(setv(setv(SRC_ECM, "DATE", DATE), "RUN", RUN), "BASIN", BASIN)))
for f in glob.glob("ecmwf_plots/*"): shutil.copy(f, PKG)

# ---------- landfall statistics of all members ----------
from shapely.geometry import Point, box
from shapely.ops import unary_union
from shapely.prepared import prep
from cartopy.io.shapereader import Reader
import matplotlib.pyplot as plt
COAST = {"Chennai": (13.08, 80.27), "Nellore": (14.44, 79.99), "Ongole": (15.50, 80.05), "Bapatla": (15.90, 80.47),
         "Machilipatnam": (16.17, 81.13), "Narsapur": (16.43, 81.70), "Yanam": (16.73, 82.21), "Kakinada": (16.93, 82.22),
         "Visakhapatnam": (17.69, 83.22), "Bhimunipatnam": (17.89, 83.45), "Kalingapatnam": (18.34, 84.12),
         "Gopalpur": (19.26, 84.90), "Puri": (19.81, 85.83), "Paradip": (20.26, 86.67), "Balasore": (21.49, 86.93),
         "Digha": (21.62, 87.51), "Sagar Island": (21.65, 88.05), "Khepupara": (21.99, 90.23), "Chittagong": (22.34, 91.83),
         "Cox's Bazar": (21.43, 92.00), "Sittwe": (20.15, 92.90), "Karaikal": (10.92, 79.84), "Puducherry": (11.93, 79.83)}
def near(la, lo):
    k = min(COAST, key=lambda c: np.hypot(COAST[c][0] - la, (COAST[c][1] - lo) * np.cos(np.radians(la))))
    dy = (la - COAST[k][0]) * 111.2; dx = (lo - COAST[k][1]) * 111.2 * np.cos(np.radians(la)); d = np.hypot(dx, dy)
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return f"near {k}" if d < 10 else f"{d:.0f} km {dirs[int((np.degrees(np.arctan2(dx, dy)) % 360 + 11.25) // 22.5) % 16]} of {k}"
bb = box(60, 0, 100, 30)
LAND = prep(unary_union([x.intersection(bb) for x in Reader("ne/ne_10m_land.shp").geometries() if x.intersects(bb)]))
def landfall(d):
    d = d.sort_values("t").reset_index(drop=True); on = [LAND.contains(Point(x.lon, x.lat)) for x in d.itertuples()]
    for i in range(1, len(d)):
        if on[i] and not on[i - 1]:
            a, b = d.iloc[i - 1], d.iloc[i]; lo_, hi_ = 0.0, 1.0
            for _ in range(25):
                f = (lo_ + hi_) / 2
                if LAND.contains(Point(a.lon + f * (b.lon - a.lon), a.lat + f * (b.lat - a.lat))): hi_ = f
                else: lo_ = f
            f = hi_; ip = lambda x, y: x + f * (y - x)
            return dict(t=(a.t + (b.t - a.t) * f).round("h"), lat=ip(a.lat, b.lat), lon=ip(a.lon, b.lon), w=ip(a.w, b.w), p=ip(a.p, b.p))
    return None
try:
    allm = pd.read_csv("ecmwf_plots/ECMWF_ENS_all_members.csv", parse_dates=["t"])
    rd = lambda fn: pd.read_csv(fn, sep=r"\s+", comment="#", header=None, names=["i", "t", "lat", "lon", "w", "p"]).assign(
        t=lambda x: pd.to_datetime(x.t.astype(str), format="%Y%m%d%H"))
    mean_lf = landfall(rd("ecmwf_plots/ECMWF_ENSMEAN_track.txt")); hres_lf = landfall(rd("ecmwf_plots/ECMWF_HRES_track.txt"))
    hres = rd("ecmwf_plots/ECMWF_HRES_track.txt"); mean = rd("ecmwf_plots/ECMWF_ENSMEAN_track.txt")
    L = pd.DataFrame([dict(member=k, **l) for k, d in allm.groupby("member") if (l := landfall(d))])
    n_all = allm.member.nunique()
    bins = pd.cut(L.lat, [0, 15, 16, 17, 18, 19, 20, 30]).value_counts().sort_index()
    top = bins.idxmax(); n_top2 = int(pd.cut(L.lat, [top.left - 0.5, top.right + 0.5]).notna().sum())
    pk = mean.loc[mean.w.idxmax()]; hp = hres.loc[hres.w.idxmax()]
    def cat(w):
        for lo, hi, n in [(0, 16, "L"), (17, 27, "D"), (28, 33, "DD"), (34, 47, "CS"), (48, 63, "SCS"), (64, 89, "VSCS"), (90, 119, "ESCS"), (120, 999, "SuCS")]:
            if lo <= round(w) <= hi: return n
    q10, q50, q90 = L.w.quantile([.1, .5, .9]).round().astype(int)
    rows = [["Ensemble-mean peak", f"{pk.w:.0f} kt ({cat(pk.w)}), {pk.t:%HZ %d %b}, {pk.p:.0f} hPa"],
            ["Ensemble-mean landfall", f"{mean_lf['t']:%HZ %d %b}, {near(mean_lf['lat'], mean_lf['lon'])}\n{mean_lf['lat']:.1f}°N {mean_lf['lon']:.1f}°E, {mean_lf['w']:.0f} kt" if mean_lf else "no landfall"],
            ["HRES peak", f"{hp.w:.0f} kt ({cat(hp.w)}), {hp.t:%HZ %d %b}, {hp.p:.0f} hPa"],
            ["HRES landfall", f"{hres_lf['t']:%HZ %d %b}, {near(hres_lf['lat'], hres_lf['lon'])}, {hres_lf['w']:.0f} kt" if hres_lf else "no landfall"],
            ["Members making landfall", f"{len(L)} / {n_all}"],
            ["Median landfall latitude", f"{L.lat.median():.1f}°N  (50 % between {L.lat.quantile(.25):.1f} and {L.lat.quantile(.75):.1f}°N)"],
            ["Median landfall time", f"{L.t.median():%HZ %d %b}  (50 % between {L.t.quantile(.25):%HZ %d} and {L.t.quantile(.75):%HZ %d})"],
            ["Median MSW at landfall", f"{q50} kt ({cat(q50)});  10–90 %: {q10}–{q90} kt"],
            ["Most likely 1° latitude band", f"{top.left:.0f}–{top.right:.0f}°N: {int(bins.max())} members"]]
    L.to_csv(f"{PKG}/ECMWF_ENS_member_landfalls.csv", index=False)
    fig = plt.figure(figsize=(7.4, 7.6), dpi=150)
    fig.text(0.5, 0.955, "ECMWF Ensemble — landfall summary", ha="center", fontsize=16, fontweight="bold", color="#1f3b63")
    fig.text(0.5, 0.92, f"IC {IC_TEXT}  ·  {n_all} members", ha="center", fontsize=11)
    ax = fig.add_axes([0.03, 0.20, 0.94, 0.68]); ax.axis("off")
    tb = ax.table(cellText=rows, cellLoc="center", bbox=[0, 0, 1, 1], colWidths=[0.34, 0.66])
    tb.auto_set_font_size(False); tb.set_fontsize(9.8)
    for (i, j), c in tb.get_celld().items():
        c.set_edgecolor("#bbb"); c.set_facecolor("#eef2f7" if j == 0 else "#fff6cc")
        if j == 0: c.get_text().set_fontweight("bold")
    lat_bins = "  ".join(f"{iv.left:.0f}–{iv.right:.0f}°N: {n}" for iv, n in bins.items() if n)
    fig.text(0.5, 0.13, "Members by landfall latitude:\n" + lat_bins, ha="center", fontsize=8.8, color="#333")
    fig.text(0.5, 0.05, "Source: ECMWF open data (CC BY 4.0); own analysis", ha="center", fontsize=8.5, style="italic", color="#555")
    plt.savefig(f"{PKG}/ECMWF_ENS_landfall_summary.png", dpi=150, bbox_inches="tight", pad_inches=0.25, facecolor="white"); plt.close()
    print("\n" + "\n".join(f"  {a:30s} {b}" for a, b in rows))
    STATUS["Landfall statistics"] = "OK"
except Exception as e:
    STATUS["Landfall statistics"] = f"FAILED: {e}"; traceback.print_exc(limit=2)

print("\n" + "=" * 70 + "\n  SUMMARY\n" + "=" * 70)
for k, v in STATUS.items(): print(f"  {k:28s} {v}")
shutil.make_archive(f"ECMWF_ENS_package_{IC}", "zip", PKG)
if IN_COLAB:
    from IPython.display import Image, display
    for p in ["E1_ENS_spaghetti.png", "E2_ENS_strike_probability.png", "ECMWF_ENS_landfall_summary.png"]:
        if os.path.exists(f"{PKG}/{p}"): display(Image(f"{PKG}/{p}", width=900))
    files.download(f"ECMWF_ENS_package_{IC}.zip")
