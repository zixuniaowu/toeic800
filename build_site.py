#!/usr/bin/env python3
"""Build the TOEIC 边听边学 static site.

Usage:  python3 build_site.py [--src /workspace/toeic_podcast]

1. Scans SRC for epN.mp4 (ep1 prefers ep1_listen.mp4) + epN_script.pdf.
2. Extracts metadata: epN_meta.json if present, else parses the script PDF
   (pdftotext) for theme / segment table / review words; duration via ffprobe.
   overrides.json (per-episode) wins over everything.
3. Writes episodes.json, copies media into site/media/, regenerates HTML.
"""
import hashlib
import argparse, datetime as dt, html, json, os, re, shutil, subprocess, sys
from pathlib import Path

# Re-exec under the shared venv (edge-tts + cmudict) when run with plain python3.
VENV_PY = "/tmp/wp/bin/python"
try:
    import edge_tts, cmudict  # noqa: F401
except ImportError:
    if os.path.exists(VENV_PY) and os.path.realpath(sys.prefix) != os.path.realpath(os.path.dirname(os.path.dirname(VENV_PY))) \
            and not os.environ.get("TOEIC_NO_REEXEC"):
        os.environ["TOEIC_NO_REEXEC"] = "1"
        os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])
sys.path.insert(0, str(Path(__file__).resolve().parent))
import words as W  # noqa: E402

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
MEDIA = SITE / "media"
# MP4s are served by GitHub Pages, but NOT from this repo (1 GB site limit) and NOT from
# GitHub Releases (release assets are application/octet-stream + Content-Disposition:
# attachment, which iPhone Safari refuses to play). Each episode MP4 is re-encoded into a
# compact web copy (static cards: 2 fps, CRF 32, AAC 48k mono, +faststart) in WEB_STAGE
# (git-ignored), then committed to one of the public "media repos"
# zixuniaowu/toeic800-media1, -media2, ... (Pages from main /, served as video/mp4 with
# Range support). media_repos.json records which repo holds each file; a new file goes to
# the first repo with room under MEDIA_REPO_CAP, and a new repo is created (public, Pages
# enabled) automatically when all are full. Originals stay in /workspace/toeic_podcast.
MEDIA_OWNER = os.environ.get("TOEIC_MEDIA_OWNER", "zixuniaowu")
MEDIA_PREFIX = os.environ.get("TOEIC_MEDIA_PREFIX", "toeic800-media")
MEDIA_REPO_CAP = int(os.environ.get("TOEIC_MEDIA_REPO_CAP", 850 * 1000 * 1000))
MEDIA_CLONES = Path(os.environ.get("TOEIC_MEDIA_CLONES", "/workspace/toeic800_media"))
WEB_STAGE = ROOT / ".pages_media"
MEDIA_MANIFEST = ROOT / "media_repos.json"
ENC_ARGS = ["-map", "0:v:0", "-map", "0:a:0", "-vf", "fps=2,format=yuv420p",
            "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-tune", "stillimage",
            "-crf", "32", "-g", "60", "-c:a", "aac", "-b:a", "48k", "-ar", "24000", "-ac", "1",
            "-movflags", "+faststart"]
_DIRTY = set()


def _manifest():
    return json.loads(MEDIA_MANIFEST.read_text()) if MEDIA_MANIFEST.exists() else {"files": {}}


def media_url(repo, name):
    return f"https://{MEDIA_OWNER}.github.io/{repo}/{name}"


def vurl(name):
    f = _manifest()["files"].get(name)
    return media_url(f["repo"], name) if f else "pending:" + name  # fixed by fix_video_urls()


def _repo_usage(man):
    use = {}
    for f in man["files"].values():
        use[f["repo"]] = use.get(f["repo"], 0) + f["size"]
    return use


def ensure_media_repo(repo):
    """Local clone of a media repo; creates the public GitHub repo + Pages if missing."""
    d = MEDIA_CLONES / repo
    if (d / ".git").exists():
        return d
    MEDIA_CLONES.mkdir(parents=True, exist_ok=True)
    full = f"{MEDIA_OWNER}/{repo}"
    if subprocess.run(["gh", "repo", "view", full], capture_output=True).returncode != 0:
        subprocess.run(["gh", "repo", "create", full, "--public",
                        "--description", "Episode videos for https://zixuniaowu.github.io/toeic800/ (GitHub Pages media host)"],
                       check=True)
    r = subprocess.run(["gh", "repo", "clone", full, str(d)], capture_output=True)
    if r.returncode != 0 or not (d / ".git").exists():
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=d, check=True)
        subprocess.run(["git", "remote", "add", "origin", f"https://github.com/{full}.git"], cwd=d, check=True)
    (d / ".nojekyll").touch()
    if not (d / "index.html").exists():
        (d / "index.html").write_text('<!doctype html><meta charset="utf-8"><title>toeic800 media</title>'
                                      '<p>Video files for <a href="https://zixuniaowu.github.io/toeic800/">TOEIC 800</a>.</p>\n')
    _DIRTY.add(repo)
    return d


def stage_video(a, name):
    """Compact web encode into WEB_STAGE, assign it to a media repo, copy into its clone."""
    WEB_STAGE.mkdir(exist_ok=True)
    b = WEB_STAGE / name
    if not b.exists() or b.stat().st_mtime < a.stat().st_mtime:
        tmp = b.with_suffix(".tmp.mp4")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(a)] + ENC_ARGS + [str(tmp)], check=True)
        tmp.replace(b)
        print(f"encoded {name}: {a.stat().st_size/1e6:.1f} MB -> {b.stat().st_size/1e6:.1f} MB")
    size = b.stat().st_size
    man = _manifest()
    use = _repo_usage(man)
    cur = man["files"].get(name)
    repo = None
    if cur and use.get(cur["repo"], 0) - cur["size"] + size <= MEDIA_REPO_CAP:
        repo = cur["repo"]
    else:
        if cur:
            use[cur["repo"]] -= cur["size"]
        i = 1
        while use.get(f"{MEDIA_PREFIX}{i}", 0) + size > MEDIA_REPO_CAP:
            i += 1
        repo = f"{MEDIA_PREFIX}{i}"
    d = ensure_media_repo(repo)
    dst = d / name
    if not dst.exists() or dst.stat().st_size != size:
        shutil.copy2(b, dst)
        _DIRTY.add(repo)
    if cur and cur["repo"] != repo:
        old = MEDIA_CLONES / cur["repo"] / name
        if old.exists():
            old.unlink(); _DIRTY.add(cur["repo"])
    man["files"][name] = {"repo": repo, "size": size}
    MEDIA_MANIFEST.write_text(json.dumps(man, indent=1, sort_keys=True) + "\n")


def publish_media():
    """Commit + push changed media repos and make sure Pages is enabled on each."""
    if os.environ.get("TOEIC_NO_UPLOAD"):
        return
    repos = _DIRTY | {f["repo"] for f in _manifest()["files"].values()}
    for repo in sorted(repos):  # also retries repos left unpushed by an earlier failed run
        d = MEDIA_CLONES / repo
        if not (d / ".git").exists():
            continue
        subprocess.run(["git", "add", "-A"], cwd=d, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=d, capture_output=True)
        for k in ("user.name", "user.email"):  # reuse this repo's commit identity
            v = subprocess.run(["git", "config", k], cwd=ROOT, capture_output=True, text=True).stdout.strip()
            if v:
                subprocess.run(["git", "config", k, v], cwd=d, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=d).returncode != 0:
            subprocess.run(["git", "commit", "-qm", "Update episode videos"], cwd=d, check=True)
        subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=d, check=True)
        full = f"{MEDIA_OWNER}/{repo}"
        if subprocess.run(["gh", "api", f"repos/{full}/pages"], capture_output=True).returncode != 0:
            subprocess.run(["gh", "api", "-X", "POST", f"repos/{full}/pages",
                            "-f", "source[branch]=main", "-f", "source[path]=/"], check=True, capture_output=True)
        print("published", repo)


def fix_video_urls(obj):
    """Point every 'video' field at its media-repo Pages URL (by file name)."""
    files = _manifest()["files"]
    if isinstance(obj, list):
        for x in obj:
            fix_video_urls(x)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k == "video" and isinstance(v, str):
                name = v.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
                if name in files:
                    obj[k] = media_url(files[name]["repo"], name)
            else:
                fix_video_urls(v)


SITE_TITLE = "TOEIC 800 冲刺 · 边听边学"
TAGLINE = "每天一期、约 40 分钟的免费 TOEIC 听力课，英文原声 + 中文讲解，单词带逐字母拼读，专为 550→800 分的同学设计。边干活、边通勤就能听。"
COUNTER_PAGE_ID = "toeic800-listen"
COUNTER_URL = ("https://visitor-badge.laobi.icu/badge?page_id=" + COUNTER_PAGE_ID +
               "&left_text=views&left_color=%23555&right_color=%230ea5e9")
MAX_BYTES = 100 * 1024 * 1024
WEEK = "一二三四五六日"


def sh(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def mmss(sec):
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_ts(t):
    sec = 0
    for p in t.split(":"):
        sec = sec * 60 + int(p)
    return sec


def ffprobe_duration(p):
    try:
        return float(sh("ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(p)).strip())
    except Exception:
        return None


def parse_pdf(pdf):
    txt = sh("pdftotext", "-layout", str(pdf), "-")
    info = {}
    m = re.search(r"主题：([^。\n]+)", txt)
    if m:
        info["theme"] = m.group(1).strip()
    segs = []
    for mm in re.finditer(r"^\s*(\d{1,2}:\d{2})\s+(\d+\.\s.+?)\s{2,}(\d{1,2}:\d{2})\s*$", txt, re.M):
        segs.append({"title": mm.group(2).strip(), "start": parse_ts(mm.group(1)),
                     "length": parse_ts(mm.group(3))})
        if len(segs) >= 12:
            break
    # dedupe: the table appears once at the top
    seen, uniq = set(), []
    for s in segs:
        if s["title"] in seen:
            break
        seen.add(s["title"]); uniq.append(s)
    info["segments"] = uniq
    # review words: in the last section "[xx:xx] N. ... 复习", pattern: word line ... 主播：中文
    review = []
    m = re.search(r"\[\d{1,2}:\d{2}\]\s*\d+\.\s*[^\n]*复习.*", txt, re.S)
    if m:
        cur = None
        for line in m.group(0).splitlines():
            line = line.strip()
            w = re.match(r"^(?:美音|英音|澳音|加音|加拿大音)\s*·\s*\S+\s{2,}(.+)$", line)
            if w and not line.startswith("拼读"):
                cur = w.group(1).strip()
            z = re.match(r"^主播：(.+)$", line)
            if z and cur:
                review.append({"word": cur, "zh": z.group(1).strip().replace("，", "；").rstrip("；。 ")})
                cur = None
    info["review_words"] = review
    return info


def find_episodes(src):
    """Return {n: {"zh": (video, pdf)|None, "ja": (video, pdf)|None}}."""
    eps = {}
    for p in src.glob("ep*_script.pdf"):
        m = re.fullmatch(r"ep(\d+)(ja)?_script\.pdf", p.name)
        if not m:
            continue
        n = int(m.group(1))
        if str(n) in os.environ.get("TOEIC_SKIP_EPS", "").split(","):
            continue
        lang = "ja" if m.group(2) else "zh"
        video = None
        if lang == "zh":
            cands = ([f"ep{n}_listen.mp4"] if n == 1 else []) + [f"ep{n}.mp4"]
        else:
            cands = [f"ep{n}ja.mp4", f"ep{n}_ja.mp4"]
        for cand in cands:
            if (src / cand).exists():
                video = src / cand; break
        if video:
            eps.setdefault(n, {})[lang] = (video, p)
    return dict(sorted(eps.items()))


def moov_first(p):
    """True if the MP4 'moov' atom comes before 'mdat' (needed for fast web start)."""
    import struct
    with open(p, "rb") as f:
        while True:
            h = f.read(8)
            if len(h) < 8:
                return False
            sz, t = struct.unpack(">I4s", h)
            if t == b"moov":
                return True
            if t == b"mdat":
                return False
            if sz == 1:
                sz = struct.unpack(">Q", f.read(8))[0]; f.seek(sz - 16, 1)
            else:
                f.seek(sz - 8, 1)


def copy_video(a, b):
    """Copy video; remux with +faststart (no re-encode) if moov is at the end."""
    if b.exists() and b.stat().st_mtime >= a.stat().st_mtime and moov_first(b):
        return
    if moov_first(a):
        shutil.copy2(a, b)
    else:
        tmp = b.with_suffix(".tmp.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(a), "-c", "copy",
                        "-movflags", "+faststart", str(tmp)], check=True)
        tmp.replace(b)


def copy_if_changed(a, b):
    if b.exists() and b.stat().st_size == a.stat().st_size and b.stat().st_mtime >= a.stat().st_mtime:
        return
    shutil.copy2(a, b)


def build_metadata(src):
    overrides = {}
    if (ROOT / "overrides.json").exists():
        overrides = json.loads((ROOT / "overrides.json").read_text("utf-8"))
    old = {}
    if (ROOT / "episodes.json").exists():
        old = {e["number"]: e for e in json.loads((ROOT / "episodes.json").read_text("utf-8"))}
    MEDIA.mkdir(parents=True, exist_ok=True)
    out = []
    bank = W.vocab_bank()
    ipa_cache = W.load_cache()
    audio_jobs = []
    found = find_episodes(src)
    for n, langs in found.items():
        # Prefer Chinese meta as primary; Japanese becomes languages.ja
        primary_lang = "zh" if "zh" in langs else "ja"
        video, pdf = langs[primary_lang]
        info = parse_pdf(pdf)
        meta_p = src / (f"ep{n}_meta.json" if primary_lang == "zh" else f"ep{n}ja_meta.json")
        meta = json.loads(meta_p.read_text("utf-8")) if meta_p.exists() else {}
        if meta.get("theme"):
            info["theme"] = meta["theme"]
        if meta.get("stamps"):
            info["segments"] = [{"title": t, "start": round(s, 2), "length": round(l, 2)} for t, s, l in meta["stamps"]]
        segs = info.get("segments") or []
        dur = None
        if segs:
            dur = segs[-1]["start"] + segs[-1]["length"]
        dur = dur or meta.get("duration_sec") or ffprobe_duration(video) or 0
        hl = meta.get("highlight_words") or info.get("review_words", [])[:5]
        date = old.get(n, {}).get("date") or dt.date.fromtimestamp(video.stat().st_mtime).isoformat()
        ep = {
            "number": n,
            "theme": info.get("theme") or f"第 {n} 期",
            "date": date,
            "duration_sec": round(dur, 1),
            "segments": segs,
            "highlight_words": hl,
            "review_words": info.get("review_words", []),
            "video": vurl(f"ep{n}.mp4"),
            "script_pdf": f"media/ep{n}_script.pdf",
            "source_video": video.name,
            "languages": {},
        }
        # copy primary (zh naming)
        if primary_lang == "zh":
            stage_video(video, f"ep{n}.mp4")
            copy_if_changed(pdf, MEDIA / f"ep{n}_script.pdf")
            cover = src / f"ep{n}_cover.png"
            if cover.exists():
                copy_if_changed(cover, MEDIA / f"ep{n}_cover.png")
                ep["poster"] = f"media/ep{n}_cover.png"
            ep["languages"]["zh"] = {
                "label": "中文版",
                "page": f"ep{n}.html",
                "video": vurl(f"ep{n}.mp4"),
                "script_pdf": f"media/ep{n}_script.pdf",
                "duration_sec": round(dur, 1),
                "theme": ep["theme"],
                "segments": segs,
            }
        # Japanese variant
        if "ja" in langs:
            jv, jpdf = langs["ja"]
            jmeta_p = src / "ep{}ja_meta.json".format(n)
            jmeta = json.loads(jmeta_p.read_text("utf-8")) if jmeta_p.exists() else {}
            jinfo = parse_pdf(jpdf) if jpdf.exists() else {}
            if jmeta.get("theme"):
                jinfo["theme"] = jmeta["theme"]
            if jmeta.get("stamps"):
                jinfo["segments"] = [{"title": t, "start": round(s, 2), "length": round(l, 2)} for t, s, l in jmeta["stamps"]]
            jsegs = jinfo.get("segments") or []
            jdur = (jsegs[-1]["start"] + jsegs[-1]["length"]) if jsegs else (jmeta.get("duration_sec") or ffprobe_duration(jv) or 0)
            stage_video(jv, f"ep{n}ja.mp4")
            copy_if_changed(jpdf, MEDIA / f"ep{n}ja_script.pdf")
            jcover = src / f"ep{n}ja_cover.png"
            if jcover.exists():
                copy_if_changed(jcover, MEDIA / f"ep{n}ja_cover.png")
            ep["languages"]["ja"] = {
                "label": "日本語版",
                "page": f"ep{n}ja.html",
                "video": vurl(f"ep{n}ja.mp4"),
                "script_pdf": f"media/ep{n}ja_script.pdf",
                "duration_sec": round(jdur, 1),
                "theme": jinfo.get("theme") or jmeta.get("theme") or ep["theme"],
                "segments": jsegs,
                "poster": f"media/ep{n}ja_cover.png" if jcover.exists() else ep.get("poster"),
                "highlight_words": jmeta.get("highlight_words") or hl,
                "review_words": jinfo.get("review_words") or [],
                "words_meta": jmeta.get("words") or [],
            }
            ep["theme_ja"] = ep["languages"]["ja"]["theme"]
            if primary_lang == "ja":
                # no zh — promote ja fields to top-level video paths still use epNja
                ep["video"] = vurl(f"ep{n}ja.mp4")
                ep["script_pdf"] = f"media/ep{n}ja_script.pdf"
                ep["duration_sec"] = round(jdur, 1)
                ep["segments"] = jsegs
                ep["theme"] = ep["languages"]["ja"]["theme"]
        # ---- full word list with IPA + pronunciation MP3s (Chinese / primary)
        wl = meta.get("words") or []
        rows = W.word_rows(src, n, wl, bank) if wl else None
        if rows is None:
            rows = old.get(n, {}).get("words") if old.get(n, {}).get("words") and \
                [r["word"] for r in old[n]["words"]] == wl else None
        if rows:
            hl_set = {h["word"] for h in hl}
            rv_set = {r["word"] if isinstance(r, dict) else r
                      for r in (meta.get("review_words") or info.get("review_words", []))}
            wdir = MEDIA / "words" / f"ep{n}"
            for r in rows:
                sl = W.slug(r["word"])
                r["ipa"] = W.ipa_for(r["word"], ipa_cache)
                r["audio"] = f"media/words/ep{n}/{sl}.mp3"
                audio_jobs.append((r["word"], wdir / f"{sl}.mp3"))
                if r.get("ex"):
                    r["ex_audio"] = f"media/words/ep{n}/{sl}_ex.mp3"
                    audio_jobs.append((r["ex"], wdir / f"{sl}_ex.mp3"))
                r["highlight"] = r["word"] in hl_set
                r["review"] = r["word"] in rv_set
            ep["words"] = rows
            if "zh" in ep["languages"]:
                ep["languages"]["zh"]["has_words"] = True
            if "ja" in ep["languages"]:
                ja_rows = W.ja_rows(src, n, rows)
                ep["languages"]["ja"]["words"] = ja_rows
                jg = {r["word"]: r["zh"] for r in ja_rows}
                ep["languages"]["ja"]["highlight_words"] = [{"word": h["word"], "zh": jg.get(h["word"], "")} for h in hl]
                ep["languages"]["ja"]["has_words"] = True
        ep.update(overrides.get(str(n), {}))
        out.append(ep)
    W.save_cache(ipa_cache)
    failed = W.make_audio(audio_jobs)
    if failed:
        print(f"WARNING: {failed} pronunciation MP3s could not be generated (re-run later)")
    for e in out:  # don't link audio files that don't exist
        for r in e.get("words", []):
            for k in ("audio", "ex_audio"):
                if r.get(k) and not (SITE / r[k]).exists():
                    r.pop(k)
    out.sort(key=lambda e: e["number"], reverse=True)
    publish_media()
    fix_video_urls(out)
    (ROOT / "episodes.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return out




# ---------------------------------------------------------------- HTML
E = html.escape

CSS = """
:root{--bg:#f6f7fb;--card:#fff;--ink:#1f2937;--muted:#6b7280;--accent:#0ea5e9;--accent2:#6366f1;--line:#e5e7eb}
@media (prefers-color-scheme:dark){:root{--bg:#0f172a;--card:#1e293b;--ink:#e5e7eb;--muted:#94a3b8;--line:#334155}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Microsoft YaHei",sans-serif}
a{color:var(--accent);text-decoration:none}
.wrap{max-width:720px;margin:0 auto;padding:0 16px}
header.hero{background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;padding:28px 0 24px}
header.hero h1{margin:0 0 8px;font-size:24px;line-height:1.3}
header.hero p{margin:0;opacity:.92;font-size:15px}
header.bar{background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;padding:12px 0}
header.bar a{color:#fff;font-weight:600}
main{padding:20px 0 8px}
.list{list-style:none;margin:0;padding:0;display:grid;gap:12px}
.ep{display:flex;gap:14px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;color:inherit;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.ep:active{transform:scale(.99)}
.ep-wrap{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:0 1px 2px rgba(0,0,0,.04);overflow:hidden}
.ep-wrap .ep{border:0;box-shadow:none;border-radius:0}
.lang-pick{display:flex;gap:8px;padding:0 14px 12px 80px}
.lang-btn{flex:none;font-size:13px;padding:6px 12px;border-radius:999px;background:rgba(14,165,233,.12);color:var(--accent);border:1px solid rgba(14,165,233,.35);font-weight:600}
.lang-btn.ja{background:rgba(99,102,241,.12);color:var(--accent2);border-color:rgba(99,102,241,.35)}
.theme-ja{font-weight:500;color:var(--muted);font-size:14px}
.lang-switch{display:flex;gap:8px;margin:8px 0 12px}

.num{flex:none;width:52px;height:52px;border-radius:12px;background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:20px;line-height:1}
.num small{font-size:10px;font-weight:500;opacity:.9;margin-top:3px}
.ep h2{margin:0 0 4px;font-size:16px;line-height:1.4}
.meta{color:var(--muted);font-size:13px}
.badge-new{display:inline-block;background:#ef4444;color:#fff;font-size:11px;border-radius:6px;padding:0 6px;margin-left:6px;vertical-align:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
.card h2{font-size:17px;margin:0 0 10px}
h1.title{font-size:21px;margin:4px 0 4px;line-height:1.4}
video{width:100%;border-radius:12px;background:#000;display:block;aspect-ratio:16/9}
.tip{color:var(--muted);font-size:13px;margin:8px 0 0}
.segs{list-style:none;margin:0;padding:0}
.segs button{width:100%;display:flex;gap:12px;align-items:center;text-align:left;background:none;border:0;border-bottom:1px solid var(--line);padding:12px 2px;font:inherit;color:inherit;cursor:pointer}
.segs li:last-child button{border-bottom:0}
.segs .t{flex:none;font-variant-numeric:tabular-nums;color:var(--accent);font-weight:600;min-width:52px}
.segs .len{margin-left:auto;color:var(--muted);font-size:13px}
.segs button.on{background:rgba(14,165,233,.08);border-radius:8px}
.words{display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0;list-style:none}
.words li{background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.25);border-radius:10px;padding:6px 10px}
.words b{font-size:15px}
.words .sep{color:var(--muted)}
.words span{font-size:13px;color:var(--muted)}
.words a{color:inherit;display:block}
.card h2 .more{float:right;font-size:13px;font-weight:500}
.wlist{list-style:none;margin:0;padding:0}
.wd{border-top:1px solid var(--line);padding:12px 0;scroll-margin-top:12px}
.wd:first-child{border-top:0;padding-top:4px}
.wh{display:flex;align-items:flex-start;gap:10px}
.wn{flex:none;width:24px;height:24px;border-radius:7px;background:rgba(99,102,241,.12);color:var(--accent2);font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center;margin-top:2px}
.wt{flex:1;min-width:0}
.wt b{font-size:17px;word-break:break-word}
.ph{font-size:14px;color:var(--muted);margin-top:1px}
.ipa{font-family:"Charis SIL","Doulos SIL","Lucida Sans Unicode","Segoe UI",system-ui,sans-serif;margin-right:8px}
.pos{display:inline-block;font-size:12px;border:1px solid var(--line);border-radius:6px;padding:0 5px;font-style:italic}
.tag{display:inline-block;font-size:11px;border-radius:6px;padding:0 5px;margin-left:6px;vertical-align:2px}
.tag.hl{background:#fef3c7;color:#92400e}
.tag.rv{background:#dcfce7;color:#166534}
.say{flex:none;width:44px;height:44px;border-radius:50%;border:1px solid var(--line);background:var(--card);font-size:20px;line-height:1;cursor:pointer;-webkit-tap-highlight-color:transparent;padding:0}
.say.sm{width:34px;height:34px;font-size:15px}
.say.playing{background:rgba(14,165,233,.15);border-color:var(--accent)}
.wd .zh{margin:4px 0 0 34px;font-size:15px}
.ex{display:flex;gap:8px;align-items:flex-start;margin:8px 0 0 34px;background:rgba(14,165,233,.06);border-radius:10px;padding:8px}
.ex .en{font-size:14px;line-height:1.5}
.ex .exzh{font-size:13px;color:var(--muted)}
.btn{display:block;text-align:center;background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;border-radius:12px;padding:12px;font-weight:600}
.btn.ghost{background:none;color:var(--accent);border:1px solid var(--accent);margin-top:10px}
.nav{display:flex;justify-content:space-between;gap:10px;margin:6px 0 4px;font-size:14px}
footer{color:var(--muted);font-size:13px;text-align:center;padding:20px 0 36px}
footer .counter{display:inline-flex;align-items:center;gap:8px;margin-bottom:6px}
footer img{height:20px;vertical-align:middle}

/* ---- layout helpers (mobile: single column, unchanged order) ---- */
.side,.sidein{display:contents}
.sidenav,.sideact{display:none}
/* ---- desktop >=1024px ---- */
@media (min-width:1024px){
.wrap{max-width:1280px;padding:0 28px}
header.hero{padding:36px 0 32px}
header.hero h1{font-size:30px}
main{padding:28px 0 12px}
.home .list{grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:16px;margin-top:8px}
.home .ep-wrap{display:flex;flex-direction:column;transition:box-shadow .15s,transform .15s}
.home .ep-wrap:hover{box-shadow:0 6px 18px rgba(15,23,42,.10);transform:translateY(-1px)}
.home .ep-wrap .ep{flex:1;align-items:flex-start}
main.epgrid{display:grid;grid-template-columns:minmax(0,1fr) 360px;column-gap:28px;align-items:start}
main.epgrid>*{grid-column:1}
main.epgrid>.side{display:block;grid-column:2;grid-row:1/span 40;align-self:start;position:sticky;top:14px}
.sidein{display:flex;flex-direction:column;gap:12px;max-height:calc(100vh - 28px);overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin}
.sidein>.card{margin-bottom:0}
.sidenav{display:block;padding:10px 14px}
.sidenav .nav{margin:6px 0 0}
.sidehome{font-weight:600;font-size:14px}
.sideact{display:block}
.mainact{display:none}
.side .segs button{padding:9px 2px;font-size:14px}
.side .words li{padding:4px 8px}
.side .words b{font-size:14px}
main.epgrid video{max-height:min(64vh,600px)}
h1.title{font-size:24px}
.wlist{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}
.wd,.wd:first-child{border:1px solid var(--line);border-radius:12px;padding:12px}
}
@media (min-width:1600px){
.wrap{max-width:1560px}
.wlist{grid-template-columns:repeat(3,minmax(0,1fr))}
}
"""

JS = """
(function(){
  // word pronunciation: one shared Audio element, play() called inside the tap handler (iOS-safe)
  var au=new Audio(); au.preload='none'; var cur=null;
  function clear(){ if(cur){cur.classList.remove('playing'); cur=null;} }
  au.addEventListener('ended',clear); au.addEventListener('pause',clear); au.addEventListener('error',clear);
  document.addEventListener('click',function(ev){
    var b=ev.target.closest&&ev.target.closest('button.say'); if(!b) return;
    var vid=document.getElementById('player'); if(vid&&!vid.paused) vid.pause();
    var src=new URL(b.dataset.src,location.href).href;
    if(cur===b&&!au.paused){au.pause();return;}
    clear(); au.src=src; au.currentTime=0;
    var p=au.play(); cur=b; b.classList.add('playing');
    if(p&&p.catch) p.catch(function(){clear();});
  });
})();
(function(){
  var v=document.getElementById('player'); if(!v) return;
  var btns=[].slice.call(document.querySelectorAll('.segs button'));
  btns.forEach(function(b){b.addEventListener('click',function(){
    var t=parseFloat(b.dataset.t)||0;
    var go=function(){try{v.currentTime=t;}catch(e){} v.play&&v.play().catch(function(){});};
    if(v.readyState>=1){go();}else{v.addEventListener('loadedmetadata',go,{once:true}); v.load();}
    v.scrollIntoView({behavior:'smooth',block:'center'});
  });});
  v.addEventListener('timeupdate',function(){
    var t=v.currentTime,cur=null;
    btns.forEach(function(b){if(parseFloat(b.dataset.t)<=t+0.5)cur=b;});
    btns.forEach(function(b){b.classList.toggle('on',b===cur);});
  });
  // allow ep page links like #t=120
  var m=location.hash.match(/t=(\\d+)/); if(m){v.addEventListener('loadedmetadata',function(){v.currentTime=+m[1];},{once:true});}
})();
"""


def fmt_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.year}年{d.month}月{d.day}日（周{WEEK[d.weekday()]}）"


def page(title, body, head_extra=""):
    CSS_VER = hashlib.sha1(CSS.encode("utf-8")).hexdigest()[:10]
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{E(title)}</title>
<meta name="description" content="{E(TAGLINE)}">
<meta name="theme-color" content="#4f7cf0">
<link rel="stylesheet" href="style.css?v={CSS_VER}">
{head_extra}</head>
<body>
{body}
<footer class="wrap">
  <div class="counter">访问人数 <img src="{E(COUNTER_URL)}" alt="访问人数" loading="lazy" referrerpolicy="no-referrer-when-downgrade"></div>
  <div>题目与例句均为原创 · 语音为 AI 合成 · 仅供学习使用</div>
  <!-- GoatCounter (optional, later): create a free site at https://www.goatcounter.com and paste:
  <script data-goatcounter="https://YOURCODE.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>
  -->
</footer>
</body>
</html>
"""


def render_index(eps):
    items = []
    for i, e in enumerate(eps):
        new = '<span class="badge-new">最新</span>' if i == 0 else ""
        langs = e.get("languages") or {}
        lang_btns = ""
        if langs.get("zh") and langs.get("ja"):
            lang_btns = (f'<div class="lang-pick">'
                         f'<a class="lang-btn" href="ep{e["number"]}.html">中文版</a>'
                         f'<a class="lang-btn ja" href="ep{e["number"]}ja.html">日本語版</a>'
                         f'</div>')
            href = f"ep{e['number']}.html"
            theme_line = E(e["theme"])
            if e.get("theme_ja"):
                theme_line += f'<span class="theme-ja"> / {E(e["theme_ja"])}</span>'
        elif langs.get("ja") and not langs.get("zh"):
            href = f"ep{e['number']}ja.html"
            theme_line = E(e.get("theme_ja") or e["theme"])
            lang_btns = f'<div class="lang-pick"><a class="lang-btn ja" href="{href}">日本語版</a></div>'
        else:
            href = f"ep{e['number']}.html"
            theme_line = E(e["theme"])
        items.append(f"""  <li class="ep-wrap">
  <a class="ep" href="{href}">
    <div class="num">{e['number']}<small>期</small></div>
    <div><h2>{theme_line}{new}</h2>
    <div class="meta">约 {round(e['duration_sec']/60)} 分钟 · {fmt_date(e['date'])}</div></div>
  </a>
  {lang_btns}
  </li>""")
    body = f"""<header class="hero"><div class="wrap">
  <h1>{E(SITE_TITLE)}</h1>
  <p>{E(TAGLINE)}</p>
  <p style="margin-top:8px;opacity:.9;font-size:14px">第 8 期起提供 <b>中文版</b> / <b>日本語版</b> 双解说（真题精讲）。</p>
</div></header>
<main class="wrap home">
<ul class="list">
{chr(10).join(items)}
</ul>
<p class="tip">💡 iPhone 上如果在线播放卡住，可以点视频后“存储到文件”再离线播放。</p>
</main>"""
    return page(SITE_TITLE, body)




def render_wordlist(e, rows=None, ja=False):
    U = (lambda z, j: j) if ja else (lambda z, j: z)
    items = []
    rows = rows if rows is not None else e["words"]
    for i, r in enumerate(rows, 1):
        tags = ""
        if r.get("highlight"):
            tags += f'<span class="tag hl">{U("重点", "重点")}</span>'
        if r.get("review"):
            tags += f'<span class="tag rv">{U("复习", "復習")}</span>'
        say = (f'<button type="button" class="say" data-src="{E(r["audio"])}" aria-label="{U("播放 " + E(r["word"]) + " 的发音", E(r["word"]) + " の発音を再生")}">🔊</button>'
               if r.get("audio") else "")
        exsay = (f'<button type="button" class="say sm" data-src="{E(r["ex_audio"])}" aria-label="{U("播放例句", "例文を再生")}">🔊</button>'
                 if r.get("ex_audio") else "")
        ipa = f'<span class="ipa">{E(r["ipa"])}</span>' if r.get("ipa") else ""
        pos = f'<span class="pos">{E(r["pos"])}</span>' if r.get("pos") else ""
        ex = ""
        if r.get("ex"):
            ex = (f'<div class="ex">{exsay}<div><div class="en">{E(r["ex"])}</div>'
                  f'<div class="exzh">{E(r.get("exzh", ""))}</div></div></div>')
        items.append(f"""<li class="wd" id="w-{W.slug(r['word'])}">
  <div class="wh"><span class="wn">{i}</span><div class="wt"><b>{E(r['word'])}</b>{tags}<div class="ph">{ipa}{pos}</div></div>{say}</div>
  <div class="zh">{E(r.get('zh', ''))}</div>
  {ex}
</li>""")
    return (f'<section class="card" id="words"><h2>{U("本期 %d 词", "今回の %d 語") % len(rows)}</h2>'
            f'<p class="tip" style="margin:-4px 0 10px">{U("点 🔊 听单词发音（美音），例句旁的 🔊 听整句。音标为美式 IPA。", "🔊 をタップすると単語の発音（米音）、例文横の 🔊 で文全体を再生します。発音記号は米式 IPA です。")}</p>'
            f'<ol class="wlist">{"".join(items)}</ol></section>')


def render_episode(e, prev_e, next_e, lang="zh"):
    n = e["number"]
    langs = e.get("languages") or {}
    is_ja = lang == "ja" and "ja" in langs
    L = langs.get("ja") if is_ja else (langs.get("zh") or {})
    theme = (L.get("theme") if L else None) or (e.get("theme_ja") if is_ja else e["theme"]) or e["theme"]
    segs_data = (L.get("segments") if L and L.get("segments") else e["segments"])
    video = (L.get("video") if L and L.get("video") else e["video"])
    pdf = (L.get("script_pdf") if L and L.get("script_pdf") else e["script_pdf"])
    dur = (L.get("duration_sec") if L and L.get("duration_sec") else e["duration_sec"])
    poster_path = (L.get("poster") if is_ja and L else None) or e.get("poster")
    hl = (L.get("highlight_words") if is_ja and L and L.get("highlight_words") else e["highlight_words"])

    if is_ja:
        back = f"← {SITE_TITLE}"
        tip_seek = "下の時刻をタップすると該当パートへジャンプします。"
        tip_save = "iPhone では、先に「ファイルに保存」してから再生すると安定します。"
        dir_title = "今期の目次"
        hl_title = "今日の重点語"
        pdf_label = "📄 スクリプト PDF を開く"
        dl_label = "⬇️ 動画をダウンロード（オフライン）"
        page_lang = "ja"
        switch = ""
        if langs.get("zh"):
            switch = f'<div class="lang-switch"><a class="lang-btn" href="ep{n}.html">中文版</a><a class="lang-btn ja" href="ep{n}ja.html">日本語版 ✓</a></div>'
        title_h1 = f"第 {n} 回 · {E(theme)}"
        meta_line = f"約 {round(dur/60)} 分 · {fmt_date(e['date'])}"
        footer_note_unused = None
        words_section_skip_zh_list = True
    else:
        tip_seek = "点下面的时间可以直接跳到对应环节。"
        tip_save = "iPhone 上如果在线播放卡住，请先存储到文件再离线播放。"
        dir_title = "本期目录"
        hl_title = "今日重点词"
        pdf_label = "📄 打开讲稿 PDF（中英对照）"
        dl_label = "⬇️ 下载视频（离线听）"
        page_lang = "zh-CN"
        switch = ""
        if langs.get("ja"):
            switch = f'<div class="lang-switch"><a class="lang-btn" href="ep{n}.html">中文版 ✓</a><a class="lang-btn ja" href="ep{n}ja.html">日本語版</a></div>'
        title_h1 = f"第 {n} 期 · {E(theme)}"
        meta_line = f"约 {round(dur/60)} 分钟 · {fmt_date(e['date'])}"
        words_section_skip_zh_list = False

    segs = "\n".join(
        f'<li><button type="button" data-t="{s["start"]}"><span class="t">{mmss(s["start"])}</span>'
        f'<span>{E(s["title"])}</span><span class="len">{mmss(s["length"])}</span></button></li>'
        for s in segs_data)
    wrows = (L.get("words") if is_ja else e.get("words")) or []
    words = "\n".join((f'<li><a href="#w-{W.slug(w["word"])}">' if wrows else '<li><span>') +
                       f'<b>{E(w["word"])}</b><span class="sep"> — </span><span class="gl">{E(w.get("zh") or w.get("ja") or "")}</span>' +
                       ('</a></li>' if wrows else '</span></li>')
                       for w in hl)
    if wrows:
        review = render_wordlist(e, wrows, ja=is_ja)
    elif (not is_ja) and e.get("review_words"):
        rw = "\n".join(f'<li><b>{E(w["word"])}</b><span>{E(w["zh"])}</span></li>' for w in e["review_words"])
        review = f'<section class="card"><h2>本期复习 10 词</h2><ul class="words">{rw}</ul></section>'
    else:
        review = ""
    poster = f' poster="{E(poster_path)}"' if poster_path else ""
    nav = '<div class="nav">'
    if is_ja:
        nav += f'<a href="ep{prev_e["number"]}ja.html">← 第 {prev_e["number"]} 回</a>' if prev_e and (prev_e.get("languages") or {}).get("ja") else (
            f'<a href="ep{prev_e["number"]}.html">← 第 {prev_e["number"]} 期</a>' if prev_e else "<span></span>")
        nav += f'<a href="ep{next_e["number"]}ja.html">第 {next_e["number"]} 回 →</a>' if next_e and (next_e.get("languages") or {}).get("ja") else (
            f'<a href="ep{next_e["number"]}.html">第 {next_e["number"]} 期 →</a>' if next_e else "<span></span>")
    else:
        nav += f'<a href="ep{prev_e["number"]}.html">← 第 {prev_e["number"]} 期</a>' if prev_e else "<span></span>"
        nav += f'<a href="ep{next_e["number"]}.html">第 {next_e["number"]} 期 →</a>' if next_e else "<span></span>"
    nav += "</div>"
    all_eps = "📚 全エピソード一覧" if is_ja else "📚 全部节目"
    more = ""
    if wrows:
        more = (' <a class="more" href="#words">全 %d 語 ↓</a>' if is_ja else ' <a class="more" href="#words">全部 %d 词 ↓</a>') % len(wrows)
    body = f"""<header class="bar"><div class="wrap"><a href="index.html">← {E(SITE_TITLE)}</a></div></header>
<main class="wrap epgrid">
<h1 class="title">{title_h1}</h1>
<div class="meta" style="margin-bottom:8px">{meta_line}</div>
{switch}
<section class="card">
  <video id="player" controls playsinline preload="metadata"{poster}>
    <source src="{E(video)}" type="video/mp4">
  </video>
  <p class="tip">{tip_seek} {tip_save}</p>
</section>
<aside class="side"><div class="sidein">
<div class="card sidenav"><a class="sidehome" href="index.html">{all_eps}</a>{nav}</div>
<section class="card"><h2>{dir_title}</h2><ul class="segs">
{segs}
</ul></section>
<section class="card"><h2>{hl_title}{more}</h2><ul class="words">
{words}
</ul></section>
<section class="card sideact">
  <a class="btn" href="{E(pdf)}" target="_blank" rel="noopener">{pdf_label}</a>
  <a class="btn ghost" href="{E(video)}" download>{dl_label}</a>
</section>
</div></aside>
{review}
<section class="card mainact">
  <a class="btn" href="{E(pdf)}" target="_blank" rel="noopener">{pdf_label}</a>
  <a class="btn ghost" href="{E(video)}" download>{dl_label}</a>
</section>
{nav}
</main>
<script src="app.js"></script>"""
    # page() hardcodes lang=zh-CN — patch via replace for ja
    html = page(f"{title_h1} | {SITE_TITLE}", body)
    if is_ja:
        html = html.replace('lang="zh-CN"', 'lang="ja"')
        html = html.replace("题目与例句均为原创 · 语音为 AI 合成 · 仅供学习使用",
                            "本番問題は ETS 950+ 準拠の学習解説 · 音声は AI 合成 · 学習用途に限る")
    return html




def render(eps):
    SITE.mkdir(exist_ok=True)
    (SITE / "style.css").write_text(CSS.strip() + "\n", "utf-8")
    (SITE / "app.js").write_text(JS.strip() + "\n", "utf-8")
    (SITE / ".nojekyll").write_text("", "utf-8")
    (SITE / "episodes.json").write_text(json.dumps(eps, ensure_ascii=False, indent=2) + "\n", "utf-8")
    (SITE / "index.html").write_text(render_index(eps), "utf-8")
    keep = {"index.html"}
    for i, e in enumerate(eps):
        newer = eps[i - 1] if i > 0 else None
        older = eps[i + 1] if i + 1 < len(eps) else None
        (SITE / f"ep{e['number']}.html").write_text(render_episode(e, older, newer, lang="zh"), "utf-8")
        keep.add(f"ep{e['number']}.html")
        if (e.get("languages") or {}).get("ja"):
            (SITE / f"ep{e['number']}ja.html").write_text(render_episode(e, older, newer, lang="ja"), "utf-8")
            keep.add(f"ep{e['number']}ja.html")
    for p in SITE.glob("ep*.html"):
        if p.name not in keep:
            p.unlink()




def check_sizes():
    total, big = 0, []
    for p in SITE.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            if p.stat().st_size >= MAX_BYTES:
                big.append(p)
    print(f"site total: {total/1024/1024:.1f} MB")
    if total > 900 * 1024 * 1024:
        print("WARNING: site is near the 1 GB GitHub Pages limit")
    for p in big:
        print(f"WARNING: {p} is >= 100 MB (GitHub limit)")
    return not big


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/workspace/toeic_podcast")
    ap.add_argument("--no-scan", action="store_true", help="only re-render HTML from episodes.json")
    a = ap.parse_args()
    if a.no_scan:
        eps = json.loads((ROOT / "episodes.json").read_text("utf-8"))
    else:
        eps = build_metadata(Path(a.src))
    fix_video_urls(eps)
    for old_mp4 in MEDIA.glob("*.mp4"):  # videos live in the toeic800-mediaN repos, never here
        old_mp4.unlink()
    render(eps)
    for e in eps:
        print(f"ep{e['number']}: {e['theme']} | {mmss(e['duration_sec'])} | {e['date']} | "
              f"{len(e['segments'])} segs | {len(e['highlight_words'])} hl | {len(e.get('words', []))} words, "
              f"{sum(1 for r in e.get('words', []) if r.get('ipa'))} with IPA, "
              f"{sum(1 for r in e.get('words', []) if r.get('audio'))} mp3")
    sys.exit(0 if check_sizes() else 1)
