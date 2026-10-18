/* Operator gate checks: key input, tier choice, and what gets posted to /api/login. */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = process.argv[2] || '.';
const html = fs.readFileSync(path.join(ROOT, 'web/gate.html'), 'utf8');
const js = fs.readFileSync(path.join(ROOT, 'web/gate.js'), 'utf8');

const calls = [];
const vc = new VirtualConsole();
const navNoise = [];
vc.on('jsdomError', (e) => {
  // location.replace is not implemented in jsdom - expected once login succeeds
  if (/Not implemented: navigation/i.test(e.message || '')) { navNoise.push(e.message); return; }
  navNoise.push('JS ERROR: ' + (e.message || e));
});
vc.on('error', (m) => navNoise.push('console.error: ' + m));

const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://127.0.0.1:8787/', virtualConsole: vc });
const { window } = dom;
const doc = window.document;

let loginReply = { status: 200, body: { ok: true, operator: { tier: 'DIREKTUR', level: 3 } } };

window.fetch = (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  calls.push({ url: String(url), body });
  if (String(url).indexOf('/api/whoami') >= 0) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({
      ok: true, operator: null, entity: '127.0.0.1', tier: 'BIASA', blocked: false }) });
  }
  return Promise.resolve({
    ok: loginReply.status >= 200 && loginReply.status < 300,
    status: loginReply.status,
    json: () => Promise.resolve(loginReply.body)
  });
};

window.eval(js);

const checks = [];
const check = (name, cond, extra) => checks.push({ name, ok: !!cond, extra });
const $ = (id) => doc.getElementById(id);

function typeKey(v) {
  const el = $('key');
  el.value = v;
  el.dispatchEvent(new window.Event('input', { bubbles: true }));
}
function submit() {
  $('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  check('three tier options', doc.querySelectorAll('.tier').length === 3,
    doc.querySelectorAll('.tier').length);
  check('tiers are BIASA/ADMIN/DIREKTUR',
    Array.from(doc.querySelectorAll('.tier')).map((b) => b.dataset.tier).join(',') ===
    'BIASA,ADMIN,DIREKTUR');
  check('key field present', !!$('key'));

  // submit with no key -> blocked before any network call
  const before = calls.length;
  submit();
  await wait(10);
  check('empty key refused locally', calls.length === before && /kosong/i.test($('status').textContent),
    $('status').textContent);

  // typing a DIR- key pre-selects DIREKTUR (server still verifies)
  typeKey('DIR-R69E-T72D-8GL3');
  const dir = doc.querySelector('.tier[data-tier="DIREKTUR"]');
  check('DIR key auto-selects DIREKTUR', dir.getAttribute('aria-checked') === 'true',
    dir.getAttribute('aria-checked'));

  // an explicit click wins over the prefix guess
  doc.querySelector('.tier[data-tier="ADMIN"]').click();
  check('clicking ADMIN switches choice',
    doc.querySelector('.tier[data-tier="ADMIN"]').getAttribute('aria-checked') === 'true' &&
    dir.getAttribute('aria-checked') === 'false');

  // mismatched tier -> error text, no navigation
  loginReply = { status: 403, body: { ok: false, error: 'Kunci ini terdaftar sebagai DIREKTUR, bukan ADMIN' } };
  submit();
  await wait(30);
  check('tier mismatch shows the server error', /bukan ADMIN/.test($('status').textContent),
    $('status').textContent);
  check('status styled as error', /bad/.test($('status').className), $('status').className);

  // correct tier -> posts key + tier and navigates
  loginReply = { status: 200, body: { ok: true, operator: { tier: 'DIREKTUR', level: 3 } } };
  doc.querySelector('.tier[data-tier="DIREKTUR"]').click();
  const lastBefore = calls.length;
  submit();
  await wait(40);
  const sent = calls[calls.length - 1];
  check('posts key + chosen tier',
    sent.url === '/api/login' && sent.body.key === 'DIR-R69E-T72D-8GL3' && sent.body.tier === 'DIREKTUR',
    JSON.stringify(sent.body));
  check('success message shown', /diterima|membuka/i.test($('status').textContent), $('status').textContent);
  check('no unexpected script errors',
    navNoise.filter((n) => !/navigation/i.test(n)).length === 0,
    navNoise.join(' | '));

  const failed = checks.filter((c) => !c.ok);
  checks.forEach((c) => console.log(
    `${c.ok ? 'PASS' : 'FAIL'}  ${c.name}${c.extra !== undefined && !c.ok ? '  -> ' + c.extra : ''}`));
  console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
})();
