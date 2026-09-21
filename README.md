Hi! So i made this server + userscript to fix two problems!

First was that the JanitorAI proxy doesn't directly work with OpenCodeGO and https://hemmingway.io/ (their AI model), so at first i made it to fix those two.

Although i quickly realized that i wanted image upload to be functional in JanitorAI too, and managed to cook this up with Pi (GLM 5.3 Flash and DeepSeek Flash v4.1).
It works pretty well, so i decided to open source it!

Won't deny AI wrote this, i just wanted something working. And this isn't plug and play, since JanitorAI's back end is purely text only. The server handles
everything JanitorAI can't, while trying to work with nothing but a link from JanitorAI's side.

Basically you just need:

1 - A server (any laptop or old pc works, although this is mostly aimed at Linux and i am unsure about Windows since i don't have a Windows machine)

2 - Ngrok (free to sign up for, and their URL system is a good stand in for a permanent deployment)

After setting those two up (Clanker instructions below) it should work fine. Compatible with desktop and mobile.

(LLM written instructions underneath)

# JanitorAI Image Intake: real images to your own proxy

Send **real images** (OpenAI-style multimodal `image_url` parts) through
JanitorAI to whatever model your proxy routes to. Two pieces that work
together:

1. **`zen-cors-proxy.py`**: a dependency-light CORS bridge you run on your own
   box (v2.5). It
   - converts image links inside chat messages into **real multimodal image
     parts** before forwarding upstream (the core feature), and
   - runs a **self-hosted image store** (`POST /img/upload`, `GET /img/<id>`),
     so the attach button uploads to *your* server (no JanitorAI Media
     Library, no moderation pipeline, no 4-link ella limit).
2. **`janitorai-image-intake.user.js`**: a Tampermonkey companion (v1.8.0)
   that adds attach buttons, drag & drop, and in-chat rendering of image
   links. Pure UX: the bridge works without it.

## Quick start (5 minutes)

```bash
git clone https://github.com/ARTHURMURILO/janitorai-go-and-image-intake-fixer
cd janitorai-go-and-image-intake-fixer
python3 -m pip install -r requirements.txt      # flask, requests (+Pillow optional)

# Tell the bridge its public URL (the host you will tunnel):
cp start-zen-proxy.local.sh.example start-zen-proxy.local.sh
$EDITOR start-zen-proxy.local.sh                # export IMAGE_PUBLIC_HOST=https://<your-tunnel>

./start-zen-proxy.sh                            # starts on :8081, prints healthz
```

Expose port 8081 with any tunnel (`ngrok http 8081`, `cloudflared tunnel …`),
then:

1. In JanitorAI: set the **custom/proxy base URL** to `https://<your-tunnel>/v1`
   and paste your provider key. Send one message, and replies prove the bridge
   works.
2. Install the userscript: open `https://<your-tunnel>/intake.user.js` in the
   browser (Tampermonkey offers to install), or install
   `janitorai-image-intake.user.js` from this repo. It updates itself from
   GitHub once installed.
3. Open a chat → buttons appear top-right (📎 attach · 🔗 paste link ·
   🖼 settings). Your first chat message teaches the script your bridge URL
   (verified and pinned from then on); your device then binds itself
   automatically from the page's own API traffic, so throwaway messages are
   never needed again. Attach away.

### Why the bridge learns itself

No manual configuration: the userscript watches JanitorAI's own outgoing chat
request for the proxy host and accepts the evidence only when it is signed
with a per-page nonce. A candidate URL must then pass two more checks before
it is adopted:

1. the host looks like **your** tunnel or LAN (ngrok, tailscale `ts.net`, a
   private IP; a random ngrok or an unrelated API is rejected on sight), and
2. it **proves it is the bridge** by answering `/healthz` with the bridge
   shape, for example `{"ok": true, "v": "2.5"}`.

Once a good URL is learned it is **pinned**: nothing can silently repoint it
again, not a page script, not another ngrok, not localStorage. On every boot
the learned URL is re-checked, and one that answers like something else (the
"learned the wrong server" case) is forgotten automatically so learning can
start over. You can also forget it yourself: clear the URL field in Advanced
and Save.

Fetching the upload token then works with zero setup: `/img/token` answers
when your device shows the pinned owner key (the page already sends it on
every API call, and the script probes with it silently) or when the device
chatted recently. Rotating the token later is just
`rm ~/.config/zen-proxy/upload-token` + restart; every device re-fetches on
its next attach, no messages involved.

### Use any API: forward mode

Point JanitorAI at whatever API you like (Xiaomi's `api.xiaomimimo.com`,
anything else) and tick **Route other APIs through my bridge** in the ⚙
sheet. Then:

- JanitorAI keeps its own proxy URL exactly as you set it (no swapping to
  ngrok, no losing your model list),
- the extension quietly reroutes those calls through your bridge, tagging the
  original origin, and
- the bridge relays them there, so you still get image intake, self hosting
  and the 4 image cap for **any** provider.

Forwarding only works with a key from the **owner set**, only over https,
and only to public hosts. A refused forward answers with a clear 403 that
names the fix (instead of silently falling through to some default upstream),
and the toggle off means everything goes direct again.

Because JanitorAI has one key slot, forward mode puts your *target API's* key
there (Xiaomi's key, say). Tell the bridge it is yours, once:

```bash
printf %s 'YOUR-XIAOMI-KEY' | sha256sum
# then, on the server (gitignored file):
echo 'export OWNER_KEY_SHA256="<hash-from-above>"' >> ~/Documents/start-zen-proxy.local.sh
bash ~/Documents/start-zen-proxy.sh
```

`OWNER_KEY_SHA256` takes a comma separated list and is **unioned with the
existing pin**, so your original key keeps working. Keys added this way are
full owner keys: they also drive device binding, so phones using forward mode
bind automatically too.

## How sending works

Any `![alt](url)` or bare image URL (`.png/.jpg/.webp/.gif`) in a `user` or
`assistant` message is fetched by the bridge, downscaled/base64-encoded, and
rewritten into `[{"type":"text"},{"type":"image_url"}]` (OpenAI vision format)
before the payload reaches the provider.

- **Self-hosted links (default)**: 📎 uploads to your server and pastes
  `![name](https://<tunnel>/img/<id>.<ext>)`. The bridge re-reads its own store
  over `127.0.0.1`, so tunnel/CDN quirks never affect attachment.
- **ella links** (`ella.janitorai.com/...`) work through the bridge as before.
- **External links** (catbox, wallpapers.com, …) work too; the userscript also
  renders them inline in chat.
- **System messages are never touched**: character cards and lorebooks stay
  plain text.

### The 4-image cap

Providers reject prompts with more than 4 image parts. So the bridge attaches
at most `IMAGE_INTAKE_MAX_IMAGES` (default 4) per request, **dedupes URLs**
across the whole history (the same image attaches once, at its newest mention),
and keeps the extras as plain text links. Links whose fetch failed are **not**
stripped, so a dead URL stays visible in the message.

## Endpoints

| route | auth | purpose |
|---|---|---|
| `POST /img/upload` | `X-Upload-Token` header | multipart `file` **or** JSON `{name, data:<base64>}` → `{ok,path,url,id,size}` |
| `GET /img/<id>.<ext>` | public | serves a stored image (immutable cache, no CORS) |
| `GET /img/token` | owner-only | hands the upload token to the owner's devices |
| `GET /intake.user.js` | public | serves the userscript for one-tap install |
| `POST /v1/chat/completions` | your provider key | the proxied API JanitorAI talks to |
| `GET /healthz` | public | `{"ok":true,"v":"2.5"}` |

### Who counts as "the owner"

`/img/token` answers only when the caller is provably one of your devices:

- a **direct LAN/loopback hit** (private peer, no proxy headers), or
- an IP that **recently completed an authenticated chat** through the tunnel
  (upstream answered `<400`), within a 12h TTL, and only when the key used is
  the **owner key** pinned on first success (`OWNER_KEY_SHA256` overrides;
  `~/.config/zen-proxy/owner-key.sha256` is the pin), or
- a request that simply **shows the owner key** in `Authorization`: it binds
  the caller and returns the token with **nothing forwarded upstream**, so no
  chat is sent, no model call is made and no tokens are spent. The userscript
  does this for you from the page's own API traffic.

Only the very first pin needs one real chat message: trust on first use
happens exclusively on a successful chat, so an unknown key can never
bootstrap itself. The same applies when you change the key you use in
JanitorAI: delete `~/.config/zen-proxy/owner-key.sha256` (or set
`OWNER_KEY_SHA256`) and send one message with the new key.

Fake keys never bind. `X-Forwarded-For` is trusted only from the loopback
tunnel peer, and only its last (edge-appended) hop. Devices behind one NAT
share a binding; a phone on mobile data binds itself automatically as soon
as its browser shows your key.

## Bridge env knobs

| var | default | meaning |
|---|---|---|
| `PORT` | `8081` (or `argv[1]`) | listen port |
| `LOG_LEVEL` | `INFO` | logging verbosity |
| `UPSTREAM_URL` / `HEMMINGWAY_URL` | built-in | override upstream bases |
| `PROXY_USER_AGENT` | `janitorai-bridge/2.5` | UA sent upstream (tagged, not spoofed) |
| `SESSION_FILE` | `~/.config/zen-proxy/session` | persisted session id |
| `IMAGE_INTAKE` | `1` | master switch |
| `IMAGE_INTAKE_MAX_IMAGES` | `4` | max images per request |
| `IMAGE_INTAKE_LAST_N` | `0` | attach only in the last N messages (0 = all) |
| `IMAGE_INTAKE_MAX_DIM` | `1568` | downscale longest side, 0 = off |
| `IMAGE_INTAKE_MAX_BYTES` | `12 MiB` | per-image byte cap (also the upload ceiling) |
| `IMAGE_INTAKE_MAX_PIXELS` | `50000000` | Pillow decompression-bomb guard |
| `IMAGE_INTAKE_HOSTS` | *(all public)* | optional host allowlist for intake |
| `IMAGE_INTAKE_CACHE` | `128` | LRU entries of fetched images |
| `IMAGE_PUBLIC_HOST` | *(unset)* | public base used in upload URLs; set it, or links come back relative |
| `IMAGE_UPLOAD_TOKEN` | auto-generated | upload auth; `off` disables (not recommended) |
| `IMAGE_DIR` | `~/Documents/zen-images` | image storage |
| `ZEN_CONFIG_DIR` | `~/.config/zen-proxy` | token + owner-key pin (kept out of `IMAGE_DIR` on purpose) |
| `USERSCRIPT_FILE` | `./janitorai-image-intake.user.js` | file served at `/intake.user.js` |
| `OWNER_KEY_SHA256` | *(unset)* | comma-separated owner key hashes; skips trust-on-first-use |

Restart: `./start-zen-proxy.sh` (kills the old instance first). Logs go to
`$ZEN_DIR/zen-proxy.log` and `~/.local/state`-style rotator path
`zen-bridge.log`: look for `[imghost]` / `[imgintake]` lines.

## Userscript

- Buttons top-right in chats: 📎 attach · 🔗 paste image link · 🖼 settings.
- **Drag & drop** (desktop): drop image files anywhere in a chat, or drag an
  image *link* in from another tab; both paste the markdown at your caret.
- **Editing a message?** Attach/paste/drop inserts at the caret **inside that
  message editor** instead of the main input.
- **Inline rendering**: every image link in a message becomes its image, in
  place; clicking opens an in-page lightbox. Dead links stay as plain links,
  and a rotated/removed image never leaves a ghost element behind.
- Chats only: the renderer refuses to run on profiles, discovery, or the chat
  list.
- **Forward mode**: the opt-in "Route other APIs through my bridge" toggle
  (see above) keeps image intake working when JanitorAI points at an API that
  is not your tunnel.
- Settings live under **Advanced** (manual base URL, write-only token field,
  connection test, verbose log). Mobile-safe: 44px targets, safe-area insets,
  bottom sheet.

## Security notes

This bridge holds your provider key and stores images, so it is deliberately
paranoid (see [`AUDIT-REPORT.md`](AUDIT-REPORT.md) for the full audit and the
fixes that followed it):

- `/img/<name>` serves **only** files the bridge itself created (strict name
  pattern), so `/img/.upload-token` (a real past exposure) is impossible now.
- Secrets live in `ZEN_CONFIG_DIR`, never in the publicly served image dir.
- `/img/*` sends **no CORS headers**: a random website cannot read the token
  endpoint out of your browser.
- Binding requires the owner's key (TOFU-pinned), so having *any* valid
  JanitorAI key is not enough to collect your upload token.
- Intake fetches are SSRF-gated: loopback, link-local (cloud metadata), and
  private ranges are refused, redirects are re-validated hop by hop, and the
  optional `IMAGE_INTAKE_HOSTS` allowlist matches bare hostnames.
- The userscript's bridge-learning events are **nonce-signed**, so a page
  script cannot repoint uploads (and your token) at a host it controls;
  localStorage learning is anchored to config-looking keys and https only.
- Preview fetches use `anonymous: true` (no cookies), enforce an `image/*`
  content type, a 20 MB cap, and never touch private-network hosts.
- The settings fields are invisible to password managers (no `type=password`,
  no credential-looking form), so Firefox/Chrome never offer to save your
  bridge URL and token as a janitorai.com login.
- Auto-learn only trusts nonce-signed events, tunnel/LAN hostnames, and a
  passing `/healthz` probe; a learned URL is pinned so nothing can repoint it.
- `@connect *` is required because the bridge host is user-specific; the only
  privileged calls are the health/token/upload calls to the bridge you learned.

## Caveats

- Vision happens **at the model**: the routed model must accept OpenAI
  `image_url` parts.
- Images ride in every request for as long as they stay in history, so tune
  `IMAGE_INTAKE_MAX_IMAGES` / `IMAGE_INTAKE_LAST_N` to control tokens.
- The chat renderer is cosmetic: if JanitorAI changes its DOM, previews may
  stop, though attachments (the bridge) are unaffected.

## Tests

```bash
python3 test_imgintake.py        # intake transform: dedupe, cap-4, localization
python3 test_security.py         # one check per audit finding (C1…M4)
python3 test_bootstrap_unit.py   # token gate: bind, forgery, TTL, owner key
npm i && npm run test:render     # userscript renderer/drag-drop (jsdom)

# live checks (need a running bridge; BASE/ZEN_DIR override the defaults)
./test_imghost.sh   ./test_json_upload.sh   ./test_live_intake.sh   ./test_bootstrap.sh
```

All Python suites are hermetic (they use temp dirs, not your real config) and
the live shell tests take `BASE=`/`ZEN_DIR=` overrides, so nothing is hardcoded
to a specific machine.

## License

MIT, see [LICENSE](LICENSE).
