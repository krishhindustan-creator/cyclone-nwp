#!/usr/bin/env python3
"""
district_runner.py - district-wise 24-hour rainfall from ECMWF IFS 0.25 deg open data.

For every district of india_districts.geojson the value is the MAXIMUM of the forecast grid-point values
that lie inside the district (the grid point nearest to the district centroid is always included, so small
districts get a value too).  Day n = rainfall in the n-th 24 h after the run's initial condition (IC).

Output (in SITE, default ./site):
    district/YYYYMMDDHH.json   one file per 00/12 UTC run: {"ic", "made", "max": [[10 values], ...]} (same order as the geojson)
    district/manifest.json     {"runs": [{"ic": ...}, ...] newest first, "updated": ...}
The last KEEP_DAYS days of runs are kept.  Writes work/district_changed when something new was added.

Called from .github/workflows/nwp.yml; can also be run by hand:   python district_runner.py
Environment: FORCE=1 (redo runs already present), SITE (default ./site), KEEP_DAYS (default 7)
Needs: numpy, matplotlib, eccodes, ecmwf-opendata
"""
import datetime as dt, glob, json, os, sys, time
import numpy as np
from matplotlib.path import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.abspath(os.environ.get("SITE", os.path.join(HERE, "site")))
WORK = os.path.join(HERE, "work")
GEO = os.path.join(HERE, "india_districts.geojson")
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "7"))
FORCE = os.environ.get("FORCE", "") not in ("", "0", "false", "False")
DAYS = 10                 # forecast days (steps 24, 48 ... 240 h)
MIN_AGE_H = 5             # do not even try a run younger than this
OUT = os.path.join(SITE, "district")


# ------------------------------------------------------------------ ECMWF open data
def download(ic, target):
    """Fetch total precipitation (tp) at steps 24, 48 ... 240 h of the IFS HRES run `ic` into `target` (GRIB2)."""
    from ecmwf.opendata import Client
    try:
        client = Client(source="ecmwf", model="ifs", resol="0p25")
    except TypeError:
        client = Client(source="ecmwf")
    client.retrieve(date=int(ic.strftime("%Y%m%d")), time=ic.hour, stream="oper", type="fc", param="tp",
                    step=[24 * d for d in range(1, DAYS + 1)], target=target)


def read_tp(path):
    """GRIB file -> ({step_hours: 2-D array [nj, ni] of tp in metres}, grid-description dict)."""
    import eccodes as ec
    fields, grid = {}, None
    with open(path, "rb") as f:
        while True:
            gid = ec.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                if ec.codes_get(gid, "shortName") != "tp":
                    continue
                ni, nj = ec.codes_get(gid, "Ni"), ec.codes_get(gid, "Nj")
                g = dict(ni=ni, nj=nj,
                         lat0=ec.codes_get(gid, "latitudeOfFirstGridPointInDegrees"),
                         lon0=ec.codes_get(gid, "longitudeOfFirstGridPointInDegrees"),
                         di=ec.codes_get(gid, "iDirectionIncrementInDegrees"),
                         dj=ec.codes_get(gid, "jDirectionIncrementInDegrees"),
                         jpos=int(ec.codes_get(gid, "jScansPositively")), ineg=int(ec.codes_get(gid, "iScansNegatively")))
                fields[int(ec.codes_get(gid, "endStep"))] = np.asarray(ec.codes_get_values(gid)).reshape(nj, ni)
                grid = g
            finally:
                ec.codes_release(gid)
    return fields, grid


# ------------------------------------------------------------------ geometry
def load_districts():
    G = json.load(open(GEO, encoding="utf-8"))
    D = []
    for f in G["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        D.append([[np.array(r) for r in poly] for poly in polys])
    return D


def centroid(polys):
    best = max(polys, key=lambda p: abs(_area(p[0])))[0]
    x, y = best[:, 0], best[:, 1]
    cr = x[:-1] * y[1:] - x[1:] * y[:-1]
    a = cr.sum() / 2
    if abs(a) < 1e-12:
        return best.mean(0)
    return np.array([((x[:-1] + x[1:]) * cr).sum() / (6 * a), ((y[:-1] + y[1:]) * cr).sum() / (6 * a)])


def _area(r):
    return 0.5 * np.sum(r[:-1, 0] * r[1:, 1] - r[1:, 0] * r[:-1, 1])


def district_points(D, di, dj):
    """For every district: list of (lat, lon) grid points inside it + the grid point nearest to its centroid."""
    out = []
    for polys in D:
        pts = []
        for poly in polys:
            o = poly[0]
            x0, y0 = o.min(0); x1, y1 = o.max(0)
            lons = np.arange(np.ceil(x0 / di), np.floor(x1 / di) + 1) * di
            lats = np.arange(np.ceil(y0 / dj), np.floor(y1 / dj) + 1) * dj
            if len(lons) == 0 or len(lats) == 0:
                continue
            gx, gy = np.meshgrid(lons, lats)
            c = np.c_[gx.ravel(), gy.ravel()]
            m = Path(o).contains_points(c)
            for h in poly[1:]:
                m &= ~Path(h).contains_points(c)
            pts.extend((float(la), float(lo)) for lo, la in c[m])
        cx, cy = centroid(polys)
        pts.append((float(np.round(cy / dj) * dj), float(np.round(cx / di) * di)))      # nearest grid point to the centroid
        out.append(sorted(set(pts)))
    return out


def grid_index(lat, lon, g):
    lat = np.asarray(lat, float); lon = np.asarray(lon, float)
    j = (lat - g["lat0"]) / g["dj"] if g["jpos"] else (g["lat0"] - lat) / g["dj"]
    d = (g["lon0"] - lon) % 360 if g["ineg"] else (lon - g["lon0"]) % 360
    return np.rint(j).astype(int), np.rint(d / g["di"]).astype(int) % g["ni"]


# ------------------------------------------------------------------ one run
def daily_max(fields, g, pts):
    """fields: {step: tp[m]} -> list (one per district) of DAYS values (mm), maximum over the district's grid points."""
    need = [24 * d for d in range(1, DAYS + 1)]
    miss = [s for s in need if s not in fields]
    if miss:
        raise RuntimeError("steps missing in GRIB file: %s" % miss)
    cum = [np.zeros_like(fields[24])] + [fields[s] for s in need]
    day = [np.clip((cum[k + 1] - cum[k]) * 1000.0, 0, None) for k in range(DAYS)]         # mm per 24 h
    res = []
    for p in pts:
        la, lo = zip(*p)
        j, i = grid_index(la, lo, g)
        res.append([round(float(day[k][j, i].max()), 1) for k in range(DAYS)])
    return res


def run_ids(now):
    base = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 12) * 12)
    return [base - dt.timedelta(hours=12 * k) for k in range(int(KEEP_DAYS * 2))]


def main():
    os.makedirs(OUT, exist_ok=True); os.makedirs(WORK, exist_ok=True)
    now = dt.datetime.utcnow()
    todo = [ic for ic in run_ids(now)
            if now - ic >= dt.timedelta(hours=MIN_AGE_H) and (FORCE or not os.path.exists(os.path.join(OUT, ic.strftime("%Y%m%d%H") + ".json")))]
    print("District rainfall: runs to try:", ", ".join(ic.strftime("%Y%m%d%H") for ic in todo) or "none", flush=True)
    D = pts = None; added = 0
    for ic in todo:
        tag = ic.strftime("%Y%m%d%H"); target = os.path.join(WORK, "tp_%s.grib2" % tag)
        try:
            t0 = time.time()
            download(ic, target)
            fields, grid = read_tp(target)
            if D is None:
                D = load_districts(); pts = district_points(D, grid["di"], grid["dj"])
                print("districts: %d, grid points used: %d" % (len(D), sum(len(p) for p in pts)), flush=True)
            res = daily_max(fields, grid, pts)
            json.dump({"ic": ic.strftime("%Y-%m-%dT%H:%M"), "made": now.strftime("%Y-%m-%d %H:%M UTC"),
                       "src": "ECMWF IFS 0.25 deg open data, tp; maximum over grid points inside each district",
                       "max": res}, open(os.path.join(OUT, tag + ".json"), "w"), separators=(",", ":"))
            added += 1
            print("  %s done (%.0f s)" % (tag, time.time() - t0), flush=True)
        except Exception as e:                 # run not released yet / not available: try again next time
            print("  %s not done: %s: %s" % (tag, type(e).__name__, str(e)[:200]), flush=True)
        finally:
            if os.path.exists(target):
                os.remove(target)
    # keep KEEP_DAYS days, write manifest (newest first)
    cut = now - dt.timedelta(days=KEEP_DAYS)
    runs = []
    for f in sorted(glob.glob(os.path.join(OUT, "??????????.json")), reverse=True):
        try:
            ic = dt.datetime.strptime(os.path.basename(f)[:10], "%Y%m%d%H")
        except ValueError:
            continue
        if ic < cut:
            os.remove(f); print("removed", os.path.basename(f)); continue
        runs.append({"ic": ic.strftime("%Y-%m-%dT%H:%M")})
    ist = now + dt.timedelta(hours=5, minutes=30)
    json.dump({"runs": runs, "updated": "%s IST" % ist.strftime("%d %b %Y %H:%M"), "keep_days": KEEP_DAYS},
              open(os.path.join(OUT, "manifest.json"), "w"), separators=(",", ":"))
    if added:
        open(os.path.join(WORK, "district_changed"), "w").close()
    print("manifest: %d runs; %d added now" % (len(runs), added))


if __name__ == "__main__":
    main()
