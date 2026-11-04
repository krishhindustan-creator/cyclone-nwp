#!/usr/bin/env python3
"""
ens_district_runner.py - Ensemble District R/F Probability from ECMWF ENS, NOAA GEFS and ECCC GEPS (00/12 UTC runs).
Each ensemble is stored separately (ensdistrict/YYYYMMDDHH_ECMWF.json, _GEFS.json, _GEPS.json) with every member's
district value, so the web page can pool any selection of ensembles (every member counts equally).
Choose ensembles with ENSEMBLES (default "ECMWF,GEFS,GEPS").

ECMWF open data has total precipitation for the 50 perturbed ENS members (0.25 deg); the control is not in it.
For every member, district and day the value is the MAXIMUM of the member's grid-point 24-h rainfall inside the
district (the same rule as the MME page, using the same district/grid-point matching from district_runner.py).
From the 50 values per district and day the file keeps:
    q    : min, 10th, 25th, 50th (median), 75th, 90th percentile and max of the members (mm)
    mean : ensemble mean (mm)
    cnt  : number of members at or above 0.1, 2.5, 15.6, 64.5, 115.6 and 204.5 mm (IMD category limits)
           -> probability of each category or more = cnt / members
Output (in SITE, default ./site):
    ensdistrict/YYYYMMDDHH.json and ensdistrict/manifest.json (last KEEP_DAYS days)
Writes work/district_changed when a new run was added (so the workflow publishes).
Environment: FORCE=1, RUN_ID=YYYYMMDDHH, SITE, KEEP_DAYS (default 7), TRY_HOURS (default 48), ENS_DAYS (default 10)
Needs: district_runner.py next to it, numpy, matplotlib, eccodes, ecmwf-opendata
"""
import contextlib, datetime as dt, glob, io, json, os, sys, time, types
os.environ.setdefault("TQDM_DISABLE", "1")          # no download progress bars in the log
import numpy as np
import district_runner as dr

SITE = dr.SITE
OUT = os.path.join(SITE, "ensdistrict")
WORK = dr.WORK
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "7"))
TRY_HOURS = float(os.environ.get("TRY_HOURS", "48"))
FORCE = os.environ.get("FORCE", "") not in ("", "0", "false", "False")
RUN_ID = os.environ.get("RUN_ID", "").strip()
DAYS = int(os.environ.get("ENS_DAYS", "10"))
MIN_AGE_H = 4.5                                       # GEFS and GEPS are out about 5 h after the IC, ECMWF ENS about 7-8 h
LIMITS = [0.1, 2.5, 15.6, 64.5, 115.6, 204.5]         # IMD 24-h category lower limits (mm)
QUANT = [0, 10, 25, 50, 75, 90, 100]


def fetch_members(ic):
    """-> lat, lon (India box) and array (members, DAYS, points) of 24-h rainfall in mm."""
    from ecmwf.opendata import Client
    tag = ic.strftime("%Y%m%d%H")
    cum, sel, lat, lon, members = {}, None, None, None, None
    for d in range(DAYS, 0, -1):                       # last step first: fails fast while the run is still coming in
        step = 24 * d
        target = os.path.join(WORK, "ens_%s_%03d.grib2" % (tag, step))
        err = None
        for src in ("ecmwf", "aws", "azure"):
            try:
                with contextlib.redirect_stderr(io.StringIO()):     # keep download progress bars out of the log
                    Client(source=src, model="ifs", resol="0p25").retrieve(
                        date=int(ic.strftime("%Y%m%d")), time=ic.hour, stream="enfo", type="pf", param="tp",
                        step=step, target=target)
                err = None; break
            except Exception as e:
                err = e
        if err:
            raise err
        try:
            msgs = read_with_number(target)
        finally:
            if os.path.exists(target):
                os.remove(target)
        if sel is None:
            la, lo = dr.regular_latlon(msgs[0][1])
            sel = (la >= dr.BOX[0]) & (la <= dr.BOX[1]) & (lo >= dr.BOX[2]) & (lo <= dr.BOX[3])
            lat, lon = la[sel], lo[sel]
            members = sorted(n for n, _ in msgs)
        got = {n: m for n, m in msgs}
        if sorted(got) != members:
            raise RuntimeError("members differ between steps")
        cum[step] = np.stack([dr.to_mm(got[n])[sel] for n in members]).astype(np.float32)
        print("    step +%d h: %d members" % (step, len(members)), flush=True)
    days = np.empty((len(members), DAYS, len(lat)), np.float32)
    prev = 0
    for d in range(1, DAYS + 1):
        days[:, d - 1, :] = np.clip(cum[24 * d] - prev, 0, None); prev = cum[24 * d]
    return lat, lon, days


def read_with_number(path):
    """GRIB messages as (member number, message dict) using district_runner's reader plus the 'number' key."""
    import eccodes as ec
    out = []
    data = path if isinstance(path, (bytes, bytearray)) else open(path, "rb").read(); pos = 0
    while True:
        k = data.find(b"GRIB", pos)
        if k < 0:
            break
        gid = ec.codes_new_from_message(bytes(data[k:]))
        try:
            ln = ec.codes_get(gid, "totalLength"); num = int(ec.codes_get(gid, "number"))
        finally:
            ec.codes_release(gid)
        out.append((num, dr.read_msgs(data[k:k + ln])[0]))
        pos = k + ln
    if not out:
        raise RuntimeError("no fields")
    return out


def member_values(D, lat, lon, days, key):
    """-> per district: DAYS lists of the members' district maxima (mm, 1 decimal)."""
    F = types.SimpleNamespace(lat=lat, lon=lon, key=key)
    pts = dr.district_index(D, F)
    out = []
    for p in pts:
        v = days[:, :, p].max(axis=2).T                # (DAYS, members)
        out.append([[_num(x) for x in row] for row in v])
    return out


def _num(x):
    x = round(float(x), 1)
    return int(x) if x == int(x) else x


# ---------------- GEFS (NOAA, 31 members: control gec00 + gep01..gep30), 0.25 deg, 6-h APCP buckets ------------
GEFS_URL = "https://noaa-gefs-pds.s3.amazonaws.com/gefs.%s/%s/atmos/pgrb2sp25/%s.t%sz.pgrb2s.0p25.f%03d"


def fetch_gefs(ic):
    from concurrent.futures import ThreadPoolExecutor
    ymd, hh = ic.strftime("%Y%m%d"), ic.strftime("%H")
    names = ["gec00"] + ["gep%02d" % i for i in range(1, 31)]
    steps = list(range(6, 24 * DAYS + 1, 6))
    dr.http_get(GEFS_URL % (ymd, hh, names[-1], hh, steps[-1]) + ".idx")     # fails fast if the run is not complete

    grid = {}

    def one(job):                                      # each field is cut to the India box at once (memory)
        n, s = job
        url = GEFS_URL % (ymd, hh, n, hh, s)
        m = dr.read_msgs(dr.http_get(url, dr.idx_range(url, r":APCP:surface:%d-%d hour acc" % (s - 6, s))))[0]
        if "sel" not in grid:
            la, lo = dr.regular_latlon(m)
            sel = (la >= dr.BOX[0]) & (la <= dr.BOX[1]) & (lo >= dr.BOX[2]) & (lo <= dr.BOX[3])
            grid.update(sel=sel, lat=la[sel], lon=lo[sel], n=len(la))
        if len(m["values"]) != grid["n"]:
            raise RuntimeError("GEFS grid changed")
        return n, s, dr.to_mm(m)[grid["sel"]].astype(np.float32)
    with ThreadPoolExecutor(16) as ex:
        six = {(n, s): v for n, s, v in ex.map(one, [(n, s) for n in names for s in steps])}
    sel = grid["sel"]; la, lo = grid["lat"], grid["lon"]
    days = np.zeros((len(names), DAYS, int(sel.sum())), np.float32)
    for i, n in enumerate(names):
        for d in range(DAYS):
            days[i, d] = sum(six[(n, s)] for s in range(24 * d + 6, 24 * d + 25, 6))
    print("    GEFS: %d members x %d steps" % (len(names), len(steps)), flush=True)
    return la, lo, np.clip(days, 0, None)


# ---------------- GEPS (ECCC, 21 members: control + 20), 0.5 deg, totals from the IC, all members in one file ---
GEPS_URL = ("https://dd.weather.gc.ca/%s/WXO-DD/ensemble/geps/grib2/raw/%s/%03d/"
            "CMC_geps-raw_APCP_SFC_0_latlon0p5x0p5_%s_P%03d_allmbrs.grib2")


def fetch_geps(ic):
    ymd, hh, tag = ic.strftime("%Y%m%d"), ic.strftime("%H"), ic.strftime("%Y%m%d%H")
    cum, sel, lat, lon, members = {}, None, None, None, None
    for d in range(DAYS, 0, -1):                       # last step first
        s = 24 * d
        raw = dr.http_get(GEPS_URL % (ymd, hh, s, tag, s))
        msgs = read_with_number(raw)
        if sel is None:
            la, lo = dr.regular_latlon(msgs[0][1])
            sel = (la >= dr.BOX[0]) & (la <= dr.BOX[1]) & (lo >= dr.BOX[2]) & (lo <= dr.BOX[3])
            lat, lon = la[sel], lo[sel]; members = sorted(n for n, _ in msgs)
        got = {n: m for n, m in msgs}
        if sorted(got) != members:
            raise RuntimeError("members differ between steps")
        cum[s] = np.stack([got[n]["values"][sel] for n in members]).astype(np.float32)   # values are mm
    days = np.empty((len(members), DAYS, len(lat)), np.float32); prev = 0
    for d in range(1, DAYS + 1):
        days[:, d - 1, :] = np.clip(cum[24 * d] - prev, 0, None); prev = cum[24 * d]
    print("    GEPS: %d members" % len(members), flush=True)
    return lat, lon, days


ENSEMBLES = {"ECMWF": fetch_members, "GEFS": fetch_gefs, "GEPS": fetch_geps}
SRC = {"ECMWF": "ECMWF ENS 0.25 deg, 50 perturbed members (ECMWF open data, CC BY 4.0)",
       "GEFS": "NOAA GEFS 0.25 deg, control + 30 members (NOAA open data)",
       "GEPS": "ECCC GEPS 0.5 deg, control + 20 members (MSC Datamart)"}
WANT = [e.strip().upper() for e in os.environ.get("ENSEMBLES", "ECMWF,GEFS,GEPS").split(",") if e.strip()]


def run_ids(now):
    base = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 12) * 12)
    return [base - dt.timedelta(hours=12 * k) for k in range(int(KEEP_DAYS * 2))]


def main():
    os.makedirs(OUT, exist_ok=True); os.makedirs(WORK, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if RUN_ID:
        ics = [dt.datetime.strptime(RUN_ID[:10], "%Y%m%d%H")]
        if ics[0].hour not in (0, 12):
            sys.exit("Only 00 and 12 UTC runs are used")
    else:
        ics = [ic for ic in run_ids(now) if dt.timedelta(hours=MIN_AGE_H) <= now - ic <= dt.timedelta(hours=TRY_HOURS)]
    D = None; added = 0
    for ic in ics:
        tag = ic.strftime("%Y%m%d%H")
        for ens in WANT:
            path = os.path.join(OUT, "%s_%s.json" % (tag, ens))
            if os.path.exists(path) and not FORCE:
                continue
            t0 = time.time()
            print("%s %s:" % (ens, tag), flush=True)
            try:
                lat, lon, days = ENSEMBLES[ens](ic)
                if D is None:
                    D = dr.load_districts()
                v = member_values(D, lat, lon, days, ens)
            except Exception as e:
                print("  %s %s not done: %s: %s" % (tag, ens, type(e).__name__, str(e)[:200]), flush=True)
                continue
            json.dump({"ic": ic.strftime("%Y-%m-%dT%H:%M"), "ens": ens, "made": now.strftime("%Y-%m-%d %H:%M UTC"),
                       "members": int(days.shape[0]), "days": DAYS, "src": SRC[ens],
                       "note": "v[district][day] = each member's maximum grid-point 24-h rainfall inside the district (mm)",
                       "v": v}, open(path, "w"), separators=(",", ":"))
            added += 1
            print("  %s %s done (%d members, %.0f s)" % (tag, ens, days.shape[0], time.time() - t0), flush=True)
    cut = now - dt.timedelta(days=KEEP_DAYS); runs = {}
    for f in sorted(glob.glob(os.path.join(OUT, "*.json"))):
        b = os.path.basename(f)
        if b == "manifest.json":
            continue
        try:
            ic = dt.datetime.strptime(b[:10], "%Y%m%d%H")
        except ValueError:
            continue
        if ic < cut or "_" not in b:                   # too old, or a file of the first (summary-only) version
            os.remove(f); continue
        try:
            m = json.load(open(f))
        except Exception:
            continue
        runs.setdefault(ic, {})[m["ens"]] = m["members"]
    runs = [{"ic": ic.strftime("%Y-%m-%dT%H:%M"), "ens": runs[ic]} for ic in sorted(runs, reverse=True)]
    ist = now + dt.timedelta(hours=5, minutes=30)
    json.dump({"runs": runs, "updated": "%s IST" % ist.strftime("%d %b %Y %H:%M"), "keep_days": KEEP_DAYS},
              open(os.path.join(OUT, "manifest.json"), "w"), separators=(",", ":"))
    if added:
        open(os.path.join(WORK, "district_changed"), "w").close()
    print("ENS manifest: %d runs; %d added now" % (len(runs), added))


if __name__ == "__main__":
    main()
