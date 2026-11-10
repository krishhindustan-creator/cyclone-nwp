#!/usr/bin/env python3
"""
subdiv_temp_runner.py - subdivision-wise maximum and minimum temperature (2 m) from several global NWP models.

For each IMD meteorological subdivision (the subdivision of every district in india_districts.geojson) and model:
    Tmax of date D = HIGHEST 2-m temperature at any grid point of the subdivision during 00-12 UTC of D (05:30-17:30 IST)
    Tmin of date D = LOWEST  2-m temperature at any grid point of the subdivision during 12 UTC of D-1 to 03 UTC of D
                     (17:30 IST of the previous evening to 08:30 IST, IMD's minimum-temperature period)
The grid points of a subdivision are those of its districts (as in district_runner.py: points inside each district plus
the point nearest to its centroid).  The MME (simple mean of the models) is computed by the web page.

Models (00 and 12 UTC runs only) and the fields used:
    IFS    ECMWF IFS 0.25 deg     mx2t3/mn2t3 (3-h max/min) to 144 h, mx2t6/mn2t6 after
    AIFS   ECMWF AIFS 0.25 deg    2t every 6 h (instantaneous: Tmax may be a little low)
    GFS    NCEP GFS 0.25 deg      TMAX/TMIN (3-h to 120 h, 6-h after)
    AIGFS  NOAA AIGFS 0.25 deg    2t every 6 h (instantaneous)
    ICON   DWD ICON ~13 km        TMAX_2M/TMIN_2M (6-h), to 180 h
    GDPS   CMC GDPS 0.15 deg      2t every 3 h (instantaneous)
    ARPEGE Meteo-France 0.25 deg  2t every hour, to 102 h
    (IMD GFS / NCUM: not yet - IMD's dr_krishna folder holds rainfall files only)

Output (in SITE, default ./site):
    subdiv/YYYYMMDDHH.json  {"ic","made","dates":[11 x YYYY-MM-DD: IC date .. +10],"sd":[names],"code":[codes],
                             "tmax":{model:[[11 values] per subdivision]},"tmin":{...}}   (deg C, null = not covered)
    subdiv/manifest.json    {"runs":[{"ic","models"}...] newest first, "updated"}
Environment as district_runner.py: FORCE, SITE, KEEP_DAYS, TRY_HOURS, RUN_ID, TMODELS (default all 7 above).
"""
import bz2, datetime as dt, glob, json, os, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import district_runner as R

SITE, WORK = R.SITE, R.WORK
OUT = os.path.join(SITE, "subdiv")
KEEP_DAYS, TRY_HOURS, FORCE, RUN_ID = R.KEEP_DAYS, R.TRY_HOURS, R.FORCE, R.RUN_ID
TMODELS = [m.strip().upper() for m in os.environ.get("TMODELS", "IFS,AIFS,GFS,AIGFS,ICON,GDPS,ARPEGE").split(",") if m.strip()]
NDATES = 11
BOX = R.BOX


class TField:
    """2-m temperature output over the India box: lat, lon and a list of (start_h, end_h, kind, values degC);
    kind 'max' / 'min' = extreme over start..end, 'inst' = value at end (start = end)."""
    def __init__(self, lat, lon, key):
        self.sel = (lat >= BOX[0]) & (lat <= BOX[1]) & (lon >= BOX[2]) & (lon <= BOX[3])
        self.lat, self.lon, self.key, self.f = lat[self.sel], lon[self.sel], key, []

    def add(self, start, end, kind, values):
        v = np.asarray(values, dtype=np.float64)[self.sel]
        if np.nanmean(v) > 150:                      # Kelvin -> deg C
            v = v - 273.15
        self.f.append((int(start), int(end), kind, v.astype(np.float32)))


def tfield_regular(msgs, key, kind_of):
    lat, lon = R.regular_latlon(msgs[0])
    F = TField(lat, lon, key)
    for m in msgs:
        k = kind_of(m)
        if k:
            F.add(m["start"], m["end"], k, m["values"])
    return F


# ------------------------------------------------------------------ the models
def _ecmwf(model, ic, req):
    from ecmwf.opendata import Client
    import contextlib, io
    msgs = []
    for params, steps in req:
        target = os.path.join(WORK, "t_%s_%s.grib2" % (model, ic.strftime("%Y%m%d%H")))
        try:
            try:
                client = Client(source="ecmwf", model=model, resol="0p25")
            except TypeError:
                client = Client(source="ecmwf")
            with contextlib.redirect_stderr(io.StringIO()):
                client.retrieve(date=int(ic.strftime("%Y%m%d")), time=ic.hour, stream="oper", type="fc",
                                param=params, step=steps, target=target)
            msgs += R.read_msgs(target)
        finally:
            if os.path.exists(target):
                os.remove(target)
    return msgs


def get_ifs(ic):
    msgs = []
    for params, steps in (([("mx2t3", "max"), ("mn2t3", "min")], list(range(3, 145, 3))),
                          ([("mx2t6", "max"), ("mn2t6", "min")], list(range(150, 241, 6)))):
        for p, kind in params:
            for m in _ecmwf("ifs", ic, [([p], steps)]):
                m["kind"] = kind; msgs.append(m)
    return tfield_regular(msgs, "IFS", lambda m: m["kind"])


def get_aifs(ic):
    msgs = _ecmwf("aifs-single", ic, [(["2t"], list(range(0, 241, 6)))])
    for m in msgs:
        m["start"] = m["end"]
    return tfield_regular(msgs, "AIFS", lambda m: "inst")


def get_gfs(ic):
    ymd, hh = R.ymd_hh(ic)
    msgs = []
    for s in (list(range(3, 121, 3)) + list(range(126, 241, 6)))[::-1]:     # last step first: fails fast
        url = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.%s/%s/atmos/gfs.t%sz.pgrb2.0p25.f%03d" % (ymd, hh, hh, s)
        lines = R.http_get(url + ".idx").decode().strip().splitlines()
        for var, kind in (("TMAX", "max"), ("TMIN", "min")):
            for k, ln in enumerate(lines):
                if (":%s:2 m above ground:" % var) in ln:
                    a = int(ln.split(":")[1]); b = int(lines[k + 1].split(":")[1]) - 1
                    for m in R.read_msgs(R.http_get(url, "%d-%d" % (a, b))):
                        m["kind"] = kind; msgs.append(m)
                    break
            else:
                raise RuntimeError("%s not in f%03d" % (var, s))
    return tfield_regular(msgs, "GFS", lambda m: m["kind"])


def get_aigfs(ic):
    ymd, hh = R.ymd_hh(ic)
    msgs = []
    for s in list(range(0, 241, 6))[::-1]:
        url = ("https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com/aigfs.%s/%s/model/atmos/grib2/aigfs.t%sz.sfc.f%03d.grib2"
               % (ymd, hh, hh, s))
        try:
            msgs += R.read_msgs(R.http_get(url, R.idx_range(url, r":TMP:2 m above ground:")))
        except RuntimeError:
            if s == 0:                               # no analysis file: fine
                continue
            raise
    for m in msgs:
        m["start"] = m["end"]
    return tfield_regular(msgs, "AIGFS", lambda m: "inst")


_icon_grid = {}


def get_icon(ic):
    ymd, hh = R.ymd_hh(ic); tag = ymd + hh
    if "grid" not in _icon_grid:
        ll = []
        for v in ("clat", "clon"):
            raw = bz2.decompress(R.http_get(R.ICON_URL % (hh, v, "time-invariant", tag, "_" + v.upper())))
            ll.append(R.read_msgs(raw)[0]["values"])
        _icon_grid["grid"] = (ll[0], (ll[1] + 180.0) % 360.0 - 180.0)
    lat, lon = _icon_grid["grid"]
    F = TField(lat, lon, "ICON")
    for s in list(range(6, 181, 6))[::-1]:
        for v, kind in (("tmax_2m", "max"), ("tmin_2m", "min")):
            raw = bz2.decompress(R.http_get(R.ICON_URL % (hh, v, "single-level", tag, "_%03d_%s" % (s, v.upper()))))
            m = R.read_msgs(raw)[0]
            if len(m["values"]) != len(lat):
                raise RuntimeError("ICON grid size changed")
            F.add(m["start"], m["end"], kind, m["values"])
    return F


def get_gdps(ic):
    ymd, hh = R.ymd_hh(ic)
    msgs = []
    for s in list(range(0, 241, 3))[::-1]:
        url = ("https://dd.weather.gc.ca/%s/WXO-DD/model_gdps/15km/%s/%03d/%sT%sZ_MSC_GDPS_AirTemp_AGL-2m_LatLon0.15_PT%03dH.grib2"
               % (ymd, hh, s, ymd, hh, s))
        m = R.read_msgs(R.http_get(url))[0]
        m["start"] = m["end"] = s
        msgs.append(m)
    return tfield_regular(msgs, "GDPS", lambda m: "inst")


def get_arpege(ic):
    iso = ic.strftime("%Y-%m-%dT%H:00:00Z")
    msgs = []
    for pkg, _ in R.ARPEGE_PKG[::-1]:
        got = R.stream_pick(R.ARPEGE_URL % (iso, pkg, iso), lambda sn, es: sn == "2t", 10 ** 6)
        if not got:
            raise RuntimeError("no 2t in package %s" % pkg)
        for g in got:
            for m in R.read_msgs(g):
                m["start"] = m["end"]
                msgs.append(m)
    return tfield_regular(msgs, "ARPEGE", lambda m: "inst")


FETCH = {"IFS": get_ifs, "AIFS": get_aifs, "GFS": get_gfs, "AIGFS": get_aigfs, "ICON": get_icon, "GDPS": get_gdps,
         "ARPEGE": get_arpege}
INFO = {"IFS": "ECMWF IFS 0.25 deg: 3-h / 6-h max and min 2-m temperature", "AIFS": "ECMWF AIFS 0.25 deg: 2-m temperature every 6 h",
        "GFS": "NCEP GFS 0.25 deg: TMAX / TMIN", "AIGFS": "NOAA AIGFS 0.25 deg: 2-m temperature every 6 h",
        "ICON": "DWD ICON ~13 km: 6-h TMAX_2M / TMIN_2M, to day 7", "GDPS": "CMC GDPS 0.15 deg: 2-m temperature every 3 h",
        "ARPEGE": "Meteo-France ARPEGE 0.25 deg: hourly 2-m temperature, to 102 h"}


# ------------------------------------------------------------------ daily extremes over the date windows
def daily_extremes(F, ic):
    """-> (tmax, tmin): lists of NDATES grid fields (or None) for the dates IC date + 0..10.
    A field is used when it lies inside the window (+3 h of slack after 12 UTC for Tmax, after 03 UTC for Tmin, so that
    6-h max/min intervals ending at 06 UTC still count; the diurnal cycle makes this slack harmless).  The window
    must be covered from its start (within 3 h) to 12 UTC (Tmax) / 00 UTC (Tmin)."""
    tmax, tmin = [], []
    for k in range(NDATES):
        d0 = 24 * k - ic.hour                        # 00 UTC of date k, in forecast hours
        for out, ws, need, last, kinds, red in ((tmax, d0, d0 + 12, d0 + 15, ("max", "inst"), np.maximum),
                                                (tmin, d0 - 12, d0, d0 + 6, ("min", "inst"), np.minimum)):
            use = [f for f in F.f if f[2] in kinds and f[0] >= ws and f[1] <= last]
            if ws < 0 or not use or min(f[0] for f in use) > ws + 3 or max(f[1] for f in use) < need:
                out.append(None); continue
            acc = use[0][3].copy()
            for f in use[1:]:
                acc = red(acc, f[3])
            out.append(acc)
    return tmax, tmin


def subdivision_points(F):
    D, _ = R.load_districts()
    pts = R.district_index(D, F)
    G = json.load(open(R.GEO, encoding="utf-8"))
    names, codes, idx = [], [], {}
    for f, p in zip(G["features"], pts):
        q = f["properties"]
        key = q.get("subdivision") or q.get("sd")
        if key not in idx:
            idx[key] = []; names.append(key); codes.append(q.get("subdivision_code"))
        idx[key].append(p)
    order = sorted(range(len(names)), key=lambda i: (codes[i] is None, codes[i] or 0))
    names = [names[i] for i in order]; codes = [codes[i] for i in order]
    return names, codes, [np.unique(np.concatenate(idx[n])) for n in names]


def main():
    os.makedirs(OUT, exist_ok=True); os.makedirs(WORK, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if RUN_ID:
        ics = [dt.datetime.strptime(RUN_ID[:10], "%Y%m%d%H")]
    else:
        ics = [ic for ic in R.run_ids(now) if dt.timedelta(hours=R.MIN_AGE_H) <= now - ic <= dt.timedelta(hours=TRY_HOURS)]
    print("Subdivision temperature: models %s; runs to check: %s" % (",".join(TMODELS), ", ".join(i.strftime("%Y%m%d%H") for i in ics)),
          flush=True)
    added = 0
    for ic in ics:
        tag = ic.strftime("%Y%m%d%H"); path = os.path.join(OUT, tag + ".json")
        run = None
        if not FORCE:
            try:
                run = json.load(open(path))
            except Exception:
                run = None
        run = run or {"ic": ic.strftime("%Y-%m-%dT%H:%M"), "tmax": {}, "tmin": {}}
        todo = [m for m in TMODELS if m not in run["tmax"]]
        changed = False
        for name in todo:
            t0 = time.time()
            try:
                F = FETCH[name](ic)
                names, codes, pts = subdivision_points(F)
                tx, tn = daily_extremes(F, ic)
                run["tmax"][name] = [[None if d is None else round(float(d[p].max()), 1) for d in tx] for p in pts]
                run["tmin"][name] = [[None if d is None else round(float(d[p].min()), 1) for d in tn] for p in pts]
                run["sd"], run["code"] = names, codes
                changed = True
                print("  %s %-6s done (%.0f s, %d fields)" % (tag, name, time.time() - t0, len(F.f)), flush=True)
            except Exception as e:
                print("  %s %-6s not done: %s: %s" % (tag, name, type(e).__name__, str(e)[:200]), flush=True)
        if changed:
            order = [m for m in FETCH if m in run["tmax"]]
            run["tmax"] = {m: run["tmax"][m] for m in order}
            run["tmin"] = {m: run["tmin"][m] for m in order}
            run["dates"] = [(ic + dt.timedelta(days=k)).strftime("%Y-%m-%d") for k in range(NDATES)]
            run["made"] = now.strftime("%Y-%m-%d %H:%M UTC")
            run["src"] = {m: INFO[m] for m in order}
            run["note"] = ("tmax = highest 2-m temperature at any grid point of the subdivision, 00-12 UTC of the date; "
                           "tmin = lowest, 12 UTC of the previous day to 03 UTC of the date (deg C).")
            json.dump(run, open(path, "w"), separators=(",", ":"))
            added += 1
    cut = now - dt.timedelta(days=KEEP_DAYS)
    runs = []
    for f in sorted(glob.glob(os.path.join(OUT, "??????????.json")), reverse=True):
        try:
            ic = dt.datetime.strptime(os.path.basename(f)[:10], "%Y%m%d%H")
        except ValueError:
            continue
        if ic < cut:
            os.remove(f); continue
        try:
            j = json.load(open(f))
        except Exception:
            continue
        runs.append({"ic": ic.strftime("%Y-%m-%dT%H:%M"), "models": list(j.get("tmax", {}))})
    ist = now + dt.timedelta(hours=5, minutes=30)
    json.dump({"runs": runs, "updated": "%s IST" % ist.strftime("%d %b %Y %H:%M"), "models": TMODELS},
              open(os.path.join(OUT, "manifest.json"), "w"), separators=(",", ":"))
    if added:
        open(os.path.join(WORK, "district_changed"), "w").close()      # tells the workflow to publish
    print("subdiv manifest: %d runs; %d run files updated now" % (len(runs), added))


if __name__ == "__main__":
    main()
