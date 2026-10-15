# TOEIC 800 冲刺 · 边听边学 — static site

- `site/` is the publishable website (index.html, epN.html, style.css, app.js, media/, .nojekyll).
- `episodes.json` is the generated data file (also copied to `site/episodes.json`).
- `overrides.json` holds manual per-episode fixes (e.g. ep1 theme, which isn't in its PDF).

## Add a new episode
1. Put `epN.mp4` + `epN_script.pdf` (optional: `epN_meta.json`, `epN_cover.png`) in `/workspace/toeic_podcast/`.
2. `python3 build_site.py`  (scans, extracts metadata, copies media with +faststart, regenerates HTML, checks 100 MB limit)
3. `git add -A && git commit -m "ep N" && git push`  (the Pages workflow deploys `site/`)

Metadata priority: overrides.json > epN_meta.json > script PDF (pdftotext) > ffprobe.
Re-render HTML only (after hand-editing episodes.json): `python3 build_site.py --no-scan`.

Visitor counter: https://visitor-badge.laobi.icu (no signup), page_id `toeic800-listen`. It counts page views (every load of any page), not unique visitors.

## Word list + pronunciation (本期 30 词)
`build_site.py` (via `words.py`) adds every episode word to its page automatically:
- word data: `epN*_content.py` (WORDS must equal `epN_meta.json` words) + VOCAB_EXTRA, resolved against `/workspace/toeic_src/day*.py` VOCAB.
- IPA (US): CMU Pronouncing Dictionary (`cmudict`) → IPA; fallback dictionaryapi.dev (US-tagged only); otherwise blank. Cached in `ipa_cache.json`; fix a word by adding it to `"overrides"`.
- MP3: edge-tts `en-US-AriaNeural` (24 kHz, 48 kbps mono) → `site/media/words/epN/<slug>.mp3` and `<slug>_ex.mp3` (example sentence). Only missing files are generated.
- Needs the venv `/tmp/wp` (edge-tts, cmudict); `build_site.py` re-execs itself there automatically. If /tmp/wp is gone:
  `python3 -m venv /tmp/wp && /tmp/wp/bin/pip install edge-tts cmudict`.

## Japanese pages (aligned, ep8+)
`epNja.html` is rendered from the same rows as `epN.html` (`words.ja_rows`): same word list, IPA, POS, MP3s, examples, tags; only gloss / example translation come from `epNja_content.py`, which is itself derived 1:1 from `epN_content.py` via `/workspace/toeic_podcast/ja_aligned.py`. Highlight words use `word — gloss` on both languages.

## Video hosting (GitHub Pages media repos)

MP4s are **not** in `site/media/` (Pages 1 GB limit) and **not** on GitHub Releases any more:
release assets redirect to `release-assets.githubusercontent.com` with `application/octet-stream`
+ `Content-Disposition: attachment`, which iPhone Safari refuses to play (crossed-out play icon).
The old release `media-v1` is no longer linked (kept only as a backup).

`build_site.py` now, for every `epN.mp4` / `epNja.mp4`:
1. re-encodes a compact web copy into `.pages_media/` (git-ignored): 2 fps, x264 CRF 32
   `-tune stillimage`, GOP 60, AAC 48 kbps 24 kHz mono, `+faststart` (~9 MB per ~55 min vs ~50 MB).
   Re-encodes only when the source in `/workspace/toeic_podcast` is newer. Originals stay there (chat delivery).
2. assigns it to a public media repo `zixuniaowu/toeic800-media1`, `-media2`, … (first one with room
   under 850 MB, `TOEIC_MEDIA_REPO_CAP`); if all are full it **creates the next repo automatically**
   (`gh repo create --public`, Pages from `main` /). Local clones live in `/workspace/toeic800_media/<repo>`
   (re-cloned with `gh repo clone` if missing). `media_repos.json` (committed) maps file → repo + size.
3. commits + pushes the changed media repos, then writes the URLs into episodes.json / pages:
   `https://zixuniaowu.github.io/toeic800-mediaN/epN.mp4` (served as `video/mp4`, Range → 206).

Daily routine (6:29): put files in `/workspace/toeic_podcast/` → `python3 build_site.py` → commit + push this
repo (include `media_repos.json`) → wait for both the media repo's Pages build and the site workflow →
check `curl -sI <video url>` is 200 + `video/mp4`. A brand-new media repo's first Pages build can take a few minutes.
Requirements: `gh` logged in with repo-create/push rights. `TOEIC_NO_UPLOAD=1` builds without pushing media repos.
Any `*.mp4` left in `site/media/` is deleted on build. PDFs, covers and word MP3s stay on this repo's Pages.
