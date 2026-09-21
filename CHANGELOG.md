# Changelog

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
