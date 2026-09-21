#!/usr/bin/env python3
"""CORS bridge for JanitorAI -> OpenAI-compatible providers.

JanitorAI runs in a browser, so every provider it talks to must answer CORS
preflight. Neither OpenCode Go nor Hemmingway does, so both are routed through
here instead.

Providers are selected per-request (see resolve_route), so one ngrok endpoint
serves all of them -- you only swap the API key in JanitorAI, not the URL.

Fixes carried over from v2:
  - JanitorAI connectivity check does GET /  -> 200 JSON instead of 404
  - OpenCode Go requires `x-opencode-session` (400 MissingSessionID otherwise)
    -> injected for OpenCode only; never sent to other providers
  - OpenCode Go wants a real client User-Agent, not python-requests/curl
  - Old code only forwarded 3 headers, stripping native session headers
  - Old code ran with Flask debug=True (double process via reloader)
    -> runs threaded, no reloader

Point JanitorAI at: https://<ngrok-host>/v1

Env config:
  UPSTREAM_URL       override the default (OpenCode) upstream base
  PORT               (default 8081)
  PROXY_USER_AGENT   (default janitorai-bridge/2.1)
  SESSION_FILE       (default ~/.zen-proxy-default-session)
  LOG_LEVEL          (default INFO)
"""

import base64
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import pathlib
import re
import socket
import sys
import threading
import time
import urllib.parse
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

import requests
from flask import Flask, Response, request as flask_req

VERSION = "2.6"
PORT = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8081))
PROXY_UA = os.environ.get("PROXY_USER_AGENT", "janitorai-bridge/" + VERSION)
SESSION_FILE = os.environ.get(
    "SESSION_FILE", os.path.expanduser("~/.zen-proxy-default-session")
)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Provider registry.
#
# `ua_tag`   - User-Agent the bridge presents upstream (generic SDK UAs are
#              replaced; a real browser UA is kept but tagged).
# `key_prefix` - API keys starting with this auto-select the provider, so the
#              same ngrok URL works for every provider just by swapping keys.
# `paths`    - explicit URL prefixes that force the provider, for when a client
#              cannot set the key (e.g. /hemmingway/v1/chat/completions).
# `session_header` - whether to inject OpenCode's x-opencode-session.
# ---------------------------------------------------------------------------
UPSTREAMS = {
    "opencode": {
        "base": os.environ.get("UPSTREAM_URL",
                               "https://opencode.ai/zen/go/v1").rstrip("/"),
        "label": "OpenCode Go",
        "ua_tag": f"{PROXY_UA} (OpenCode-Go-Compatible)",
        "key_prefix": None,
        "paths": ("/zen/go", "/zen"),
        "session_header": True,
    },
    "hemmingway": {
        "base": os.environ.get("HEMMINGWAY_URL",
                               "https://hemmingway.io/v1").rstrip("/"),
        "label": "Hemmingway",
        "ua_tag": f"{PROXY_UA} (Hemmingway-Compatible)",
        "key_prefix": "hemmingway_live_",
        "paths": ("/hemmingway", "/hw"),
        "session_header": False,
    },
}
DEFAULT_PROVIDER = "opencode"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("zen-proxy")

# Durable file log: the tmux pane wraps long lines and its scrollback is
# finite, so keep a rotating file copy on a box that runs unattended.
try:
    import logging.handlers as _lh
    _logdir = pathlib.Path.home() / "logs"
    _logdir.mkdir(parents=True, exist_ok=True)
    _fh = _lh.RotatingFileHandler(_logdir / "zen-bridge.log",
                                  maxBytes=8 * 1024 * 1024, backupCount=4)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(_fh)
except Exception as _e:  # never let logging setup break the bridge
    print("file-log setup failed: %r" % (_e,), file=sys.stderr)

app = Flask(__name__)

# Hop-by-hop headers that must never be forwarded (requests recalculates some).
HOP_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "upgrade",
}

# Generic UAs that trigger Go's abuse heuristics — always override these.
GENERIC_UA_SUBSTRINGS = (
    "python-requests", "python-urllib", "urllib3", "curl/",
    "wget/", "go-http-client", "axios", "node-fetch", "undici",
    "java/", "okhttp", "apache-httpclient",
)

# ---------------------------------------------------------------------------
# Image intake — turn markdown image links inside chat messages into real
# OpenAI multimodal image parts before the payload is forwarded upstream.
#
# JanitorAI persists `![alt](https://ella.janitorai.com/media-approved/....webp)`
# in message text (its own Media Library CDN), and users paste external image
# URLs the same way. The LLM only receives that URL as plain text — it cannot
# fetch it — so this module downloads each referenced image, base64-encodes it
# and rewrites the message `content` string into
#   [{"type":"text",...},{"type":"image_url",...}]
# Everything else in the payload is left untouched.
#
# Env knobs:
#   IMAGE_INTAKE            1=on (default), 0=off
#   IMAGE_INTAKE_MAX_IMAGES max images attached per request (default 4 —
#                           providers reject prompts with more image parts)
#   IMAGE_INTAKE_LAST_N     attach only within the last N messages (0 = all)
#   IMAGE_INTAKE_MAX_DIM    downscale images whose longest side exceeds this
#                           (default 1568, 0=never resize)
#   IMAGE_INTAKE_MAX_BYTES  per-image download cap (default 12 MiB)
#   IMAGE_INTAKE_HOSTS      comma-separated host allowlist (empty = all hosts)
#   IMAGE_INTAKE_CACHE      LRU size for url -> dataURL cache (default 128)
#
# Self-hosted image store (userscript attach button default target):
#   IMAGE_PUBLIC_HOST       public base for returned upload URLs,
#                           e.g. https://<ngrok-host>. Set in start script.
#   IMAGE_UPLOAD_TOKEN      required X-Upload-Token for POST /img/upload;
#                           generated + persisted under IMAGE_DIR when unset.
#   IMAGE_DIR               storage dir (default ~/Documents/zen-images)
# ---------------------------------------------------------------------------
IMAGE_INTAKE = os.environ.get("IMAGE_INTAKE", "1") not in ("0", "false", "no")
# Providers commonly reject prompts with more than 4 image parts
# ("At most 4 image(s) may be provided in one prompt"), so default to 4.
IMAGE_INTAKE_MAX_IMAGES = int(os.environ.get("IMAGE_INTAKE_MAX_IMAGES", "4"))
IMAGE_INTAKE_MAX_DIM = int(os.environ.get("IMAGE_INTAKE_MAX_DIM", "1568"))
IMAGE_INTAKE_MAX_BYTES = int(os.environ.get("IMAGE_INTAKE_MAX_BYTES", str(12 * 1024 * 1024)))

# Hard request-body ceiling (Flask answers 413 past this). The JSON upload
# shape inflates bytes by 4/3 in base64, so it needs headroom over the
# decoded image cap; without any ceiling a single request could exhaust RAM.
app.config["MAX_CONTENT_LENGTH"] = int(IMAGE_INTAKE_MAX_BYTES * 1.45) + (1 << 20)
IMAGE_INTAKE_HOSTS = tuple(
    h.strip().lower() for h in os.environ.get("IMAGE_INTAKE_HOSTS", "").split(",")
    if h.strip()
)
IMAGE_INTAKE_CACHE_SIZE = int(os.environ.get("IMAGE_INTAKE_CACHE", "128"))
IMAGE_INTAKE_LAST_N = int(os.environ.get("IMAGE_INTAKE_LAST_N", "0"))  # 0 = all messages

_IMG_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".webp": "image/webp",
                    ".gif": "image/gif"}

# --- self-hosted image store (the "own server" attachment pipeline) ---------
# POST /img/upload -> {url: PUBLIC/img/<id>.<ext>}; GET /img/<id> serves it.
# Uploads land on this box and persist; intake fetches them back via localhost
# so ngrok/CDN quirks never affect vision attachment.
IMAGE_DIR = pathlib.Path(os.environ.get("IMAGE_DIR",
                                        str(pathlib.Path.home() / "Documents" / "zen-images")))
IMAGE_PUBLIC_HOST = os.environ.get("IMAGE_PUBLIC_HOST", "").rstrip("/")

# Secrets live OUTSIDE IMAGE_DIR on purpose. The store is served publicly, so
# anything parked next to the images is one routing slip away from being
# readable — /img/.upload-token being publicly fetchable is exactly that bug.
ZEN_CONFIG_DIR = pathlib.Path(os.environ.get(
    "ZEN_CONFIG_DIR", str(pathlib.Path.home() / ".config" / "zen-proxy")))

# One-tap userscript install: GET /intake.user.js serves the companion script.
USERSCRIPT_FILE = pathlib.Path(os.environ.get(
    "USERSCRIPT_FILE",
    str(pathlib.Path(__file__).with_name("janitorai-image-intake.user.js"))))


@app.route("/intake.user.js", methods=["GET"])
def serve_userscript():
    """Serve the companion userscript so any device can install it by URL.

    Public and secret-free (the script contains no credentials); Tampermonkey
    offers to install when the phone's browser opens this URL.
    """
    try:
        data = USERSCRIPT_FILE.read_text()
    except OSError:
        return json_resp({"error": {"message": "Userscript file missing on server",
                                    "type": "not_found"}}, 404)
    h = cors_headers()
    h["Content-Disposition"] = 'attachment; filename="janitorai-image-intake.user.js"'
    h.pop("Access-Control-Max-Age", None)
    return Response(data, status=200, content_type="text/javascript; charset=utf-8",
                    headers=h)


def _load_or_create_upload_token():
    """Env token wins; 'off' disables auth; otherwise generate once + persist."""
    tok = os.environ.get("IMAGE_UPLOAD_TOKEN", "").strip()
    if tok.lower() in ("off", "0", "no", "none"):
        return ""
    if tok:
        return tok
    tokfile = ZEN_CONFIG_DIR / "upload-token"
    legacy = IMAGE_DIR / ".upload-token"
    try:
        if not tokfile.exists() and legacy.exists():  # migrate pre-2.3 layout
            ZEN_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tokfile.write_text(legacy.read_text())
            legacy.unlink(missing_ok=True)
        if tokfile.exists():
            t = tokfile.read_text().strip()
            if t:
                return t
        ZEN_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tok = uuid.uuid4().hex
        tokfile.write_text(tok + "\n")
        try:
            tokfile.chmod(0o600)
        except OSError:
            pass
        log.info("[imghost] generated upload token in %s", tokfile)
        return tok
    except OSError as e:
        log.warning("[imghost] token persistence failed (%s) — using ephemeral", e)
        return uuid.uuid4().hex


IMAGE_UPLOAD_TOKEN = _load_or_create_upload_token()


def _localize_url(url):
    """Own-hosted images are fetched via localhost, not the public ngrok host."""
    if IMAGE_PUBLIC_HOST and url.startswith(IMAGE_PUBLIC_HOST + "/img/"):
        return "http://127.0.0.1:" + str(PORT) + url[len(IMAGE_PUBLIC_HOST):]
    return url

IMG_MARKDOWN_RE = re.compile(r"!\[([^\]]*)\]\(\s*(https?://[^\s)\]\"'>,;]+)")
# Bare URL ending in an image extension, not already inside markdown parens.
IMG_BARE_RE = re.compile(
    r"(?<![()\[\]])https?://[^\s)\]\"'<>]+"
    r"\.(?:png|jpe?g|webp|gif)(?:\?[^\s)\]\"'<>]*)?",
    re.IGNORECASE,
)

# LRU cache url -> data URL (str) or None (recent failure, negative cache).
_image_cache = OrderedDict()
_image_cache_lock = threading.Lock()
_image_fail_times = {}
_fail_lock = threading.Lock()   # bounded dict, but still shared across threads
_FAIL_TTL = 60.0

try:
    from PIL import Image as _PILImage
    _HAVE_PIL = True
    # Bound pixel count explicitly: a small file can still decompress into an
    # enormous bitmap (decompression bomb). Pillow warns at this threshold and
    # refuses past 2x; the byte cap in _persist_image_bytes/fetch covers the rest.
    _PILImage.MAX_IMAGE_PIXELS = int(os.environ.get("IMAGE_INTAKE_MAX_PIXELS",
                                                    "50000000"))
except ImportError:
    _PILImage = None
    _HAVE_PIL = False
    log.warning("Pillow not available — image intake will forward original bytes without downscaling")

# A browser-ish UA so image hosts that block python-requests don't 403 us.
_IMG_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Accept": "image/avif,image/webp,image/png,image/*;q=0.8,*/*;q=0.5",
}


def _host_allowed(url):
    m = re.match(r"https?://([^/?#]+)/", url + "/")
    if not m:
        return False
    host = m.group(1).lower()
    # Compare BARE hostnames. The old form compared 'host:port' against the
    # bare allowlist entries, so a URL with an explicit port silently skipped
    # the check entirely. Also strip any user:pass@ prefix.
    host = host.rpartition("@")[2]
    if host.startswith("["):           # [::1]:8081
        host = host.split("]")[0][1:]
    else:
        host = host.split(":")[0]
    if not IMAGE_INTAKE_HOSTS:
        return True
    return any(host == h or host.endswith("." + h) for h in IMAGE_INTAKE_HOSTS)


def _is_fetchable_public_url(url):
    """SSRF gate for every hop of an intake fetch.

    Conversation text supplies these URLs, so a crafted link would otherwise
    turn the bridge into a blind prober of LAN/loopback services (routers,
    cloud metadata, the bridge's own admin surface). Hostnames are resolved
    and every answer must be a public address.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parts.hostname, port,
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    return all(not _is_private_ip(info[4][0]) for info in infos)


def _cache_get(url):
    """Return (state, data_url): state is 'hit', 'negative' or 'miss'."""
    with _image_cache_lock:
        if url in _image_cache:
            _image_cache.move_to_end(url)
            return "hit", _image_cache[url]
    with _fail_lock:
        fail_t = _image_fail_times.get(url)
    if fail_t and (time.time() - fail_t) < _FAIL_TTL:
        return "negative", None
    return "miss", None


def _note_failure(url):
    """Remember a recent fetch failure so we stop hammering the host for a bit."""
    with _fail_lock:
        _image_fail_times[url] = time.time()
        while len(_image_fail_times) > 1024:  # bounded (it used to grow forever)
            _image_fail_times.pop(next(iter(_image_fail_times)))


def _cache_put(url, data_url):
    with _image_cache_lock:
        _image_cache[url] = data_url
        _image_cache.move_to_end(url)
        while len(_image_cache) > IMAGE_INTAKE_CACHE_SIZE:
            _image_cache.popitem(last=False)


def _encode_image(mime, raw):
    """Return a data: URL. Optionally downscale/reshape via Pillow."""
    if not (_HAVE_PIL and IMAGE_INTAKE_MAX_DIM):
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    try:
        img = _PILImage.open(io.BytesIO(raw))
        w, h = img.size
    except Exception:
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    if max(w, h) <= IMAGE_INTAKE_MAX_DIM and len(raw) <= 1_500_000:
        # Small enough already — original bytes, no re-encode.
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    scale = IMAGE_INTAKE_MAX_DIM / float(max(w, h))
    resample = getattr(_PILImage, "LANCZOS", 1)
    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                     resample)
    buf = io.BytesIO()
    try:
        if (img.mode in ("RGBA", "LA", "P") and
                "A" in img.convert("RGBA").getbands()):
            img.convert("RGBA").save(buf, "PNG", optimize=True)
            out_mime = "image/png"
        else:
            img.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
            out_mime = "image/jpeg"
    except Exception:
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return f"data:{out_mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def fetch_image_data_url(url):
    """Download url, cache the data URL. Returns str or None on failure."""
    if not _host_allowed(url):
        log.warning("[imgintake] host not allowlisted, skipped: %s", url[:120])
        return None
    hit_state, hit = _cache_get(url)
    if hit_state == "hit":
        return hit
    if hit_state == "negative":  # recent failure, don't hammer the host
        return None
    fetch_url = _localize_url(url)
    own_host = fetch_url != url  # our own store: loopback is intentional
    if not own_host and not _is_fetchable_public_url(fetch_url):
        log.warning("[imgintake] blocked non-public fetch target: %s", url[:120])
        _note_failure(url)
        return None
    try:
        def _one_get(target):
            return requests.get(target, headers=_IMG_FETCH_HEADERS, stream=True,
                                timeout=(8, 30), allow_redirects=False)

        r = _one_get(fetch_url)
        hops = 0
        while r.is_redirect and hops < 3:
            nxt = urllib.parse.urljoin(fetch_url, r.headers.get("Location", ""))
            if not own_host and not _is_fetchable_public_url(nxt):
                raise RuntimeError("redirect to non-public target blocked")
            fetch_url = nxt
            hops += 1
            r = _one_get(fetch_url)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        chunks, total = [], 0
        for chunk in r.iter_content(65536):
            if chunk:
                total += len(chunk)
                if total > IMAGE_INTAKE_MAX_BYTES:
                    raise RuntimeError(f"image exceeds {IMAGE_INTAKE_MAX_BYTES} byte cap")
                chunks.append(chunk)
        raw = b"".join(chunks)
        mime = r.headers.get("Content-Type", "").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/webp" if ".webp" in url.lower() else "image/jpeg"
        data_url = _encode_image(mime, raw)
        _cache_put(url, data_url)
        with _fail_lock:
            _image_fail_times.pop(url, None)
        return data_url
    except Exception as e:
        log.warning("[imgintake] fetch failed for %s: %s", url[:120], e)
        _note_failure(url)
        return None


def _extract_urls(text):
    """Ordered unique image URLs in text: markdown ones first, then bare ones
    found in the text that remains after markdown removal."""
    md_urls = [m.group(2) for m in IMG_MARKDOWN_RE.finditer(text)]
    remaining = IMG_MARKDOWN_RE.sub("", text)
    bare = [m.group(0) for m in IMG_BARE_RE.finditer(remaining)]
    urls, seen = [], set()
    for u in md_urls + bare:
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _strip_urls_from_text(text, urls_to_strip):
    """Remove exactly the given URLs (markdown-wrapped or bare) from text.

    Tolerates malformed markdown — missing closing paren, trailing quote/bracket
    — and consumes a couple of stray characters after the URL.
    """
    for u in urls_to_strip:
        md = re.compile(r"!\[[^\]]*\]\(\s*" + re.escape(u) + r"[\)\]\"'>,;]{0,2}")
        text = md.sub("", text)
        text = re.sub(r"(?<![\\w./-])" + re.escape(u) + r"[\)\]\"'>,;]{0,2}(?![\\w./-])", "", text)
    # Collapse runs of blanks left behind by removals (newlines preserved).
    return re.sub(r"[ \t]{2,}", " ", text)


def _message_text(msg):
    """Return (content, combined_text, already_multimodal) for a message."""
    content = msg.get("content")
    if isinstance(content, str):
        return content, content, False
    if isinstance(content, list):
        texts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
        return content, "\n".join(texts), True
    return None, None, False


def _apply_attachments(msg, urls, role, idx):
    """Attach urls to one message. Mutates content. Returns images attached."""
    content = msg.get("content")
    attached = 0
    if isinstance(content, str):
        parts = []
        attached_urls = []
        for u in urls:
            du = fetch_image_data_url(u)
            if du:
                parts.append({"type": "image_url", "image_url": {"url": du}})
                attached_urls.append(u)
                attached += 1
                log.info("[imgintake] msg#%d (%s): attached %s (%.1f KB)",
                         idx, role, u.rsplit("/", 1)[-1][:60], len(du) / 1024.0)
        # Strip ONLY the links that actually became images: a fetch failure
        # must leave its link visible in the text (and visible to the model).
        new_text = _strip_urls_from_text(content, attached_urls) if attached_urls else content
        if new_text.strip():
            parts.insert(0, {"type": "text", "text": new_text.strip()})
        if parts:
            msg["content"] = parts
    elif isinstance(content, list):
        attached_urls = []
        for u in urls:
            du = fetch_image_data_url(u)
            if du:
                attached_urls.append(u)
                attached += 1
                log.info("[imgintake] msg#%d (%s): attached %s (%.1f KB)",
                         idx, role, u.rsplit("/", 1)[-1][:60], len(du) / 1024.0)
        new_parts = []
        for part in content:
            if (isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                    and _extract_urls(part["text"])):
                txt = _strip_urls_from_text(part["text"], attached_urls) \
                    if attached_urls else part["text"]
                if txt.strip():
                    new_parts.append({"type": "text", "text": txt.strip()})
            else:
                new_parts.append(part)
        for u in attached_urls:
            du = fetch_image_data_url(u)
            if du:
                new_parts.append({"type": "image_url", "image_url": {"url": du}})
        msg["content"] = new_parts
    return attached


def transform_chat_payload(parsed):
    """Mutate parsed payload in place. Returns number of images attached.

    Budget is spent newest-first and each distinct URL is attached only once
    per request, so re-sent history and repeated links never exceed the
    provider's per-prompt image cap."""
    if not isinstance(parsed, dict):
        return 0
    messages = parsed.get("messages")
    if not isinstance(messages, list):
        return 0

    # Pass 1 — collect eligible (user/assistant) messages that carry image URLs.
    eligible = []  # (idx, msg, role, text)
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).lower()
        # System/developer messages hold character cards and lorebook text —
        # image URLs there must stay plain text.
        if role not in ("user", "assistant"):
            continue
        _, text, _ = _message_text(msg)
        if not text or ("![" not in text and not IMG_BARE_RE.search(text)):
            continue
        eligible.append((idx, msg, role, text))
    if not eligible:
        return 0
    if IMAGE_INTAKE_LAST_N > 0:
        eligible = eligible[-IMAGE_INTAKE_LAST_N:]

    # Pass 2 — spend the budget newest-first; dedupe across the whole request.
    budget = IMAGE_INTAKE_MAX_IMAGES
    seen_urls = set()
    attach_map = {}  # idx -> ordered urls to attach on that message
    for idx, _msg, _role, text in reversed(eligible):
        if budget <= 0:
            break
        for u in _extract_urls(text):
            if budget <= 0:
                break
            if u in seen_urls:
                continue
            seen_urls.add(u)
            attach_map.setdefault(idx, []).append(u)
            budget -= 1
    if not attach_map:
        return 0

    # Pass 3 — apply (fetch + rewrite content).
    attached = 0
    for idx, msg, role, _text in eligible:
        urls = attach_map.get(idx)
        if urls:
            attached += _apply_attachments(msg, urls, role, idx)
    return attached


def maybe_transform_body(body_bytes, path):
    """Fast-path body transform for chat-completions payloads."""
    if not IMAGE_INTAKE or not body_bytes:
        return body_bytes, 0
    decoded = body_bytes.decode("utf-8", "ignore")
    if "![" not in decoded and not IMG_BARE_RE.search(decoded):
        return body_bytes, 0
    try:
        parsed = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body_bytes, 0
    if not (isinstance(parsed, dict) and isinstance(parsed.get("messages"), list)):
        return body_bytes, 0
    n = transform_chat_payload(parsed)
    if n == 0:
        return body_bytes, 0
    log.info("[imgintake] %s: %d image(s) attached this request", path, n)
    return json.dumps(parsed, ensure_ascii=False).encode("utf-8"), n


def get_default_session():
    """Single persistent UUID used when we have nothing better (no auth, no client session)."""
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE) as f:
                sid = f.read().strip()
                if sid:
                    return sid
    except OSError as e:
        log.warning("Could not read session file: %s", e)
    sid = f"zen-bridge-{uuid.uuid4()}"
    try:
        with open(SESSION_FILE, "w") as f:
            f.write(sid + "\n")
        log.info("Generated new default session ID: %s", sid)
    except OSError as e:
        log.warning("Could not persist session file: %s", e)
    return sid


DEFAULT_SESSION = get_default_session()
log.info("Default session ID: %s", DEFAULT_SESSION)
for _k, _v in UPSTREAMS.items():
    log.info("Upstream[%s]: %s", _k, _v["base"])


def derive_session_from_auth(auth_value):
    """Deterministic stable session per API key so different keys don't share cache."""
    if not auth_value:
        return None
    # Don't leak the key: hash it.
    digest = hashlib.sha256(auth_value.strip().encode()).hexdigest()[:16]
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"zen-bridge:{digest}"))


def pick_session_id(incoming_headers, body_bytes, auth_value):
    """Resolution order: client x-opencode-session > native session headers > body id > per-key > default."""
    # 1. Direct header (case-insensitive — Flask/Werkzeug already lowercases in .headers keys,
    #    but be explicit).
    for k, v in incoming_headers.items():
        if k.lower() == "x-opencode-session" and v.strip():
            return v.strip(), "client"

    # 2. Native session headers from validated clients (Codex, Claude Code, ZCode, Pi, etc).
    #    Go recognises these natively, so we forward them AND mirror into x-opencode-session.
    native_candidates = {}
    for k, v in incoming_headers.items():
        lk = k.lower()
        if not v or not v.strip():
            continue
        if lk in ("x-session-id", "x-codex-session", "x-claude-code-session",
                  "anthropic-session-id", "x-anthropic-session",
                  "x-stainless-session-id", "x-request-id") or (
                      lk.startswith("x-") and "session" in lk):
            native_candidates[k] = v.strip()
    if native_candidates:
        # prefer anything with 'opencode' in the name, else first
        for k, v in native_candidates.items():
            if "opencode" in k.lower():
                return v, f"native:{k}"
        k, v = next(iter(native_candidates.items()))
        return v, f"native:{k}"

    # 3. Body-embedded conversation identifiers (some OpenAI-compatible clients send these).
    if body_bytes:
        try:
            parsed = json.loads(body_bytes)
            if isinstance(parsed, dict):
                for field in ("session_id", "sessionId", "conversation_id",
                              "conversationId", "chat_id", "chatId"):
                    val = parsed.get(field)
                    if isinstance(val, str) and val.strip():
                        return val.strip(), f"body:{field}"
                # `user` field is often stable per JanitorAI character/chat.
                user = parsed.get("user")
                if isinstance(user, str) and user.strip():
                    stable = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                            f"zen-bridge-user:{user.strip()}"))
                    return stable, "body:user-derived"
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    # 4. Deterministic per-API-key session (isolates cache between keys).
    derived = derive_session_from_auth(auth_value)
    if derived:
        return derived, "derived:api-key"

    # 5. Global fallback.
    return DEFAULT_SESSION, "default"


def bearer_token(auth_value):
    """'Bearer xyz' -> 'xyz' (case-insensitive), else the raw value."""
    v = (auth_value or "").strip()
    if v.lower().startswith("bearer "):
        return v[7:].strip()
    return v


def _validated_forward_origin():
    """(origin, refusal_reason) for the X-Zen-Forward-Origin relay request.

    An absent header is not a forward request at all: (None, ""). A present
    but refused header returns a reason that is handed to JanitorAI verbatim:
    the old silent fallthrough showed users a misleading 401 from whichever
    default upstream happened to answer instead. Only the owner key set may
    ask for a relay, and only to public https targets: the tunnel is on the
    internet, so this must never become a general relay or an SSRF trampoline.
    """
    raw = (flask_req.headers.get("X-Zen-Forward-Origin") or "").strip()
    if not raw:
        return None, ""
    if "," in raw:  # duplicated header (or a comma): refuse to guess
        return None, "duplicated X-Zen-Forward-Origin header"
    bearer = bearer_token(flask_req.headers.get("Authorization", ""))
    if not (_owner_hashes() and key_is_owner(bearer)):
        return None, ("the API key in this request is not in the owner set; "
                      "hash it with printf %s 'YOUR-KEY' | sha256sum and add "
                      "it to OWNER_KEY_SHA256 in start-zen-proxy.local.sh")
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return None, "unparsable target URL"
    if parts.scheme != "https" or not parts.hostname:
        return None, "target must be a public https origin"
    if parts.username or parts.password:
        return None, "target must not embed credentials"
    if not _is_fetchable_public_url(raw):
        return None, "target is not a public host (private/loopback/link-local refused)"
    return raw.rstrip("/"), ""


def build_upstream_headers(provider_key, neutral=False):
    incoming = dict(flask_req.headers)
    auth_value = incoming.get("Authorization", "")
    body = flask_req.get_data()

    session_id, session_source = pick_session_id(incoming, body, auth_value)

    out = {}
    for k, v in incoming.items():
        lk = k.lower()
        if lk in HOP_HEADERS:
            continue
        # Never leak client network details to the LLM provider.
        if lk in ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                  "x-real-ip", "forwarded"):
            continue
        # Bridge-internal routing headers must not travel downstream.
        if lk in ("x-zen-forward-origin", "x-upstream", "x-zen-upstream"):
            continue
        # Forward: auth, content negotiation, all x-*, anthropic/openai namespaced headers.
        if lk in ("authorization", "content-type", "accept",
                  "accept-language", "accept-encoding") or \
           lk.startswith(("x-", "anthropic-", "openai-", "stainless-")):
            out[k] = v

    # x-opencode-session is OpenCode-specific. Sending it to another provider
    # leaks a session identifier and can trip strict request validation.
    # neutral=True means "relay to someone else's API": no session header at all.
    if not neutral and UPSTREAMS[provider_key]["session_header"]:
        out["x-opencode-session"] = session_id

    # User-Agent: override generic SDK names, keep real custom ones but tag them.
    incoming_ua = incoming.get("User-Agent", "")
    tag = PROXY_UA if neutral else UPSTREAMS[provider_key]["ua_tag"]
    if not incoming_ua or any(g in incoming_ua.lower() for g in GENERIC_UA_SUBSTRINGS):
        out["User-Agent"] = tag
    else:
        # Keep client identity but make clear this hop is our bridge.
        out["User-Agent"] = f"{tag} (via {incoming_ua[:120]})"

    return out, session_id, session_source


def cors_headers():
    """CORS for the API surface JanitorAI calls cross-origin (/v1/*).

    Deliberately NOT applied to /img/* — a wildcard ACAO on the token
    endpoint let any website read the owner's upload token from their browser.
    """
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
        # Allow everything JanitorAI/browser might send, incl. session header.
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Max-Age": "86400",
    }


@app.before_request
def handle_preflight():
    if flask_req.method == "OPTIONS":
        if flask_req.path.startswith("/img/"):
            # No CORS grant for the image store: browsers must not be able to
            # read /img/token (or anything else there) cross-origin.
            return Response(status=204)
        h = cors_headers()
        # Echo requested headers explicitly for picky browsers.
        req_h = flask_req.headers.get("Access-Control-Request-Headers")
        if req_h:
            h["Access-Control-Allow-Headers"] = req_h
        return Response(status=204, headers=h)


INFO_BODY = {
    "status": "ok",
    "proxy": "janitorai-bridge/" + VERSION,
    "upstreams": {k: v["base"] for k, v in UPSTREAMS.items()},
    "usage": "Point JanitorAI base URL at /v1 (e.g. http://SERVER:8081/v1) "
             "and paste the provider's key — it is routed by key prefix.",
    "endpoints": ["/v1/models", "/v1/chat/completions", "/healthz"],
}


def json_resp(obj, status=200, cors=True):
    h = cors_headers() if cors else {}
    h.pop("Access-Control-Max-Age", None)
    h["Content-Type"] = "application/json"
    return Response(json.dumps(obj), status=status, headers=h)


@app.route("/", methods=["GET"])
def root():
    # JanitorAI does a GET connectivity check here — must be 200, not 404.
    return json_resp(INFO_BODY)


@app.route("/healthz", methods=["GET"])
def healthz():
    # Minimal on purpose: a public health probe should not fingerprint which
    # upstreams a stranger's tunnel is wired to.
    return json_resp({"ok": True, "v": VERSION})


@app.route("/v1", methods=["GET"])
@app.route("/zen/v1", methods=["GET"])
def base_info():
    return json_resp(INFO_BODY)


def looks_like_chat_request(body_bytes):
    """True if the body is a chat-completions payload (has a `messages` list)."""
    if not body_bytes:
        return False
    try:
        parsed = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(parsed, dict) and isinstance(parsed.get("messages"), list)


def strip_api_prefix(full, method=None, body_bytes=None):
    """Map a public path onto an upstream suffix, ignoring which provider it is.

    '/v1/chat/completions' -> '/chat/completions'
    '/zen/go/v1/models'    -> '/models'
    '/models'              -> '/models'   (bare, misconfiguration tolerance)
    Returns None if the path isn't a recognised API path.

    A POST straight at a bare base ('/v1') means the client was configured with
    the completions endpoint AS the URL (JanitorAI's custom-proxy field works
    that way) and posts to exactly that. Forwarding that to upstream's root
    returns a 404 page, so map it to '/chat/completions' instead.
    """
    for prefix in ("/zen/go/v1", "/zen/v1", "/v1"):
        if full == prefix or full.startswith(prefix + "/"):
            rest = full[len(prefix):]
            if rest:
                return rest
            if method == "POST" and looks_like_chat_request(body_bytes):
                return "/chat/completions"
            return "/"
    if full in ("/models", "/chat/completions", "/responses", "/messages"):
        return full
    if full.startswith(("/models/", "/chat/", "/responses/", "/messages/")):
        return full
    return None


def resolve_route(path, auth_value, body_bytes, incoming_headers, method=None):
    """Pick the provider for this request.

    Order matters: an explicit URL prefix or header beats key sniffing, so a
    client that cannot change its key can still be pinned to a provider.

    Returns (provider_key, upstream_suffix) or (None, None) if unroutable.
    """
    full = "/" + path if path else "/"

    # 1. Explicit provider path prefix: /hemmingway/v1/chat/completions
    for key, cfg in UPSTREAMS.items():
        for p in cfg["paths"]:
            if full == p or full.startswith(p + "/"):
                suffix = strip_api_prefix(full[len(p):] or "/", method, body_bytes)
                if suffix is not None:
                    return key, suffix

    # 2. Explicit header, e.g. X-Upstream: hemmingway
    hdr = (incoming_headers.get("X-Upstream")
           or incoming_headers.get("X-Zen-Upstream") or "").strip().lower()
    if hdr in UPSTREAMS:
        suffix = strip_api_prefix(full, method, body_bytes)
        if suffix is not None:
            return hdr, suffix

    # 3. API key prefix — lets one ngrok URL serve every provider with no
    #    per-provider path. This is the normal path for JanitorAI.
    token = bearer_token(auth_value)
    for key, cfg in UPSTREAMS.items():
        if cfg["key_prefix"] and token.startswith(cfg["key_prefix"]):
            suffix = strip_api_prefix(full, method, body_bytes)
            if suffix is not None:
                return key, suffix

    # 4. Model name in the body (covers clients that send no usable key).
    if body_bytes:
        try:
            parsed = json.loads(body_bytes)
            if isinstance(parsed, dict):
                model = str(parsed.get("model") or "").lower()
                if model.startswith("hemmingway"):
                    suffix = strip_api_prefix(full, method, body_bytes)
                    if suffix is not None:
                        return "hemmingway", suffix
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    # 5. Default provider.
    suffix = strip_api_prefix(full, method, body_bytes)
    if suffix is not None:
        return DEFAULT_PROVIDER, suffix
    return None, None


@app.route("/models", methods=["GET"])
def root_models():
    # In case JanitorAI is pointed at the bare host instead of /v1.
    return proxy_passthrough("models")


def _client_ip():
    """Best client IP for this request.

    X-Forwarded-For is trusted ONLY when the direct peer is loopback — that is
    the tunnel agent forwarding for the whole internet. A direct LAN hit can
    forge XFF, so there remote_addr is the truth. Inside a trusted XFF the
    LAST entry is the edge-appended real client (forged earlier hops are
    irrelevant because the edge appends after them).
    """
    remote = flask_req.remote_addr or ""
    peer = remote[7:] if remote.startswith("::ffff:") else remote
    if peer in ("127.0.0.1", "::1", "localhost"):
        xff = flask_req.headers.get("X-Forwarded-For", "")
        if xff:
            last = xff.split(",")[-1].strip()
            if last:
                return last
    return remote


def _is_private_ip(ip):
    """True for any address that must never count as 'the owner's network'.

    Uses the ipaddress module rather than hand-rolled octets: the old version
    treated every ::ffff:* address as private (a public IPv4-mapped address
    got owner trust) and missed fd00::/8.
    """
    if not ip:
        return False
    v = ip.strip().strip("[]")
    if v.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(v)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


# ---------------------------------------------------------------------------
# Owner-key pinning (trust on first use).
#
# Binding an IP as an owner device is what hands out the upload token, so it
# must not accept just any key: any visitor with their OWN valid upstream key
# could otherwise POST one chat through the tunnel, get bound, and collect
# the token. The first successful authenticated chat pins sha256(bearer);
# every later bind must match it. OWNER_KEY_SHA256 overrides the pin.
# ---------------------------------------------------------------------------
OWNER_KEY_SHA256 = os.environ.get("OWNER_KEY_SHA256", "").strip().lower()
_owner_hash_cache = None
_owner_hash_lock = threading.Lock()


def _owner_hash_file():
    return ZEN_CONFIG_DIR / "owner-key.sha256"


def _key_fingerprint(bearer):
    return hashlib.sha256((bearer or "").encode("utf-8")).hexdigest()


def _owner_hashes():
    """Every accepted owner-key hash: the OWNER_KEY_SHA256 list union the
    TOFU-pinned file. Multiple keys are a first-class case: forward mode puts
    the third-party API's key in JanitorAI, so it must be enterable as an
    owner too, and switching providers must not invalidate the old one.
    """
    global _owner_hash_cache
    if _owner_hash_cache is None:
        with _owner_hash_lock:
            if _owner_hash_cache is None:
                s = set()
                if OWNER_KEY_SHA256:
                    s.update(p for p in
                             (x.strip().lower() for x in OWNER_KEY_SHA256.split(",")) if p)
                try:
                    pinned = _owner_hash_file().read_text().strip().lower()
                    if pinned:
                        s.add(pinned)
                except OSError:
                    pass
                _owner_hash_cache = s
    return _owner_hash_cache


def key_is_owner(bearer):
    """True when this key is an owner's (pinning it on the very first success)."""
    global _owner_hash_cache
    if not bearer:
        return False
    fp = _key_fingerprint(bearer)
    known = _owner_hashes()
    if known:
        return any(hmac.compare_digest(fp, h) for h in known)
    with _owner_hash_lock:
        known = _owner_hashes()
        if known:
            return any(hmac.compare_digest(fp, h) for h in known)
        try:
            ZEN_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            f = _owner_hash_file()
            f.write_text(fp + "\n")
            try:
                f.chmod(0o600)
            except OSError:
                pass
            log.warning("[imghost] owner key pinned (first successful chat) "
                        "fp=%s… — later device binds must use the same key", fp[:12])
        except OSError as e:
            log.warning("[imghost] owner-key pin failed (%s) — binding disabled", e)
            return False
        _owner_hash_cache = {fp}
        return True


# IPs that have recently sent an authenticated chat request (the owner's
# devices, wherever they are). Bootstrapping the upload token is allowed
# only for these.
_seen_auth_ips = OrderedDict()  # ip -> last-seen ts
_auth_ip_lock = threading.Lock()
IP_BIND_TTL = 12 * 3600.0


def remember_auth_ip(bearer=""):
    """Bind this IP as an owner device — only for the owner's own key."""
    ip = _client_ip()
    if not ip:
        return
    if not key_is_owner(bearer):
        log.info("[imghost] successful chat from %s not bound (key is not the "
                 "pinned owner key)", ip)
        return
    with _auth_ip_lock:
        _seen_auth_ips[ip] = time.time()
        _seen_auth_ips.move_to_end(ip)
        while len(_seen_auth_ips) > 256:
            _seen_auth_ips.popitem(last=False)
        stale = [k for k, t in _seen_auth_ips.items() if time.time() - t > IP_BIND_TTL]
        for k in stale:
            _seen_auth_ips.pop(k, None)


def client_is_bound():
    """True for direct private-network clients or IPs seen chatting recently."""
    ip = _client_ip()
    if not ip:
        return False
    if _is_private_ip(ip) and not flask_req.headers.get("X-Forwarded-For"):
        return True  # direct hit from inside the LAN
    with _auth_ip_lock:
        t = _seen_auth_ips.get(ip)
    return bool(t and (time.time() - t) < IP_BIND_TTL)


@app.route("/img/token", methods=["GET"])
def img_token():
    """Zero-setup bootstrap: hand the upload token to the user's own network.

    Accepted proofs, cheapest first:
      1. already-bound IP or a direct LAN/loopback hit;
      2. Authorization carries the pinned OWNER key: binds the caller and
         returns the token immediately. Nothing is forwarded upstream, so no
         chat message is sent and no model call is spent (this replaces the
         old "send one chat message to bind" dance). Trust-on-first-use stays
         exclusive to successful chats, so an unknown key cannot bootstrap
         itself through this door.
    Anyone else (public internet) gets 403. Never CORS-open: a browser page
    must not be able to read this response.
    """
    if not IMAGE_UPLOAD_TOKEN:
        return json_resp({"ok": True, "token": "", "auth": "disabled"}, cors=False)
    if client_is_bound():
        return json_resp({"ok": True, "token": IMAGE_UPLOAD_TOKEN}, cors=False)
    bearer = bearer_token(flask_req.headers.get("Authorization", ""))
    if bearer and _owner_hashes() and key_is_owner(bearer):
        remember_auth_ip(bearer)
        log.info("[imghost] token bootstrap by owner key from %s (no chat needed)",
                 _client_ip())
        return json_resp({"ok": True, "token": IMAGE_UPLOAD_TOKEN}, cors=False)
    log.info("[imghost] token bootstrap refused for non-private client %s (xff=%s)",
             flask_req.remote_addr,
             (flask_req.headers.get("X-Forwarded-For") or "").split(",")[-1][:20])
    return json_resp({"error": {"message": "Not on the owner's network",
                                "type": "auth_error"}}, 403, cors=False)


def _persist_image_bytes(raw_chunks, orig_name):
    """Write image bytes to the store. Returns (name, total)."""
    orig = orig_name or "image.png"
    ext = pathlib.Path(orig).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        ext = ".png"
    name = f"{int(time.time())}-{uuid.uuid4().hex[:10]}{ext}"

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    dest = IMAGE_DIR / name
    total = 0
    try:
        with dest.open("wb") as out:
            for chunk in raw_chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > IMAGE_INTAKE_MAX_BYTES:
                    raise RuntimeError(
                        f"image exceeds {IMAGE_INTAKE_MAX_BYTES} byte cap")
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    mime = _IMG_MIME_BY_EXT.get(ext, "image/png")
    meta = {"mime": mime, "orig": pathlib.Path(orig).name,  # basename only:
            "size": total,  # a full path here leaked the owner's disk layout
            "at": datetime.now(timezone.utc).isoformat()}
    (IMAGE_DIR / (name + ".json")).write_text(json.dumps(meta))
    return name, mime, total


def _img_upload_response(name, total):
    path = "/img/" + name
    url = (IMAGE_PUBLIC_HOST if IMAGE_PUBLIC_HOST else "") + path
    log.info("[imghost] stored %s (%.1f KB) -> %s", name, total / 1024.0, url)
    return json_resp({"ok": True, "path": path, "url": url,
                      "id": name, "size": total}, cors=False)


@app.route("/img/upload", methods=["POST"])
def img_upload():
    """Self-hosted image store: persistent, no moderation, no 4-link cap.

    Two upload shapes:
      - multipart form with a `file` part (curl / desktop tooling)
      - JSON {"name": "file.png", "data": "<base64>"} — what the userscript
        sends, immune to FormData quirks inside userscript sandboxes.
    Intake later fetches stored images back via localhost (see _localize_url),
    so vision attachment never depends on the public tunnel."""
    tok = flask_req.headers.get("X-Upload-Token", "")
    if IMAGE_UPLOAD_TOKEN and not hmac.compare_digest(tok, IMAGE_UPLOAD_TOKEN):
        log.warning("[imghost] upload rejected (bad/missing token) from %s",
                    flask_req.remote_addr)
        return json_resp({"error": {"message": "Invalid or missing X-Upload-Token",
                                    "type": "auth_error"}}, 401, cors=False)

    if flask_req.is_json:
        payload = flask_req.get_json(silent=True) or {}
        b64 = str(payload.get("data") or "")
        if not b64:
            return json_resp({"error": {"message": "No 'data' (base64) in JSON upload",
                                        "type": "invalid_request_error"}}, 400, cors=False)
        # Cap BEFORE decoding: b64 inflates 4/3, and decoding a huge string
        # first is exactly how a memory spike happens.
        if len(b64) > int(IMAGE_INTAKE_MAX_BYTES * 4 / 3) + 4096:
            return json_resp({"error": {"message": "Image too large",
                                        "type": "invalid_request_error"}}, 413, cors=False)
        try:
            raw = base64.b64decode(b64)
        except Exception as e:
            return json_resp({"error": {"message": "Invalid base64 data: %s" % e,
                                        "type": "invalid_request_error"}}, 400, cors=False)
        try:
            name, _mime, total = _persist_image_bytes([raw],
                                                      str(payload.get("name") or "image.png"))
        except Exception as e:
            log.warning("[imghost] upload failed: %s", e)
            return json_resp({"error": {"message": str(e),
                                        "type": "invalid_request_error"}}, 400, cors=False)
        return _img_upload_response(name, total)

    f = flask_req.files.get("file")
    if f is None and flask_req.files:
        f = next(iter(flask_req.files.values()))
    if f is None or not f.filename:
        return json_resp({"error": {"message": "No file part in upload",
                                    "type": "invalid_request_error"}}, 400, cors=False)

    def _chunks():
        while True:
            chunk = f.stream.read(65536)
            if not chunk:
                break
            yield chunk

    try:
        name, _mime, total = _persist_image_bytes(_chunks(), f.filename)
    except Exception as e:
        log.warning("[imghost] upload failed: %s", e)
        return json_resp({"error": {"message": str(e),
                                    "type": "invalid_request_error"}}, 400, cors=False)
    return _img_upload_response(name, total)


_IMG_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".webp": "image/webp",
                    ".gif": "image/gif"}


# Only files THIS bridge created may be served: <epoch>-<10 hex>.<ext>.
# pathlib.Path(name).name blocks traversal but NOT dotfiles, which is exactly
# how /img/.upload-token was publicly readable.
_SAFE_IMAGE_NAME_RE = re.compile(r"^\d{10}-[0-9a-f]{10}\.(?:png|jpe?g|webp|gif)$")


def _static_headers():
    """Headers for public static files — deliberately NO CORS.

    /v1/* is CORS-open because JanitorAI calls it cross-origin. /img/* is a
    plain image host: a wildcard ACAO there let any website read the token
    endpoint's response out of the owner's browser.
    """
    return {"Cache-Control": "public, max-age=31536000, immutable"}


@app.route("/img/<name>", methods=["GET"])
def img_get(name):
    """Serve a stored image. Public read — but only files we created."""
    if not _SAFE_IMAGE_NAME_RE.match(name or ""):
        return json_resp({"error": {"message": "Not found",
                                    "type": "invalid_request_error"}}, 404, cors=False)
    safe = pathlib.Path(name).name  # belt and braces
    p = IMAGE_DIR / safe
    if not p.is_file():
        return json_resp({"error": {"message": "Not found",
                                    "type": "invalid_request_error"}}, 404, cors=False)
    mime = None
    try:
        meta = json.loads((IMAGE_DIR / (safe + ".json")).read_text())
        mime = meta.get("mime")
    except (OSError, ValueError):
        pass
    if not mime:
        mime = _IMG_MIME_BY_EXT.get(p.suffix.lower(), "application/octet-stream")
    return Response(p.read_bytes(), status=200, content_type=mime,
                    headers=_static_headers())


@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def proxy_passthrough(path=""):
    t0 = time.time()
    body = flask_req.get_data()
    body, _n_images = maybe_transform_body(body, flask_req.path)

    # Forward mode (userscript "Route other APIs through my bridge"): the
    # owner's key may ask us to relay this exact request to the API origin
    # JanitorAI was originally configured with. Everything else, including
    # non-owner keys, follows the normal route table.
    fwd_header = (flask_req.headers.get("X-Zen-Forward-Origin") or "").strip()
    fwd_origin, fwd_reason = _validated_forward_origin()
    neutral = False
    if fwd_header and not fwd_origin:
        log.warning("[forward] refused (%s) from %s", fwd_reason, _client_ip())
        return json_resp({"error": {"message": "Forward refused: " + fwd_reason,
                                    "type": "proxy_error"}}, 403)
    if fwd_origin:
        provider_key = DEFAULT_PROVIDER
        cfg = UPSTREAMS[provider_key]
        q = flask_req.query_string.decode("utf-8", "replace") if flask_req.query_string else ""
        url = fwd_origin + flask_req.path + (("?" + q) if q else "")
        suffix = flask_req.path
        neutral = True
        log.info("[forward] %s %s -> %s", flask_req.method, flask_req.path, fwd_origin)
    else:
        provider_key, suffix = resolve_route(path, flask_req.headers.get("Authorization", ""),
                                             body, flask_req.headers, flask_req.method)
        if suffix is None:
            log.info("404 %s %s", flask_req.method, flask_req.path)
            return json_resp(
                {"error": {"message": f"Not found: {flask_req.path}. Use /v1/... ",
                           "type": "invalid_request_error"}}, 404)

        cfg = UPSTREAMS[provider_key]
        url = cfg["base"] + suffix
    params = flask_req.args
    headers, session_id, session_source = build_upstream_headers(provider_key, neutral=neutral)

    # Stream if client asked for it or expects SSE.
    wants_stream = False
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and parsed.get("stream") is True:
                wants_stream = True
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    if "text/event-stream" in flask_req.headers.get("Accept", ""):
        wants_stream = True

    log.info("%s %s -> %s [%s, session:%s... via %s] stream=%s %db",
             flask_req.method, flask_req.path, url, provider_key,
             session_id[:8], session_source, wants_stream, len(body or b""))

    try:
        if wants_stream:
            up = requests.request(flask_req.method, url, headers=headers,
                                  data=body, params=params, stream=True,
                                  timeout=(10, 300))
            # Self-heal: if we somehow still missed the session, retry once.
            # OpenCode-only — no other provider has this requirement (and
            # forwarded requests are someone else's API: no session header).
            if not neutral and up.status_code == 400 and cfg["session_header"]:
                try:
                    err = up.json()
                    if "MissingSessionID" in json.dumps(err):
                        headers["x-opencode-session"] = DEFAULT_SESSION
                        up = requests.request(flask_req.method, url, headers=headers,
                                              data=body, params=params, stream=True,
                                              timeout=(10, 300))
                except ValueError:
                    pass
            if up.status_code < 400:
                remember_auth_ip(bearer_token(flask_req.headers.get("Authorization", "")))

            def generate():
                try:
                    for chunk in up.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
                except (ConnectionError, BrokenPipeError):
                    pass

            h = cors_headers()
            h.pop("Access-Control-Max-Age", None)
            for k, v in up.headers.items():
                if k.lower() not in ("transfer-encoding", "content-encoding",
                                     "content-length", "connection"):
                    h[k] = v
            log.info("<- %d %s (stream, %.1fs)", up.status_code, url,
                     time.time() - t0)
            return Response(generate(), status=up.status_code,
                            content_type=up.headers.get("content-type",
                                                        "text/event-stream"),
                            headers=h)
        else:
            up = requests.request(flask_req.method, url, headers=headers,
                                  data=body, params=params, timeout=(10, 120))
            if not neutral and up.status_code == 400 and cfg["session_header"]:
                try:
                    if "MissingSessionID" in up.text:
                        log.warning("Upstream still reports MissingSessionID, "
                                    "retrying with default session")
                        headers["x-opencode-session"] = DEFAULT_SESSION
                        up = requests.request(flask_req.method, url, headers=headers,
                                              data=body, params=params,
                                              timeout=(10, 120))
                except requests.RequestException:
                    pass
            if up.status_code < 400:
                remember_auth_ip(bearer_token(flask_req.headers.get("Authorization", "")))

            h = cors_headers()
            ct = up.headers.get("content-type", "")
            h["Content-Type"] = ct if ct else "application/json"
            if "text/html" in ct:
                out = json.dumps({"error": {
                    "message": f"Upstream returned HTML (status {up.status_code}) — "
                               f"invalid API path: {suffix}",
                    "type": "invalid_request_error",
                    "code": up.status_code}}).encode()
                h["Content-Type"] = "application/json"
            else:
                out = up.content
            log.info("<- %d %s (%db, %.1fs)", up.status_code, url,
                     len(out), time.time() - t0)
            return Response(out, status=up.status_code, headers=h)

    except requests.exceptions.ConnectionError as e:
        log.error("Upstream connection failed: %s", e)
        return json_resp({"error": {"message": f"Connection to upstream failed: {e}",
                                    "type": "proxy_error"}}, 502)
    except requests.exceptions.Timeout:
        log.error("Upstream timeout: %s", url)
        return json_resp({"error": {"message": "Upstream request timed out",
                                    "type": "proxy_error"}}, 504)
    except requests.exceptions.RequestException as e:
        log.error("Upstream error: %s", e)
        return json_resp({"error": {"message": str(e),
                                    "type": "proxy_error"}}, 502)


if __name__ == "__main__":
    print(f"JanitorAI CORS bridge v{VERSION} + image intake on http://0.0.0.0:{PORT}", flush=True)
    for _k, _v in UPSTREAMS.items():
        print(f"  {_k}: {_v['base']}", flush=True)
    print(f"  image store: {IMAGE_DIR} (public host: {IMAGE_PUBLIC_HOST or 'unset — uploads return relative paths'})", flush=True)
    print("Point JanitorAI at http://SERVER:%d/v1" % PORT, flush=True)
    # NOTE: debug=False + use_reloader=False — debug reloader spawns a duplicate
    # process (that was the double `zen-cors-proxy.py` in ps output).
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False,
            threaded=True)
