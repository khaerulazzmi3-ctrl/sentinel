/* SENTINEL ARENA - operator gate.
   The key is posted once to /api/login, which sets an HttpOnly cookie; the key
   never lands in a URL, in localStorage, or in the dashboard DOM. */
(() => {
  const $ = (id) => document.getElementById(id);
  const form = $('form'), keyEl = $('key'), goBtn = $('go'), statusEl = $('status');
  const tiers = Array.from(document.querySelectorAll('.tier'));
  let chosen = '';

  function setStatus(msg, kind) {
    statusEl.textContent = msg || '';
    statusEl.className = 'status' + (kind ? ' ' + kind : '');
  }

  function pick(tier) {
    chosen = tier;
    tiers.forEach((b) => b.setAttribute('aria-checked', String(b.dataset.tier === tier)));
    setStatus('');
  }

  tiers.forEach((b) => {
    b.addEventListener('click', () => pick(b.dataset.tier));
    b.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(b.dataset.tier); }
    });
  });

  keyEl.addEventListener('input', () => {
    const v = keyEl.value.toUpperCase();
    if (v !== keyEl.value) keyEl.value = v;
    // auto-select the tier that matches the key prefix - still verified server side
    const p = v.slice(0, 3);
    if (p === 'DIR') pick('DIREKTUR');
    else if (p === 'ADM') pick('ADMIN');
    else if (p === 'USR') pick('BIASA');
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const key = keyEl.value.trim().toUpperCase();
    if (!key) { setStatus('API key kosong', 'bad'); return; }
    if (!chosen) { setStatus('Pilih tier: BIASA, ADMIN, atau DIREKTUR', 'bad'); return; }
    goBtn.disabled = true;
    setStatus('memverifikasi…');
    try {
      const r = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key, tier: chosen })
      });
      let d = {};
      try { d = await r.json(); } catch (err) { /* ignore */ }
      if (r.ok && d.ok) {
        setStatus('diterima — membuka command deck', 'ok');
        location.replace('/deck');
        return;
      }
      setStatus(d.error || ('ditolak (HTTP ' + r.status + ')'), 'bad');
      keyEl.select();
    } catch (err) {
      setStatus('server tidak menjawab', 'bad');
    } finally {
      goBtn.disabled = false;
    }
  });

  // show which identity the gate sees, so a block is never a mystery
  fetch('/api/whoami').then((r) => r.json()).then((d) => {
    const el = $('who');
    if (d && d.operator) {
      el.textContent = `sesi aktif: ${d.operator.tier} · entitas ${d.entity || '—'}`;
      setStatus('sudah ada sesi — klik MASUK untuk melanjutkan', 'ok');
      pick(d.operator.tier);
      goBtn.textContent = 'LANJUTKAN';
    } else {
      el.textContent = `entitas ${d.entity || '—'} · tier ${d.tier || 'BIASA'} · status ${d.blocked ? 'BLOKIR' : 'boleh masuk'}`;
    }
  }).catch(() => { /* offline: the form still works when the server returns */ });

  keyEl.focus();
})();
