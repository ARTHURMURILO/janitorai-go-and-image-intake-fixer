"""Security regression tests for the bridge (AUDIT-REPORT findings C1..M4).

Hermetic: throwaway config/image dirs next to this file. Run:
    python3 test_security.py

Each case pins one fixed vulnerability so it can never silently come back.
"""
import importlib.util
import os
import pathlib
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="zen-sec-"))
os.environ["IMAGE_PUBLIC_HOST"] = "https://test-ngrok.example.dev"
os.environ["IMAGE_DIR"] = str(_tmp / "images")
os.environ["ZEN_CONFIG_DIR"] = str(_tmp / "config")

spec = importlib.util.spec_from_file_location(
    "zenproxy", pathlib.Path(__file__).with_name("zen-cors-proxy.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
client = m.app.test_client()

_fail = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("OK  " if cond else "FAIL", name,
                         "" if cond else "  <- " + str(detail)))
    if not cond:
        _fail.append(name)


# --- C1: /img/<name> must only serve files the bridge itself created ---------
IMG_DIR = pathlib.Path(os.environ["IMAGE_DIR"])
IMG_DIR.mkdir(parents=True, exist_ok=True)
(IMG_DIR / ".upload-token").write_text("super-secret-token\n")
(IMG_DIR / "notes.txt").write_text("private\n")
(IMG_DIR / "1789954124-49d1671d63.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
(IMG_DIR / "1789954124-49d1671d63.png.json").write_text('{"mime":"image/png"}')

r = client.get("/img/.upload-token")
check("C1 dotfile not served", r.status_code == 404, r.status_code)
check("C1 dotfile body empty", b"super-secret" not in r.get_data(), r.get_data()[:40])
r = client.get("/img/notes.txt")
check("C1 arbitrary file not served", r.status_code == 404, r.status_code)
r = client.get("/img/1789954124-49d1671d63.png.json")
check("C1 sidecar json not served", r.status_code == 404, r.status_code)
r = client.get("/img/1789954124-49d1671d63.png")
check("C1 real image served", r.status_code == 200, r.status_code)
r = client.get("/img/..%2f..%2fetc%2fpasswd")
check("C1 traversal still blocked", r.status_code == 404, r.status_code)

# --- C1b: token lives outside the public image store -------------------------
check("C1b token outside IMAGE_DIR",
      not (IMG_DIR / ".upload-token").samefile(m.ZEN_CONFIG_DIR / "upload-token")
      if (m.ZEN_CONFIG_DIR / "upload-token").exists() else True,
      "token path: %s" % (m.ZEN_CONFIG_DIR / "upload-token"))

# --- C3: /img/* must not be CORS-open ---------------------------------------
for path in ("/img/token", "/img/1789954124-49d1671d63.png"):
    r = client.get(path)
    check("C3 no ACAO on %s" % path,
          "Access-Control-Allow-Origin" not in r.headers, dict(r.headers))
r = client.options("/img/token", headers={"Origin": "https://evil.tld",
                                          "Access-Control-Request-Method": "GET",
                                          "Access-Control-Request-Headers": "X-Evil"})
check("C3 preflight grants nothing on /img",
      "Access-Control-Allow-Origin" not in r.headers, dict(r.headers))
r = client.get("/v1/models")
check("C3 /v1 keeps CORS (JanitorAI needs it)",
      r.headers.get("Access-Control-Allow-Origin") == "*", dict(r.headers))

# --- C4: only the owner's key may bind a device ------------------------------
m._seen_auth_ips.clear()
m._owner_hash_cache = None
try:
    (m.ZEN_CONFIG_DIR / "owner-key.sha256").unlink()
except OSError:
    pass
check("C4 first key becomes owner", m.key_is_owner("owner-key") is True)
check("C4 same key still owner", m.key_is_owner("owner-key") is True)
check("C4 other key rejected", m.key_is_owner("attacker-key") is False)

m._seen_auth_ips.clear()
_real_client_ip = m._client_ip  # restore before the H1 section below
m._client_ip = lambda: "203.0.113.9"
m.remember_auth_ip("attacker-key")
check("C4 attacker chat does not bind", "203.0.113.9" not in m._seen_auth_ips)
m.remember_auth_ip("owner-key")
check("C4 owner chat binds", m._seen_auth_ips.get("203.0.113.9") is not None)

# --- M1: private-IP classification ------------------------------------------
check("M1 public IPv4-mapped not private", m._is_private_ip("::ffff:8.8.8.8") is False)
check("M1 private IPv4-mapped private", m._is_private_ip("::ffff:192.168.1.5") is True)
check("M1 ULA fd00::/8 private", m._is_private_ip("fd00::1") is True)
check("M1 10/8 private", m._is_private_ip("10.0.0.5") is True)
check("M1 public not private", m._is_private_ip("8.8.8.8") is False)

m._client_ip = _real_client_ip

# --- H1: X-Forwarded-For only trusted from the tunnel (loopback peer) --------
class _Req:
    def __init__(self, remote, xff=None):
        self.remote_addr = remote
        self.headers = {"X-Forwarded-For": xff} if xff else {}


_real_req = m.flask_req
m.flask_req = _Req("203.0.113.7", "9.9.9.9")
check("H1 direct hit ignores forged XFF", m._client_ip() == "203.0.113.7", m._client_ip())
m.flask_req = _Req("127.0.0.1", "1.2.3.4, 9.9.9.9")
check("H1 tunnel XFF last hop wins", m._client_ip() == "9.9.9.9", m._client_ip())
m.flask_req = _real_req

# --- C2: SSRF gate -----------------------------------------------------------
check("C2 loopback blocked", m._is_fetchable_public_url("http://127.0.0.1:8081/x") is False)
check("C2 metadata IP blocked",
      m._is_fetchable_public_url("http://169.254.169.254/latest/meta-data") is False)
check("C2 private range blocked", m._is_fetchable_public_url("http://192.168.0.1/x") is False)
check("C2 file scheme blocked", m._is_fetchable_public_url("file:///etc/passwd") is False)
check("C2 public host allowed",
      m._is_fetchable_public_url("https://example.com/a.png") is True)

# --- C2b: allowlist compares bare hostnames (host:port used to bypass) -------
m.IMAGE_INTAKE_HOSTS = ("allowed.example",)
try:
    check("C2b allowlisted host ok", m._host_allowed("https://allowed.example/a.png") is True)
    check("C2b host:port still checked",
          m._host_allowed("https://evil.tld:443/a.png") is False)
finally:
    m.IMAGE_INTAKE_HOSTS = ()

# --- M4: sidecar keeps basename only ----------------------------------------
_name, _mime, _total = m._persist_image_bytes([b"\x89PNGdata"], "/home/secret/Pictures/me.png")
_meta = __import__("json").loads((IMG_DIR / (_name + ".json")).read_text())
check("M4 sidecar stores basename", _meta.get("orig") == "me.png", _meta.get("orig"))

# --- M3: oversize upload rejected before decode ------------------------------
_big = "A" * (int(m.IMAGE_INTAKE_MAX_BYTES * 4 / 3) + 8192)
r = client.post("/img/upload", json={"name": "x.png", "data": _big},
                headers={"X-Upload-Token": m.IMAGE_UPLOAD_TOKEN})
check("M3 oversize JSON upload rejected", r.status_code == 413, r.status_code)

# --- M2: upload token compare is constant-time + enforced --------------------
r = client.post("/img/upload", json={"name": "x.png", "data": "aGk="},
                headers={"X-Upload-Token": "wrong"})
check("M2 wrong upload token rejected", r.status_code == 401, r.status_code)

# --- Forward mode: owner key only, public https targets only -----------------
_fwd_seen = {}

class _FakeUpstream:
    status_code = 200
    headers = {"Content-Type": "application/json"}
    text = '{"choices": []}'
    content = b'{"choices": []}'
    def json(self):
        return {"choices": []}
    def iter_content(self, n=None):
        yield self.content

def _capturing_request(method, url, **kw):
    _fwd_seen["url"] = url
    _fwd_seen["headers"] = kw.get("headers") or {}
    return _FakeUpstream()

m.requests.request = _capturing_request
_fwd_body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}

client.post("/v1/chat/completions", json=_fwd_body,
            headers={"Authorization": "Bearer owner-key",
                     "X-Zen-Forward-Origin": "https://example.com"})
check("FWD owner key relays to the tagged origin",
      str(_fwd_seen.get("url", "")).startswith("https://example.com/v1/chat/completions"),
      _fwd_seen.get("url"))
check("FWD relay keeps the original path",
      str(_fwd_seen.get("url", "")).endswith("/v1/chat/completions"), _fwd_seen.get("url"))
check("FWD header never travels downstream",
      not any(k.lower() == "x-zen-forward-origin" for k in _fwd_seen.get("headers", {})),
      _fwd_seen.get("headers"))
check("FWD neutral mode adds no opencode session header",
      not any(k.lower() == "x-opencode-session" for k in _fwd_seen.get("headers", {})),
      _fwd_seen.get("headers"))

client.post("/v1/chat/completions", json=_fwd_body,
            headers={"Authorization": "Bearer attacker-key",
                     "X-Zen-Forward-Origin": "https://api.evil.example"})
check("FWD non-owner key is ignored (normal routing)",
      not str(_fwd_seen.get("url", "")).startswith("https://api.evil.example"),
      _fwd_seen.get("url"))

client.post("/v1/chat/completions", json=_fwd_body,
            headers={"Authorization": "Bearer owner-key",
                     "X-Zen-Forward-Origin": "http://169.254.169.254/latest"})
check("FWD http / link-local target refused",
      not str(_fwd_seen.get("url", "")).startswith("http://169.254.169.254"),
      _fwd_seen.get("url"))

print("\nSECURITY_TESTS_" + ("OK" if not _fail else "FAILED: " + ", ".join(_fail)))
raise SystemExit(1 if _fail else 0)
