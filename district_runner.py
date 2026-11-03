#!/usr/bin/env python3
"""
district_runner.py - district-wise 24-hour rainfall from several global NWP models, plus their MME.

Models (only the 00 and 12 UTC runs are used; 06/18 UTC runs are never fetched):
    IFS    ECMWF IFS HRES 0.25 deg         ECMWF open data            days 1-10
    AIFS   ECMWF AIFS Single 0.25 deg      ECMWF open data            days 1-10
    GFS    NCEP GFS 0.25 deg               NOAA open data (AWS)       days 1-10
    ICON   DWD ICON global, ~13 km native  DWD open data              days 1-7 (00/12 UTC runs end at 180 h)
    GDPS   CMC GDPS 0.15 deg (~15 km)      MSC Datamart               days 1-10
    AIGFS  NOAA AIGFS 0.25 deg (AI model)  NOAA open data (AWS)       days 1-10
    ARPEGE Meteo-France ARPEGE 0.25 deg    Meteo-France open data     days 1-4 (runs end at 102 h)
    IMDGFS IMD GFS 0.125 deg               IMD SFTP (dr_krishna)      days 1-5 (file ends at 120 h)
    NCUMG  NCMRWF NCUM-G 0.18x0.12 deg     IMD SFTP (dr_krishna)      days 1-10
    NCUMR  NCMRWF NCUM-R regional          IMD SFTP (dr_krishna)      as far as the file goes
    (IMD models need IMD_PWD, the password of the imdnwp SFTP login)
Choose the models with the environment variable MODELS (default "IFS,AIFS,GFS,AIGFS,ICON,GDPS,ARPEGE").

For every district of india_districts.geojson and every model, the value is the MAXIMUM of the model's grid-point
values that lie inside the district (the grid point nearest to the district centroid is always included, so small
districts get a value too).  Day n = rainfall in the n-th 24 h after the run's initial condition (IC).
The MME (simple, equal-weight mean of the models) is computed by the web page from these numbers.

Models are released at different times, so each run file is filled in model by model: every call adds the
models that are still missing for the recent runs (younger than TRY_HOURS).

Output (in SITE, default ./site):
    district/YYYYMMDDHH.json   {"ic", "made", "models": {"IFS": [[10 values], ...], ...}, "shift": {...}}
                               (districts in geojson order; null = day not covered by that model).
                               "shift" holds the same calendar windows as Day 1..10 of the NEXT run (IC + 12 h):
                               the page shows them for a model whose newer run is not out yet, marked e.g. "(12Z)".
    district/manifest.json     {"runs": [{"ic": ..., "models": [...]}, ...] newest first, "updated": ...}
The last KEEP_DAYS days of runs are kept.  Writes work/district_changed when something new was added.

Called from .github/workflows/nwp.yml; can also be run by hand:   python district_runner.py
Environment: FORCE=1 (redo runs already present), SITE (default ./site), KEEP_DAYS (default 7),
             MODELS (default IFS,AIFS,GFS,AIGFS,ICON,GDPS,ARPEGE), TRY_HOURS (default 60), RUN_ID (YYYYMMDDHH: only this run)
Needs: numpy, matplotlib, eccodes, ecmwf-opendata
"""
import bz2, datetime as dt, glob, json, os, re, sys, time, urllib.error, urllib.request
import numpy as np
from matplotlib.path import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.abspath(os.environ.get("SITE", os.path.join(HERE, "site")))
WORK = os.path.join(HERE, "work")
GEO = os.path.join(HERE, "india_districts.geojson")
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "7"))
TRY_HOURS = float(os.environ.get("TRY_HOURS", "60"))
FORCE = os.environ.get("FORCE", "") not in ("", "0", "false", "False")
RUN_ID = os.environ.get("RUN_ID", "").strip()
MODELS = [m.strip().upper() for m in os.environ.get("MODELS", "IFS,AIFS,GFS,AIGFS,ICON,GDPS,ARPEGE,IMDGFS,NCUMG,NCUMR").split(",") if m.strip()]
DAYS = 10
MIN_AGE_H = 3             # no model has a run out earlier than this
OUT = os.path.join(SITE, "district")
BOX = (4.0, 40.0, 64.0, 102.0)            # lat0, lat1, lon0, lon1: India and surroundings (grid points kept)
UA = {"User-Agent": "district-rainfall/2.0 (github actions)"}


# ------------------------------------------------------------------ download helpers
def http_get(url, rng=None, tries=3):
    for t in range(tries):
        h = dict(UA)
        if rng:
            h["Range"] = "bytes=" + rng
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                raise RuntimeError("not available (yet) [%d] %s" % (e.code, url.split("/")[-1][:90]))
            if t == tries - 1:
                raise
        except Exception:
            if t == tries - 1:
                raise
        time.sleep(5 * (t + 1))


def idx_range(url, pattern):
    """GRIB .idx inventory -> byte range of the first message whose line matches `pattern`."""
    lines = http_get(url + ".idx").decode().strip().splitlines()
    for k, ln in enumerate(lines):
        if re.search(pattern, ln):
            a = int(ln.split(":")[1])
            b = str(int(lines[k + 1].split(":")[1]) - 1) if k + 1 < len(lines) else ""
            return "%d-%s" % (a, b)
    raise RuntimeError("field not in inventory: %s" % url.split("/")[-1])


# ------------------------------------------------------------------ GRIB reading
def read_msgs(src):
    """All GRIB messages of a file (path) or bytes -> list of dicts (start, end, units, values, grid info)."""
    import eccodes as ec
    data = src if isinstance(src, (bytes, bytearray)) else open(src, "rb").read()
    out, pos = [], 0
    while True:
        k = data.find(b"GRIB", pos)
        if k < 0:
            break
        gid = ec.codes_new_from_message(bytes(data[k:]))
        try:
            ln = ec.codes_get(gid, "totalLength")
            m = dict(start=_hours(ec.codes_get(gid, "startStep")), end=_hours(ec.codes_get(gid, "endStep")),
                     units=ec.codes_get(gid, "units"), gridType=ec.codes_get(gid, "gridType"),
                     values=np.asarray(ec.codes_get_values(gid), dtype=np.float64))
            if m["gridType"] == "regular_ll":
                for key, name in (("Ni", "ni"), ("Nj", "nj"), ("latitudeOfFirstGridPointInDegrees", "lat0"),
                                  ("longitudeOfFirstGridPointInDegrees", "lon0"), ("iDirectionIncrementInDegrees", "di"),
                                  ("jDirectionIncrementInDegrees", "dj"), ("jScansPositively", "jpos"),
                                  ("iScansNegatively", "ineg")):
                    m[name] = ec.codes_get(gid, key)
            out.append(m)
        finally:
            ec.codes_release(gid)
        pos = k + ln
    return out


def _hours(v):
    """Step as returned by ecCodes (int, '24', '0m', '30m', ...) -> hours (int)."""
    if isinstance(v, (int, np.integer)):
        return int(v)
    n, u = re.match(r"(-?\d+)\s*([a-z]*)", str(v)).groups()
    return int(n) // 60 if u == "m" else int(n)


def regular_latlon(m):
    """Latitude / longitude (deg, lon in -180..180) of every value of a regular_ll message, in storage order."""
    j, i = np.divmod(np.arange(m["ni"] * m["nj"]), m["ni"])
    lat = m["lat0"] + (1 if m["jpos"] else -1) * j * m["dj"]
    lon = m["lon0"] + (-1 if m["ineg"] else 1) * i * m["di"]
    return lat, (lon + 180.0) % 360.0 - 180.0


def to_mm(m):
    u = str(m["units"]).strip().lower()
    return m["values"] * (1000.0 if u in ("m", "m of water equivalent") else 1.0)


def daily_from_acc(acc, ndays, offset=0):
    """acc: {(start_h, end_h): field_mm} -> list of DAYS 24-h fields for the windows [offset+24k, offset+24k+24].
    offset 0: Day 1..Day ndays of this run (must all be there; None after ndays).
    offset 12: the same calendar windows as Day 1..Day 10 of the NEXT run (IC + 12 h); None where not reached."""
    days = []
    for k in range(DAYS):
        a, b = offset + 24 * k, offset + 24 * k + 24
        if (a, b) in acc:
            days.append(np.clip(acc[(a, b)], 0, None))
        elif (0, b) in acc and (a == 0 or (0, a) in acc):
            days.append(np.clip(acc[(0, b)] - (acc[(0, a)] if a else 0), 0, None))
        elif offset == 0 and k < ndays:
            raise RuntimeError("accumulation for day %d missing" % (k + 1))
        else:
            days.append(None)
    if offset == 0:
        days = [d if k < ndays else None for k, d in enumerate(days)]
    return days


class Field:
    """Model output over the India box: lat, lon (1-D), the daily rainfall of this run (days) and the 24-h windows
    of the next run's days (shift); both lists of 1-D arrays or None."""
    def __init__(self, lat, lon, acc, ndays, key):
        sel = (lat >= BOX[0]) & (lat <= BOX[1]) & (lon >= BOX[2]) & (lon <= BOX[3])
        self.lat, self.lon, self.key = lat[sel], lon[sel], key
        self.days = [None if d is None else d[sel] for d in daily_from_acc(acc, ndays, 0)]
        self.shift = [None if d is None else d[sel] for d in daily_from_acc(acc, ndays, 12)]


def field_regular(msgs, ndays, key):
    acc = {(m["start"], m["end"]): to_mm(m) for m in msgs}
    lat, lon = regular_latlon(msgs[0])
    return Field(lat, lon, acc, ndays, key)


# ------------------------------------------------------------------ the models
STEPS = list(range(12, 24 * DAYS + 1, 12))       # 12, 24, ..., 240 h: run totals needed for both window sets


def ymd_hh(ic):
    return ic.strftime("%Y%m%d"), ic.strftime("%H")


def get_ecmwf(model, key, ic):
    from ecmwf.opendata import Client
    target = os.path.join(WORK, "%s_%s.grib2" % (key, ic.strftime("%Y%m%d%H")))
    try:
        try:
            client = Client(source="ecmwf", model=model, resol="0p25")
        except TypeError:
            client = Client(source="ecmwf")
        client.retrieve(date=int(ic.strftime("%Y%m%d")), time=ic.hour, stream="oper", type="fc", param="tp",
                        step=STEPS, target=target)
        msgs = read_msgs(target)
    finally:
        if os.path.exists(target):
            os.remove(target)
    return field_regular(msgs, DAYS, key)


def get_ifs(ic):
    return get_ecmwf("ifs", "IFS", ic)


def get_aifs(ic):
    return get_ecmwf("aifs-single", "AIFS", ic)


def _ncep(url_of):
    """GFS-type output on AWS: for each step, the run-total APCP (0-N h) message, fetched by byte range."""
    msgs = []
    for s in STEPS[::-1]:                        # last step first: fails fast while the run is still coming in
        url = url_of(s)
        msgs += read_msgs(http_get(url, idx_range(url, r":APCP:surface:0-\d+ (hour|day) acc")))
    return msgs


def get_gfs(ic):
    ymd, hh = ymd_hh(ic)
    try:
        msgs = _ncep(lambda s: "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.%s/%s/atmos/gfs.t%sz.pgrb2.0p25.f%03d"
                     % (ymd, hh, hh, s))
    except Exception as e:                       # fall back to NOMADS (grib filter, India box only)
        print("    GFS on AWS: %s - trying NOMADS" % str(e)[:120], flush=True)
        msgs = []
        for s in STEPS:
            url = ("https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?dir=%%2Fgfs.%s%%2F%s%%2Fatmos"
                   "&file=gfs.t%sz.pgrb2.0p25.f%03d&var_APCP=on&lev_surface=on&subregion=&toplat=%g&leftlon=%g"
                   "&rightlon=%g&bottomlat=%g" % (ymd, hh, hh, s, BOX[1], BOX[2], BOX[3], BOX[0]))
            msgs += [m for m in read_msgs(http_get(url)) if m["start"] == 0]
            time.sleep(1)
    return field_regular(msgs, DAYS, "GFS")


def get_aigfs(ic):
    ymd, hh = ymd_hh(ic)
    msgs = _ncep(lambda s: "https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com/aigfs.%s/%s/model/atmos/grib2/"
                 "aigfs.t%sz.sfc.f%03d.grib2" % (ymd, hh, hh, s))
    return field_regular(msgs, DAYS, "AIGFS")


ICON_URL = "https://opendata.dwd.de/weather/nwp/icon/grib/%s/%s/icon_global_icosahedral_%s_%s%s.grib2.bz2"
ICON_DAYS = 7                                    # 00/12 UTC runs end at +180 h
_icon_grid = {}


def get_icon(ic):
    ymd, hh = ymd_hh(ic); tag = ymd + hh
    acc = {}
    for s in [x for x in STEPS if x <= 180][::-1]:   # last step first: fails fast while the run is still coming in
        raw = bz2.decompress(http_get(ICON_URL % (hh, "tot_prec", "single-level", tag, "_%03d_TOT_PREC" % s)))
        m = read_msgs(raw)[0]
        acc[(m["start"], m["end"])] = to_mm(m)
    if "grid" not in _icon_grid:                 # cell-centre coordinates (degrees) of the icosahedral grid
        ll = []
        for v in ("clat", "clon"):
            raw = bz2.decompress(http_get(ICON_URL % (hh, v, "time-invariant", tag, "_" + v.upper())))
            ll.append(read_msgs(raw)[0]["values"])
        _icon_grid["grid"] = (ll[0], (ll[1] + 180.0) % 360.0 - 180.0)
    lat, lon = _icon_grid["grid"]
    if len(lat) != len(m["values"]):
        raise RuntimeError("ICON grid size changed")
    return Field(lat, lon, acc, ICON_DAYS, "ICON")


def get_gdps(ic):
    ymd, hh = ymd_hh(ic)
    msgs = []
    for s in [x for x in STEPS if x >= 24][::-1]:    # 24-h totals ending at 24, 36, ..., 240 h; last step first
        url = ("https://dd.weather.gc.ca/%s/WXO-DD/model_gdps/15km/%s/%03d/%sT%sZ_MSC_GDPS_Precip-Accum24h_Sfc_LatLon0.15_PT%03dH.grib2"
               % (ymd, hh, s, ymd, hh, s))
        m = read_msgs(http_get(url))[0]
        m["units"] = "kg m**-2"                  # MSC parameter is not in the ecCodes tables; values are mm
        m["start"], m["end"] = s - 24, s         # 24-h total ending at the step
        msgs.append(m)
    return field_regular(msgs, DAYS, "GDPS")


ARPEGE_URL = ("https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt/%s/arpege/025/SP1/"
              "arpege__025__SP1__%s__%s.grib2")
ARPEGE_PKG = [("000H024H", (12, 24)), ("025H048H", (36, 48)), ("049H072H", (60, 72)), ("073H102H", (84, 96))]
ARPEGE_DAYS = 4                                  # runs end at +102 h


def stream_pick(url, want, n):
    """Stream a (large) GRIB2 file and return the messages for which want(shortName, endStep) is true.
    Reading stops as soon as n such messages have been found."""
    import eccodes as ec
    req = urllib.request.Request(url, headers=UA)
    try:
        r = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        raise RuntimeError("not available (yet) [%d] %s" % (e.code, url.split("/")[-1]))
    keep, buf = [], b""
    with r:
        while True:
            while len(buf) >= 16:
                k = buf.find(b"GRIB")
                if k < 0:
                    buf = buf[-3:]; break
                buf = buf[k:]
                if len(buf) < 16:
                    break
                ln = int.from_bytes(buf[8:16], "big")
                if len(buf) < ln:
                    break
                msg, buf = buf[:ln], buf[ln:]
                gid = ec.codes_new_from_message(msg)
                try:
                    ok = want(ec.codes_get(gid, "shortName"), _hours(ec.codes_get(gid, "endStep")))
                finally:
                    ec.codes_release(gid)
                if ok:
                    keep.append(msg)
                    if len(keep) >= n:
                        return keep
            chunk = r.read(1 << 20)
            if not chunk:
                return keep
            buf += chunk


def get_arpege(ic):
    iso = ic.strftime("%Y-%m-%dT%H:00:00Z")
    msgs = []
    for pkg, steps in ARPEGE_PKG[::-1]:          # last package first: fails fast while the run is still coming in
        got = stream_pick(ARPEGE_URL % (iso, pkg, iso), lambda sn, es, steps=steps: sn == "tp" and es in steps,
                          len(steps))
        if len(got) < len(steps):
            raise RuntimeError("tp at +%s h not found in package %s" % (steps, pkg))
        for g in got:
            msgs += read_msgs(g)
    return field_regular(msgs, ARPEGE_DAYS, "ARPEGE")


# ---- IMD models: rain files in IMD's SFTP folder nwp_data/gfs_rimes/dr_krishna/YYYYMMDDHH (login imdnwp, IMD_PWD)
IMD_HOST, IMD_USER = "sftp.nwp.imd.gov.in", "imdnwp"
IMD_DIR = "nwp_data/gfs_rimes/dr_krishna/%s"
IMD_FILE = {"IMDGFS": "gfs_apcp_%s.grib", "NCUMG": "um_apcp_%s.grib", "NCUMR": "umreg_apcp_%s.grib"}
IMD_LOCAL = os.environ.get("IMD_LOCAL", "")      # testing: a local folder holding the same files
_imd = {}


def _imd_sftp():
    if "c" not in _imd:
        pw = os.environ.get("IMD_PWD", "")
        if not pw:
            raise RuntimeError("IMD_PWD not set")
        import paramiko
        tr = paramiko.Transport((IMD_HOST, 22)); tr.banner_timeout = 60
        tr.connect(username=IMD_USER, password=pw)
        c = paramiko.SFTPClient.from_transport(tr); c.get_channel().settimeout(120)
        _imd.update(c=c, t=tr, home=c.normalize("."))       # full paths from the home folder (as the cyclone script does)
    return _imd["c"]


def _imd_file(name, ic):
    tag = ic.strftime("%Y%m%d%H"); fn = IMD_FILE[name] % tag
    if IMD_LOCAL:
        return os.path.join(IMD_LOCAL, fn)
    if _imd.get("down"):
        raise RuntimeError("IMD server not reachable in this run")
    try:
        c = _imd_sftp()
    except Exception as e:
        _imd["down"] = True
        raise RuntimeError("IMD login failed: %s" % e)
    rem = _imd["home"].rstrip("/") + "/" + (IMD_DIR % tag) + "/" + fn
    try:
        st = c.stat(rem)
    except IOError:
        raise RuntimeError("not available (yet): " + fn)
    if time.time() - st.st_mtime < 300:        # still being written
        raise RuntimeError("being written: " + fn)
    loc = os.path.join(WORK, fn)
    t0 = time.time()
    try:
        c.get(rem, loc + ".part")
    except PermissionError:
        raise RuntimeError("IMD server refused to send %s (permission denied)" % fn)
    if os.path.getsize(loc + ".part") != st.st_size:
        raise RuntimeError("incomplete download: " + fn)
    os.replace(loc + ".part", loc)
    print("    %s: %.0f MB in %.0f s" % (fn, st.st_size / 1e6, time.time() - t0), flush=True)
    return loc


def get_imd(name, ic):
    path = _imd_file(name, ic)
    try:
        msgs = read_msgs(path)
    finally:
        if not IMD_LOCAL and os.path.exists(path):
            os.remove(path)
    if not msgs:
        raise RuntimeError("no fields in the file")
    for m in msgs:                              # IMD's NCUM code is not in the ecCodes tables; values are mm
        if str(m["units"]).lower() in ("unknown", "kg m-2 s-1"):
            m["units"] = "kg m**-2"
    acc = {}
    if all(m["start"] == 0 for m in msgs):       # totals from the IC
        for m in msgs:
            acc[(0, m["end"])] = to_mm(m)
        last = max(m["end"] for m in msgs)
    else:                                        # consecutive buckets (1 h or 3 h): add them up from the IC
        cum, last = 0, 0
        for m in sorted(msgs, key=lambda m: (m["start"], m["end"])):
            if m["start"] != last:
                raise RuntimeError("gap in the rainfall steps at +%d h" % last)
            cum = cum + to_mm(m); last = m["end"]; acc[(0, last)] = cum
    lat, lon = regular_latlon(msgs[0])
    return Field(lat, lon, acc, min(DAYS, last // 24), name)


FETCH = {"IFS": get_ifs, "AIFS": get_aifs, "GFS": get_gfs, "AIGFS": get_aigfs, "ICON": get_icon, "GDPS": get_gdps,
         "ARPEGE": get_arpege, "IMDGFS": lambda ic: get_imd("IMDGFS", ic),
         "NCUMG": lambda ic: get_imd("NCUMG", ic), "NCUMR": lambda ic: get_imd("NCUMR", ic)}
INFO = {"IFS": "ECMWF IFS HRES 0.25 deg (ECMWF open data)", "AIFS": "ECMWF AIFS Single 0.25 deg (ECMWF open data)",
        "GFS": "NCEP GFS 0.25 deg (NOAA open data)", "ICON": "DWD ICON global ~13 km native grid (DWD open data), days 1-7",
        "GDPS": "CMC GDPS 0.15 deg (MSC Datamart)", "AIGFS": "NOAA AIGFS 0.25 deg (NOAA open data)",
        "ARPEGE": "Meteo-France ARPEGE 0.25 deg (Meteo-France open data), days 1-4",
        "IMDGFS": "IMD GFS 0.125 deg (IMD SFTP), days 1-5", "NCUMG": "NCMRWF NCUM-G (IMD SFTP)",
        "NCUMR": "NCMRWF NCUM-R regional (IMD SFTP)"}


# ------------------------------------------------------------------ geometry
def load_districts():
    G = json.load(open(GEO, encoding="utf-8"))
    D = []
    for f in G["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        D.append([[np.array(r, dtype=float) for r in poly] for poly in polys])
    return D


def _area(r):
    return 0.5 * np.sum(r[:-1, 0] * r[1:, 1] - r[1:, 0] * r[:-1, 1])


def centroid(polys):
    best = max(polys, key=lambda p: abs(_area(p[0])))[0]
    x, y = best[:, 0], best[:, 1]
    cr = x[:-1] * y[1:] - x[1:] * y[:-1]
    a = cr.sum() / 2
    if abs(a) < 1e-12:
        return best.mean(0)
    return np.array([((x[:-1] + x[1:]) * cr).sum() / (6 * a), ((y[:-1] + y[1:]) * cr).sum() / (6 * a)])


_pts_cache = {}


def district_index(D, F):
    """For every district: indices (into F.lat/F.lon) of the points inside it + the point nearest to its centroid."""
    key = (F.key, len(F.lat), round(float(F.lat[0]), 4), round(float(F.lon[0]), 4))
    if key in _pts_cache:
        return _pts_cache[key]
    xy = np.c_[F.lon, F.lat]
    coslat = np.cos(np.radians(22.0))
    out = []
    for polys in D:
        idx = []
        for poly in polys:
            o = poly[0]
            x0, y0 = o.min(0); x1, y1 = o.max(0)
            cand = np.nonzero((F.lon >= x0) & (F.lon <= x1) & (F.lat >= y0) & (F.lat <= y1))[0]
            if len(cand) == 0:
                continue
            m = Path(o).contains_points(xy[cand])
            for h in poly[1:]:
                m &= ~Path(h).contains_points(xy[cand])
            idx.extend(cand[m].tolist())
        cx, cy = centroid(polys)
        near = np.nonzero((np.abs(F.lon - cx) < 1) & (np.abs(F.lat - cy) < 1))[0]
        if len(near) == 0:
            near = np.arange(len(F.lat))
        idx.append(int(near[np.argmin(((F.lon[near] - cx) * coslat) ** 2 + (F.lat[near] - cy) ** 2)]))
        out.append(np.array(sorted(set(idx))))
    _pts_cache[key] = out
    return out


def district_max(D, F, days):
    pts = district_index(D, F)
    return [[None if day is None else round(float(day[p].max()), 1) for day in days] for p in pts]


# ------------------------------------------------------------------ run bookkeeping
def run_ids(now):
    base = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 12) * 12)
    return [base - dt.timedelta(hours=12 * k) for k in range(int(KEEP_DAYS * 2))]


def load_run(path):
    try:
        j = json.load(open(path))
    except Exception:
        return None
    if "models" not in j and "max" in j:          # file written by the ECMWF-only version
        j["models"] = {"IFS": j.pop("max")}
    j.setdefault("models", {})
    j.setdefault("shift", {})
    return j


def main():
    os.makedirs(OUT, exist_ok=True); os.makedirs(WORK, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if RUN_ID:
        ics = [dt.datetime.strptime(RUN_ID[:10], "%Y%m%d%H")]
        if ics[0].hour not in (0, 12):
            sys.exit("Only 00 and 12 UTC runs are used")
    else:
        ics = [ic for ic in run_ids(now) if dt.timedelta(hours=MIN_AGE_H) <= now - ic <= dt.timedelta(hours=TRY_HOURS)]
    print("District rainfall: models %s; runs to check: %s" % (",".join(MODELS), ", ".join(i.strftime("%Y%m%d%H") for i in ics)),
          flush=True)
    D = None; added = 0
    for ic in ics:
        tag = ic.strftime("%Y%m%d%H"); path = os.path.join(OUT, tag + ".json")
        run = (None if FORCE else load_run(path)) or {"ic": ic.strftime("%Y-%m-%dT%H:%M"), "models": {}, "shift": {}}
        todo = [m for m in MODELS if m not in run["models"] or m not in run["shift"]]
        if not todo:
            continue
        changed = False
        for name in todo:
            t0 = time.time()
            try:
                F = FETCH[name](ic)
                if D is None:
                    D = load_districts()
                run["models"][name] = district_max(D, F, F.days)
                run["shift"][name] = district_max(D, F, F.shift)
                changed = True
                print("  %s %-5s done (%.0f s, %d grid points in the India box)" % (tag, name, time.time() - t0, len(F.lat)),
                      flush=True)
            except Exception as e:                # not released yet / not available: try again next time
                print("  %s %-5s not done: %s: %s" % (tag, name, type(e).__name__, str(e)[:200]), flush=True)
        if changed:
            order = [m for m in FETCH if m in run["models"]]
            run["models"] = {m: run["models"][m] for m in order}
            run["shift"] = {m: run["shift"][m] for m in FETCH if m in run["shift"]}
            run["made"] = now.strftime("%Y-%m-%d %H:%M UTC")
            run["src"] = {m: INFO[m] for m in order}
            run["note"] = ("Value = maximum of the model grid-point values inside each district (mm per 24 h). "
                           "models: Day 1..10 of this run; shift: the 24-h windows of Day 1..10 of the next run (IC + 12 h).")
            json.dump(run, open(path, "w"), separators=(",", ":"))
            added += 1
    # keep KEEP_DAYS days, write manifest (newest first)
    cut = now - dt.timedelta(days=KEEP_DAYS)
    runs = []
    for f in sorted(glob.glob(os.path.join(OUT, "??????????.json")), reverse=True):
        try:
            ic = dt.datetime.strptime(os.path.basename(f)[:10], "%Y%m%d%H")
        except ValueError:
            continue
        if ic < cut or ic.hour not in (0, 12):
            os.remove(f); print("removed", os.path.basename(f)); continue
        j = load_run(f) or {"models": {}}
        runs.append({"ic": ic.strftime("%Y-%m-%dT%H:%M"), "models": list(j["models"]), "shift": list(j.get("shift", {}))})
    ist = now + dt.timedelta(hours=5, minutes=30)
    json.dump({"runs": runs, "updated": "%s IST" % ist.strftime("%d %b %Y %H:%M"), "keep_days": KEEP_DAYS,
               "models": MODELS}, open(os.path.join(OUT, "manifest.json"), "w"), separators=(",", ":"))
    if added:
        open(os.path.join(WORK, "district_changed"), "w").close()
    print("manifest: %d runs; %d run files updated now" % (len(runs), added))


if __name__ == "__main__":
    main()
