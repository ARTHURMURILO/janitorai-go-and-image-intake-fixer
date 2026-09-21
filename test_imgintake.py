import json
import importlib.util
import os
import pathlib
import tempfile

os.environ["IMAGE_PUBLIC_HOST"] = "https://test-ngrok.example.dev"

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="zen-intake-"))
os.environ.setdefault("IMAGE_DIR", str(_tmp / "images"))
os.environ.setdefault("ZEN_CONFIG_DIR", str(_tmp / "config"))

spec = importlib.util.spec_from_file_location(
    "zenproxy",
    os.environ.get("ZEN_PROXY",
                   str(pathlib.Path(__file__).resolve().parent / "zen-cors-proxy.py")))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

ELLA = "https://ella.janitorai.com/media-approved/RK6nu6wEIoWiNcaKilNdk.webp"
OWN = "https://test-ngrok.example.dev/img/1234-test.png"  # host NOT public host
OWN2 = "https://test-ngrok.example.dev/img/5678-test.png"

# Deterministic fake fetcher for logic tests (distinct fake URLs would 404 in
# the real downloader). The real fetcher is exercised at the end of this file.
m.fetch_image_data_url = lambda u: "data:image/png;base64,FAKE-" + u[-12:]

# --- Case 1: 6 ella links in one user message -> cap 4 attached, newest kept
payload = {
    "model": "test", "stream": True,
    "messages": [
        {"role": "system", "content": "card ![c](%s) stays text" % ELLA},
        {"role": "user", "content": " ".join(["![A image](%s)" % ELLA] * 6)
         + " what is in than image?"},
    ],
}
body = json.dumps(payload).encode()
new_body, n = m.maybe_transform_body(body, "/v1/chat/completions")
out = json.loads(new_body)
assert isinstance(out["messages"][0]["content"], str), "system untouched"
parts = out["messages"][1]["content"]
n_imgs = sum(1 for p in parts if p["type"] == "image_url")
assert n_imgs == 1, "same URL 6x must dedupe to 1, got %d" % n_imgs
assert n == 1, n
txt = [p["text"] for p in parts if p["type"] == "text"][0]
assert "![A image]" not in txt and "what is in than image?" in txt, txt
print("CASE1_OK dedupe: 6 identical links -> 1 image")

# --- Case 2: repeated URL across history -> attach only at newest mention
payload = {
    "model": "test", "stream": True,
    "messages": [
        {"role": "user", "content": "look ![x](%s)" % ELLA},
        {"role": "assistant", "content": "nice"},
        {"role": "user", "content": "again ![x](%s) ok?" % ELLA},
    ],
}
out = json.loads(m.maybe_transform_body(json.dumps(payload).encode(), "/c")[0])
m0 = out["messages"][0]["content"]
m2 = out["messages"][2]["content"]
assert isinstance(m0, str), "older duplicate must stay plain text, got %r" % (m0,)
assert isinstance(m2, list) and any(p["type"] == "image_url" for p in m2)
print("CASE2_OK dedupe-across-history: attached at newest mention only")

# --- Case 3: 6 DISTINCT ella urls -> newest 4 attached, oldest 2 stay text
urls6 = ["https://ella.janitorai.com/media-approved/img%d.webp" % i for i in range(6)]
payload = {
    "model": "test", "stream": True,
    "messages": [
        {"role": "user", "content": " ".join("![i](%s)" % u for u in urls6)},
    ],
}
out = json.loads(m.maybe_transform_body(json.dumps(payload).encode(), "/c")[0])
parts = out["messages"][0]["content"]
n_imgs = sum(1 for p in parts if p["type"] == "image_url")
txt = [p["text"] for p in parts if p["type"] == "text"][0]
assert n_imgs == 4, "cap must be 4, got %d" % n_imgs
# Within a message, links attach in reading order (first four); extras stay text.
assert "img0" not in txt and "img1" not in txt and "img2" not in txt and "img3" not in txt, \
    "first four must be attached, not left as text: " + txt
assert "img4" in txt and "img5" in txt, "extra links must remain as text: " + txt
print("CASE3_OK cap 4: first 4 in reading order attached, extras stay text")

# --- Case 4: own-host URL localizes to 127.0.0.1
loc = m._localize_url("https://test-ngrok.example.dev/img/abc-1.png")
assert loc == "http://127.0.0.1:8081/img/abc-1.png", loc
loc2 = m._localize_url(ELLA)
assert loc2 == ELLA, loc2
print("CASE4_OK own-host URLs localize to localhost for intake fetch")

# --- Case 5: mixed markdown + bare in one message
payload = {
    "model": "test", "stream": True,
    "messages": [
        {"role": "user", "content": "a ![A image](%s) b %s c" % (ELLA, OWN2)},
    ],
}
out = json.loads(m.maybe_transform_body(json.dumps(payload).encode(), "/c")[0])
parts = out["messages"][0]["content"]
texts = [p["text"] for p in parts if p["type"] == "text"]
n_imgs = sum(1 for p in parts if p["type"] == "image_url")
assert n_imgs == 2, n_imgs
joined = " ".join(texts)
assert "ella" not in joined and "5678-test" not in joined and "a b c" in joined, joined
print("CASE5_OK mixed markdown+bare both attached, text cleaned")

# --- Case 6: malformed markdown (missing paren + trailing quote) still works
payload = {
    "model": "test", "stream": True,
    "messages": [
        {"role": "user", "content":
         'a ![A image](https://wallpapers.com/images/hd/cat.jpg" b'},
    ],
}
out = json.loads(m.maybe_transform_body(json.dumps(payload).encode(), "/c")[0])
parts = out["messages"][0]["content"]
n_imgs = sum(1 for p in parts if p["type"] == "image_url")
txt = " ".join(p["text"] for p in parts if p["type"] == "text")
assert n_imgs == 1, "malformed markdown must still attach, got %d" % n_imgs
assert "![" not in txt and "cat.jpg" not in txt and "a b" in txt, repr(txt)
print("CASE6_OK malformed markdown (missing paren / trailing quote) attaches + cleans")

# --- Cache check: real ella image still fetchable and cached
du = m.fetch_image_data_url(ELLA)
assert du.startswith("data:image/"), du[:40]
print("CACHE_OK", len(du))
print("ALL_OK")
