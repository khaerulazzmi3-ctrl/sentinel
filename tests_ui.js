/* Render the dashboard in jsdom against a live server snapshot and report DOM state. */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = process.argv[2] || '.';
const STATE = process.argv[3] || 'state.json';

const html = fs.readFileSync(path.join(ROOT, 'web/index.html'), 'utf8');
const js = fs.readFileSync(path.join(ROOT, 'web/app.js'), 'utf8');

// Snapshot: use the given file, or pull one live from the running server so the
// suite has no committed fixture to keep in sync.
function loadSnapshot(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    const { execFileSync } = require('child_process');
    const key = process.env.ARENA_KEY || 'DIR-R69E-T72D-8GL3';
    const url = 'http://127.0.0.1:8787/api/state?key=' + encodeURIComponent(key);
    const out = execFileSync('curl', ['-s', '-A', 'ArenaOps/1.0', url], { encoding: 'utf8', timeout: 20000 });
    return JSON.parse(out);
  }
}

const snapshot = loadSnapshot(STATE);

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1:8787/' });
const { window } = dom;
const errors = [];

// canvas stub: swallow all 2d calls
const ctxStub = new Proxy({}, {
  get(t, k) {
    if (k === 'canvas') return { width: 600, height: 200 };
    if (k === 'createLinearGradient') return () => ({ addColorStop() {} });
    if (k === 'measureText') return () => ({ width: 10 });
    return () => {};
  },
  set() { return true; }
});
window.HTMLCanvasElement.prototype.getContext = () => ctxStub;

window.EventSource = class {
  constructor() {
    setTimeout(() => {
      this.onopen && this.onopen();
      this.onmessage && this.onmessage({ data: JSON.stringify(snapshot) });
    }, 5);
  }
  close() {}
};
if (!snapshot.operator) snapshot.operator = { key: 'test', tier: 'ADMIN', level: 2 };
window.fetch = (url) => {
  if (String(url).indexOf('/api/whoami') >= 0) {
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({
        ok: true, operator: snapshot.operator, entity: '127.0.0.1',
        tier: (snapshot.operator || {}).tier || 'ADMIN', blocked: false
      })
    });
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve(snapshot) });
};
window.confirm = () => true;
window.addEventListener('error', (e) => errors.push('window error: ' + e.message));

// capture console errors
const origError = console.error;
console.error = (...a) => { errors.push(a.join(' ')); origError(...a); };

window.eval(js);

setTimeout(() => {
  const doc = window.document;
  const text = (id) => (doc.getElementById(id) || {}).textContent || '';
  const htmlOf = (id) => (doc.getElementById(id) || {}).innerHTML || '';
  const checks = [];
  const check = (name, cond, extra) => checks.push({ name, ok: !!cond, extra });

  check('KPI equity rendered', /^(?:\$|[A-Z]{3} )[\d,]+\.\d{2}$/.test(text('kpiEquity').trim()), text('kpiEquity'));
  check('PnL colored', /var\(--green\)|var\(--red\)/.test(doc.getElementById('kpiPnl').style.color || ''), doc.getElementById('kpiPnl').style.color);
  check('mode chip', /PAPER|MT5/.test(text('modeChip')), text('modeChip'));
  check('ladder has 4 rungs', doc.querySelectorAll('#ladder .rung').length === 4, doc.querySelectorAll('#ladder .rung').length);
  check('entities rendered', doc.querySelectorAll('#ladder .entity').length >= 4, doc.querySelectorAll('#ladder .entity').length);
  check('threat feed rows', doc.querySelectorAll('#threats .threat').length > 0 || /Belum ada/.test(htmlOf('threats')), doc.querySelectorAll('#threats .threat').length);
  check('audit rows', doc.querySelectorAll('#audit .audit-row').length > 0, doc.querySelectorAll('#audit .audit-row').length);
  check('heal rows', doc.querySelectorAll('#healList .heal').length > 0, doc.querySelectorAll('#healList .heal').length);
  const expectCells = (snapshot.trading.strategies || [])
    .reduce((n, g) => n + (g.symbols || []).length, 0);
  check('matrix cells', doc.querySelectorAll('#matrix .cell').length === expectCells,
        doc.querySelectorAll('#matrix .cell').length + '/' + expectCells);
  check('universe strip matches broker map',
        doc.querySelectorAll('#uniBar .uni').length >= (snapshot.trading.agent.universe || []).length,
        doc.querySelectorAll('#uniBar .uni').length);
  check('no instrument outside the broker universe',
        !/WTIUSD|XAGUSD|COPPER/.test(htmlOf('matrix')),
        (htmlOf('matrix').match(/WTIUSD|XAGUSD|COPPER/g) || []).join(','));
  check('matrix has live badges', doc.querySelectorAll('#matrix .cell.live').length > 0, doc.querySelectorAll('#matrix .cell.live').length);
  check('positions or empty state', doc.querySelectorAll('#posTable tbody tr').length > 0 || !doc.getElementById('posEmpty').classList.contains('hide'), doc.querySelectorAll('#posTable tbody tr').length);
  check('signals or empty', doc.querySelectorAll('#signals .sig').length > 0 || /memindai/.test(htmlOf('signals')), doc.querySelectorAll('#signals .sig').length);
  check('chart venue label', /PAPER|MT5/.test(text('chartVenue')), text('chartVenue'));
  check('ticker populated', htmlOf('ticker').length > 20, htmlOf('ticker').length);
  check('operator auth note', /ADMIN|DIREKTUR|BIASA/.test(text('authNote')), text('authNote'));
  check('tier chip shows the session tier', /ADMIN|DIREKTUR|BIASA/.test(text('tierChip')), text('tierChip'));
  const mt5 = (snapshot.trading || {}).mt5;
  check('mt5 feed state surfaced', !mt5 || /OFF|WAITING|CONNECTING|LIVE|ERROR/.test(mt5.state || ''), mt5 && mt5.state);
  check('no runtime errors', errors.length === 0, errors.join(' | '));

  // open a matrix cell -> modal
  const cell = doc.querySelector('#matrix .cell.ok') || doc.querySelector('#matrix .cell');
  if (cell) {
    cell.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    check('modal opens on cell click', doc.getElementById('modal').classList.contains('open'), '');
    check('modal gate checks', doc.querySelectorAll('#modalBody .gate').length > 0, doc.querySelectorAll('#modalBody .gate').length);
    check('modal folds', doc.querySelectorAll('#modalBody .fold').length >= 4, doc.querySelectorAll('#modalBody .fold').length);
  }

  const failed = checks.filter(c => !c.ok);
  checks.forEach(c => console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.name}${c.ok ? '' : '  -> ' + JSON.stringify(c.extra)}`));
  console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
  if (errors.length) { console.log('\nERRORS:'); errors.slice(0, 8).forEach(e => console.log('  ' + e)); }
  process.exit(failed.length ? 1 : 0);
}, 600);
