#!/usr/bin/env python3
"""
ens_district_runner.py - Ensemble District R/F Probability from the ECMWF ensemble (ENS), 00 and 12 UTC runs.

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
MIN_AGE_H = 6.5                                       # ENS is out about 7-8 h after the IC
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
    data = open(path, "rb").read(); pos = 0
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


def summarise(D, lat, lon, days):
    F = types.SimpleNamespace(lat=lat, lon=lon, key="ENS")
    pts = dr.district_index(D, F)
    lim = np.array(LIMITS)
    Q, M, C = [], [], []
    for p in pts:
        v = days[:, :, p].max(axis=2)                  # (members, DAYS): district maximum of each member
        q = np.percentile(v, QUANT, axis=0).T          # (DAYS, 7)
        Q.append([[round(float(x), 1) for x in row] for row in q])
        M.append([round(float(x), 1) for x in v.mean(axis=0)])
        C.append([[int(x) for x in (v[:, d][:, None] >= lim[None, :]).sum(axis=0)] for d in range(v.shape[1])])
    return Q, M, C


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
        tag = ic.strftime("%Y%m%d%H"); path = os.path.join(OUT, tag + ".json")
        if os.path.exists(path) and not FORCE:
            continue
        t0 = time.time()
        print("ENS %s:" % tag, flush=True)
        try:
            lat, lon, days = fetch_members(ic)
            if D is None:
                D = dr.load_districts()
            Q, M, C = summarise(D, lat, lon, days)
        except Exception as e:
            print("  %s not done: %s: %s" % (tag, type(e).__name__, str(e)[:200]), flush=True)
            continue
        json.dump({"ic": ic.strftime("%Y-%m-%dT%H:%M"), "made": now.strftime("%Y-%m-%d %H:%M UTC"),
                   "members": int(days.shape[0]), "days": DAYS, "limits": LIMITS, "quantiles": QUANT,
                   "src": "ECMWF ENS 0.25 deg, perturbed members (ECMWF open data, CC BY 4.0)",
                   "note": "District value of each member = maximum of its grid-point 24-h rainfall inside the district.",
                   "q": Q, "mean": M, "cnt": C}, open(path, "w"), separators=(",", ":"))
        added += 1
        print("  %s done (%d members, %.0f s)" % (tag, days.shape[0], time.time() - t0), flush=True)
    cut = now - dt.timedelta(days=KEEP_DAYS); runs = []
    for f in sorted(glob.glob(os.path.join(OUT, "??????????.json")), reverse=True):
        try:
            ic = dt.datetime.strptime(os.path.basename(f)[:10], "%Y%m%d%H")
        except ValueError:
            continue
        if ic < cut:
            os.remove(f); continue
        try:
            m = json.load(open(f))["members"]
        except Exception:
            continue
        runs.append({"ic": ic.strftime("%Y-%m-%dT%H:%M"), "members": m})
    ist = now + dt.timedelta(hours=5, minutes=30)
    json.dump({"runs": runs, "updated": "%s IST" % ist.strftime("%d %b %Y %H:%M"), "keep_days": KEEP_DAYS},
              open(os.path.join(OUT, "manifest.json"), "w"), separators=(",", ":"))
    if added:
        open(os.path.join(WORK, "district_changed"), "w").close()
    print("ENS manifest: %d runs; %d added now" % (len(runs), added))


if __name__ == "__main__":
    main()
