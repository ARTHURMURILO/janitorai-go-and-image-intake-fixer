// Renderer smoke test for janitorai-image-intake.user.js.
//
//   npm i jsdom && node test_render_smoke.js
//
// Replays the real chat DOM shape in jsdom with GM_* stubs and asserts the
// injection / self-heal behaviour that kept breaking in the wild (inline
// placement, same URL twice, React re-renders, dead images, edit mode).
//
// Original: smoke test for janitorai-image-intake.user.js renderer logic.
// Replays the real chat DOM shape inside jsdom + the GM_* stubs, then
// asserts the injection/self-heal behaviour that kept breaking in the wild.
const fs = require('fs');
const { JSDOM } = require('jsdom');

const path = require('path');
const SRC = fs.readFileSync(path.join(__dirname, 'janitorai-image-intake.user.js'), 'utf8');

const IMG_A = 'https://example.ngrok.app/img/aaa.png';
const IMG_B = 'https://example.ngrok.app/img/bbb.png';

let pass = 0, fail = 0;
function ok(cond, name) {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ FAIL: ' + name); }
}

function makeDom(bodyHtml, opts = {}) {
  const url = opts.url || 'https://janitorai.com/chats/1';
  const editor = opts.editor === false ? '' : '<div class="ql-editor" contenteditable="true"></div>';
  const dom = new JSDOM(
    `<!DOCTYPE html><html><head></head><body>
       ${editor}
       <div data-testid="virtuoso-item-list">${bodyHtml}</div>
     </body></html>`,
    { url, pretendToBeVisual: true, runScripts: 'outside-only' }
  );
  const w = dom.window;
  // GM stubs
  const store = {};
  if (opts.bridge) store.settings = JSON.stringify({ bridgeBase: 'https://bridge.example', uploadToken: '' });
  w.GM_getValue = (k, d) => (k in store ? store[k] : d);
  w.GM_setValue = (k, v) => { store[k] = v; };
  w.GM_info = { script: { version: 'test' } };
  const fetched = [];
  let respond = (url) => ({ status: 200, headers: 'content-type: image/png', buf: 'PNGDATA' });
  w.GM_xmlhttpRequest = (req) => {
    fetched.push(req.url);
    setTimeout(() => {
      if (/\/img\/token/.test(req.url)) {
        req.onload({ status: 200, responseText: JSON.stringify({ token: 'tok-123' }),
                     responseHeaders: 'content-type: application/json' });
      } else if (/\/img\/upload/.test(req.url)) {
        req.onload({ status: 200, responseText: JSON.stringify({ url: 'https://bridge.example/img/uploaded.png' }),
                     responseHeaders: 'content-type: application/json' });
      } else {
        const r = respond(req.url);
        if (r.status === 200) {
          const ab = new w.ArrayBuffer(r.buf.length);
          req.onload({ status: 200, response: ab, responseHeaders: r.headers || 'content-type: image/png' });
        } else {
          req.onload({ status: r.status, response: null, responseHeaders: 'content-type: text/html' });
        }
      }
    }, 0);
  };
  w.URL.createObjectURL = (blob) => 'blob:fake/' + Math.random().toString(36).slice(2);
  w.URL.revokeObjectURL = () => {};
  // jsdom never actually loads images: emulate by making any blob-src img
  // report real pixels and fire 'load' right after src is assigned.
  const srcDesc = Object.getOwnPropertyDescriptor(w.HTMLImageElement.prototype, 'src');
  Object.defineProperty(w.HTMLImageElement.prototype, 'src', {
    get() { return srcDesc.get.call(this); },
    set(v) {
      srcDesc.set.call(this, v);
      if (typeof v === 'string' && v.startsWith('blob:')) {
        this.__natural = 64;
        setTimeout(() => this.dispatchEvent(new w.Event('load')), 0);
      } else { this.__natural = 0; }
    },
  });
  Object.defineProperty(w.HTMLImageElement.prototype, 'naturalWidth', { get() { return this.__natural || 0; } });

  w.eval(SRC);
  return { dom, w, fetched, setRespond: (fn) => { respond = fn; } };
}

const flush = (w, n = 8) => new Promise((res) => {
  let i = 0;
  const tick = () => (++i >= n ? res() : setTimeout(tick, 5));
  setTimeout(tick, 5);
});
// MutationObserver -> queueScan has a 250ms debounce; wait it out.
const settle = () => new Promise((r) => setTimeout(r, 700));

const rowCount = (w) => w.document.querySelectorAll('.ji-img-row').length;
const anchors = (w) => [...w.document.querySelectorAll('[data-index] a[href]')]
  .filter((a) => !a.closest('.ji-img-row'));

(async () => {
  console.log('\n1) single image link → inline figure, link hidden after load');
  {
    const { w } = makeDom(`<div data-index="0"><div>hey <a href="${IMG_A}">Image</a> there</div></div>`);
    await flush(w);
    ok(rowCount(w) === 1, 'exactly one figure row');
    const a = anchors(w)[0];
    ok(a.style.display === 'none' && a.dataset.jiPreviewed === '1', 'original link hidden');
    const img = w.document.querySelector('.ji-img-row img');
    ok(img && img.src.startsWith('blob:'), 'img has blob src');
    ok(!w.document.querySelector('.ji-preview.pending'), 'pending class cleared');
    // Rescan must not duplicate.
    w.document.querySelector('.ql-editor').dispatchEvent(new w.Event('input', { bubbles: true }));
    await flush(w);
    ok(rowCount(w) === 1, 'rescan does not duplicate');
  }

  console.log('\n2) SAME image twice in one message → two figures (the reported bug)');
  {
    const { w } = makeDom(
      `<div data-index="0"><div><a href="${IMG_A}">Image</a> text</div><div>tail <a href="${IMG_A}">Image</a></div></div>`);
    await flush(w);
    ok(rowCount(w) === 2, 'two figure rows for the same URL');
    ok(anchors(w).every((a) => a.style.display === 'none'), 'both links hidden');
  }

  console.log('\n3) React wipe: message content re-rendered (nodes replaced) → self-heal');
  {
    const { w } = makeDom(`<div data-index="0"><div>x <a href="${IMG_A}">Image</a></div></div>`);
    await flush(w);
    ok(rowCount(w) === 1, 'initial figure');
    // Simulate React replacing the whole content subtree (new nodes, no figures).
    const host = w.document.querySelector('[data-index]');
    host.innerHTML = `<div>y <a href="${IMG_A}">Image</a></div>`;
    await settle();
    ok(rowCount(w) === 1, 'figure re-attached exactly once');
    const a = anchors(w)[0];
    ok(a.style.display === 'none', 'recreated link hidden again');
  }

  console.log('\n4) React keeps the figure but recreates the anchor → orphan removed, no duplicate');
  {
    const { w } = makeDom(`<div data-index="0"><div>x <a href="${IMG_A}">Image</a></div></div>`);
    await flush(w);
    const oldAnchor = anchors(w)[0];
    // Replace ONLY the anchor node with a fresh one; the figure stays behind.
    const fresh = w.document.createElement('a');
    fresh.href = IMG_A; fresh.textContent = 'Image';
    oldAnchor.replaceWith(fresh);
    await settle();
    ok(rowCount(w) === 1, 'still exactly one figure row (no stack, no orphan)');
    ok(fresh.style.display === 'none', 'fresh anchor hidden');
  }

  console.log('\n5) dead image (404) → link stays visible, no ghost row');
  {
    const { w, setRespond } = makeDom(`<div data-index="0"><div>x <a href="${IMG_B}">Image</a></div></div>`);
    setRespond(() => ({ status: 404 }));
    await flush(w);
    ok(rowCount(w) === 0, 'figure removed');
    const a = anchors(w)[0];
    ok(a.style.display !== 'none', 'link still visible');
    ok(!w.document.querySelector('.ji-preview.pending'), 'no pending skeleton left');
  }

  console.log('\n6) edit mode: raw markdown in the message → nothing injected');
  {
    const { w } = makeDom(
      `<div data-index="0"><textarea>![Image](${IMG_A})</textarea></div>`);
    await flush(w);
    ok(rowCount(w) === 0, 'no injection while editing');
  }

  console.log('\n7) normal (non-image) links untouched');
  {
    const { w } = makeDom(`<div data-index="0"><div><a href="https://example.com/page">a page</a></div></div>`);
    await flush(w);
    ok(rowCount(w) === 0, 'no figure for a plain link');
    ok(anchors(w)[0].style.display !== 'none', 'plain link untouched');
  }

  console.log('\n8) non-chat pages (profile / discovery / chat list) stay untouched');
  {
    const { w } = makeDom(
      `<div data-index="0"><a href="${IMG_A}">Image</a></div>`,
      { url: 'https://janitorai.com/profile/tin-folk', editor: false });
    await flush(w);
    ok(rowCount(w) === 0, 'no injection on a profile page');
    const { w: w2 } = makeDom(
      `<div data-index="0"><a href="${IMG_A}">Image</a></div>`,
      { url: 'https://janitorai.com/chats', editor: false });
    await flush(w2);
    ok(rowCount(w2) === 0, 'no injection on the chat list page');
    const { w: w3 } = makeDom(
      `<div data-index="0"><a href="${IMG_A}">Image</a></div>`,
      { url: 'https://janitorai.com/chats/12345', editor: false });
    await flush(w3);
    ok(rowCount(w3) === 0, 'chat URL with no editor yet → still waits');
    const { w: w4 } = makeDom(
      `<div data-index="0"><a href="${IMG_A}">Image</a></div>`,
      { url: 'https://janitorai.com/chats/12345' });
    await flush(w4);
    ok(rowCount(w4) === 1, 'real chat page still renders');
  }

  console.log('\n9) drag & drop: dropping an image FILE while editing a message pastes at the caret');
  {
    const { w } = makeDom(
      `<div data-index="0"><textarea id="medit">hello world</textarea></div>`, { bridge: true });
    await flush(w); // let DOMContentLoaded -> boot -> bindDragDrop settle
    const ta = w.document.getElementById('medit');
    ta.focus();
    ta.selectionStart = ta.selectionEnd = 5; // caret between "hello" and " world"
    const ev = new w.Event('drop', { bubbles: true, cancelable: true });
    ev.dataTransfer = {
      types: ['Files'],
      files: [new w.File(['xx'], 'cat.png', { type: 'image/png' })],
      getData: () => '',
    };
    w.document.dispatchEvent(ev);
    await flush(w, 40);
    ok(ta.value === 'hello![cat](https://bridge.example/img/uploaded.png) world',
       'markdown inserted at the caret inside the message editor');
    ok(ev.defaultPrevented, 'drop handled (page default prevented)');
  }

  console.log('\n10) drag & drop: an image LINK dragged in (no file) pastes the markdown');
  {
    const { w } = makeDom(`<div data-index="0"><div>hi</div></div>`);
    await flush(w);
    const ev = new w.Event('drop', { bubbles: true, cancelable: true });
    ev.dataTransfer = { types: ['text/uri-list'], files: [], getData: () => IMG_B };
    w.document.dispatchEvent(ev);
    await flush(w);
    const ed = w.document.querySelector('.ql-editor');
    ok(/!\[bbb\]\(https:\/\/example\.ngrok\.app\/img\/bbb\.png\)/.test(ed.textContent),
       'markdown link appended to the main input');
  }

  console.log('\n11) drag & drop: overlay opens while dragging, closes after drop');
  {
    const { w } = makeDom(`<div data-index="0"><div>hi</div></div>`);
    await flush(w);
    const enter = new w.Event('dragenter', { bubbles: true, cancelable: true });
    enter.dataTransfer = { types: ['Files'] };
    w.document.dispatchEvent(enter);
    ok(!!w.document.querySelector('.ji-drop.open'), 'dashed overlay shown on dragenter');
    const drop = new w.Event('drop', { bubbles: true, cancelable: true });
    drop.dataTransfer = { types: ['Files'], files: [], getData: () => 'https://example.com/not-an-image' };
    w.document.dispatchEvent(drop);
    ok(!w.document.querySelector('.ji-drop.open'), 'overlay hidden after drop');
    ok(!drop.defaultPrevented, 'non-image drop left for the page to handle');
  }

  console.log('\n12) drag & drop outside chats (or on non-chat pages) does nothing');
  {
    const { w } = makeDom(`<div data-index="0"><div>hi</div></div>`,
      { url: 'https://janitorai.com/profile/x', editor: false });
    await flush(w);
    const ev = new w.Event('drop', { bubbles: true, cancelable: true });
    ev.dataTransfer = {
      types: ['Files'],
      files: [new w.File(['xx'], 'cat.png', { type: 'image/png' })],
      getData: () => '',
    };
    w.document.dispatchEvent(ev);
    await flush(w, 20);
    ok(!ev.defaultPrevented, 'ignored outside chat pages');
    ok(!w.document.querySelector('.ji-drop'), 'no overlay outside chat pages');
  }

  console.log(`\nRESULT: ${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
