// ==UserScript==
// @name         JanitorAI: Real Image Intake (bridge companion)
// @namespace    https://github.com/ARTHURMURILO/janitorai-go-and-image-intake-fixer
// @version      1.9.2
// @description  One-click image attach that uploads to YOUR server's image store (zen-bridge /img/upload), renders external image links in chat, and pairs with the bridge's real image intake. No JanitorAI Media Library needed.
// @author       Arthur + Pi
// @license      MIT
// @homepageURL  https://github.com/ARTHURMURILO/janitorai-go-and-image-intake-fixer
// @supportURL   https://github.com/ARTHURMURILO/janitorai-go-and-image-intake-fixer/issues
// @updateURL    https://raw.githubusercontent.com/ARTHURMURILO/janitorai-go-and-image-intake-fixer/main/janitorai-image-intake.user.js
// @downloadURL  https://raw.githubusercontent.com/ARTHURMURILO/janitorai-go-and-image-intake-fixer/main/janitorai-image-intake.user.js
// @match        https://janitorai.com/*
// @match        https://*.janitorai.com/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_xmlhttpRequest
// @grant        GM_info
// @connect      *   (deliberate: the bridge host is user-specific — ngrok,
//                   cloudflared, a personal domain. Every remote call this
//                   script makes is token-free except uploads, which go only
//                   to the bridge host it learned by nonce-signed evidence.)
// @run-at       document-idle
// @noframes
// ==/UserScript==

/*
 * HOW IT WORKS (pair with zen-cors-proxy.py >= 2.2):
 *   - The bridge converts `![alt](url)` and bare image URLs in outgoing
 *     messages into real base64 image parts for the model. That's the feature.
 *   - This script adds:
 *       1. A button cluster at the top-right of the chat header:
 *            📎 attach  — uploads the picked file to YOUR image store
 *                         (bridge /img/upload) and inserts `![name](url)`
 *            🔗 paste   — wraps a copied image URL into markdown
 *            🖼 gear    — settings (bridge URL, upload token, toggles)
 *       2. Renders external image links in chat (JanitorAI only renders its
 *          own ella CDN; everything else would be a plain link).
 *
 *   Setup (once): open 🖼 → paste the bridge base URL (the same ngrok URL
 *   JanitorAI uses, e.g. https://xxx.ngrok-free.dev) and the upload token
 *   (server: ~/Documents/zen-images/.upload-token). Hit "Test".
 */

(function () {
  'use strict';

  if (!/(^|\.)janitorai\.com$/.test(location.hostname)) return;

  const TAG = '[ji-img]';
  const CSS_PREFIX = 'ji-';

  // ------------------------------------------------------------------
  // State (persisted via GM storage)
  // ------------------------------------------------------------------
  const DEFAULTS = {
    debugLog: false,
    bridgeBase: '',         // learned from JanitorAI's own chat requests
    bridgeManual: false,    // true when the user typed a base by hand
    bridgePinned: false,    // a verified bridge: auto-learning stops for good
    forwardMode: false,     // opt-in: reroute other APIs through the bridge
    storeOpen: false,       // bridge told us: store needs no token at all
    uploadToken: '',        // auto-fetched from the bridge (owner-bound)
  };
  let settings = Object.assign({}, DEFAULTS);
  try {
    const saved = JSON.parse(GM_getValue('settings', '{}'));
    settings = Object.assign(settings, saved || {});
  } catch (e) { /* fresh install */ }
  const saveSettings = () => GM_setValue('settings', JSON.stringify(settings));
  const dlog = (...a) => { if (settings.debugLog) console.log(TAG, ...a); };

  // ------------------------------------------------------------------
  // 1) External image renderer
  // ------------------------------------------------------------------
  const MESSAGE_SEL = '[data-testid="virtuoso-item-list"] > div[data-index]';
  // Tolerant: missing closing paren / stray quotes still match.
  const IMG_MD_RE = /!\[([^\]]*)\]\(\s*(https?:\/\/[^\s)\]<>"',;]+)/g;
  const IMG_BARE_RE = /(?:^|[\s>])(https?:\/\/[^\s)\]<>"']+\.(?:png|jpe?g|webp|gif)(?:\?[^\s)\]<>"']*)?)/gi;

  const IMG_EXT_RE = /\.(?:png|jpe?g|webp|gif)(?:\?|$)/i;
  function isImgUrl(u) {
    // Path-with-query (…/img.png?v=2) or plain ending; must be absolute http(s).
    return IMG_EXT_RE.test(u.split('?')[0] + '?') || IMG_EXT_RE.test(u);
  }

  function extractFromText(text) {
    const urls = [];
    let m;
    IMG_MD_RE.lastIndex = 0;
    while ((m = IMG_MD_RE.exec(text))) urls.push({ url: m[2], alt: m[1] || 'image' });
    IMG_BARE_RE.lastIndex = 0;
    while ((m = IMG_BARE_RE.exec(text))) {
      const u = m[1];
      if (!urls.some((x) => x.url === u)) urls.push({ url: u, alt: 'image' });
    }
    return urls;
  }

  function alreadyDisplayed(wrapper, url) {
    // JanitorAI renders some (ella) images itself — skip those URLs.
    for (const img of wrapper.querySelectorAll('img[src]')) {
      if (img.src === url) return true;
    }
    return false;
  }

  // Self-hosted (and hotlink-protected) images can't be loaded by <img> tags
  // directly: ngrok's free tier serves its browser-warning page to plain img
  // requests, and some hosts block hotlinking. So previews are fetched via
  // GM_xmlhttpRequest (privileged, skip-header included) and shown from blob
  // URLs — pixels guaranteed regardless of tunnel/host quirks.
  const blobUrlCache = new Map(); // url -> objectURL

  function isPrivateHost(hostname) {
    const h = (hostname || '').toLowerCase();
    if (h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]') return true;
    if (/^(10|127)\./.test(h)) return true;
    if (/^192\.168\./.test(h)) return true;
    if (/^172\.(1[6-9]|2\d|3[01])\./.test(h)) return true;
    if (/^169\.254\./.test(h)) return true;
    if (/\.local$/.test(h)) return true;
    return false;
  }

  function isBridgeHost(url) {
    try {
      const u = new URL(url);
      const base = settings.bridgeBase ? new URL(settings.bridgeBase) : null;
      if (base && u.host === base.host) return true;
      return /(^|\.)ngrok(-free)?\.(dev|app)$/.test(u.hostname);
    } catch (e) { return false; }
  }

  function loadPreviewViaGM(imgEl, url) {
    // Preview of a chat-supplied URL. Hardening (US-2): never let a message
    // turn the extension into a LAN prober, never send cookies, cap the size,
    // and only take the privileged GM path for the bridge/ngrok hosts that
    // actually need the skip-warning header — everything else loads as a
    // plain <img>, exactly like any other image on the page.
    let host = '';
    try { host = new URL(url).hostname; } catch (e) { host = ''; }
    if (isPrivateHost(host) && !isBridgeHost(url)) {
      dlog('preview blocked (private host):', host);
      imgEl.dispatchEvent(new Event('error'));
      return;
    }
    if (!isBridgeHost(url)) {
      imgEl.src = url;  // ordinary image load: no GM privilege involved
      return;
    }
    if (typeof GM_xmlhttpRequest !== 'function') {
      imgEl.src = url; // plain fallback
      return;
    }
    const fail = () => imgEl.dispatchEvent(new Event('error'));
    const cached = blobUrlCache.get(url);
    if (cached) { imgEl.src = cached; return; }
    GM_xmlhttpRequest({
      method: 'GET',
      url,
      headers: { 'ngrok-skip-browser-warning': '1' },
      responseType: 'arraybuffer',
      timeout: 30000,
      anonymous: true,          // never attach the user's cookies to a fetch
      onload: (r) => {
        try {
          if (r.status !== 200 || !r.response) { fail(); return; }
          const ct = ((/content-type:\s*([^\r\n;]+)/i.exec(r.responseHeaders || '') || [])[1] || '').trim().toLowerCase();
          const declared = /content-length:\s*(\d+)/i.exec(r.responseHeaders || '');
          if (declared && Number(declared[1]) > 20 * 1024 * 1024) { fail(); return; }
          if (!ct.startsWith('image/')) { fail(); return; }   // HTML/JSON is not an image
          const buf = r.response;
          if (buf.byteLength > 20 * 1024 * 1024) { fail(); return; }
          const objUrl = URL.createObjectURL(new Blob([buf], { type: ct }));
          blobUrlCache.set(url, objUrl);
          while (blobUrlCache.size > 64) {
            const oldest = blobUrlCache.keys().next().value;
            URL.revokeObjectURL(blobUrlCache.get(oldest));
            blobUrlCache.delete(oldest);
          }
          imgEl.src = objUrl;
        } catch (e) { dlog('blob load failed', e); fail(); }
      },
      onerror: () => { imgEl.src = url; }, // last-ditch direct load
      ontimeout: fail,
    });
  }

  // Per-page memory of URLs whose preview fetch failed (deleted file, 404,
  // dead host). Stops rescans from re-fetching dead URLs forever.
  const failedUrls = new Set();

  // Wire a preview figure's <img> and start the fetch. Call ONLY after the
  // figure is in the document — a detached `loading="lazy"` <img> never
  // fetches (that exact deadlock killed v1.5.1's deferred renderer).
  // `replaced` = the original message link this figure stands in for:
  // hidden on success, restored on failure.
  function startPreviewLoad(pa, url, replaced) {
    const img = pa && pa.querySelector('img');
    if (!img) return;
    pa.classList.add(CSS_PREFIX + 'pending');
    const ok = () => {
      pa.classList.remove(CSS_PREFIX + 'pending');
      if (replaced) {
        replaced.style.display = 'none';
        replaced.dataset.jiPreviewed = '1';
      }
    };
    const bad = () => {
      pa.classList.remove(CSS_PREFIX + 'pending');
      failedUrls.add(url);
      const row = pa.parentElement;
      pa.remove();
      if (row && row.classList.contains(CSS_PREFIX + 'img-row') && !row.children.length) row.remove();
      if (replaced) {
        replaced.style.removeProperty('display');
        replaced.dataset.jiPreviewed = '';
      }
      dlog('preview failed — original link kept visible:', url);
    };
    img.addEventListener('load', () => { if (img.naturalWidth) ok(); else bad(); }, { once: true });
    img.addEventListener('error', bad, { once: true });
    loadPreviewViaGM(img, url);
  }

  // Returns the <a class="ji-preview"> itself — callers wrap it in a
  // .ji-img-row: one row per message link (inline), or one shared row for
  // bare URLs at the end of a message.
  function buildPreviewAnchor(x) {
    const a = document.createElement('a');
    a.className = CSS_PREFIX + 'preview';
    a.href = x.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.dataset.jiUrl = x.url;
    const img = document.createElement('img');
    img.className = CSS_PREFIX + 'preview-img';
    img.alt = x.alt;
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    // Loading starts in startPreviewLoad AFTER the row is in the document.
    a.addEventListener('click', (ev) => {
      // In-page floating viewer (like JanitorAI's profile popup) instead of
      // navigating away — but only when pixels are real; a dead image falls
      // back to a normal link navigation.
      if (!img.naturalWidth) return;
      ev.preventDefault();
      openLightbox(img.currentSrc || img.src || x.url);
    });
    a.appendChild(img);
    return a;
  }

  function isOurRow(el) {
    return !!el && el.classList && el.classList.contains(CSS_PREFIX + 'img-row');
  }

  // ------------------------------------------------------------------
  // In-page floating image viewer (mirrors JanitorAI's profile popup):
  // dark overlay, centered image, ✕ / ESC / click-to-close.
  // ------------------------------------------------------------------
  let lightboxEl = null;
  function openLightbox(src) {
    if (!lightboxEl) {
      lightboxEl = document.createElement('div');
      lightboxEl.className = CSS_PREFIX + 'lightbox';
      const img = document.createElement('img');
      img.alt = 'image preview';
      const x = document.createElement('button');
      x.type = 'button';
      x.className = CSS_PREFIX + 'lightbox-x';
      x.textContent = '✕';
      x.setAttribute('aria-label', 'Close preview');
      lightboxEl.appendChild(img);
      lightboxEl.appendChild(x);
      lightboxEl.addEventListener('click', () => lightboxEl.classList.remove('open'));
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') lightboxEl.classList.remove('open');
      });
      document.body.appendChild(lightboxEl);
    }
    lightboxEl.querySelector('img').src = src;
    lightboxEl.classList.add('open');
  }

  // ChatGPT-style rendering: each markdown link IN the message becomes its
  // image (the link itself is hidden), so multiple images per message all
  // show up inline, right where they were referenced. Bare URLs (no anchor)
  // fall back to a preview row at the end of the message.
  function injectPreviews(wrapper, forced) {
    if (!wrapper) return;
    // Never touch a message while it is being edited: the editor holds the
    // raw markdown, whose bare URLs would be mistaken for message content.
    if (wrapper.querySelector('textarea, .ql-editor, [contenteditable="true"]')) return;

    // Circuit breaker: if a previous runaway ever stacked figures here, stop
    // instead of piling on. Healthy messages have a handful at most.
    if (wrapper.querySelectorAll('.' + CSS_PREFIX + 'img-row').length > 12) return;

    // 1) Anchor-driven: every image link gets its own figure directly after
    //    it. Identity is the anchor NODE, not the URL — the same image may
    //    legitimately appear twice in one message and must render twice.
    for (const anchor of [...wrapper.querySelectorAll('a[href]')]) {
      const h = anchor.getAttribute('href') || '';
      if (!/^https?:\/\//i.test(h) || !isImgUrl(h)) continue;
      if (failedUrls.has(h)) continue; // dead URL — keep the link as-is
      if (anchor.closest('.' + CSS_PREFIX + 'img-row')) continue; // our own nodes
      if (alreadyDisplayed(wrapper, h) && anchor.dataset.jiPreviewed !== '1') continue;

      // Row already attached to THIS anchor (maybe still loading)?
      const next = anchor.nextElementSibling;
      if (isOurRow(next) && next.previousElementSibling === anchor &&
          next.dataset.jiUrl === h) {
        const nimg = next.querySelector('img');
        // Deferred reveal: hide the link only once pixels are real.
        if (nimg && nimg.naturalWidth) {
          anchor.style.display = 'none';
          anchor.dataset.jiPreviewed = '1';
        }
        continue; // already present — never duplicate
      }
      const row = document.createElement('div');
      row.className = CSS_PREFIX + 'img-row';
      row.dataset.jiUrl = h;
      row.appendChild(buildPreviewAnchor({ url: h, alt: 'image' }));
      anchor.insertAdjacentElement('afterend', row);
      startPreviewLoad(row.querySelector('a'), h, anchor);
    }

    // 2) Self-heal: a row whose owning anchor no longer precedes it was left
    //    behind by a React re-render — drop it (step 1 re-attaches a fresh
    //    one if the link came back). A row whose image definitively failed
    //    also goes, restoring its link.
    for (const row of [...wrapper.querySelectorAll('.' + CSS_PREFIX + 'img-row')]) {
      if (row.dataset.jiKeep === '1') continue; // bare-URL rows stand alone
      if (row.parentElement && row.parentElement.closest('.' + CSS_PREFIX + 'img-row')) continue;
      const prev = row.previousElementSibling;
      const url = row.dataset.jiUrl || '';
      const owned = prev && prev.tagName === 'A' && (prev.getAttribute('href') || '') === url;
      const img = row.querySelector('img');
      const dead = img && img.complete && img.naturalWidth === 0 && img.getAttribute('src');
      if (owned && !dead) continue;
      row.remove();
      if (owned && dead) {
        failedUrls.add(url);
        if (prev.dataset.jiPreviewed === '1') {
          prev.style.removeProperty('display');
          prev.dataset.jiPreviewed = '';
        }
      }
    }

    // 3) Bare URLs sitting in the text (no anchor) -> one row at the end.
    const done = new Set(doneUrls(wrapper));
    const found = extractFromText(wrapper.textContent || '');
    for (const x of (forced || [])) {
      if (!found.some((y) => y.url === x.url)) found.push(x);
    }
    const covered = new Set([...wrapper.querySelectorAll('a[href]')].map((a) => a.getAttribute('href') || ''));
    const bare = found.filter((x) => /^https?:\/\//i.test(x.url))
                      .filter((x) => !covered.has(x.url))
                      .filter((x) => !done.has(x.url) && !failedUrls.has(x.url) &&
                                     !alreadyDisplayed(wrapper, x.url));
    if (bare.length) {
      const row = document.createElement('div');
      row.className = CSS_PREFIX + 'img-row';
      row.dataset.jiKeep = '1';
      for (const x of bare) row.appendChild(buildPreviewAnchor(x));
      const rail = wrapper.querySelector('[class^="_botChoicesContainer_"]');
      if (rail && rail.parentElement) rail.parentElement.insertBefore(row, rail);
      else wrapper.appendChild(row);
      // The row is connected now — start the fetches (a detached lazy <img>
      // never loads).
      for (const pa of row.querySelectorAll('a.' + CSS_PREFIX + 'preview[data-ji-url]')) {
        startPreviewLoad(pa, pa.dataset.jiUrl, null);
      }
      wrapper.setAttribute('data-ji-urls', [...done, ...bare.map((x) => x.url)].join('|'));
    }
  }

  function doneUrls(wrapper) {
    const v = wrapper.getAttribute('data-ji-urls');
    return v ? v.split('|') : [];
  }

  // The renderer is chat-only. JanitorAI's other surfaces (character
  // discovery grids, profiles, the chat list) are full of image-like anchors
  // and virtualized lists — without this gate previews get painted onto
  // pages that have nothing to do with chats. Requires BOTH a chat URL and
  // the message editor, so SPA transitions and half-loaded pages never leak.
  function isChatPage() {
    return /^\/chats?\/\d+/i.test(location.pathname) && !!findEditor();
  }

  function scanMessages() {
    // Chats only — never touch the rest of the site.
    if (!isChatPage()) return;
    // Full rescan each cycle: injectPreviews is idempotent and self-healing
    // (it re-attaches figures React wiped and drops orphaned ones), so a
    // change-signature shortcut is unnecessary — and worse, signatures go
    // stale exactly when React re-renders a message with the same text and
    // links (which is what the edit-open/close cycle does).
    let nodes = [...document.querySelectorAll(MESSAGE_SEL)];
    if (!nodes.length) {
      // Fallback: any virtuoso row carrying a data-index.
      nodes = [...document.querySelectorAll('[data-index]')];
    }
    for (const n of nodes) {
      try { injectPreviews(n); } catch (e) { dlog('preview failed', e); }
    }
    // Last-resort sweep: any image-URL anchor anywhere in the chat that never
    // got a wrapper match (e.g. selector drift) still gets a preview injected
    // into its closest message container.
    if (!nodes.length) {
      for (const a of document.querySelectorAll('a[href]')) {
        const h = a.getAttribute('href') || '';
        if (!/^https?:\/\//i.test(h) || !isImgUrl(h)) continue;
        const wrapper = a.closest('[data-index]') || a.parentElement;
        if (!wrapper) continue;
        try { injectPreviews(wrapper, [{ url: h, alt: 'image' }]); } catch (e) {}
      }
    }
  }

  let scanQueued = 0;
  function queueScan() {
    if (scanQueued) return;
    scanQueued = 1;
    setTimeout(() => { scanQueued = 0; scanMessages(); }, 250);
  }

  // ------------------------------------------------------------------
  // 2) Editor helpers + bridge upload
  // ------------------------------------------------------------------
  const EDITOR_SEL = ['.ql-editor', 'textarea[placeholder]', '[contenteditable="true"]'];
  const SEND_SEL = 'button[aria-label*="Send" i]';
  // Set when an upload fails for binding reasons; the send guard then offers
  // a confirm before a text-only message goes out (no wasted tokens, no
  // confused bot reply — the bridge can't know the user meant to attach).
  let lastBindFailAt = 0;

  function findEditor() {
    for (const sel of EDITOR_SEL) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  // A message editor currently open (editing an old message): a textarea or
  // contenteditable living inside the message list. Uploads/links go there at
  // the cursor instead of into the main chat input.
  function findMsgEditEl() {
    const sel = '[data-testid="virtuoso-item-list"] textarea, [data-index] textarea, ' +
                '[data-testid="virtuoso-item-list"] [contenteditable="true"], ' +
                '[data-index] [contenteditable="true"]';
    for (const el of document.querySelectorAll(sel)) {
      if (el === findEditor()) continue; // the main input, not a message editor
      if (el.disabled || el.readOnly) continue;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      return el;
    }
    return null;
  }

  function insertAtCursor(el, text) {
    const v = el.value || '';
    const start = typeof el.selectionStart === 'number' ? el.selectionStart : v.length;
    const end = typeof el.selectionEnd === 'number' ? el.selectionEnd : start;
    const next = v.slice(0, start) + text + v.slice(end);
    // Native setter + input event: keeps React's controlled textarea in sync.
    const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
    if (desc && desc.set) desc.set.call(el, next); else el.value = next;
    try { el.selectionStart = el.selectionEnd = start + text.length; } catch (e) { /* no selection API */ }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    try { el.focus(); } catch (e) { /* detached */ }
  }

  function insertIntoEditor(text) {
    const msgEdit = findMsgEditEl();
    if (msgEdit && 'value' in msgEdit) {
      insertAtCursor(msgEdit, text); // pasted exactly where the caret is
      return true;
    }
    const ed = findEditor();
    if (!ed) { toast('No chat input found'); return false; }
    if (ed.classList && ed.classList.contains('ql-editor')) {
      const p = document.createElement('p');
      p.textContent = text;
      ed.appendChild(p);
    } else if ('value' in ed) {
      ed.value = (ed.value ? ed.value + '\n' : '') + text;
    } else {
      ed.appendChild(document.createTextNode(text));
    }
    ed.dispatchEvent(new Event('input', { bubbles: true }));
    try { ed.focus(); } catch (e) { /* detached */ }
    return true;
  }

  function safeName(name) {
    const base = (name || 'image').replace(/\.[^.]+$/, '').replace(/[\[\](){}<>]/g, '').trim();
    return (base.slice(0, 40) || 'image');
  }

  function bridgeBase() {
    return (settings.bridgeBase || '').trim().replace(/\/+$/, '');
  }

  // All bridge traffic goes through GM_xmlhttpRequest when available: it is
  // extension-privileged (no CORS, no mixed-content surprises) and we send
  // ngrok's skip-warning header so the free-tier interstitial can never
  // masquerade as a response. Page fetch is only a fallback.
  function gmFetch(method, url, opts) {
    opts = opts || {};
    const headers = Object.assign({ 'ngrok-skip-browser-warning': '1' }, opts.headers || {});
    if (typeof GM_xmlhttpRequest === 'function') {
      return new Promise((resolve, reject) => {
        GM_xmlhttpRequest({
          method,
          url,
          headers,
          data: opts.data,
          timeout: 45000,
          onload: (r) => resolve({ status: r.status, text: r.responseText || '',
                                  ok: r.status >= 200 && r.status < 400 }),
          onerror: () => reject(new Error('NetworkError')),
          ontimeout: () => reject(new Error('Timeout — bridge did not answer in 45s')),
        });
      });
    }
    return fetch(url, { method, headers, body: opts.data, mode: 'cors' })
      .then(async (r) => ({ status: r.status, text: await r.text(),
                            ok: r.status >= 200 && r.status < 400 }));
  }

  function looksLikeNgrokInterstitial(text) {
    return /you are about to visit|ngrok-skip-browser-warning|<\!doctype html/i.test(text || '');
  }

  function parseJsonSafe(text) {
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  // Zero-setup bootstrap: when on the owner's home network, the bridge hands
  // out the upload token to us automatically (GET /img/token is private-IP
  // gated server-side). Falls back to manual paste when remote.
  // Bind without spending a message: show the owner key (observed from the
  // page's own API traffic) to /img/token. The bridge verifies the pinned
  // hash, binds this IP and returns the upload token without forwarding
  // anything upstream, so no chat is sent and no model call is made.
  // The key itself is kept in memory only: never stored, never logged.
  let lastKey = '';
  let lastKeyProbeAt = 0;
  async function tryAutoBind(bearer) {
    if (settings.uploadToken || !bearer) return false;
    const base = bridgeBase();
    if (!base) return false;
    lastKeyProbeAt = Date.now();
    try {
      const r = await gmFetch('GET', base + '/img/token', {
        headers: { Authorization: /^bearer\s/i.test(bearer) ? bearer : 'Bearer ' + bearer },
      });
      if (!r.ok) return false;
      const d = parseJsonSafe(r.text);
      if (d && d.token) {
        settings.uploadToken = d.token;
        settings.storeOpen = false;
        saveSettings();
        lastBindFailAt = 0;
        refreshSheetStatus();
        toast('Device bound automatically ✓ no message needed');
        return true;
      }
      if (d && d.auth === 'disabled') {
        settings.uploadToken = '';
        settings.storeOpen = true;
        saveSettings();
        return true;
      }
    } catch (e) { /* bridge offline */ }
    return false;
  }

  async function ensureUploadToken(retry = true) {
    if (settings.uploadToken) return true;
    const base = bridgeBase();
    if (!base) return false;
    if (lastKey && await tryAutoBind(lastKey)) return true;
    try {
      const r = await gmFetch('GET', base + '/img/token');
      if (!r.ok) {
        // The chat that taught us the URL may still be in flight — its
        // success is what binds this device. Give it a beat, retry once.
        if (retry && r.status === 403) {
          await new Promise((res) => setTimeout(res, 2500));
          return ensureUploadToken(false);
        }
        return false;
      }
      const d = parseJsonSafe(r.text);
      if (d && d.auth === 'disabled') {
        settings.uploadToken = '';
        settings.storeOpen = true;   // open store: no token needed, ever
        saveSettings();
        return true;
      }
      if (d && d.token) {
        settings.uploadToken = d.token;
        settings.storeOpen = false;
        saveSettings();
        dlog('upload token auto-provisioned from bridge bootstrap');
        return true;
      }
    } catch (e) { /* offline / remote */ }
    return false;
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result).split(',')[1] || '');
      fr.onerror = () => reject(new Error('File read failed'));
      fr.readAsDataURL(file);
    });
  }

  async function uploadToBridge(file) {
    const base = bridgeBase();
    if (!base) throw new Error('NO_BASE');
    // JSON/base64 body: immune to multipart quirks in userscript sandboxes.
    const b64 = await fileToBase64(file);
    const r = await gmFetch('POST', base + '/img/upload', {
      data: JSON.stringify({ name: file.name || 'image.png', data: b64 }),
      headers: Object.assign(
        { 'Content-Type': 'application/json' },
        settings.uploadToken ? { 'X-Upload-Token': settings.uploadToken } : {}),
    });
    if (r.status === 401) throw new Error('BAD_TOKEN');
    if (!r.ok) {
      if (looksLikeNgrokInterstitial(r.text)) throw new Error('NGROK_WARNING');
      throw new Error('HTTP ' + r.status);
    }
    const data = parseJsonSafe(r.text);
    let url = (data && (data.url || data.path)) || '';
    if (!url) throw new Error('NO_URL');
    if (url.startsWith('/')) url = base + url; // relative -> absolute
    return url;
  }

  function toast(msg, kind) {
    const t = document.createElement('div');
    t.className = CSS_PREFIX + 'toast' + (kind ? ' ' + CSS_PREFIX + kind : '');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => { t.classList.add(CSS_PREFIX + 'out'); }, 2600);
    setTimeout(() => t.remove(), 3100);
  }

  // ------------------------------------------------------------------
  // 2b) Bridge auto-learn — zero configuration.
  // JanitorAI's browser POSTs every chat message to the configured proxy
  // URL (your bridge). We watch for those requests, remember the origin,
  // and fetch the upload token. No fields to fill on the happy path.
  // ------------------------------------------------------------------
  function randomNonce() {
    try {
      const a = new Uint8Array(16);
      crypto.getRandomValues(a);
      return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
    } catch (e) {
      return String(Date.now()) + String(Math.random()).slice(2);
    }
  }

  let sniffNonce = '';
  let hookGen = 0;

  // (Re)injects the page hooks. Each generation carries a fresh nonce, the
  // current bridge origin and the forward flag, so stale wrappers see a gen
  // mismatch and pass traffic through untouched. Called at boot and whenever
  // the bridge/forward settings change.
  function reapplyPageHooks() {
    if (!document.documentElement) return;
    hookGen += 1;
    sniffNonce = randomNonce();
    dlog('page hooks gen', hookGen, 'forward=', settings.forwardMode);
    const bridge = bridgeBase();
    const src = document.createElement('script');
    src.textContent = `(${function (NONCE, GEN, BRIDGE, FORWARD) {
      const EVT = 'ji-bridge-url';
      const CHAT_RE = /\/(v1(?:\/|$)|chat\/completions|hemmingway|zen)/;
      function pageish(url) {
        try {
          const u = new URL(url, location.href);
          if (u.origin === location.origin) return true;
          if (/(^|\.)janitorai\.com$/.test(u.hostname)) return true;
          if (/(^|\.)supabase\.co$/.test(u.hostname)) return true;
        } catch (e) { return true; }
        return false;
      }
      function consider(url, method) {
        try {
          const u = new URL(url, location.href);
          if (pageish(u.href)) return;
          if (String(method || '').toUpperCase() === 'GET') return;
          if (!CHAT_RE.test(u.pathname)) return;
          window.dispatchEvent(new CustomEvent(EVT, { detail: { url: u.origin, nonce: NONCE } }));
        } catch (e) { /* not a usable URL */ }
      }
      // Forward mode: swap ONLY the origin of chat-ish calls to the pinned
      // bridge and tag the true target, so the bridge can relay to the API
      // JanitorAI was actually configured with. Path and query stay intact.
      function forwardPlan(url) {
        if (!FORWARD || !BRIDGE) return null;
        try {
          const u = new URL(url, location.href);
          if (u.origin === BRIDGE || pageish(u.href)) return null;
          if (!CHAT_RE.test(u.pathname)) return null;
          return { target: BRIDGE + u.pathname + u.search, origin: u.origin };
        } catch (e) { return null; }
      }
      // Observe the Authorization header the page sends (it holds the owner
      // key) so the sandbox can bind this device without a throwaway chat.
      // The page already knows its own key; nothing new is exposed.
      function emitKey(value) {
        try {
          window.dispatchEvent(new CustomEvent('ji-observed-key',
            { detail: { nonce: NONCE, key: String(value) } }));
        } catch (e) { /* never break the page */ }
      }
      function grabAuth(config) {
        try {
          const h = config && config.headers;
          if (!h) return;
          if (typeof Headers !== 'undefined' && h instanceof Headers) {
            const v = h.get('Authorization') || h.get('authorization');
            if (v) emitKey(v);
          } else if (Array.isArray(h)) {
            for (const p of h) if (p && String(p[0]).toLowerCase() === 'authorization') emitKey(String(p[1]));
          } else if (typeof h === 'object') {
            for (const k of Object.keys(h)) if (k.toLowerCase() === 'authorization') emitKey(h[k]);
          }
        } catch (e) { /* headers not readable */ }
      }
      const origFetch = window.fetch;
      window.fetch = async function (...args) {
        if (window.__ji_gen !== GEN) return origFetch.apply(this, args);
        try {
          const [resource, config] = args;
          const url = typeof resource === 'string' ? resource : (resource && resource.url) || '';
          const method = (config && config.method) || (resource && resource.method) || 'GET';
          grabAuth(config);
          consider(url, method);
          if (typeof resource === 'string') {
            const plan = forwardPlan(resource);
            if (plan) {
              const cfg = Object.assign({}, config || {});
              try {
                const hdrs = new Headers(cfg.headers || undefined);
                if (!hdrs.has('X-Zen-Forward-Origin')) hdrs.set('X-Zen-Forward-Origin', plan.origin);
                cfg.headers = hdrs;
              } catch (e) {
                cfg.headers = Object.assign({}, (config && config.headers) || {},
                  { 'X-Zen-Forward-Origin': plan.origin });
              }
              return origFetch.call(this, plan.target, cfg);
            }
          }
        } catch (e) { /* never break the page's fetch */ }
        return origFetch.apply(this, args);
      };
      const XHR = XMLHttpRequest.prototype;
      const origOpen = XHR.open;
      const origSRH = XHR.setRequestHeader;
      XHR.setRequestHeader = function (name, value) {
        try {
          if (String(name).toLowerCase() === 'authorization') emitKey(value);
        } catch (e) { /* never break the page */ }
        return origSRH.apply(this, arguments);
      };
      XHR.open = function (method, url) {
        if (window.__ji_gen === GEN) {
          try { consider(url, method); } catch (e) { /* same */ }
          try {
            const plan = forwardPlan(String(url));
            if (plan) {
              const rest = Array.prototype.slice.call(arguments, 2);
              const ret = origOpen.call(this, method, plan.target, ...rest);
              try { this.setRequestHeader('X-Zen-Forward-Origin', plan.origin); } catch (e2) {}
              return ret;
            }
          } catch (e) { /* fall through */ }
        }
        return origOpen.apply(this, arguments);
      };
      window.__ji_gen = GEN;
    }.toString()})(${JSON.stringify(sniffNonce)}, ${hookGen}, ${JSON.stringify(bridge)}, ${settings.forwardMode ? 'true' : 'false'});`;
    (document.head || document.documentElement).appendChild(src);
    src.remove();
  }

  // Only hosts that look like YOUR tunnel or LAN are candidates for
  // auto-learn. This is what stops an unrelated ngrok (or a random API)
  // from ever being adopted as the bridge.
  function trustedBridgeHost(hostname) {
    const h = String(hostname || '').toLowerCase();
    if (!h) return false;
    if (h === 'localhost' || h === '127.0.0.1' || h === '[::1]') return true;
    if (/(^|\.)ngrok-free\.dev$/.test(h) || /(^|\.)ngrok-free\.app$/.test(h) ||
        /(^|\.)ngrok\.app$/.test(h) || /(^|\.)ngrok\.io$/.test(h)) return true;
    if (/(^|\.)ts\.net$/.test(h)) return true;  // tailscale MagicDNS
    if (/^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(h)) return true; // tailscale IPs
    if (/^(10\.|192\.168\.)/.test(h)) return true;                      // LAN
    if (/^172\.(1[6-9]|2\d|3[01])\./.test(h)) return true;               // LAN
    return false;
  }

  // 'ok' = answers our /healthz shape, 'foreign' = answers with something
  // else (an unrelated service), 'unreachable' = no answer at all.
  async function verifyBridge(base) {
    try {
      const r = await gmFetch('GET', String(base).replace(/\/+$/, '') + '/healthz');
      if (looksLikeNgrokInterstitial(r.text)) return 'unreachable'; // not proven yet
      if (!r.ok) return 'foreign';  // reachable, but not our bridge
      try {
        const d = JSON.parse(r.text || '');
        if (d && d.ok === true && typeof d.v === 'string') return 'ok';
        return 'foreign';
      } catch (e) { return 'foreign'; }
    } catch (e) { return 'unreachable'; }
  }

  async function learnBridgeFromUrl(origin, source) {
    if (settings.bridgeManual) return false;
    if (settings.bridgePinned && settings.bridgeBase) {
      dlog('bridge pinned, ignoring learn from', source || '?');
      return false;
    }
    if (!origin || origin === settings.bridgeBase) return !!origin;
    let u;
    try { u = new URL(origin); } catch (e) { return false; }
    // Only https, or loopback for LAN installs: a page script (or a stray
    // localStorage value) must not point uploads at plain http:// elsewhere.
    const okScheme = u.protocol === 'https:' ||
      (u.protocol === 'http:' && /^(localhost|127\.0\.0\.1|\[::1\])$/.test(u.hostname));
    if (!okScheme) return false;
    if (/(^|\.)janitorai\.com$/.test(u.hostname)) return false;
    if (!trustedBridgeHost(u.hostname)) {
      dlog('learn rejected (not a trusted bridge host):', u.hostname);
      return false;
    }
    const verdict = await verifyBridge(u.origin);
    if (verdict !== 'ok') {
      dlog('learn rejected (' + verdict + '):', u.origin);
      return false;
    }
    const base = u.origin;
    settings.bridgeBase = base;
    settings.bridgePinned = true;   // good path found: never auto-learn again
    saveSettings();
    dlog('bridge base learned and pinned from ' + (source || '?') + ':', base);
    reapplyPageHooks();
    refreshSheetStatus();
    ensureUploadToken().then(() => {
      refreshSheetStatus();
      toast('Bridge learned and pinned ✓ — images ready');
    });
    return true;
  }

  // One-time self heal: if a previously learned base turns out not to be a
  // bridge (the wrong-learn bug), forget it so learning can start over.
  // Unreachable is left alone: your bridge may just be offline right now.
  function healLearnedBridge() {
    if (!settings.bridgeBase || settings.bridgeManual) return;
    verifyBridge(settings.bridgeBase).then((v) => {
      if (v === 'ok') {
        if (!settings.bridgePinned) { settings.bridgePinned = true; saveSettings(); }
        refreshSheetStatus();
        reapplyPageHooks();
      } else if (v === 'foreign') {
        dlog('forgetting non-bridge base:', settings.bridgeBase);
        settings.bridgeBase = '';
        settings.bridgePinned = false;
        saveSettings();
        refreshSheetStatus();
        toast('Learned URL was not your bridge, forgotten', 'warn');
        scanLocalStorageForBridge();
      }
    });
  }

  function scanLocalStorageForBridge() {
    if (settings.bridgeManual || settings.bridgeBase) return;
    try {
      // Anchored on purpose: scan only plausibly-JanitorAI config keys, so an
      // unrelated site setting that happens to hold a "/v1" URL can never
      // repoint the bridge (and the upload token with it).
      const KEY_RE = /janitorai|jai|proxy|zen|bridge|api|chat/i;
      const URL_RE = /https:\/\/[^\s"'<>]+?\/(?:v1(?:\/|$)|chat\/completions|hemmingway|zen)/i;
      for (let i = 0; i < localStorage.length; i++) {
        const k = String(localStorage.key(i) || '');
        if (!KEY_RE.test(k)) continue;
        const v = String(localStorage.getItem(k) || '');
        const m = v.match(URL_RE);
        if (m) { learnBridgeFromUrl(new URL(m[0]).origin, 'localStorage'); return; }
      }
    } catch (e) { /* no storage access */ }
  }

  window.addEventListener('ji-bridge-url', (e) => {
    const d = e.detail || {};
    // Nonce-signed events only (see reapplyPageHooks).
    if (!sniffNonce || d.nonce !== sniffNonce) {
      dlog('ignored unsigned bridge-url event');
      return;
    }
    if (d.url) learnBridgeFromUrl(d.url, 'sniffer');
  });

  // The page's own API traffic carries the owner key (even the model list
  // request does). Observe it and, while we still lack the upload token, use
  // it to bind this device silently. Throttled, and only runs when there is
  // something to gain.
  window.addEventListener('ji-observed-key', (e) => {
    const d = e.detail || {};
    if (!sniffNonce || d.nonce !== sniffNonce || !d.key) return;
    lastKey = d.key;   // memory only: never persisted or logged
    if (settings.uploadToken) return;
    if (Date.now() - lastKeyProbeAt < 15000) return;
    tryAutoBind(lastKey).catch(() => {});
  });

  // ------------------------------------------------------------------
  // 2c) Attach flow (classic): upload -> paste the markdown link straight
  // into the input. The link in the message text IS the attachment: the
  // bridge turns it into real image parts on every send, and editing the
  // message edits what the model sees. Kept simple by popular demand.
  // ------------------------------------------------------------------

  // Shared attach flow (📎 button and drag & drop): upload, then paste the
  // markdown link where the caret is — a message editor if one is open,
  // otherwise the main chat input.
  async function attachFile(f) {
    toast('Uploading to your server…');
    try {
      await ensureUploadToken();
      let url;
      try {
        url = await uploadToBridge(f);
      } catch (err) {
        // Token rotated on the bridge (e.g. after a leak cleanup): drop the
        // stale cached copy, re-bootstrap from /img/token, retry once.
        if (err && err.message === 'BAD_TOKEN') {
          settings.uploadToken = '';
          saveSettings();
          if (await ensureUploadToken()) url = await uploadToBridge(f);
          else throw err;
        } else {
          throw err;
        }
      }
      lastBindFailAt = 0;
      insertIntoEditor('![' + safeName(f.name) + '](' + url + ')');
      toast('Image attached ✓ — link pasted, model sees the real pixels');
      return true;
    } catch (err) {
      dlog('upload failed:', err);
      if (err && err.message === 'NO_BASE') {
        openSheet(true, 'Attach failed: no bridge URL yet — send one chat message so I can learn it, or fill Advanced.');
        toast('No bridge learned yet — see settings', 'warn');
      } else if (err && err.message === 'BAD_TOKEN') {
        lastBindFailAt = Date.now();
        openSheet(true, 'Device not bound yet: send any chat message once (that binds this network), then attach again. Manual token works under Advanced.');
        toast('Device not bound yet — send a chat message once, then attach again', 'warn');
      } else if (err && err.message === 'NGROK_WARNING') {
        openSheet(true, 'ngrok warning page blocked the upload — open the bridge URL in this browser once and click “Visit” (one-time per session).');
        toast('ngrok warning blocked the upload — accept it once, then retry', 'warn');
      } else {
        openSheet(true, 'Attach failed: ' + (err && err.message) + ' — try 🔗 paste-link as a fallback, or hit Test in Advanced.');
        toast('Upload failed (' + (err && err.message) + ') — see settings', 'warn');
      }
      return false;
    }
  }

  let fileInput = null;
  function ensureFileInput() {
    if (fileInput) return fileInput;
    fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = 'image/*';
    fileInput.style.display = 'none';
    document.body.appendChild(fileInput);
    fileInput.addEventListener('change', async () => {
      const f = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (!f) return;
      await attachFile(f);
    });
    return fileInput;
  }

  // ------------------------------------------------------------------
  // 2d) Drag & drop (the desktop nicety): drop image file(s) — or an image
  // link dragged from another tab — anywhere in a chat and it attaches at
  // the caret. A dashed overlay shows while a droppable payload is over the
  // page. Non-image drops are left alone for the site to handle.
  // ------------------------------------------------------------------
  let dragDepth = 0;
  function dragTypes(e) {
    const t = e.dataTransfer && e.dataTransfer.types;
    return t ? Array.from(t) : [];
  }
  function dragDroppable(e) {
    const types = dragTypes(e);
    return types.includes('Files') || types.includes('text/uri-list');
  }
  function dropOverlay(show) {
    let el = document.querySelector('.' + CSS_PREFIX + 'drop');
    if (!el && show) {
      el = document.createElement('div');
      el.className = CSS_PREFIX + 'drop';
      const inner = document.createElement('div');
      inner.className = CSS_PREFIX + 'drop-inner';
      inner.textContent = 'Drop image to attach';
      el.appendChild(inner);
      document.body.appendChild(el);
    }
    if (el) el.classList.toggle('open', !!show);
  }
  function bindDragDrop() {
    document.addEventListener('dragenter', (e) => {
      if (!isChatPage() || !dragDroppable(e)) return;
      dragDepth++;
      dropOverlay(true);
    });
    document.addEventListener('dragover', (e) => {
      if (!isChatPage() || !dragDroppable(e)) return;
      e.preventDefault(); // required, or the browser refuses the drop
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    });
    document.addEventListener('dragleave', () => {
      if (dragDepth > 0) dragDepth--;
      if (dragDepth === 0) dropOverlay(false);
    });
    document.addEventListener('drop', async (e) => {
      dragDepth = 0;
      dropOverlay(false);
      if (!isChatPage()) return;
      const dt = e.dataTransfer;
      if (!dt) return;
      const files = [...(dt.files || [])].filter((f) => /^image\//i.test(f.type || ''));
      let uri = '';
      try {
        uri = ((dt.getData('text/uri-list') || dt.getData('text/plain') || '')
          .trim().split(/\s+/)[0]) || '';
      } catch (err) { uri = ''; }
      if (!files.length && !isImgUrl(uri)) return; // not ours — let the page handle it
      e.preventDefault();
      if (files.length) {
        for (const f of files) await attachFile(f);
        return;
      }
      const name = (uri.split('/').pop() || 'image').split('?')[0];
      insertIntoEditor('![' + safeName(name) + '](' + uri + ')');
      toast('Image link pasted ✓');
    });
  }

  // ------------------------------------------------------------------
  // 3) Button cluster — top-right of the chat header (per the red arrows)
  // ------------------------------------------------------------------
  function makeIconBtn(label, title, cb) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = CSS_PREFIX + 'cluster-btn';
    b.setAttribute('aria-label', title);
    b.title = title;
    b.textContent = label;
    b.addEventListener('click', (ev) => { ev.preventDefault(); cb(ev); });
    return b;
  }

  // Send guard (binding only): if an attach just failed for binding reasons
  // and the editor has no image markdown, ask before a text-only send goes
  // out — avoids wasted tokens and a confused bot reply.
  function guardSendButtons() {
    if (!isChatPage()) return;
    const sendBtn = document.querySelector(SEND_SEL);
    if (!sendBtn || sendBtn.dataset.jiGuard === '1') return;
    sendBtn.dataset.jiGuard = '1';
    sendBtn.addEventListener('click', (ev) => {
      if (Date.now() - lastBindFailAt > 45000) return; // nothing pending
      const ed = findEditor();
      const txt = (ed && (ed.textContent || ed.value || '')) || '';
      if (/!\[[^\]]*\]\(\s*https?:\/\/|https?:\/\/[^\s]+\.(?:png|jpe?g|webp|gif)/i.test(txt)) return;
      // Capture-phase stop: prevents JanitorAI's own handler from firing.
      ev.stopImmediatePropagation();
      ev.preventDefault();
      lastBindFailAt = 0; // either choice ends the nagging
      if (window.confirm('The image could not be uploaded yet (this device is not bound — one normal chat message binds it).\n\nSend this message WITHOUT the image anyway?')) {
        setTimeout(() => sendBtn.click(), 0);
      }
    }, true);
  }

  function ensureCluster() {
    let c = document.querySelector('.' + CSS_PREFIX + 'cluster');
    const inChat = isChatPage();
    if (c && !inChat) { c.remove(); return; }
    if (!inChat) return;
    if (!c) {
      c = document.createElement('div');
      c.className = CSS_PREFIX + 'cluster';
      c.appendChild(makeIconBtn('📎', 'Attach image (uploads to your server)', () => ensureFileInput().click()));
      c.appendChild(makeIconBtn('🔗', 'Paste an image URL as markdown', async () => {
        try {
          const txt = await navigator.clipboard.readText();
          const u = (txt || '').trim();
          if (!/^https?:\/\//i.test(u)) { toast('Clipboard has no URL', 'warn'); return; }
          const name = decodeURIComponent(u.split('/').pop() || 'image').split('?')[0] || 'image';
          insertIntoEditor('![' + safeName(name) + '](' + u + ')');
          toast('Markdown inserted ✓');
        } catch (e) {
          toast('Clipboard read failed — paste into the message as ![name](url)', 'warn');
        }
      }));
      c.appendChild(makeIconBtn('🖼', 'Image intake settings', () => openSheet(true)));
      document.body.appendChild(c);
    }
  }

  // ------------------------------------------------------------------
  // 4) Settings sheet (bottom sheet on mobile, panel on desktop)
  // ------------------------------------------------------------------
  let sheetEl = null;
  function buildSheet() {
    sheetEl = document.createElement('div');
    sheetEl.className = CSS_PREFIX + 'sheet';
    sheetEl.innerHTML = `
      <div class="${CSS_PREFIX}sheet-backdrop"></div>
      <div class="${CSS_PREFIX}sheet-panel" role="dialog" aria-label="Image intake settings">
        <div class="${CSS_PREFIX}sheet-grip"></div>
        <div class="${CSS_PREFIX}sheet-title">Image intake</div>
        <div class="${CSS_PREFIX}sheet-status" data-role="status"></div>
        <label class="${CSS_PREFIX}row"><input type="checkbox" data-role="debug"> Verbose console log</label>
        <label class="${CSS_PREFIX}row"><input type="checkbox" data-role="forward"> Route other APIs through my bridge</label>
        <details class="${CSS_PREFIX}adv">
          <summary>Advanced</summary>
          <label class="${CSS_PREFIX}field">Bridge base URL (learned automatically)
            <input type="url" data-role="base" placeholder="https://…ngrok-free.dev" autocomplete="off" spellcheck="false" name="ji-bridge-url-field" data-form-type="other" data-1p-ignore="1" data-lpignore="true" data-bwignore="true">
          </label>
          <label class="${CSS_PREFIX}field">Upload token (auto-fetched)
            <input type="text" data-role="token" placeholder="leave blank to keep the stored one" autocomplete="off" spellcheck="false" name="ji-intake-token" data-form-type="other" data-1p-ignore="1" data-lpignore="true" data-bwignore="true">
          </label>
          <div class="${CSS_PREFIX}btn-row">
            <button type="button" class="${CSS_PREFIX}btn" data-role="save">Save</button>
            <button type="button" class="${CSS_PREFIX}btn ghost" data-role="test">Test</button>
          </div>
        </details>
        <button type="button" class="${CSS_PREFIX}btn ghost" data-role="close">Close</button>
        <p class="${CSS_PREFIX}hint">Zero setup: one chat request and I learn the
        bridge URL from it, verify it is really your bridge, and pin it so
        nothing else can repoint it. Your device then binds itself from the
        page's own API traffic using your key: no throwaway message, no wasted
        reply, no tokens burned. 📎 uploads to your server and the model
        receives the real pixels. Advanced lets you clear the URL to let it
        learn again. "Route other APIs" makes JanitorAI keep using any API
        you point it at (Xiaomi, anything) while every call is quietly passed
        through your bridge for image intake.</p>
      </div>`;
    document.body.appendChild(sheetEl);

    sheetEl.querySelector('.' + CSS_PREFIX + 'sheet-backdrop').addEventListener('click', () => openSheet(false));
    sheetEl.querySelector('[data-role="close"]').addEventListener('click', () => openSheet(false));
    sheetEl.querySelector('[data-role="base"]').value = settings.bridgeBase || '';
    // US-3: the token is NEVER written into the DOM. The page (and anything
    // running on it) can read input values, so the stored token stays in GM
    // storage only; this field is write-only (typing in it replaces the token).
    sheetEl.querySelector('[data-role="token"]').value = '';
    sheetEl.querySelector('[data-role="save"]').addEventListener('click', () => {
      const nb = sheetEl.querySelector('[data-role="base"]').value.trim();
      settings.bridgeBase = nb;
      settings.bridgeManual = !!nb;
      if (!nb) settings.bridgePinned = false;  // cleared: learning may run again
      const typed = sheetEl.querySelector('[data-role="token"]').value.trim();
      if (typed) settings.uploadToken = typed;   // blank keeps the stored one
      saveSettings();
      toast('Saved ✓');
      testBridge();
      ensureUploadToken();
      reapplyPageHooks();
    });
    sheetEl.querySelector('[data-role="test"]').addEventListener('click', testBridge);
    sheetEl.querySelector('[data-role="debug"]').checked = settings.debugLog;
    sheetEl.querySelector('[data-role="debug"]').addEventListener('change', (e) => { settings.debugLog = e.target.checked; saveSettings(); });
    sheetEl.querySelector('[data-role="forward"]').checked = !!settings.forwardMode;
    sheetEl.querySelector('[data-role="forward"]').addEventListener('change', (e) => {
      settings.forwardMode = e.target.checked;
      saveSettings();
      reapplyPageHooks();
      toast(e.target.checked
        ? 'Forwarding on: chat calls now pass through your bridge'
        : 'Forwarding off: JanitorAI talks to its API directly');
    });
    refreshSheetStatus();
  }

  async function testBridge() {
    const st = sheetEl.querySelector('[data-role="status"]');
    st.textContent = 'Testing bridge…';
    const base = bridgeBase();
    if (!base) { st.textContent = 'No bridge URL set.'; return; }
    try {
      const r = await gmFetch('GET', base + '/healthz');
      if (looksLikeNgrokInterstitial(r.text)) {
        st.textContent = 'ngrok warning page intercepted — open the bridge URL once in this browser and click “Visit”, then Test again.';
        return;
      }
      const d = parseJsonSafe(r.text);
      if (!r.ok || !d || !d.ok) {
        st.textContent = 'Bridge answered but not healthy (HTTP ' + r.status + ')';
        return;
      }
      st.textContent = 'Bridge: ok ✓ — uploads ready (' + Object.keys(d.upstreams || {}).join(', ') + ')';
    } catch (e) {
      st.textContent = 'Bridge unreachable (' + (e && e.message) +
        ') — is the tunnel up? Try opening ' + bridgeBase() + '/healthz in a tab.';
    }
  }

  function refreshSheetStatus() {
    if (!sheetEl) return;
    const st = sheetEl.querySelector('[data-role="status"]');
    if (!st) return;
    const base = bridgeBase();
    if (!base) {
      st.textContent = 'No bridge learned yet — send one chat message and I pick up the URL + token automatically.';
      return;
    }
    st.textContent = 'Bridge: ' + base.replace(/^https?:\/\//, '').slice(0, 40) +
      (settings.uploadToken ? ' · ready ✓'
        : settings.storeOpen ? ' · open store (no token needed) ✓'
        : ' · token auto-fetches once you chat');
  }

  function openSheet(show, note) {
    if (!sheetEl) buildSheet();
    sheetEl.classList.toggle('open', !!show);
    if (show && note) {
      const st = sheetEl.querySelector('[data-role="status"]');
      if (st) st.textContent = note; // persistent, overrides the computed line
    } else if (show) {
      refreshSheetStatus();
    }
  }

  // ------------------------------------------------------------------
  // CSS — mobile-first, fluid, safe-area aware
  // ------------------------------------------------------------------
  function injectCss() {
    const css = `
      .${CSS_PREFIX}img-row {
        display: flex; flex-wrap: wrap; gap: 8px;
        margin: 6px 0 2px; width: 100%;
      }
      .${CSS_PREFIX}preview {
        display: block; border-radius: 12px; overflow: hidden;
        max-width: min(480px, 92vw); border: 1px solid rgba(255,255,255,.12);
        background: rgba(0,0,0,.25);
      }
      .${CSS_PREFIX}preview.${CSS_PREFIX}pending {
        min-width: 150px; min-height: 92px;
        background: rgba(255,255,255,.07);
        animation: ${CSS_PREFIX}pulse 1.1s ease-in-out infinite;
      }
      @keyframes ${CSS_PREFIX}pulse {
        0%, 100% { opacity: .55; }
        50% { opacity: .45; }
      }
      .${CSS_PREFIX}preview-img {
        display: block; max-width: 100%;
        max-width: min(420px, 92vw);
        max-height: min(52vh, 460px);
        width: auto; height: auto; object-fit: contain;
      }
      .${CSS_PREFIX}cluster {
        position: fixed; z-index: 2147482000;
        top: 60px; right: max(10px, env(safe-area-inset-right));
        display: flex; flex-direction: column; gap: 8px;
      }
      .${CSS_PREFIX}cluster-btn {
        width: 28px; height: 28px; padding: 0;
        display: flex; align-items: center; justify-content: center;
        font-size: 13px; line-height: 1; border-radius: 50%;
        border: 1px solid rgba(255,255,255,.16);
        background: rgba(20,20,26,.82); color: #fff;
        cursor: pointer; backdrop-filter: blur(6px);
        box-shadow: 0 4px 14px rgba(0,0,0,.35);
        -webkit-tap-highlight-color: transparent;
      }
      .${CSS_PREFIX}cluster-btn:active { background: rgba(60,60,80,.9); }
      .${CSS_PREFIX}toast {
        position: fixed; left: 50%; transform: translateX(-50%);
        top: calc(14px + env(safe-area-inset-top));
        z-index: 2147483600; padding: 10px 16px; border-radius: 999px;
        background: rgba(24,24,32,.92); color: #fff; font-size: 14px;
        border: 1px solid rgba(255,255,255,.14);
        box-shadow: 0 6px 18px rgba(0,0,0,.4);
        opacity: 1; transition: opacity .4s; max-width: 88vw; text-align: center;
      }
      .${CSS_PREFIX}toast.${CSS_PREFIX}warn { background: rgba(120,60,10,.92); }
      .${CSS_PREFIX}toast.${CSS_PREFIX}out { opacity: 0; }
      .${CSS_PREFIX}lightbox {
        position: fixed; inset: 0; z-index: 2147483700;
        display: none; align-items: center; justify-content: center;
        background: rgba(0,0,0,.85); padding: 24px; cursor: zoom-out;
      }
      .${CSS_PREFIX}lightbox.open { display: flex; }
      .${CSS_PREFIX}lightbox img {
        max-width: min(92vw, 1200px); max-height: 84vh;
        object-fit: contain; border-radius: 14px;
        box-shadow: 0 10px 50px rgba(0,0,0,.6);
        background: rgba(0,0,0,.4);
      }
      .${CSS_PREFIX}lightbox-x {
        position: fixed;
        top: calc(14px + env(safe-area-inset-top));
        right: max(14px, env(safe-area-inset-right));
        width: 40px; height: 40px; border-radius: 50%;
        border: 1px solid rgba(255,255,255,.2);
        background: rgba(24,24,32,.9); color: #fff; font-size: 15px;
        cursor: pointer; display: flex; align-items: center; justify-content: center;
      }
      .${CSS_PREFIX}drop {
        position: fixed; inset: 10px; z-index: 2147483400;
        border: 2px dashed rgba(139,92,246,.85); border-radius: 18px;
        background: rgba(18,18,26,.5); backdrop-filter: blur(3px);
        display: none; align-items: center; justify-content: center;
        pointer-events: none;
      }
      .${CSS_PREFIX}drop.open { display: flex; }
      .${CSS_PREFIX}drop-inner {
        padding: 12px 20px; border-radius: 999px;
        background: rgba(24,24,32,.92); color: #fff; font-size: 15px;
        border: 1px solid rgba(139,92,246,.5);
        box-shadow: 0 6px 18px rgba(0,0,0,.4);
      }
      .${CSS_PREFIX}sheet-backdrop {
        position: fixed; inset: 0; z-index: 2147483600;
        background: rgba(0,0,0,.45); opacity: 0; pointer-events: none;
        transition: opacity .2s;
      }
      .${CSS_PREFIX}sheet-panel {
        position: fixed; z-index: 2147483601;
        left: 0; right: 0; bottom: 0;
        margin: 0 auto; width: min(560px, 100%);
        max-height: 72dvh; overflow: auto;
        background: #17171d; color: #e8e8ef;
        border: 1px solid rgba(255,255,255,.12); border-bottom: 0;
        border-radius: 18px 18px 0 0;
        padding: 10px 16px calc(16px + env(safe-area-inset-bottom));
        transform: translateY(100%); transition: transform .22s ease;
      }
      .${CSS_PREFIX}sheet-grip {
        width: 44px; height: 4px; border-radius: 2px;
        background: rgba(255,255,255,.25); margin: 6px auto 10px;
      }
      .${CSS_PREFIX}sheet.open .${CSS_PREFIX}sheet-backdrop { opacity: 1; pointer-events: auto; }
      .${CSS_PREFIX}sheet.open .${CSS_PREFIX}sheet-panel { transform: translateY(0); }
      .${CSS_PREFIX}sheet-title { font-weight: 600; font-size: 17px; margin: 2px 0 8px; }
      .${CSS_PREFIX}sheet-status {
        font-size: 13px; opacity: .85; margin-bottom: 10px; word-break: break-word;
      }
      .${CSS_PREFIX}field {
        display: block; font-size: 13px; opacity: .95; margin-bottom: 10px;
      }
      .${CSS_PREFIX}field input {
        display: block; width: 100%; box-sizing: border-box;
        margin-top: 4px; min-height: 42px; padding: 8px 10px;
        border-radius: 8px; border: 1px solid rgba(255,255,255,.18);
        background: rgba(0,0,0,.3); color: #fff; font-size: 14px;
      }
      .${CSS_PREFIX}btn-row { display: flex; gap: 8px; margin: 10px 0; flex-wrap: wrap; }
      .${CSS_PREFIX}row {
        display: flex; align-items: center; gap: 10px;
        min-height: 44px; font-size: 14px; cursor: pointer;
      }
      .${CSS_PREFIX}row input { width: 18px; height: 18px; accent-color: #8b5cf6; }
      .${CSS_PREFIX}btn {
        min-height: 44px; padding: 10px 14px;
        border-radius: 10px; border: 1px solid rgba(139,92,246,.5);
        background: rgba(139,92,246,.18); color: #fff; font-size: 14px;
        cursor: pointer;
      }
      .${CSS_PREFIX}btn.ghost { border-color: rgba(255,255,255,.18); background: transparent; }
      .${CSS_PREFIX}hint { font-size: 12px; opacity: .7; margin-top: 10px; line-height: 1.5; }
      .${CSS_PREFIX}adv { margin-top: 6px; }
      .${CSS_PREFIX}adv summary {
        cursor: pointer; min-height: 40px; display: flex; align-items: center;
        font-size: 14px; opacity: .8;
      }
      .${CSS_PREFIX}adv[open] summary { opacity: 1; }
      @media (min-width: 720px) {
        .${CSS_PREFIX}sheet-panel {
          left: auto; right: 16px; bottom: 16px; top: auto;
          width: 420px; border-radius: 16px; border-bottom: 1px solid rgba(255,255,255,.12);
          transform: translateY(12px); opacity: 0; pointer-events: none;
          transition: opacity .18s, transform .18s;
          padding-bottom: 16px;
        }
        .${CSS_PREFIX}sheet.open .${CSS_PREFIX}sheet-panel { transform: translateY(0); opacity: 1; pointer-events: auto; }
        .${CSS_PREFIX}sheet-grip { display: none; }
        .${CSS_PREFIX}cluster-btn { width: 42px; height: 42px; font-size: 18px; }
      }
    `;
    const st = document.createElement('style');
    st.textContent = css;
    document.head.appendChild(st);
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  function boot() {
    injectCss();
    reapplyPageHooks();
    healLearnedBridge();
    scanLocalStorageForBridge();
    buildSheet();
    ensureCluster();
    scanMessages();
    queueScan();

    bindDragDrop();
    const mo = new MutationObserver(() => { queueScan(); ensureCluster(); guardSendButtons(); });
    mo.observe(document.body, { childList: true, subtree: true });
    // React re-renders can tear nodes down; re-check periodically.
    setInterval(() => { queueScan(); ensureCluster(); guardSendButtons(); }, 2000);
    const ver = (typeof GM_info !== 'undefined' && GM_info.script) ? GM_info.script.version : 'dev';
    console.log(TAG, 'v' + ver + ' loaded — bridge converts links, cluster attaches/uploads');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
