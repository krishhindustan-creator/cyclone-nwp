#!/usr/bin/env python3
"""
verif_runner.py - observed district rainfall for the Verification tab, from two sources, keyed by district_code:

  IMD gridded : IMD real-time gridded rainfall 0.25 deg (IMD Pune), via imdlib; district MAXIMUM and MEAN of the
                grid-point values inside each district (no login).
  IMD DRMS    : iRAINS District Rainfall Monitoring Scheme, via the fetchDistrictDataAPIexport API; the district
                AVERAGE rainfall IMD reports for each district (needs IRAINS_USER / IRAINS_PASS).

IMD's rainfall day runs 03 UTC (08:30 IST) -> 03 UTC of the next day (the IMD / WMO convention).

Output (in SITE, default ./site):
  verif/obs_YYYYMMDD.json   {"date","window","codes",
                             "gridded":{"max":{code:v},"mean":{code:v}}, "drms":{code:v}}
  verif/manifest.json       {"days":[...], ...}
Permanent archive (branch verif-archive, also copied to verif/archive/): one file per observed day with the observed
values and every model's forecast MAX and MEAN for the day's six forecasts, all keyed by district_code; plus
index.json and scores.csv (daily hits/misses/false-alarms per observed source, forecast value, model/MME, lead, threshold).

Environment: SITE, VERIF_KEEP_DAYS (default 10), VERIF_LOOKBACK (default 3), VERIF_ARCHIVE (default ./archive),
             IRAINS_USER / IRAINS_PASS (for the DRMS source; skipped if unset).
Needs: imdlib, numpy, district_runner.py
"""
import csv, datetime as dt, glob, json, os, shutil, ssl, types, urllib.error, urllib.request
import numpy as np
import district_runner as dr

SITE = dr.SITE
OUT = os.path.join(SITE, "verif")
WORK = dr.WORK
KEEP_DAYS = int(os.environ.get("VERIF_KEEP_DAYS", "10"))
LOOKBACK = int(os.environ.get("VERIF_LOOKBACK", "3"))
ARCH = os.path.abspath(os.environ.get("VERIF_ARCHIVE", os.path.join(dr.HERE, "archive")))
DIST = os.path.join(SITE, "district")
MODELS = list(dr.FETCH)
NAMES = {"IFS": "ECMWF IFS", "AIFS": "ECMWF AIFS", "GFS": "NCEP GFS", "AIGFS": "NOAA AIGFS", "ICON": "DWD ICON",
         "GDPS": "CMC GDPS", "ARPEGE": "Meteo-France ARPEGE", "IMDGFS": "IMD GFS", "NCUMG": "NCUM-G", "NCUMR": "NCUM-R"}
LEADS = [("1-12", 1, 12), ("1-00", 1, 0), ("2-12", 2, 12), ("2-00", 2, 0), ("3-12", 3, 12), ("3-00", 3, 0)]
THRESH = [64.5, 115.6, 204.5]
IRAINS_URL = "https://irainshydro.imd.gov.in:3000/api/v1/fetchDistrictDataAPIexport"


# ------------------------------------------------------------------ observed sources
def gridded(day):
    """IMD real-time gridded rainfall of `day` -> lat, lon, values (1-D, valid points only)."""
    import imdlib as imd
    d = day.strftime("%Y-%m-%d")
    folder = os.path.join(WORK, "imdrt")
    for f in glob.glob(os.path.join(folder, "rain_*%s*.grd" % day.strftime("%y_%m_%d"))):
        os.remove(f)                                    # always fetch afresh (provisional data may be revised)
    x = imd.get_real_data("rain", d, d, file_dir=folder).get_xarray()["rain"]
    v = np.asarray(x.values, dtype=np.float64).reshape(x.shape[-2:]) if x.ndim == 3 else np.asarray(x.values, np.float64)
    lat = np.asarray(x["lat"].values, np.float64); lon = np.asarray(x["lon"].values, np.float64)
    if list(x.dims)[-2:] == ["lat", "lon"]:
        LA, LO = np.meshgrid(lat, lon, indexing="ij")
    else:
        LO, LA = np.meshgrid(lon, lat, indexing="ij")
    ok = np.isfinite(v) & (v >= 0) & (v < 2000)
    if ok.sum() < 1000:
        raise RuntimeError("too few valid grid points (%d)" % ok.sum())
    return LA[ok], LO[ok], v[ok]


def drms(day):
    """iRAINS DRMS district rainfall of `day` -> {district_code(int): actual_rainfall(mm)}."""
    user, pw = os.environ.get("IRAINS_USER"), os.environ.get("IRAINS_PASS")
    if not (user and pw):
        raise RuntimeError("IRAINS_USER / IRAINS_PASS not set")
    d = day.strftime("%Y-%m-%d")
    body = json.dumps({"user": user, "pass": pw, "fromDate": d, "toDate": d}).encode()
    req = urllib.request.Request(IRAINS_URL, data=body, headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
        j = json.loads(r.read())
    if not j.get("success") or not j.get("data"):
        raise RuntimeError("DRMS: " + str(j.get("message"))[:120])
    out = {}
    for x in j["data"]:
        try:
            out[int(x["district_code"])] = round(float(x["actual_rainfall"]), 1)
        except (KeyError, TypeError, ValueError):
            pass
    if len(out) < 100:
        raise RuntimeError("DRMS returned only %d districts" % len(out))
    return out


# ------------------------------------------------------------------ observed file per day
def write_observed(day, D, codes, now):
    path = os.path.join(OUT, "obs_%s.json" % day.strftime("%Y%m%d"))
    rec = {"date": day.strftime("%Y-%m-%d"),
           "window": [day.strftime("%Y-%m-%dT03:00"), (day + dt.timedelta(days=1)).strftime("%Y-%m-%dT03:00")],
           "codes": codes}
    got = []
    try:                                                # IMD gridded: district max and mean
        lat, lon, val = gridded(day)
        F = types.SimpleNamespace(lat=lat, lon=lon, key="IMDOBS:%d" % len(lat))
        pts = dr.district_index(D, F)
        rec["gridded"] = {"max": {codes[i]: round(float(val[p].max()), 1) for i, p in enumerate(pts) if len(p)},
                          "mean": {codes[i]: round(float(val[p].mean()), 1) for i, p in enumerate(pts) if len(p)}}
        got.append("gridded(%d)" % len(rec["gridded"]["max"]))
    except Exception as e:
        print("  IMD gridded %s: %s: %s" % (day, type(e).__name__, str(e)[:150]), flush=True)
    try:                                                # IMD DRMS: district average from the API, matched by code
        dd = drms(day)
        rec["drms"] = {c: dd[c] for c in codes if c in dd}
        got.append("drms(%d)" % len(rec["drms"]))
    except Exception as e:
        print("  IMD DRMS %s: %s: %s" % (day, type(e).__name__, str(e)[:150]), flush=True)
    if "gridded" not in rec and "drms" not in rec:
        return False
    old = json.load(open(path)) if os.path.exists(path) else None
    if old and old.get("gridded") == rec.get("gridded") and old.get("drms") == rec.get("drms"):
        return False
    rec["made"] = now.strftime("%Y-%m-%d %H:%M UTC")
    json.dump(rec, open(path, "w"), separators=(",", ":"))
    print("  %s: %s" % (day, ", ".join(got) or "nothing"), flush=True)
    return True


# ------------------------------------------------------------------ forecasts for a day (by code, max and mean)
def ic_for(day, lead, hour):
    d = dt.datetime(day.year, day.month, day.day)
    return d - dt.timedelta(days=lead) + dt.timedelta(hours=12) if hour == 12 else d - dt.timedelta(days=lead - 1)


def forecasts_for(day):
    """{lead: {"ic":..., "max":{model:{code:v}}, "mean":{model:{code:v}}}} from the run files still on the site."""
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
        if (not j.get("verif") or not j.get("vwin") or len(j["vwin"]) <= k
                or j["vwin"][k] != day.strftime("%Y-%m-%dT03:00") or "codes" not in j):
            continue
        codes = j["codes"]
        mx, mn = {}, {}
        for m in MODELS:
            if m in j.get("verif", {}) and m in j.get("meanv", {}):
                a = {codes[i]: j["verif"][m][i][k] for i in range(len(codes)) if j["verif"][m][i][k] is not None}
                b = {codes[i]: j["meanv"][m][i][k] for i in range(len(codes)) if j["meanv"][m][i][k] is not None}
                if a:
                    mx[m] = a; mn[m] = b
        if mx:
            out[key] = {"ic": ic.strftime("%Y-%m-%dT%H:%M"), "max": mx, "mean": mn}
    return out


def mme_of(models_by_code, codes):
    """models_by_code: {model:{code:v}} -> ({code: mean}, {code: median}) over the models present per district."""
    mean, med = {}, {}
    for c in codes:
        a = sorted(mb[c] for mb in models_by_code.values() if c in mb)
        if not a:
            continue
        mean[c] = sum(a) / len(a)
        n = len(a); med[c] = a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2
    return mean, med


# ------------------------------------------------------------------ scoring for the CSV
def counts(obs, fc, t):
    h = m_ = fa = cn = 0
    for c, o in obs.items():
        x = fc.get(c)
        if x is None:
            continue
        O, X = o >= t, x >= t
        if O and X: h += 1
        elif O: m_ += 1
        elif X: fa += 1
        else: cn += 1
    return h, m_, fa, cn


def score_rows(rec):
    rows = []
    obs_sets = []
    if rec.get("gridded"):
        obs_sets.append(("gridded-max", rec["gridded"]["max"]))
        obs_sets.append(("gridded-mean", rec["gridded"]["mean"]))
    if rec.get("drms"):
        obs_sets.append(("drms", rec["drms"]))
    for key, f in sorted(rec.get("fc", {}).items()):
        lead, run = "Day " + key.split("-")[0], key.split("-")[1] + " UTC run"
        for fval in ("max", "mean"):
            mods = f.get(fval, {})
            if not mods:
                continue
            codes = sorted({c for mb in mods.values() for c in mb})
            mean, med = mme_of(mods, codes)
            srcs = [("MME median", med), ("MME mean", mean)] + [(NAMES.get(m, m), mods[m]) for m in MODELS if m in mods]
            for oname, obs in obs_sets:
                for fname, fc in srcs:
                    for t in THRESH:
                        h, m_, fa, cn = counts(obs, fc, t)
                        rows.append([rec["date"], oname, fval, lead, run, f["ic"], fname, t, h, m_, fa, cn])
    return rows


# ------------------------------------------------------------------ archive
def update_archive(days, now):
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
        rec = {"date": obs["date"], "window": obs["window"],
               "gridded": obs.get("gridded"), "drms": obs.get("drms"), "fc": fc,
               "note": "observed and forecasts keyed by district_code. gridded.max/mean = IMD gridded district max/mean; "
                       "drms = iRAINS district-average rainfall; fc[lead].max/mean[model][code] = forecast district "
                       "max/mean for the same 24 h (03-03 UTC). lead '1-12' = Day 1 from the 12 UTC run of the day before."}
        if old.get("gridded") == rec["gridded"] and old.get("drms") == rec["drms"] and old.get("fc") == rec["fc"]:
            continue
        rec["made"] = now.strftime("%Y-%m-%d %H:%M UTC")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(rec, open(path, "w"), separators=(",", ":")); changed.append(rec)
    all_days = sorted(os.path.basename(f)[:8] for f in glob.glob(os.path.join(ARCH, "????", "????????.json")))
    json.dump({"days": ["%s-%s-%s" % (d[:4], d[4:6], d[6:]) for d in all_days],
               "updated": now.strftime("%Y-%m-%d %H:%M UTC")},
              open(os.path.join(ARCH, "index.json"), "w"), separators=(",", ":"))
    head = ["date", "observed", "forecast_value", "lead", "run", "ic", "forecast", "threshold_mm",
            "hits", "misses", "false_alarms", "correct_negatives"]
    sc = os.path.join(ARCH, "scores.csv")
    if changed:                                           # rebuild the whole CSV from the stored day files
        rows = []
        for f in sorted(glob.glob(os.path.join(ARCH, "????", "????????.json"))):
            try:
                rows += [[str(x) for x in row] for row in score_rows(json.load(open(f)))]
            except Exception:
                pass
        rows.sort(key=lambda r: (r[0], r[1], r[2], r[3], r[4], r[6], float(r[7])))
        with open(sc, "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(head); w.writerows(rows)
    dst = os.path.join(OUT, "archive")
    if os.path.abspath(dst) != ARCH:
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(ARCH, dst, ignore=shutil.ignore_patterns(".git"))
    print("archive: %d observed days in total; %d updated now" % (len(all_days), len(changed)))
    return len(changed)


def main():
    os.makedirs(OUT, exist_ok=True); os.makedirs(WORK, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    last = (now - dt.timedelta(hours=27)).date()          # day D is complete at 03 UTC of D+1
    D, codes = dr.load_districts()
    changed = 0
    for k in range(KEEP_DAYS):
        day = last - dt.timedelta(days=k)
        path = os.path.join(OUT, "obs_%s.json" % day.strftime("%Y%m%d"))
        if os.path.exists(path) and k >= LOOKBACK:
            continue
        if write_observed(day, D, codes, now):
            changed += 1
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
        changed += update_archive([last - dt.timedelta(days=k) for k in range(KEEP_DAYS)], now)
    except Exception as e:
        print("archive update failed: %s: %s" % (type(e).__name__, e))
    if changed:
        open(os.path.join(WORK, "district_changed"), "w").close()
    print("verification: %d observed days; %d written now" % (len(days), changed))


if __name__ == "__main__":
    main()
