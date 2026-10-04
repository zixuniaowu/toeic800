"""Word data, US IPA and pronunciation MP3s for the episode word lists.

- Word rows (pos, zh, example, example zh) come from the episode content file
  (/workspace/toeic_podcast/ep{N}*_content.py: WORDS + VOCAB_EXTRA) resolved
  against the shared vocab bank /workspace/toeic_src/day*.py (VOCAB "w|pos|zh|ex|exzh").
- IPA (General American): CMU Pronouncing Dictionary (pip `cmudict`) converted to
  IPA; fallback Free Dictionary API, only phonetics tagged US (audio "-us.mp3").
  Phrases are joined word by word. Unknown -> "" (never invented).
  Results cached in ipa_cache.json (committed), so rebuilds are offline/stable.
- MP3: edge-tts en-US-AriaNeural (24 kHz 48 kbps mono, the edge-tts default).
"""
import asyncio, importlib.util, json, re, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "ipa_cache.json"
VOCAB_SRC = Path("/workspace/toeic_src")
VOICE = "en-US-AriaNeural"

# ------------------------------------------------------------------ word rows
def _load_py(p):
    import sys
    if str(p.parent) not in sys.path:
        sys.path.insert(0, str(p.parent))
    spec = importlib.util.spec_from_file_location(f"m_{p.stem}", p)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def vocab_bank():
    bank = {}
    for p in sorted(VOCAB_SRC.glob("day*.py")):
        try:
            for line in _load_py(p).VOCAB.strip().splitlines():
                r = line.split("|")
                if len(r) == 5:
                    bank[r[0].strip()] = [x.strip() for x in r]
        except Exception:
            pass
    return bank


def find_content(src, n, words):
    """Content module for episode n whose WORDS match the meta word list."""
    cands = sorted(src.glob(f"ep{n}*_content.py"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        if not re.fullmatch(rf"ep{n}(v\d+)?_content\.py", p.name):
            continue
        try:
            m = _load_py(p)
        except Exception:
            continue
        if list(getattr(m, "WORDS", [])) == list(words):
            return m
    return None


def word_rows(src, n, words, bank=None):
    m = find_content(src, n, words)
    if m is None:
        return None
    bank = dict(bank if bank is not None else vocab_bank())
    for w, v in getattr(m, "VOCAB_EXTRA", {}).items():
        bank[w] = [w, *v]
    rows = []
    for w in words:
        r = bank.get(w)
        if not r:
            rows.append({"word": w, "pos": "", "zh": "", "ex": "", "exzh": ""}); continue
        rows.append({"word": w, "pos": r[1], "zh": r[2], "ex": r[3], "exzh": r[4]})
    return rows


def slug(w):
    return re.sub(r"[^a-z0-9]+", "-", w.lower()).strip("-") or "word"

# ------------------------------------------------------------------ IPA
V = {"AA": "ɑ", "AE": "æ", "AO": "ɔ", "AW": "aʊ", "AY": "aɪ", "EH": "ɛ", "EY": "eɪ", "IH": "ɪ",
     "IY": "i", "OW": "oʊ", "OY": "ɔɪ", "UH": "ʊ", "UW": "u"}
C = {"B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "F": "f", "G": "ɡ", "HH": "h", "JH": "dʒ", "K": "k",
     "L": "l", "M": "m", "N": "n", "NG": "ŋ", "P": "p", "R": "r", "S": "s", "SH": "ʃ", "T": "t",
     "TH": "θ", "V": "v", "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ"}
ONSETS = {tuple(x.split()) for x in """
P L|B L|K L|G L|F L|S L|P R|B R|T R|D R|K R|G R|F R|TH R|SH R|S P|S T|S K|S M|S N|S W|T W|D W|K W|G W|TH W|
P Y|B Y|F Y|M Y|K Y|HH Y|V Y|N Y|S P L|S P R|S T R|S K R|S K W|S P Y|S K Y""".replace("\n", "").split("|") if x.strip()}


def _vowel(ph):
    return ph[-1].isdigit()


def arpabet_to_ipa(phones):
    ph = list(phones)
    nvow = sum(_vowel(p) for p in ph)
    out = []  # list of (ipa, is_vowel, stress)
    for p in ph:
        if _vowel(p):
            base, s = p[:-1], int(p[-1])
            if base == "AH":
                ipa = "ə" if s == 0 else "ʌ"
            elif base == "ER":
                ipa = "ɚ" if s == 0 else "ɝ"
            else:
                ipa = V[base]
            out.append([ipa, True, s, p])
        else:
            out.append([C[p], False, 0, p])
    # place stress marks before the syllable onset (maximal legal onset)
    marks = {}
    for i, (ipa, isv, s, p) in enumerate(out):
        if not isv or s == 0 or nvow == 1:
            continue
        j = i
        while j > 0 and not out[j - 1][1]:
            j -= 1
        cons = [out[k][3] for k in range(j, i)]
        # previous vowel exists? then keep at least the coda for it (legal onset only)
        if j == 0:
            start = 0
        else:
            start = i
            for k in range(j, i):
                cl = tuple(cons[k - j:])
                if len(cl) == 1 and cl[0] != "NG" or cl in ONSETS:
                    start = k; break
        marks[start] = "ˈ" if s == 1 else "ˌ"
    res = ""
    for i, (ipa, *_rest) in enumerate(out):
        res += marks.get(i, "") + ipa
    return res


_CMU = None


def cmu():
    global _CMU
    if _CMU is None:
        try:
            import cmudict
            _CMU = cmudict.dict()
        except Exception:
            _CMU = {}
    return _CMU


def _api_us(word):
    url = "https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(word)
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 toeic800-site"})
            d = json.load(urllib.request.urlopen(req, timeout=8))
            for e in d:
                for p in e.get("phonetics", []):
                    if p.get("text") and p.get("audio", "").endswith("-us.mp3"):
                        return p["text"].strip("/[] ").replace(".", "")
            return ""
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return ""
            time.sleep(2 + 3 * attempt)
        except Exception:
            time.sleep(2 + 3 * attempt)
    return None  # unknown (network) -> don't cache


def token_ipa(tok, cache):
    t = tok.lower()
    if t in cache.get("tokens", {}):
        return cache["tokens"][t]
    d = cmu()
    val = None
    if t in d:
        val = arpabet_to_ipa(d[t][0])
    else:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        if cache.get("failed", {}).get(t) == today:
            return ""          # network failed earlier today; retry tomorrow
        api = _api_us(t)
        if api is None:
            cache.setdefault("failed", {})[t] = today
            return ""
        cache.get("failed", {}).pop(t, None)
        val = api
    cache.setdefault("tokens", {})[t] = val
    return val


WEAK = {"of": "əv"}  # weak forms inside multi-word phrases


def ipa_for(word, cache):
    """US IPA for a word/phrase; '' if any part unknown."""
    if word in cache.get("overrides", {}):
        return cache["overrides"][word]
    parts = []
    for tok in word.split():
        if "-" in tok and tok.lower() not in cmu():
            sub = [token_ipa(x, cache) for x in tok.split("-") if x]
            if not all(sub):
                return ""
            parts.append("-".join(sub))
        elif len(word.split()) > 1 and tok.lower() in WEAK:
            parts.append(WEAK[tok.lower()])
        else:
            v = token_ipa(tok, cache)
            if not v:
                return ""
            parts.append(v)
    return "/" + " ".join(parts) + "/"


def load_cache():
    if CACHE.exists():
        return json.loads(CACHE.read_text("utf-8"))
    return {"_note": "tokens: auto (CMU->IPA, else dictionaryapi US); overrides: manual word->'/ipa/' wins", "overrides": {}, "tokens": {}}


def save_cache(c):
    CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8")

# ------------------------------------------------------------------ TTS
async def _tts_all(jobs, conc=4):
    import edge_tts
    sem = asyncio.Semaphore(conc)

    async def one(text, path):
        async with sem:
            tmp = path.with_suffix(".part")
            for attempt in range(4):
                try:
                    await edge_tts.Communicate(text, VOICE, rate="-5%").save(str(tmp))
                    if tmp.stat().st_size > 1000:
                        tmp.replace(path); return True
                except Exception:
                    await asyncio.sleep(2 + 2 * attempt)
            return False
    return await asyncio.gather(*(one(t, p) for t, p in jobs))


def make_audio(jobs):
    """jobs: [(text, Path)]; only missing files are synthesized. Returns #failed."""
    todo = [(t, p) for t, p in jobs if not p.exists()]
    if not todo:
        return 0
    for _, p in todo:
        p.parent.mkdir(parents=True, exist_ok=True)
    try:
        res = asyncio.run(_tts_all(todo))
    except ImportError:
        print("WARNING: edge-tts not installed; skipped", len(todo), "word MP3s")
        return len(todo)
    return sum(1 for r in res if not r)
