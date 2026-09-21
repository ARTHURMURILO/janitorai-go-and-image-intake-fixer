"""Unit test for the /img/token bind+bootstrap flow with mocked upstream.

Hermetic: runs against a throwaway config/image dir next to this file, so a
test run can never pin a fake owner key (or write images) into the real
~/.config/zen-proxy or image store. Run: python3 test_bootstrap_unit.py
"""
import importlib.util
import os
import pathlib
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="zen-test-"))
os.environ["IMAGE_PUBLIC_HOST"] = "https://test-ngrok.example.dev"
os.environ["IMAGE_DIR"] = str(_tmp / "images")
os.environ["ZEN_CONFIG_DIR"] = str(_tmp / "config")

spec = importlib.util.spec_from_file_location(
    "zenproxy", pathlib.Path(__file__).with_name("zen-cors-proxy.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {"Content-Type": "application/json"}
        self.text = '{"ok": true}'
        self.content = b'{"ok": true}'

    def json(self):
        return self._payload

    def iter_content(self, n):
        yield self.content


calls = {"n": 0, "last_upstream_status": 200}


def fake_request(method, url, **kw):
    calls["n"] += 1
    return FakeResp(calls["last_upstream_status"], {"choices": []})


m.requests.request = fake_request
client = m.app.test_client()

AUTH = {"Content-Type": "application/json", "Authorization": "Bearer good-key"}


def chat(xff=None, status=200):
    calls["last_upstream_status"] = status
    headers = dict(AUTH)
    if xff:
        headers["X-Forwarded-For"] = xff
    return client.post("/v1/chat/completions", headers=headers,
                       json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})


def token(xff=None):
    headers = {}
    if xff:
        headers["X-Forwarded-For"] = xff
    return client.get("/img/token", headers=headers)


def expect(name, resp, status):
    assert resp.status_code == status, "%s: got %d want %d (%s)" % (
        name, resp.status_code, status, resp.get_data(as_text=True)[:120])
    print("  %s -> %d OK" % (name, status))


# direct private (test client remote=127.0.0.1, no XFF)
expect("direct private", token(), 200)
# unbound public IP
expect("unbound via XFF", token("9.9.9.9"), 403)
# successful chat binds the IP
r = chat("9.9.9.9", 200)
assert r.status_code == 200, r.status_code
expect("bound after 200 chat", token("9.9.9.9"), 200)
# failed (401) chat must NOT bind
chat("7.7.7.7", 401)
expect("401 chat does not bind", token("7.7.7.7"), 403)
# forged FIRST XFF hop cannot smuggle the bound IP (last hop decides)
expect("forged first hop w/ bound last", token("7.7.7.7, 9.9.9.9"), 200)
expect("forged first hop w/ unbound last", token("9.9.9.9, 7.7.7.7"), 403)
# TTL expiry
m._seen_auth_ips["9.9.9.9"] = m.time.time() - (m.IP_BIND_TTL + 10)
expect("expired binding", token("9.9.9.9"), 403)
print("BOOTSTRAP_UNIT_OK")
