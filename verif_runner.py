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

Permanent archive (for monthly / seasonal verification): VERIF_ARCHIVE (default ./archive, in the workflow the
branch 'verif-archive') gets one file per observed day, YYYY/YYYYMMDD.json, holding the observed district values and
every model's district values for the six forecasts of that day (Day 1-3 from the 12 and 00 UTC runs), plus
index.json and scores.csv (daily hits / misses / false alarms per model, MME mean and median, lead and threshold).
Nothing is ever deleted from the archive; it is copied to the site as verif/archive/.
"""
import csv, datetime as dt, glob, io, json, os, shutil, types
import numpy as np
import district_runner as dr

SITE = dr.SITE
OUT = os.path.join(SITE, "verif")
WORK = dr.WORK
KEEP_DAYS = int(os.environ.get("VERIF_KEEP_DAYS", "10"))
LOOKBACK = int(os.environ.get("VERIF_LOOKBACK", "3"))
ARCH = os.path.abspath(os.environ.get("VERIF_ARCHIVE", os.path.join(dr.HERE, "archive")))
DIST = os.path.join(SITE, "district")
MODELS = list(dr.FETCH)                                   # IFS, AIFS, GFS, ... in the page's order
LEADS = [("1-12", 1, 12), ("1-00", 1, 0), ("2-12", 2, 12), ("2-00", 2, 0), ("3-12", 3, 12), ("3-00", 3, 0)]
THRESH = [64.5, 115.6, 204.5]
NAMES = {"IFS": "ECMWF IFS", "AIFS": "ECMWF AIFS", "GFS": "NCEP GFS", "AIGFS": "NOAA AIGFS", "ICON": "DWD ICON",
         "GDPS": "CMC GDPS", "ARPEGE": "Meteo-France ARPEGE", "IMDGFS": "IMD GFS", "NCUMG": "NCUM-G", "NCUMR": "NCUM-R"}


def ic_for(day, lead, hour):
    """Run whose verification window k = lead-1 covers 03 UTC day -> 03 UTC day+1."""
    d = dt.datetime(day.year, day.month, day.day)
    return d - dt.timedelta(days=lead) + dt.timedelta(hours=12) if hour == 12 else d - dt.timedelta(days=lead - 1)


def forecasts_for(day):
    """{lead key: {"ic": ..., "models": {model: [district values]}}} from the run files still on the site."""
    out = {}
    for key, lead, hour in LEADS:
        ic = ic_for(day, lead, hour)
        f = os.path.join(DIST, ic.strftime("%Y%m%d%H") + ".json")
        if not os.path.exists(f):
            continue
        try:
            j = json.load(open(f))
        except Exception:
            continue
        k = lead - 1
        if not j.get("verif") or not j.get("vwin") or len(j["vwin"]) <= k or j["vwin"][k] != day.strftime("%Y-%m-%dT03:00"):
            continue
        mods = {m: [r[k] for r in j["verif"][m]] for m in MODELS if m in j["verif"]}
        mods = {m: v for m, v in mods.items() if any(x is not None for x in v)}
        if mods:
            out[key] = {"ic": ic.strftime("%Y-%m-%dT%H:%M"), "models": mods}
    return out


def mme(models, n):
    mean, med = [], []
    for i in range(n):
        a = sorted(v[i] for v in models.values() if v[i] is not None)
        if not a:
            mean.append(None); med.append(None); continue
        mean.append(sum(a) / len(a))
        m = len(a); med.append(a[m // 2] if m % 2 else (a[m // 2 - 1] + a[m // 2]) / 2)
    return mean, med


def score_rows(rec):
    obs = rec["obs"]; rows = []
    for key, f in sorted(rec.get("fc", {}).items()):
        mean, med = mme(f["models"], len(obs))
        srcs = [("MME median", med), ("MME mean", mean)] + [(NAMES.get(m, m), f["models"][m]) for m in MODELS if m in f["models"]]
        for name, fc in srcs:
            for t in THRESH:
                h = m_ = fa = cn = 0
                for o, x in zip(obs, fc):
                    if o is None or x is None:
                        continue
                    O, X = o >= t, x >= t
                    if O and X: h += 1
                    elif O: m_ += 1
                    elif X: fa += 1
                    else: cn += 1
                rows.append([rec["date"], "Day " + key.split("-")[0], key.split("-")[1] + " UTC run", f["ic"], name, t, h, m_, fa, cn])
    return rows


def update_archive(days):
    """Write / refresh the archive record of each observed day; rebuild index and scores for changed days."""
    os.makedirs(ARCH, exist_ok=True)
    changed = []
    for day in days:
        of = os.path.join(OUT, "obs_%s.json" % day.strftime("%Y%m%d"))
        if not os.path.exists(of):
            continue
        obs = json.load(open(of))
        path = os.path.join(ARCH, day.strftime("%Y"), day.strftime("%Y%m%d") + ".json")
        old = json.load(open(path)) if os.path.exists(path) else {}
        fc = dict(old.get("fc", {})); fc.update(forecasts_for(day))      # keep forecasts whose run files are gone
        rec = {"date": obs["date"], "window": obs["window"], "obs_src": obs.get("src", ""), "obs": obs["max"], "fc": fc,
               "note": "obs = observed district maximum (mm, IMD gridded); fc[lead]['models'][model] = forecast district "
                       "maximum for the same 24 h (03-03 UTC); lead '1-12' = Day 1 from the 12 UTC run of the day before, etc."}
        if old.get("obs") == rec["obs"] and old.get("fc") == rec["fc"]:
            continue
        rec["made"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(rec, open(path, "w"), separators=(",", ":")); changed.append(rec)
    all_days = sorted(os.path.basename(f)[:8] for f in glob.glob(os.path.join(ARCH, "????", "????????.json")))
    json.dump({"days": ["%s-%s-%s" % (d[:4], d[4:6], d[6:]) for d in all_days],
               "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
              open(os.path.join(ARCH, "index.json"), "w"), separators=(",", ":"))
    head = ["date", "lead", "run", "ic", "forecast", "threshold_mm", "hits", "misses", "false_alarms", "correct_negatives"]
    sc = os.path.join(ARCH, "scores.csv")
    rows = []
    if os.path.exists(sc):
        dates = {r["date"] for r in changed}
        with open(sc, newline="") as fh:
            rows = [r for r in csv.reader(fh)][1:]
        rows = [r for r in rows if r[0] not in dates]
    for r in changed:
        rows += [[str(x) for x in row] for row in score_rows(r)]
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[4], float(r[5])))
    with open(sc, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(head); w.writerows(rows)
    dst = os.path.join(OUT, "archive")                    # copy to the site
    if os.path.abspath(dst) != ARCH:
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(ARCH, dst, ignore=shutil.ignore_patterns(".git"))
    print("archive: %d observed days in total; %d updated now" % (len(all_days), len(changed)))
    return len(changed)


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
    try:
        changed += update_archive([last - dt.timedelta(days=k) for k in range(KEEP_DAYS)])
    except Exception as e:
        print("archive update failed: %s: %s" % (type(e).__name__, e))
    if changed:
        open(os.path.join(WORK, "district_changed"), "w").close()
    print("verification: %d observed days; %d written now" % (len(days), changed))


if __name__ == "__main__":
    main()
