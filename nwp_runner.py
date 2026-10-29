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
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "15"))
FORCE = os.environ.get("FORCE", "") not in ("", "0", "false", "False")

def latest_run():
    """04–15 UTC -> today's 00 UTC run; 16–23 UTC -> today's 12 UTC run; 00–03 UTC -> yesterday's 12 UTC run."""
    now = dt.datetime.utcnow()
    if 4 <= now.hour < 16: return now.strftime("%Y%m%d") + "00"
    if now.hour >= 16:     return now.strftime("%Y%m%d") + "12"
    return (now - dt.timedelta(days=1)).strftime("%Y%m%d") + "12"

IMD_MODELS = os.environ.get("IMD_MODELS", "on").strip().lower()       # "on" (IMD files are small now) or "off"
if IMD_MODELS != "on":
    os.environ["IMD_PWD"] = ""     # IMD GFS / NCUM-G / NCUM-R skipped: IMD's server is too slow from GitHub
RUN_ID = (os.environ.get("RUN_ID") or "").strip() or latest_run()

# Several runs in one go: RUN_ID = "backfill" (every 00/12 UTC run of the last KEEP_DAYS days)
# or a comma-separated list, e.g. "2026102500,2026102512". Each run is done by a child process.
if RUN_ID.lower() in ("backfill", "all") or "," in RUN_ID:
    if "," in RUN_ID:
        ids = [x.strip() for x in RUN_ID.split(",") if x.strip()]
    else:
        last = dt.datetime.strptime(latest_run(), "%Y%m%d%H"); t = last - dt.timedelta(days=KEEP_DAYS) + dt.timedelta(hours=12)
        t = t.replace(hour=0 if t.hour < 12 else 12, minute=0, second=0, microsecond=0)
        ids = []
        while t <= last: ids.append(t.strftime("%Y%m%d%H")); t += dt.timedelta(hours=12)
    print("Processing runs:", ", ".join(ids), flush=True)
    for rid in ids:
        env = dict(os.environ, RUN_ID=rid)
        subprocess.run([sys.executable, "-u", os.path.abspath(__file__)], env=env)
    sys.exit(0)
IC_T = dt.datetime.strptime(RUN_ID, "%Y%m%d%H")
LABEL = f"{IC_T:%H} UTC {IC_T.day} {IC_T:%b %Y}"
os.makedirs(SITE, exist_ok=True); os.makedirs(WORK, exist_ok=True)
print(f"Run {RUN_ID} ({LABEL}); site {SITE}; keep {KEEP_DAYS:g} days; IMD models {'on' if IMD_MODELS == 'on' else 'off'}")

def headless(script, out):
    """Colab script -> plain Python: drop '!pip' lines, set DATE/RUN, password from IMD_PWD."""
    s = open(os.path.join(HERE, script), encoding="utf-8").read()
    s = "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("!"))
    s = re.sub(r'^DATE\s*=.*$', f'DATE  = "{RUN_ID[:8]}"', s, count=1, flags=re.M)
    s = re.sub(r'^RUN\s*=.*$', f'RUN   = "{RUN_ID[8:]}"', s, count=1, flags=re.M)
    # on GitHub the IMD step may take at most IMD_MAX_MINUTES (default 10), then the MME goes on without it
    s = re.sub(r'^IMD_MAX_MINUTES\s*=.*$', f'IMD_MAX_MINUTES = {float(os.environ.get("IMD_MAX_MINUTES", "10")):g}', s, count=1, flags=re.M)
    s = ("import getpass as _gp, os as _os\n_gp.getpass = lambda prompt='': _os.environ.get('IMD_PWD', '')\n"
         "import matplotlib; matplotlib.use('Agg')\n") + s
    open(out, "w", encoding="utf-8").write(s)

def run(script, tag, minutes):
    path = os.path.join(WORK, f"{tag}_headless.py"); headless(script, path)
    print(f"\n===== {script} =====", flush=True)
    t0 = time.time(); lines = []
    # stream the output live (so the GitHub log shows progress) and keep a copy for the notes
    p = subprocess.Popen([sys.executable, "-u", path], cwd=WORK, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    try:
        for line in p.stdout:
            print(line, end="", flush=True); lines.append(line)
            if time.time() - t0 > minutes * 60:
                p.kill(); lines.append(f"\n[timed out after {minutes} min]\n"); print(lines[-1]); break
        p.wait(timeout=60)
    except Exception as e:
        p.kill(); lines.append(f"\n[stopped: {e}]\n")
    print(f"({(time.time() - t0) / 60:.1f} min)", flush=True)
    return "".join(lines)

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
    publish("ens", [("Comparison with previous 2 runs", f"{P}/ECMWF_ENS_comparison_last3runs.png"),
                    ("Landfall summary", f"{P}/ECMWF_ENS_landfall_summary.png"), ("Spaghetti tracks", f"{P}/E1_ENS_spaghetti.png"),
                    ("Strike probability", f"{P}/E2_ENS_strike_probability.png"), ("MSW plume", f"{P}/E3_ENS_MSW_plume.png"),
                    ("MSLP plume", f"{P}/E4_ENS_MSLP_plume.png")],
            [("Package (zip)", Z), ("Ensemble-mean track", f"{P}/ECMWF_ENSMEAN_track.txt"), ("HRES track", f"{P}/ECMWF_HRES_track.txt"),
             ("Member landfalls (csv)", f"{P}/ECMWF_ENS_member_landfalls.csv")], note)

# ---------------- 2. IMD MME 7 models ----------------
if done("mme"):
    print("MME already complete on the site for this run (set FORCE=1 to redo)")
else:
    out = run("MME_7models_colab.py", "mme", 60 if IMD_MODELS == "on" else 40)
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
    skipped = [x for x in skipped if "skipped by user" not in x]          # IMD switched off on purpose
    if IMD_MODELS != "on": note.append("IMD GFS, NCUM-G and NCUM-R are not run on GitHub (IMD server too slow from there) — use the Colab MME script for them")
    if skipped: note.append("Not included: " + "; ".join(s.split("  ")[0] for s in skipped[:6]))
    publish("mme", [("Combined: track, MSW, MSLP", f"{F}/0_combined_track_MSW_MSLP.png"), ("Model tracks", f"{F}/1_model_tracks.png"),
                    ("MSW, all models", f"{F}/2_model_MSW.png"), ("MSLP, all models", f"{F}/3_model_MSLP.png"),
                    ("MME track and MSW", f"{F}/4_MME_track_MSW.png"), ("MME MSLP and 6-hourly table", f"{F}/5_MME_MSLP_table.png"),
                    ("Landfall table", f"{F}/6_landfall_table.png")],
            [("Package (zip)", Z), ("MMM_track.txt (IMD format)", f"{P}/MMM_track.txt")], "<br>".join(note),
            complete=not older and not [x for x in skipped if "not numbered" not in x])

# ---------------- 2b. A-Deck (from the MME package) ----------------
A = os.path.join(WORK, f"MME7_{RUN_ID}", f"MME7_package_{RUN_ID}", "adeck")
if os.path.isdir(A) and not done("adeck"):
    names = {"NGX_track.txt": "NAVGEM (US Navy)", "UKM_track.txt": "UKMO", "CMC_ADECK_track.txt": "CMC (a-deck)",
             "GEFS_MEAN_track.txt": "GEFS ensemble mean", "CENS_MEAN_track.txt": "Canadian ensemble mean",
             "NAVENS_MEAN_track.txt": "Navy ensemble mean"}
    present = [v for k, v in names.items() if os.path.exists(os.path.join(A, k))]
    storm_ = grab(out, r"a-deck: storm (io\d+)").upper() if "out" in globals() else ""
    note = ["JTWC/UCAR a-deck model tracks" + (f" — storm {storm_}" if storm_ else "")]
    if present: note.append("Models: " + ", ".join(present))
    note.append("NAVGEM is also an MME member; the other a-deck models are shown here for comparison only")
    publish("adeck", [("A-Deck model tracks", os.path.join(A, "ADECK_quicklook.png"))],
            [(f"{v} track", os.path.join(A, k)) for k, v in names.items()], "<br>".join(note), complete=False)
elif not os.path.isdir(A):
    print("adeck: no a-deck this run (system not numbered by JTWC, or a-deck not available)")

# ---------------- 3. keep KEEP_DAYS days, write manifest ----------------
cut = dt.datetime.utcnow() - dt.timedelta(days=KEEP_DAYS)
man = {"ens": [], "mme": [], "adeck": []}
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
man["keep_days"] = KEEP_DAYS

def ens_compare_png(runs, out):
    """WhatsApp-size table comparing the kept ECMWF ENS runs (oldest -> newest), built from their notes."""
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt, textwrap
    cols = list(reversed(runs))[-6:]
    S = []
    for r in cols:
        o = {}
        for l in (r.get("note") or "").split("<br>"):
            if ": " in l: k, v = l.split(": ", 1); o[k.strip()] = v.strip()
        S.append(o)
    keys = []
    for o in S:
        for k in o:
            if k not in keys: keys.append(k)
    if len(cols) < 2 or not keys: return False
    wrap = lambda v: "\n".join(textwrap.wrap(v, 18)) if v else "–"
    rows = [[k] + [wrap(o.get(k, "")) for o in S] for k in keys]
    hdr = [""] + [r["label"].rsplit(" ", 1)[0].replace(" UTC ", " UTC\n") for r in cols]
    w = 2.6 + 2.0 * len(cols)
    fig = plt.figure(figsize=(w, 1.05 * len(rows) + 2.2), dpi=150)
    fig.text(0.5, 0.97, "ECMWF Ensemble — run comparison", ha="center", va="top", fontsize=16, fontweight="bold", color="#1f3b63")
    fig.text(0.5, 0.925, f"Last {len(cols)} runs (oldest → newest)  ·  {man['updated']}", ha="center", va="top", fontsize=10)
    ax = fig.add_axes([0.02, 0.06, 0.96, 0.82]); ax.axis("off")
    tb = ax.table(cellText=rows, colLabels=hdr, cellLoc="center", bbox=[0, 0, 1, 1],
                  colWidths=[2.6 / w] + [2.0 / w] * len(cols))
    tb.auto_set_font_size(False); tb.set_fontsize(8.6)
    for (i, j), c in tb.get_celld().items():
        c.set_edgecolor("#bbb")
        if i == 0: c.set_facecolor("#1f3b63" if j else "white"); c.get_text().set_color("white"); c.get_text().set_fontweight("bold")
        elif j == 0: c.set_facecolor("#eef2f7"); c.get_text().set_fontweight("bold")
        elif j == len(cols): c.set_facecolor("#fff6cc"); c.get_text().set_fontweight("bold")
    fig.text(0.5, 0.02, "Source: ECMWF open data (CC BY 4.0); own analysis", ha="center", fontsize=8, style="italic", color="#555")
    plt.savefig(out, dpi=150, facecolor="white", bbox_inches="tight", pad_inches=0.2); plt.close()
    return True
try:
    if ens_compare_png(man["ens"], os.path.join(SITE, "ens", "ENS_comparison.png")):
        man["ens_compare"] = "ens/ENS_comparison.png"; print("ENS comparison table written")
except Exception as e:
    print("ENS comparison table failed:", e)
json.dump(man, open(os.path.join(SITE, "manifest.json"), "w"), separators=(",", ":"))
shutil.copy(os.path.join(HERE, "index.html"), os.path.join(SITE, "index.html"))
open(os.path.join(SITE, ".nojekyll"), "w").close()
print(f"\nmanifest: {len(man['ens'])} ENS runs, {len(man['mme'])} MME runs, {len(man['adeck'])} A-Deck runs")
