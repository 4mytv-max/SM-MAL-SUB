"""
SM OPEN SUBTITLES FOR MALAYALAM
================================
Stremio subtitle addon that auto-translates subtitles to Malayalam.

Flow:
  1. Stremio asks /trsub/subtitles/movie/{tt}.json (or series/{tt}:{s}:{e})
  2. We search OpenSubtitles for that title (prefer English source)
  3. The returned subtitle entry points at /trsub/srt/{file_id}.srt
  4. On first request, we download the source SRT, translate every cue
     to Malayalam (Google Translate, MyMemory fallback), cache it 24h,
     and serve the translated SRT.

Config (Render env vars):
  OPENSUBTITLES_API_KEY  - required (free at https://www.opensubtitles.com)
  OPENSUBTITLES_USER     - optional (free account, higher download quota)
  OPENSUBTITLES_PASSWORD - optional
"""

import hashlib
import io
import os
import re
import threading
import time
import zipfile

import requests
from flask import Flask, jsonify, request, Response

try:
    from deep_translator import GoogleTranslator, MyMemoryTranslator
    HAS_TRANSLATOR = True
except Exception:  # pragma: no cover
    HAS_TRANSLATOR = False

# ---------------- config ----------------
PREFIX = "/trsub"
ADDON_NAME = "SM OPEN SUBTITLES FOR MALAYALAM"
MANIFEST_ID = "com.smopensubtitles.malayalam"
ADDON_VERSION = "1.0.7"

OS_API = "https://api.opensubtitles.com/api/v1"
OS_KEY = os.environ.get("OPENSUBTITLES_API_KEY", "").strip()
OS_USER = os.environ.get("OPENSUBTITLES_USER", "").strip()
OS_PASS = os.environ.get("OPENSUBTITLES_PASSWORD", "").strip()

CACHE_TTL = 24 * 3600          # translated SRT cache: 24 hours
MIN_JOB_GAP = 5.0              # min seconds between translation jobs
BATCH_SIZE = 25                # subtitle cues per translation request
BATCH_DELAY = 2.0              # pause between batches (politeness)
REQ_TIMEOUT = 25

app = Flask(__name__)

# ---------------- state ----------------
_cache = {}                    # file_id -> {translated, src_lang, expires}
_cache_lock = threading.Lock()
_job_lock = threading.Lock()
_last_job_start = 0.0
_os_token = {"token": None, "expires": 0.0}

UA = "SMOpenSubtitlesMalayalam v1.0.0"

# ISO-639 -> MyMemory language name (fallback translator needs names)
ISO_TO_MYMEM = {
    "en": "english", "hi": "hindi", "ta": "tamil india", "te": "telugu",
    "kn": "kannada", "ml": "malayalam", "bn": "bengali", "mr": "marathi",
    "es": "spanish", "fr": "french", "de": "german", "it": "italian",
    "pt": "portuguese", "ru": "russian", "ja": "japanese", "ko": "korean",
    "zh": "chinese simplified", "ar": "arabic", "tr": "turkish",
    "nl": "dutch", "pl": "polish", "sv": "swedish", "da": "danish",
    "no": "norwegian bokmål", "fi": "finnish", "el": "greek",
    "he": "hebrew", "th": "thai", "id": "indonesian", "ms": "malay",
    "vi": "vietnamese", "ur": "urdu", "fa": "persian",
}

# ---------------- helpers ----------------
def _now():
    return time.time()


def cache_get(file_id):
    with _cache_lock:
        e = _cache.get(file_id)
        if e and e["expires"] > _now():
            return e
        if e:
            del _cache[file_id]
    return None


def cache_put(file_id, translated, src_lang):
    with _cache_lock:
        _cache[file_id] = {
            "translated": translated,
            "src_lang": src_lang,
            "expires": _now() + CACHE_TTL,
        }


# ---------------- OpenSubtitles client ----------------
def _os_headers(auth=False):
    h = {"Api-Key": OS_KEY, "User-Agent": UA, "Content-Type": "application/json"}
    if auth and _os_token["token"]:
        h["Authorization"] = "Bearer " + _os_token["token"]
    return h


def os_login():
    """Login to OpenSubtitles (needs API key + user/pass). Caches token ~24h."""
    if not (OS_KEY and OS_USER and OS_PASS):
        return None
    if _os_token["token"] and _os_token["expires"] > _now() + 300:
        return _os_token["token"]
    try:
        r = requests.post(
            OS_API + "/login",
            headers=_os_headers(),
            json={"username": OS_USER, "password": OS_PASS},
            timeout=REQ_TIMEOUT,
        )
        if r.status_code == 200:
            tok = r.json().get("token")
            if tok:
                _os_token["token"] = tok
                _os_token["expires"] = _now() + 23 * 3600
                return tok
    except Exception:
        pass
    return None


def os_search(imdb_id, season=None, episode=None, languages="en"):
    """Search subtitles. Returns list of dicts with file_id, file_name, language."""
    if not OS_KEY:
        return []
    params = {
        "imdb_id": imdb_id.replace("tt", ""),
        "languages": languages,
        "order_by": "download_count",
        "order_direction": "desc",
    }
    if season is not None:
        params["season_number"] = season
    if episode is not None:
        params["episode_number"] = episode
    try:
        r = requests.get(OS_API + "/subtitles", headers=_os_headers(),
                         params=params, timeout=REQ_TIMEOUT)
        if r.status_code != 200:
            return []
        out = []
        for sub in r.json().get("data", []):
            attrs = sub.get("attributes", {})
            lang = attrs.get("language", "?")
            for f in attrs.get("files", []):
                out.append({
                    "file_id": f.get("file_id"),
                    "file_name": f.get("file_name", ""),
                    "language": lang,
                    "downloads": attrs.get("download_count", 0),
                })
        return out
    except Exception:
        return []


def os_download_link(file_id):
    """Get a temporary download URL for a subtitle file. Needs login token."""
    tok = os_login()
    if not tok:
        return None
    try:
        r = requests.post(OS_API + "/download", headers=_os_headers(auth=True),
                          json={"file_id": file_id}, timeout=REQ_TIMEOUT)
        if r.status_code == 200:
            return r.json().get("link")
    except Exception:
        pass
    return None


# ---------------- subtitle fetch / parse ----------------
def fetch_srt_text(url):
    """Download a subtitle file; unzip if needed; decode to text."""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    data = r.content
    if data[:2] == b"PK":  # zip archive
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist()
                     if n.lower().endswith((".srt", ".sub", ".txt"))]
            pick = names[0] if names else z.namelist()[0]
            data = z.read(pick)
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


SRT_BLOCK = re.compile(
    r"(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n(.*?)(?=\n\s*\n|\Z)",
    re.S,
)
TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")


def parse_srt(text):
    """-> list of (index, start, end, text)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = []
    for m in SRT_BLOCK.finditer(text):
        idx, start, end, body = m.groups()
        blocks.append((idx.strip(), start.strip().replace(",", "."),
                       end.strip().replace(",", "."), body.strip()))
    return blocks


def build_srt(blocks):
    out = []
    for i, (idx, start, end, body) in enumerate(blocks, 1):
        out.append(f"{i}\n{start} --> {end}\n{body}\n")
    return "\n".join(out) + "\n"


def clean_for_translation(text):
    """Strip styling tags so translators don't mangle them."""
    return TAG_RE.sub("", text).strip()


# ---------------- translation ----------------
def _translate_google(texts, src="en"):
    gt = GoogleTranslator(source="auto", target="ml")
    return gt.translate_batch(texts)


def _translate_mymemory_direct(texts, src="en"):
    """Translate using MyMemory API directly (no library). Returns list or None."""
    src_code = (src or "en").lower().split("-")[0]
    # MyMemory has a 500 char limit per request, batch carefully
    results = []
    # Process in smaller groups to stay under limits
    for i in range(0, len(texts), 10):
        batch = texts[i:i+10]
        # Join with a separator that won't appear in dialogue
        sep = " ||| "
        combined = sep.join(batch)
        if len(combined) > 450:
            # Fallback: translate one by one
            for t in batch:
                try:
                    r = requests.get(
                        "https://api.mymemory.translated.net/get",
                        params={"q": t[:450], "langpair": f"{src_code}|ml"},
                        timeout=15,
                    )
                    if r.status_code == 200:
                        tr = r.json().get("responseData", {}).get("translatedText", "")
                        results.append(tr.strip() if tr else t)
                    else:
                        results.append(t)
                except Exception:
                    results.append(t)
                time.sleep(0.5)
        else:
            try:
                r = requests.get(
                    "https://api.mymemory.translated.net/get",
                    params={"q": combined, "langpair": f"{src_code}|ml"},
                    timeout=15,
                )
                if r.status_code == 200:
                    tr = r.json().get("responseData", {}).get("translatedText", "")
                    if tr and sep in tr:
                        parts = tr.split(sep)
                        if len(parts) == len(batch):
                            results.extend([p.strip() for p in parts])
                        else:
                            results.extend(batch)
                    else:
                        results.extend(batch)
                else:
                    results.extend(batch)
            except Exception:
                results.extend(batch)
            time.sleep(0.5)
    return results if len(results) == len(texts) else None


def translate_lines(texts, src_lang="en"):
    """Translate a list of cue texts to Malayalam. Never raises."""
    if not HAS_TRANSLATOR:
        return list(texts)
    jobs = [(i, t) for i, t in enumerate(texts) if t.strip()]
    if not jobs:
        return list(texts)
    result = list(texts)
    # batch - use direct MyMemory API (works on Render, no library issues)
    for b in range(0, len(jobs), BATCH_SIZE):
        chunk = jobs[b:b + BATCH_SIZE]
        idxs = [i for i, _ in chunk]
        srcs = [t for _, t in chunk]
        done = False
        try:
            out = _translate_mymemory_direct(srcs, src_lang)
            if out and len(out) == len(srcs):
                for i, tr in zip(idxs, out):
                    result[i] = (tr or result[i]).strip() or result[i]
                done = True
        except Exception:
            pass
        # if failed, keep original lines (graceful fallback)
        if b + BATCH_SIZE < len(jobs):
            time.sleep(BATCH_DELAY)
    return result


def translate_srt_text(srt_text, src_lang="en"):
    blocks = parse_srt(srt_text)
    if not blocks:
        return None
    cleaned = [clean_for_translation(b[3]) for b in blocks]
    translated = translate_lines(cleaned, src_lang)
    new_blocks = [(b[0], b[1], b[2], tr if tr else b[3])
                  for b, tr in zip(blocks, translated)]
    return build_srt(new_blocks)


def get_translated_srt(file_id, src_lang="en"):
    """Full pipeline: cache -> download -> translate -> cache. Returns srt or None."""
    hit = cache_get(str(file_id))
    if hit:
        return hit["translated"], True

    # global rate limit: one translation job per MIN_JOB_GAP seconds
    global _last_job_start
    with _job_lock:
        wait = MIN_JOB_GAP - (_now() - _last_job_start)
        if wait > 0:
            time.sleep(wait)
        _last_job_start = _now()

        # double-check cache (another request may have filled it)
        hit = cache_get(str(file_id))
        if hit:
            return hit["translated"], True

        link = os_download_link(file_id)
        if not link:
            return None, False
        try:
            raw = fetch_srt_text(link)
        except Exception:
            return None, False
        translated = translate_srt_text(raw, src_lang)
        if not translated:
            return None, False
        cache_put(str(file_id), translated, src_lang)
        return translated, False


# ---------------- routes ----------------
@app.route("/")
def index():
    base = request.url_root.rstrip("/")
    return Response(
        f"""<h2>{ADDON_NAME} 🇮🇳</h2>
        <p>Auto-translates movie &amp; series subtitles to Malayalam.</p>
        <p>Install in Stremio / Nuvio:<br>
        <code>{base}{PREFIX}/manifest.json</code></p>""",
        mimetype="text/html")


@app.route(PREFIX + "/manifest.json")
def manifest():
    base = request.url_root.rstrip("/")
    return jsonify({
        "id": MANIFEST_ID,
        "version": ADDON_VERSION,
        "name": ADDON_NAME,
        "description": ("Auto-translates subtitles to Malayalam using "
                        "OpenSubtitles + machine translation. Quality varies; "
                        "human-made Malayalam subtitles are always better."),
        "logo": f"{base}/static/logo.png",
        "resources": ["subtitles"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": [],
    })


def _pick_best(files):
    """Prefer .srt files, highest download count."""
    def score(f):
        name = (f.get("file_name") or "").lower()
        srt_bonus = 100000 if name.endswith(".srt") else 0
        return srt_bonus + (f.get("downloads") or 0)
    return sorted(files, key=score, reverse=True)


def _subtitle_entries(files, base):
    entries = []
    for f in _pick_best(files)[:1]:
        fid = f.get("file_id")
        if not fid:
            continue
        entries.append({
            "id": f"mltrans:{fid}",
            "lang": "Malayalam (Auto-Translate)",
            "url": f"{base}{PREFIX}/srt/{fid}.srt",
        })
    return entries


@app.route(PREFIX + "/subtitles/movie/<tt_id>.json")
def subtitles_movie(tt_id):
    base = request.url_root.rstrip("/")
    if not tt_id.startswith("tt"):
        return jsonify({"subtitles": []})
    # try English first, then any language
    files = os_search(tt_id, languages="en")
    if not files:
        files = os_search(tt_id, languages="en,hi,ta,te,kn")
    return jsonify({"subtitles": _subtitle_entries(files, base)})


@app.route(PREFIX + "/subtitles/series/<path:sid>.json")
def subtitles_series(sid):
    base = request.url_root.rstrip("/")
    # sid like tt1234567:1:2
    m = re.match(r"^(tt\d+)(?::(\d+):(\d+))?$", sid)
    if not m:
        return jsonify({"subtitles": []})
    tt_id, season, episode = m.group(1), m.group(2), m.group(3)
    season = int(season) if season else None
    episode = int(episode) if episode else None
    files = os_search(tt_id, season=season, episode=episode, languages="en")
    if not files:
        files = os_search(tt_id, season=season, episode=episode,
                          languages="en,hi,ta,te,kn")
    return jsonify({"subtitles": _subtitle_entries(files, base)})


@app.route(PREFIX + "/srt/<file_id>.srt")
def serve_srt(file_id):
    try:
        hit = cache_get(str(file_id))
        src_lang = (hit or {}).get("src_lang", "en")
        translated, cached = get_translated_srt(file_id, src_lang)
        if not translated:
            return Response("Subtitle unavailable", status=404,
                            mimetype="text/plain")
        return Response(translated, mimetype="text/srt; charset=utf-8",
                        headers={"X-Translate-Cache": "HIT" if cached else "MISS"})
    except Exception as e:
        # Log the error and return 404 instead of 500
        print(f"SRT error for {file_id}: {e}", flush=True)
        return Response("Subtitle unavailable", status=404,
                        mimetype="text/plain")


@app.route(PREFIX + "/debug/<file_id>")
def debug_file(file_id):
    """Debug endpoint: test each step separately."""
    result = {"file_id": file_id, "steps": {}}
    
    # Step 1: Check env vars (without revealing values)
    result["steps"]["env"] = {
        "api_key_set": bool(OS_KEY),
        "user_set": bool(OS_USER),
        "pass_set": bool(OS_PASS),
    }
    
    # Step 2: Test login
    try:
        tok = os_login()
        result["steps"]["login"] = {"success": bool(tok)}
    except Exception as e:
        result["steps"]["login"] = {"success": False, "error": str(e)[:100]}
    
    # Step 3: Test download link
    try:
        link = os_download_link(file_id)
        result["steps"]["download_link"] = {"success": bool(link)}
    except Exception as e:
        result["steps"]["download_link"] = {"success": False, "error": str(e)[:100]}
    
    # Step 4: Test MyMemory API directly
    try:
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": "Hello", "langpair": "en|ml"},
            timeout=10,
        )
        result["steps"]["mymemory"] = {
            "success": r.status_code == 200,
            "status_code": r.status_code,
        }
    except Exception as e:
        result["steps"]["mymemory"] = {"success": False, "error": str(e)[:100]}
    
    return jsonify(result)


@app.route(PREFIX + "/cache/stats.json")
def cache_stats():
    with _cache_lock:
        n = len(_cache)
    return jsonify({"cached_files": n, "ttl_hours": CACHE_TTL / 3600})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
