# Changelog

## Bridge 2.7 and userscript 1.9.2
- **Open by default (the silly images model)**: no token needed to upload.
  Abuse control is a store quota instead: `IMAGE_DIR_MAX_BYTES` (2 GiB by
  default) with oldest-first eviction on every upload. Setting
  `IMAGE_UPLOAD_TOKEN=on` (or a fixed value) opts into the full token,
  binding and owner-key system for the security conscious.
- **Forward mode needs no key ceremony**: the relay uses whatever API key
  JanitorAI was configured with; the target only has to be an https origin
  speaking an OpenAI compatible API. No `OWNER_KEY_SHA256` step needed for
  third-party APIs such as Xiaomi.
- The ⚙ sheet now reports "open store (no token needed)" instead of
  promising a token that will never come.

## Bridge 2.6
- **Multiple owner keys**: `OWNER_KEY_SHA256` now accepts a comma separated
  list and is unioned with the trust-on-first-use pin instead of replacing
  it. Needed for forward mode, where JanitorAI's one key slot holds the
  target API's key (Xiaomi etc.): add its hash once and forwarding, binding
  and auto-bind all work with it.
- **Honest forward failures**: a forward request that is refused (key not in
  the owner set, non-https target, private host) now returns 403 with the
  exact reason and the fix, instead of silently falling through to the
  default upstream where it surfaced as a misleading 401.

## Bridge 2.5 and userscript 1.9.1
- **Bind without spending a message**: `/img/token` accepts the pinned owner
  key in `Authorization` and binds the caller immediately, forwarding nothing
  upstream (no chat, no model call, no tokens). The userscript observes the
  key from the page's own API traffic and probes it whenever the upload token
  is missing, so rebinding after a rotation, a new device or TTL expiry is
  automatic. Trust on first use stays exclusive to successful chats, so an
  unknown key cannot bootstrap through this door.

## Bridge 2.4 and userscript 1.9.0
- **Forward mode** (opt in): point JanitorAI at any API you like and tick
  "Route other APIs through my bridge"; calls are rerouted through your
  bridge and relayed to the origin you configured, with image intake intact.
  Owner key only, https only, public hosts only.
- **Learn trust**: auto-learn now requires a tunnel/LAN hostname, a signed
  event, and a passing `/healthz` proof that the host really is the bridge.
  A learned URL is pinned (clear it in Advanced to re-learn); on boot, a base
  that answers like something else is forgotten automatically.
- **Password manager fix**: the settings fields no longer look like a login
  form, so browsers stop offering to save your bridge URL and token as
  janitorai.com credentials (which then autofilled on the home screen).
  Delete any previously saved janitorai.com entry in about:logins to finish
  cleaning up.

## Bridge 2.3 — 2026-09-21
Security release (see AUDIT-REPORT.md):
- **Fixed a real exposure:** `/img/.upload-token` was publicly readable; the
  image store now serves only files it created (strict name pattern) and
  secrets moved to `~/.config/zen-proxy/`.
- `/img/*` no longer sends CORS headers (no drive-by token reads).
- Device binding now requires the owner's key (pinned on first success;
  `OWNER_KEY_SHA256` for explicit setups).
- Intake fetches are SSRF-gated (no loopback/link-local/private targets,
  per-hop redirect validation, bare-hostname allowlist).
- IPv4-mapped/IPv6 correctness in the private-IP check; XFF trusted only from
  the tunnel peer; constant-time token compare; body-size and pixel guards;
  upload errors no longer leak exception text; bounds on the failure cache.
- Failed fetches no longer strip their link from the message text.

## Userscript 1.8.0 — 2026-09-21
- Bridge-learning events are nonce-signed (a page script can no longer
  repoint uploads at an attacker host).
- Upload token is no longer written into the DOM; the settings field is
  write-only.
- Preview fetches: `anonymous: true`, `image/*` content-type check, 20 MB cap,
  private-host block, and the privileged GM path only for the bridge/ngrok
  hosts.
- localStorage learning anchored to config-looking keys + https only; a
  changed bridge URL now raises a toast.
- Automatic recovery when the bridge token rotates (clear → re-bootstrap →
  retry once).

## Userscript 1.7.1 / 1.7.0
- Drag & drop (files or an image link) with a drop overlay; non-image drops
  left to the page.
- Attach/paste/drop inserts at the caret when a message editor is open.
- Renderer is chat-only; per-link figures with self-healing against React
  re-renders; dead links stay as links.
