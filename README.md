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
