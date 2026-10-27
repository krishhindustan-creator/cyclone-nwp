#!/usr/bin/env python3
"""
nwp_runner.py — runs the ECMWF ensemble and the IMD MME 7-model scripts for the latest run,
adds the results to the web site folder, keeps the last KEEP_DAYS days and writes manifest.json.
Called by GitHub Actions (.github/workflows/nwp.yml); can also be run by hand:
    RUN_ID=2026102600 IMD_PWD=... python nwp_runner.py
Environment:  RUN_ID (YYYYMMDDHH, default: 04–15 UTC -> today 00 UTC, otherwise the latest 12 UTC run), IMD_PWD (secret),
              KEEP_DAYS (default 3), FORCE=1 (redo a run already on the site), SITE (default ./site)
"""
import datetime as dt, glob, json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.abspath(os.environ.get("SITE", os.path.join(HERE, "site")))
WORK = os.path.abspath(os.path.join(HERE, "work"))
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "3"))
FORCE = os.environ.get("FORCE", "") not in ("", "0", "false", "False")

def latest_run():
    """04–15 UTC -> today's 00 UTC run; 16–23 UTC -> today's 12 UTC run; 00–03 UTC -> yesterday's 12 UTC run."""
    now = dt.datetime.utcnow()
    if 4 <= now.hour < 16: return now.strftime("%Y%m%d") + "00"
    if now.hour >= 16:     return now.strftime("%Y%m%d") + "12"
    return (now - dt.timedelta(days=1)).strftime("%Y%m%d") + "12"

RUN_ID = (os.environ.get("RUN_ID") or "").strip() or latest_run()
IC_T = dt.datetime.strptime(RUN_ID, "%Y%m%d%H")
LABEL = f"{IC_T:%H} UTC {IC_T.day} {IC_T:%b %Y}"
os.makedirs(SITE, exist_ok=True); os.makedirs(WORK, exist_ok=True)
print(f"Run {RUN_ID} ({LABEL}); site {SITE}; keep {KEEP_DAYS:g} days")

def headless(script, out):
    """Colab script -> plain Python: drop '!pip' lines, set DATE/RUN, password from IMD_PWD."""
    s = open(os.path.join(HERE, script), encoding="utf-8").read()
    s = "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("!"))
    s = re.sub(r'^DATE\s*=.*$', f'DATE  = "{RUN_ID[:8]}"', s, count=1, flags=re.M)
    s = re.sub(r'^RUN\s*=.*$', f'RUN   = "{RUN_ID[8:]}"', s, count=1, flags=re.M)
    s = ("import getpass as _gp, os as _os\n_gp.getpass = lambda prompt='': _os.environ.get('IMD_PWD', '')\n"
         "import matplotlib; matplotlib.use('Agg')\n") + s
    open(out, "w", encoding="utf-8").write(s)

def run(script, tag, minutes):
    path = os.path.join(WORK, f"{tag}_headless.py"); headless(script, path)
    print(f"\n===== {script} =====", flush=True)
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, path], cwd=WORK, capture_output=True, text=True, timeout=minutes * 60)
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        out += f"\n[timed out after {minutes} min]"
    print(out[-6000:], flush=True)
    print(f"({(time.time() - t0) / 60:.1f} min)")
    return out

def done(kind):
    """ENS: done once published. MME: done only when it was complete (every model's own run, none missing);
    otherwise it is rebuilt at the next update so late models are added."""
    f = os.path.join(SITE, kind, RUN_ID, "meta.json")
    if FORCE or not os.path.exists(f): return False
    return kind == "ens" or json.load(open(f)).get("complete", False)

def publish(kind, items, files, note, complete=True):
    d = os.path.join(SITE, kind, RUN_ID); shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    its, fls = [], []
    for title, src in items:
        if src and os.path.exists(src):
            fn = os.path.basename(src); shutil.copy(src, os.path.join(d, fn)); its.append({"t": title, "f": f"{kind}/{RUN_ID}/{fn}"})
    for title, src in files:
        if src and os.path.exists(src):
            fn = os.path.basename(src); shutil.copy(src, os.path.join(d, fn)); fls.append({"t": title, "f": f"{kind}/{RUN_ID}/{fn}"})
    if not its:
        shutil.rmtree(d, ignore_errors=True); print(f"{kind}: nothing to publish"); return False
    json.dump({"ic": RUN_ID, "label": LABEL, "items": its, "files": fls, "note": note, "complete": complete,
               "made": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}, open(os.path.join(d, "meta.json"), "w"))
    print(f"{kind}: published {len(its)} figures, {len(fls)} files"); return True

def grab(text, pattern):
    m = re.search(pattern, text, re.M); return m.group(1).strip() if m else ""

# ---------------- 1. ECMWF ensemble ----------------
if done("ens"):
    print("ECMWF ENS already on the site for this run (set FORCE=1 to redo)")
else:
    out = run("ECMWF_ensemble_colab.py", "ens", 30)
    P = os.path.join(WORK, f"ECMWF_ENS_{RUN_ID}", f"ECMWF_ENS_package_{RUN_ID}")
    Z = os.path.join(WORK, f"ECMWF_ENS_{RUN_ID}", f"ECMWF_ENS_package_{RUN_ID}.zip")
    lines = [l.strip() for l in out.splitlines() if re.match(r"\s+(Ensemble-mean|HRES|Members|Median|Most likely)", l)]
    note = ("<br>".join(re.sub(r"\s{2,}", ": ", l, count=1) for l in lines)) if lines else ""
    publish("ens", [("Landfall summary", f"{P}/ECMWF_ENS_landfall_summary.png"), ("Spaghetti tracks", f"{P}/E1_ENS_spaghetti.png"),
                    ("Strike probability", f"{P}/E2_ENS_strike_probability.png"), ("MSW plume", f"{P}/E3_ENS_MSW_plume.png"),
                    ("MSLP plume", f"{P}/E4_ENS_MSLP_plume.png")],
            [("Package (zip)", Z), ("Ensemble-mean track", f"{P}/ECMWF_ENSMEAN_track.txt"), ("HRES track", f"{P}/ECMWF_HRES_track.txt"),
             ("Member landfalls (csv)", f"{P}/ECMWF_ENS_member_landfalls.csv")], note)

# ---------------- 2. IMD MME 7 models ----------------
if done("mme"):
    print("MME already complete on the site for this run (set FORCE=1 to redo)")
else:
    out = run("MME_7models_colab.py", "mme", 110)
    P = os.path.join(WORK, f"MME7_{RUN_ID}", f"MME7_package_{RUN_ID}")
    Z = os.path.join(WORK, f"MME7_{RUN_ID}", f"MME7_package_{RUN_ID}.zip")
    F = os.path.join(P, "figures")
    hdr = grab(out, r"^(#MME-Mean of .*)$").lstrip("#"); lf = grab(out, r"^%%Landfall (.*?)%%")
    older = grab(out, r"Older runs used:\s*(.*)$"); members = grab(out, r"MME members:\s*(.*)$")
    note = []
    if hdr: note.append(hdr)
    if lf:
        p_ = lf.split()
        try: note.append(f"MME landfall {p_[0][6:8]}/{p_[0][8:]} UTC at {float(p_[1]):.2f}°N {float(p_[2]):.2f}°E, {float(p_[3]):.0f} kt"
                         + ("" if p_[4].lower() == "nan" else f", {float(p_[4]):.0f} hPa"))
        except Exception: note.append("MME landfall " + lf)
    if older: note.append("Older runs used: " + older)
    skipped = [l.strip() for l in out.splitlines() if re.search(r"\s(skipped|FAILED|stopped)", l) and l.startswith("  ")]
    if skipped: note.append("Not included: " + "; ".join(s.split("  ")[0] for s in skipped[:6]))
    publish("mme", [("Combined: track, MSW, MSLP", f"{F}/0_combined_track_MSW_MSLP.png"), ("Model tracks", f"{F}/1_model_tracks.png"),
                    ("MSW, all models", f"{F}/2_model_MSW.png"), ("MSLP, all models", f"{F}/3_model_MSLP.png"),
                    ("MME track and MSW", f"{F}/4_MME_track_MSW.png"), ("MME MSLP and 6-hourly table", f"{F}/5_MME_MSLP_table.png"),
                    ("Landfall table", f"{F}/6_landfall_table.png")],
            [("Package (zip)", Z), ("MMM_track.txt (IMD format)", f"{P}/MMM_track.txt")], "<br>".join(note),
            complete=not older and not [x for x in skipped if "not numbered" not in x])

# ---------------- 3. keep KEEP_DAYS days, write manifest ----------------
cut = dt.datetime.utcnow() - dt.timedelta(days=KEEP_DAYS)
man = {"ens": [], "mme": []}
for kind in man:
    for d in sorted(glob.glob(os.path.join(SITE, kind, "*")), reverse=True):
        ic = os.path.basename(d)
        try: t = dt.datetime.strptime(ic, "%Y%m%d%H")
        except ValueError: continue
        if t < cut:
            shutil.rmtree(d, ignore_errors=True); print(f"removed {kind}/{ic} (older than {KEEP_DAYS:g} days)"); continue
        try: man[kind].append(json.load(open(os.path.join(d, "meta.json"))))
        except Exception: pass
ist = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
man["updated"] = f"{ist:%d %b %Y %H:%M} IST"
json.dump(man, open(os.path.join(SITE, "manifest.json"), "w"), separators=(",", ":"))
shutil.copy(os.path.join(HERE, "index.html"), os.path.join(SITE, "index.html"))
open(os.path.join(SITE, ".nojekyll"), "w").close()
print(f"\nmanifest: {len(man['ens'])} ENS runs, {len(man['mme'])} MME runs")
