#!/usr/bin/env python3
"""
verif_runner.py - observed district rainfall for the Verification tab, from IMD's real-time gridded rainfall
(0.25 deg, IMD Pune, provisional; downloaded with the imdlib package, no login needed).

IMD's rainfall of date D is the 24 h ending 08:30 IST (03 UTC) of D+1, i.e. 03 UTC D -> 03 UTC D+1.
For every district the value is the MAXIMUM of the IMD grid-point values inside it (the same rule and the same
district/grid-point matching as the forecasts in district_runner.py).

Output (in SITE, default ./site):  verif/obs_YYYYMMDD.json  {"date", "window", "made", "max": [... per district]}
                                   verif/manifest.json      {"days": ["YYYY-MM-DD", ...] newest first}
The last LOOKBACK days are (re)fetched each time, as IMD's provisional values can still change; KEEP_DAYS are kept.
Writes work/district_changed when something new was written.  Needs: imdlib, numpy, matplotlib, district_runner.py
"""
import datetime as dt, glob, json, os, types
import numpy as np
import district_runner as dr

SITE = dr.SITE
OUT = os.path.join(SITE, "verif")
WORK = dr.WORK
KEEP_DAYS = int(os.environ.get("VERIF_KEEP_DAYS", "10"))
LOOKBACK = int(os.environ.get("VERIF_LOOKBACK", "3"))


def observed(day):
    """IMD real-time gridded rainfall of `day` -> lat, lon, values (1-D, valid points only)."""
    import imdlib as imd
    d = day.strftime("%Y-%m-%d")
    folder = os.path.join(WORK, "imdrt")
    for f in glob.glob(os.path.join(folder, "rain_*%s*.grd" % day.strftime("%y_%m_%d"))):
        os.remove(f)                                    # always fetch afresh (provisional data may be revised)
    x = imd.get_real_data("rain", d, d, file_dir=folder).get_xarray()["rain"]
    v = np.asarray(x.values, dtype=np.float64).reshape(x.shape[-2:]) if x.ndim == 3 else np.asarray(x.values, np.float64)
    names = list(x.dims)[-2:]                          # (lat, lon) or (lon, lat)
    lat = np.asarray(x["lat"].values, np.float64); lon = np.asarray(x["lon"].values, np.float64)
    if names == ["lat", "lon"]:
        LA, LO = np.meshgrid(lat, lon, indexing="ij")
    else:
        LO, LA = np.meshgrid(lon, lat, indexing="ij")
    ok = np.isfinite(v) & (v >= 0) & (v < 2000)
    if ok.sum() < 1000:
        raise RuntimeError("too few valid grid points (%d)" % ok.sum())
    return LA[ok], LO[ok], v[ok]


def main():
    os.makedirs(OUT, exist_ok=True); os.makedirs(WORK, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    last = (now - dt.timedelta(hours=27)).date()      # day D is complete at 03 UTC of D+1
    D = dr.load_districts(); changed = 0
    for k in range(KEEP_DAYS):
        day = last - dt.timedelta(days=k)
        path = os.path.join(OUT, "obs_%s.json" % day.strftime("%Y%m%d"))
        if os.path.exists(path) and k >= LOOKBACK:
            continue
        try:
            lat, lon, val = observed(day)
        except Exception as e:
            print("  IMD gridded %s not available: %s: %s" % (day, type(e).__name__, str(e)[:150]), flush=True)
            continue
        F = types.SimpleNamespace(lat=lat, lon=lon, key="IMDOBS:%d" % len(lat))
        pts = dr.district_index(D, F)
        mx = [round(float(val[p].max()), 1) if len(p) else None for p in pts]
        new = {"date": day.strftime("%Y-%m-%d"),
               "window": [day.strftime("%Y-%m-%dT03:00"), (day + dt.timedelta(days=1)).strftime("%Y-%m-%dT03:00")],
               "src": "IMD real-time gridded rainfall 0.25 deg (IMD Pune), provisional", "max": mx}
        old = json.load(open(path)) if os.path.exists(path) else None
        if not old or old.get("max") != mx:
            new["made"] = now.strftime("%Y-%m-%d %H:%M UTC")
            json.dump(new, open(path, "w"), separators=(",", ":")); changed += 1
            print("  IMD gridded %s: written (max %.1f mm)" % (day, val.max()), flush=True)
    keep = {(last - dt.timedelta(days=k)).strftime("%Y%m%d") for k in range(KEEP_DAYS)}
    days = []
    for f in sorted(glob.glob(os.path.join(OUT, "obs_*.json")), reverse=True):
        tag = os.path.basename(f)[4:12]
        if tag not in keep:
            os.remove(f); continue
        days.append("%s-%s-%s" % (tag[:4], tag[4:6], tag[6:]))
    json.dump({"days": days, "updated": (now + dt.timedelta(hours=5.5)).strftime("%d %b %Y %H:%M IST")},
              open(os.path.join(OUT, "manifest.json"), "w"), separators=(",", ":"))
    if changed:
        open(os.path.join(WORK, "district_changed"), "w").close()
    print("verification: %d observed days; %d written now" % (len(days), changed))


if __name__ == "__main__":
    main()
