#!/usr/bin/env python3
"""Build the TOEIC 边听边学 static site.

Usage:  python3 build_site.py [--src /workspace/toeic_podcast]

1. Scans SRC for epN.mp4 (ep1 prefers ep1_listen.mp4) + epN_script.pdf.
2. Extracts metadata: epN_meta.json if present, else parses the script PDF
   (pdftotext) for theme / segment table / review words; duration via ffprobe.
   overrides.json (per-episode) wins over everything.
3. Writes episodes.json, copies media into site/media/, regenerates HTML.
"""
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
    eps = {}
    for p in src.glob("ep*_script.pdf"):
        m = re.fullmatch(r"ep(\d+)_script\.pdf", p.name)
        if not m:
            continue
        n = int(m.group(1))
        video = None
        for cand in ([f"ep{n}_listen.mp4"] if n == 1 else []) + [f"ep{n}.mp4"]:
            if (src / cand).exists():
                video = src / cand; break
        if video:
            eps[n] = (video, p)
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
    for n, (video, pdf) in find_episodes(src).items():
        info = parse_pdf(pdf)
        meta_p = src / f"ep{n}_meta.json"
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
            "video": f"media/ep{n}.mp4",
            "script_pdf": f"media/ep{n}_script.pdf",
            "source_video": video.name,
        }
        copy_video(video, MEDIA / f"ep{n}.mp4")
        copy_if_changed(pdf, MEDIA / f"ep{n}_script.pdf")
        cover = src / f"ep{n}_cover.png"
        if cover.exists():
            copy_if_changed(cover, MEDIA / f"ep{n}_cover.png")
            ep["poster"] = f"media/ep{n}_cover.png"
        # ---- full word list with IPA + pronunciation MP3s
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
.words b{display:block;font-size:15px}
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
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{E(title)}</title>
<meta name="description" content="{E(TAGLINE)}">
<meta name="theme-color" content="#4f7cf0">
<link rel="stylesheet" href="style.css">
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
        items.append(f"""  <li><a class="ep" href="ep{e['number']}.html">
    <div class="num">{e['number']}<small>期</small></div>
    <div><h2>{E(e['theme'])}{new}</h2>
    <div class="meta">约 {round(e['duration_sec']/60)} 分钟 · {fmt_date(e['date'])}</div></div>
  </a></li>""")
    body = f"""<header class="hero"><div class="wrap">
  <h1>{E(SITE_TITLE)}</h1>
  <p>{E(TAGLINE)}</p>
</div></header>
<main class="wrap">
<ul class="list">
{chr(10).join(items)}
</ul>
<p class="tip">💡 iPhone 上如果在线播放卡住，可以点视频后“存储到文件”再离线播放。</p>
</main>"""
    return page(SITE_TITLE, body)


def render_wordlist(e):
    items = []
    for i, r in enumerate(e["words"], 1):
        tags = ""
        if r.get("highlight"):
            tags += '<span class="tag hl">重点</span>'
        if r.get("review"):
            tags += '<span class="tag rv">复习</span>'
        say = (f'<button type="button" class="say" data-src="{E(r["audio"])}" aria-label="播放 {E(r["word"])} 的发音">🔊</button>'
               if r.get("audio") else "")
        exsay = (f'<button type="button" class="say sm" data-src="{E(r["ex_audio"])}" aria-label="播放例句">🔊</button>'
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
    return (f'<section class="card" id="words"><h2>本期 {len(e["words"])} 词</h2>'
            '<p class="tip" style="margin:-4px 0 10px">点 🔊 听单词发音（美音），例句旁的 🔊 听整句。音标为美式 IPA。</p>'
            f'<ol class="wlist">{"".join(items)}</ol></section>')


def render_episode(e, prev_e, next_e):
    n = e["number"]
    segs = "\n".join(
        f'<li><button type="button" data-t="{s["start"]}"><span class="t">{mmss(s["start"])}</span>'
        f'<span>{E(s["title"])}</span><span class="len">{mmss(s["length"])}</span></button></li>'
        for s in e["segments"])
    words = "\n".join(f'<li><a href="#w-{W.slug(w["word"])}"><b>{E(w["word"])}</b><span>{E(w["zh"])}</span></a></li>'
                       if e.get("words") else f'<li><b>{E(w["word"])}</b><span>{E(w["zh"])}</span></li>'
                       for w in e["highlight_words"])
    if e.get("words"):
        review = render_wordlist(e)
    elif e.get("review_words"):
        rw = "\n".join(f'<li><b>{E(w["word"])}</b><span>{E(w["zh"])}</span></li>' for w in e["review_words"])
        review = f'<section class="card"><h2>本期复习 10 词</h2><ul class="words">{rw}</ul></section>'
    else:
        review = ""
    poster = f' poster="{E(e["poster"])}"' if e.get("poster") else ""
    nav = '<div class="nav">'
    nav += f'<a href="ep{prev_e["number"]}.html">← 第 {prev_e["number"]} 期</a>' if prev_e else "<span></span>"
    nav += f'<a href="ep{next_e["number"]}.html">第 {next_e["number"]} 期 →</a>' if next_e else "<span></span>"
    nav += "</div>"
    body = f"""<header class="bar"><div class="wrap"><a href="index.html">← {E(SITE_TITLE)}</a></div></header>
<main class="wrap">
<h1 class="title">第 {n} 期 · {E(e['theme'])}</h1>
<div class="meta" style="margin-bottom:12px">约 {round(e['duration_sec']/60)} 分钟 · {fmt_date(e['date'])}</div>
<section class="card">
  <video id="player" controls playsinline preload="metadata"{poster}>
    <source src="{E(e['video'])}" type="video/mp4">
    你的浏览器不支持视频播放，<a href="{E(e['video'])}">点此下载</a>。
  </video>
  <p class="tip">点下面的时间可以直接跳到对应环节。</p>
</section>
<section class="card"><h2>本期目录</h2><ul class="segs">
{segs}
</ul></section>
<section class="card"><h2>今日重点词{' <a class="more" href="#words">全部 %d 词 ↓</a>' % len(e["words"]) if e.get("words") else ""}</h2><ul class="words">
{words}
</ul></section>
{review}
<section class="card">
  <a class="btn" href="{E(e['script_pdf'])}" target="_blank" rel="noopener">📄 打开讲稿 PDF（中英对照）</a>
  <a class="btn ghost" href="{E(e['video'])}" download>⬇️ 下载视频（离线听）</a>
</section>
{nav}
</main>
<script src="app.js"></script>"""
    return page(f"第 {n} 期 · {e['theme']} | {SITE_TITLE}", body)


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
        (SITE / f"ep{e['number']}.html").write_text(render_episode(e, older, newer), "utf-8")
        keep.add(f"ep{e['number']}.html")
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
    render(eps)
    for e in eps:
        print(f"ep{e['number']}: {e['theme']} | {mmss(e['duration_sec'])} | {e['date']} | "
              f"{len(e['segments'])} segs | {len(e['highlight_words'])} hl | {len(e.get('words', []))} words, "
              f"{sum(1 for r in e.get('words', []) if r.get('ipa'))} with IPA, "
              f"{sum(1 for r in e.get('words', []) if r.get('audio'))} mp3")
    sys.exit(0 if check_sizes() else 1)
