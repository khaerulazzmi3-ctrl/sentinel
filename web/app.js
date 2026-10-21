/* SENTINEL ARENA - dashboard controller */
(() => {
  const $ = (id) => document.getElementById(id);
  let authed = false;
  let state = null;
  let es = null;
  let pollTimer = null;
  let prevBlocked = 0;
  let prevThreat = 0;
  let prevMt5Live = null;
  let lastLogTs = 0;

  function paintTier(op) {
    const el = $('tierChip');
    if (!el) return;
    if (!op) { el.textContent = '—'; el.className = 'tier-chip'; return; }
    el.textContent = op.tier;
    el.className = 'tier-chip lv' + (op.level || 1);
  }

  /* ------------------------------------------------ formatting */
  let CUR = 'USD';
  const curSym = () => (CUR === 'USD' ? '$' : CUR + ' ');
  const money = (v, d = 2) => (v == null || isNaN(v)) ? '—' :
    (v < 0 ? '-' : '') + curSym() + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const signed = (v, d = 2) => (v == null || isNaN(v)) ? '—' :
    (v >= 0 ? '+' : '-') + curSym() + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (v, d = 2) => (v == null || isNaN(v)) ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(d) + '%';
  const num = (v, d = 2) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d);
  const hhmm = (ts) => new Date(ts).toLocaleTimeString('id-ID', { hour12: false });
  const hhmmss = (ts) => new Date(ts).toLocaleTimeString('id-ID', { hour12: false });
  const up = (s) => {
    if (s == null) return '—';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return (h ? h + 'j ' : '') + m + 'm ' + (s % 60) + 's';
  };
  const cls = (v) => v >= 0 ? 'pos' : 'neg';

  /* ------------------------------------------------ connection */
  const SNAP_KEY = 'sentinel.lastState.v1';

  function saveSnapshot(s) {
    try {
      localStorage.setItem(SNAP_KEY, JSON.stringify({ ts: Date.now(), data: s }));
    } catch (e) { /* quota or private mode: not fatal */ }
  }

  function loadSnapshot() {
    try {
      const raw = localStorage.getItem(SNAP_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return parsed && parsed.data ? parsed : null;
    } catch (e) { return null; }
  }

  function showStale(snap) {
    const bar = $('staleBar');
    if (!bar) return;
    if (!snap) { bar.hidden = true; return; }
    const age = Math.max(0, Math.round((Date.now() - snap.ts) / 1000));
    const mins = Math.floor(age / 60), secs = age % 60;
    const when = mins ? `${mins}m ${secs}s` : `${secs}s`;
    $('staleText').textContent =
      `data terakhir ${when} lalu — PC/agent tidak terjangkau, angka di bawah adalah snapshot`;
    bar.hidden = false;
  }

  function hideStale() {
    const bar = $('staleBar');
    if (bar) bar.hidden = true;
  }

  function offline() {
    setConn(false, 'OFFLINE');
    const snap = loadSnapshot();
    if (snap) { apply(snap.data); showStale(snap); }
    else {
      // nothing cached: say so instead of showing an empty dashboard
      const bar = $('staleBar');
      if (bar) {
        $('staleText').textContent =
          'belum ada snapshot tersimpan — buka dashboard sekali saat agent hidup';
        bar.hidden = false;
      }
    }
  }

  function connect() {
    if (es) { es.close(); es = null; }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    const url = '/api/stream';
    es = new EventSource(url);
    es.onopen = () => { setConn(true, 'LIVE'); hideStale(); };
    es.onmessage = (ev) => {
      try {
        const s = JSON.parse(ev.data);
        apply(s);
        saveSnapshot(s);
        hideStale();
        setConn(true, 'LIVE');
      } catch (e) { /* ignore */ }
    };
    es.onerror = () => {
      setConn(false, 'RETRY');
      if (es) { es.close(); es = null; }
      startPolling();
    };
  }
  function startPolling() {
    if (pollTimer) return;
    const tick = async () => {
      try {
        const r = await fetch('/api/state');
        if (r.ok) {
          const s = await r.json();
          apply(s);
          saveSnapshot(s);
          hideStale();
          setConn(true, 'POLL');
        } else {
          offline();
        }
      } catch (e) { offline(); }
    };
    tick();
    pollTimer = setInterval(tick, 2000);
  }
  function setConn(on, label) {
    const el = $('conn');
    el.className = 'conn ' + (on ? 'on' : 'off');
    el.querySelector('span').textContent = label;
  }

  /* ------------------------------------------------ main render */
  function apply(s) {
    state = s;
    const t = s.trading, sec = s.security, ag = t.agent, acc = t.account;
    CUR = acc.currency || 'USD';   // MT5 cent accounts report USC, not USD

    const isMt5 = ag.mode === 'MT5-LIVE' || ag.mode === 'MT5-FEED' || ag.mode === 'MT5';
    $('modeChip').textContent = isMt5 ? (ag.mode === 'MT5-LIVE' ? 'MT5 · LIVE' : 'MT5 · FEED')
                                      : 'PAPER · SIM';
    $('modeChip').className = 'chip' + (isMt5 ? ' mt5' : '');
    $('modeChip').title = ag.mode_note || '';
    $('uptime').textContent = 'uptime ' + up(ag.uptime_s);

    // KPI
    $('kpiEquity').textContent = money(acc.equity);
    $('kpiEquitySub').textContent = `balance ${money(acc.balance)} · ${acc.currency || 'USD'}`;
    $('kpiMode').textContent = ag.mode;

    const dp = acc.day_pnl || 0;
    $('kpiPnl').textContent = signed(dp);
    $('kpiPnl').style.color = dp >= 0 ? 'var(--green)' : 'var(--red)';
    $('kpiPnlPct').textContent = pct(acc.day_pnl_pct);
    $('kpiPnlPct').className = 'tag ' + (dp >= 0 ? '' : 'danger');
    $('kpiPnlSub').textContent = `unrealized ${signed(acc.unrealized)}`;

    const heat = ag.heat_pct || 0, cap = ag.max_heat_pct || 4;
    $('kpiHeat').textContent = num(heat, 1) + '%';
    $('kpiHeat').style.color = heat > cap * 0.8 ? 'var(--amber)' : '';
    $('kpiHeatCap').textContent = 'cap ' + num(cap, 1) + '%';
    $('heatBar').style.width = Math.min(100, heat / cap * 100) + '%';
    $('heatBar').parentElement.className = 'bar' + (heat > cap * 0.8 ? ' hot' : '');
    $('kpiPos').textContent = `${t.positions.length} posisi · maks ${ag.max_open}`;

    $('kpiBlocked').textContent = sec.stats.blocked;
    $('kpiThreats').textContent = `${sec.agent.intrusions} intrusi · ${sec.agent.requests} request diperiksa`;
    $('kpiIntegrity').textContent = sec.agent.integrity_ok ? 'CHAIN OK' : 'CHAIN ALERT';
    $('kpiIntegrity').className = 'tag ' + (sec.agent.integrity_ok ? '' : 'danger');

    // operator
    const op = s.operator;
    $('authNote').textContent = op ? `anda: ${op.tier}` : 'sesi berakhir';
    $('authNote').style.color = op && op.level >= 2 ? 'var(--cy)' : 'var(--amber)';
    paintTier(op);
    const canAdmin = op && op.level >= 2;
    ['btnAttack', 'btnPause', 'btnKill', 'btnRevalidate'].forEach(id => { $(id).disabled = !canAdmin; });

    renderSecurity(sec);
    renderAccount(t, acc, ag);
    renderPositions(t.positions);
    renderSignals(t.signals);
    renderThreats(sec);
    renderAudit(sec);
    renderMatrix(t.strategies);
    renderUniverse(ag);
    renderTicker(t.log);
    renderMiniLadder(sec);
    drawChart(t.equity, t.pnl);
    spark('sparkEquity', (t.equity || []).map(p => p[1]), '46,230,200');
    spark('sparkPnl', (t.pnl || []).map(p => p[1]), '167,139,250');
    alerts(sec);
    watchMt5(t.mt5);
    renderWarn(t.mt5, ag);
  }

  /* ------------------------------------------------ security */
  function watchMt5(m) {
    const live = !!(m && m.state === 'LIVE');
    if (prevMt5Live === null) { prevMt5Live = live; return; }
    if (live && !prevMt5Live) {
      toast(`MT5 tersambung — akun ${m.login || '?'} @ ${m.server || '?'}`, 'gold');
    } else if (!live && prevMt5Live) {
      toast('MT5 terputus — kembali ke data terakhir', 'bad');
    }
    prevMt5Live = live;
  }

  function renderWarn(m, ag) {
    const bar = $('warnBar');
    let msg = '';
    if (m && m.state === 'LIVE' && m.trade_allowed === false) {
      msg = 'AUTOTRADING MT5 MASIH OFF — klik tombol "Algo Trading" di toolbar MT5. ' +
            'Agent sudah siap; order terkirim sendiri begitu tombol itu aktif.';
    } else if (m && m.state === 'LIVE' && m.trade_enabled === false) {
      msg = 'Mode feed MT5 (read-only): sinyal hanya tampil, order tidak dikirim. ' +
            'Set "mt5.trade_enabled": true di config.json untuk eksekusi otomatis.';
    } else if (ag && ag.mode === 'PAPER-SHADOW') {
      msg = 'Terminal MT5 terputus — agent tetap trading di buku bayangan (PAPER-SHADOW) ' +
            'sampai terminal kembali.';
    }
    if (!msg) { bar.hidden = true; bar.textContent = ''; return; }
    bar.hidden = false;
    bar.textContent = msg;
  }

  function renderSecurity(sec) {
    const a = sec.agent;
    const st = $('secStatus');
    st.textContent = a.status;
    st.className = 'status' + (a.intrusions ? ' warn' : '');
    $('secIntegrity').textContent = a.integrity_ok ? 'SEALED' : 'BROKEN';
    $('secIntegrity').style.color = a.integrity_ok ? 'var(--green)' : 'var(--red)';
    $('secWaf').textContent = a.waf_sensitivity.toFixed(2) + 'x';
    $('secReq').textContent = a.requests.toLocaleString('en-US');

    const order = sec.tier_order || ['BLOCKED', 'BIASA', 'ADMIN', 'DIREKTUR'];
    const byTier = {};
    order.forEach(k => byTier[k] = []);
    (sec.entities || []).forEach(e => { (byTier[e.tier] = byTier[e.tier] || []).push(e); });
    const labels = { BLOCKED: 'BLOKIR', BIASA: 'BIASA', ADMIN: 'ADMIN', DIREKTUR: 'DIREKTUR' };
    const myTier = (state && state.operator && state.operator.tier) || 'BIASA';
    const myLevel = (state && state.operator && state.operator.level) || 1;
    $('ladder').innerHTML = order.map((tier, i) => {
      const list = byTier[tier] || [];
      const chips = list.slice(0, 5).map(e => {
        const c = tier === 'BLOCKED' ? 'blocked' : tier === 'DIREKTUR' ? 'director' : tier === 'ADMIN' ? 'admin' : '';
        const menu = myLevel >= 2 ? `
          <span class="dot-menu">
            <button class="dot-menu-btn" title="aksi">&#8942;</button>
            <div class="dot-menu-list">
              ${tier === 'BLOCKED' ? `<button data-eid="${e.eid}" data-to="BIASA">Buka blokir → BIASA</button>` : ''}
              ${myTier === 'DIREKTUR' && tier !== 'DIREKTUR' ? `
                <button data-eid="${e.eid}" data-to="BIASA">Turunkan → BIASA</button>
                <button data-eid="${e.eid}" data-to="ADMIN">Naikkan → ADMIN</button>
                ${tier !== 'DIREKTUR' ? `<button data-eid="${e.eid}" data-to="DIREKTUR">Naikkan → DIREKTUR</button>` : ''}
              ` : ''}
              ${myTier === 'ADMIN' && tier === 'BIASA' ? `
                <button data-eid="${e.eid}" data-to="ADMIN">Naikkan → ADMIN</button>
              ` : ''}
            </div>
          </span>` : '';
        return `<span class="entity ${c}" title="${e.ip} · skor ${e.score} · ${e.block_reason || 'aktif'}">${e.label}${menu}</span>`;
      }).join('');
      const more = list.length > 5 ? `<span class="entity">+${list.length - 5}</span>` : '';
      return `<div class="rung t${i}">
        <span class="lvl">${labels[tier]}</span>
        <div class="who-list">${chips}${more}</div>
        <span class="cnt">${list.length}</span>
      </div>`;
    }).join('');
    if (myLevel >= 2) attachDotMenus();
    document.querySelectorAll('.dot-menu-list button[data-eid]').forEach(btn => {
      btn.onclick = async (ev) => {
        ev.stopPropagation();
        const eid = btn.dataset.eid, to = btn.dataset.to;
        const code = to === 'DIREKTUR' ? prompt('Masukkan kode konfirmasi DIREKTUR:') || '' : '';
        try {
          await post('/api/security/act', { target: eid, tier: to, reason: 'Manual via dashboard', code });
          toast(`Tier ${eid} diubah ke ${to}`);
        } catch (e) { toast('Gagal: ' + e.message, 'bad'); }
      };
    });

    $('canaryTimer').textContent = `rotasi ${a.next_rotation_s}s · ${a.canaries} aktif`;
    $('canaryList').innerHTML = (sec.canary_links || []).map(c =>
      `<div class="canary-row"><code>${c.path}</code><span>${c.age_s}s</span></div>`).join('') ||
      '<div class="canary-row"><span>—</span></div>';

    $('healList').innerHTML = (sec.heals || []).slice(0, 6).map(h =>
      `<div class="heal"><b>${h.action}</b><span>${h.detail}</span></div>`).join('') ||
      '<div class="heal"><b>STANDBY</b><span>tidak ada remediasi aktif</span></div>';
  }

  function renderMiniLadder(sec) {
    const order = sec.tier_order || [];
    const counts = order.map(t => (sec.entities || []).filter(e => e.tier === t).length);
    const max = Math.max(1, ...counts);
    const colors = ['#ff4d67', '#5b7089', '#2ee6c8', '#ffcb6b'];
    $('miniLadder').innerHTML = counts.map((c, i) =>
      `<i style="background:${colors[i]};opacity:${0.25 + 0.75 * (c / max)}"></i>`).join('');
  }

  function renderThreats(sec) {
    const rows = sec.threats || [];
    $('threats').innerHTML = rows.slice(0, 12).map(t => `
      <div class="threat ${t.severity}">
        <span class="t-time">${hhmmss(t.ts)}</span>
        <span class="t-kind">${t.kind}<span class="t-ip">${t.ip} · ${t.path || ''}</span></span>
        <span class="t-act ${t.action}">${t.action}</span>
      </div>`).join('') || '<div class="empty">Belum ada anomali terdeteksi.</div>';
  }

  function renderAudit(sec) {
    $('auditState').textContent = sec.agent.integrity_ok ? 'hash-chain valid' : 'chain rusak';
    $('auditState').style.color = sec.agent.integrity_ok ? 'var(--green)' : 'var(--red)';
    $('audit').innerHTML = (sec.audit_tail || []).slice(0, 7).map(e => `
      <div class="audit-row">
        <b>${e.action}</b><em>${e.actor}</em>
        <span>${e.target} · ${e.detail}</span>
      </div>`).join('');
  }

  function alerts(sec) {
    if (!prevBlocked) { prevBlocked = sec.stats.blocked; prevThreat = (sec.threats[0] || {}).ts || 0; return; }
    if (sec.stats.blocked > prevBlocked) {
      toast(`INTRUSION BLOCKED · total ${sec.stats.blocked} identitas diblokir`, 'bad');
      document.body.classList.add('shake');
      setTimeout(() => document.body.classList.remove('shake'), 480);
    }
    prevBlocked = sec.stats.blocked;
    const latest = (sec.threats[0] || {}).ts || 0;
    if (latest > prevThreat) {
      const t = sec.threats[0];
      if (t.action !== 'BLOCK') toast(`Threat scored: ${t.kind}`, 'gold');
      prevThreat = latest;
    }
  }

  /* ------------------------------------------------ trading */
  function renderAccount(t, acc, ag) {
    $('chartVenue').textContent = `· ${ag.mode}${ag.paused ? ' · PAUSED' : ''}${ag.killed ? ' · HALTED' : ''}`;
    $('roBalance').textContent = money(acc.balance);
    $('roUnreal').textContent = signed(acc.unrealized);
    $('roUnreal').style.color = (acc.unrealized || 0) >= 0 ? 'var(--green)' : 'var(--red)';
    $('roMargin').textContent = money(acc.margin_used);
    const tr = t.trades || [];
    const wins = tr.filter(x => (x.pnl || 0) > 0).length;
    $('roWin').textContent = tr.length ? Math.round(wins / tr.length * 100) + '%' : '—';
    $('posCount').textContent = `${t.positions.length} / ${ag.max_open}`;
  }

  function renderPositions(list) {
    const tb = $('posTable').querySelector('tbody');
    if (!list.length) {
      tb.innerHTML = '';
      $('posEmpty').classList.remove('hide');
      return;
    }
    $('posEmpty').classList.add('hide');
    tb.innerHTML = list.map(p => `
      <tr>
        <td class="sym">${p.symbol}</td>
        <td class="${p.side}">${p.side.toUpperCase()}</td>
        <td>${num(p.lots, 2)}</td>
        <td>${p.entry}</td>
        <td>${p.price}</td>
        <td class="${cls(p.r)}">${(p.r >= 0 ? '+' : '') + num(p.r, 2)}R</td>
        <td class="${cls(p.pnl)}">${signed(p.pnl)}</td>
        <td style="color:var(--dim)">${p.strategy}</td>
      </tr>`).join('');
  }

  function renderSignals(list) {
    $('signals').innerHTML = (list || []).slice(0, 6).map(s => `
      <div class="sig ${s.status.toLowerCase()}">
        <span class="s-side ${s.side}">${s.side.toUpperCase()}</span>
        <span style="color:#dff4ff">${s.symbol}</span>
        <span class="s-reason">${s.reason}</span>
        <span class="s-note">${s.note || ''}</span>
        <span class="s-badge ${s.status.toLowerCase()}">${s.status}</span>
      </div>`).join('') ||
      '<div class="empty">Agent memindai struktur pasar setiap bar baru…</div>';
  }

  /* ------------------------------------------------ matrix */
  function renderMatrix(groups) {
    const syms = (groups[0] && groups[0].symbols || []).map(s => s.symbol);
    let html = `<div class="mrow"><div class="mhead name">STRATEGI / INSTRUMEN</div>` +
      syms.map(s => `<div class="mhead">${s}</div>`).join('') + `</div>`;
    (groups || []).forEach(g => {
      html += `<div class="mrow"><div class="mname">${g.label}<span>${g.family}</span></div>`;
      g.symbols.forEach(s => {
        const st = s.status || 'PENDING';
        const c = st === 'SIM-VALIDATED' || st === 'LIVE-ELIGIBLE' ? 'ok'
          : st === 'PROBATION' ? 'prob' : st === 'FAILED' ? 'bad' : 'wait';
        const live = s.tradeable && st !== 'PENDING';
        html += `<div class="cell ${c} ${live ? 'live' : ''}" data-k="${g.key}" data-s="${s.symbol}"
            title="${st} · skor ${s.score} · PF ${s.metrics.pf || '—'}">
          <div class="c-score">${st === 'PENDING' ? '—' : Math.round(s.score)}</div>
          <div class="c-meta">${s.metrics.pf ? 'PF ' + s.metrics.pf : (st === 'PENDING' ? 'queued' : 'fail')}</div>
        </div>`;
      });
      html += `</div>`;
    });
    $('matrix').innerHTML = html;
  }

  /* ------------------------------------------------ broker universe */
  function renderUniverse(ag) {
    const bar = $('uniBar');
    if (!ag) { bar.textContent = '—'; return; }
    const map = ag.broker_map || {};
    const blocked = ag.blocked_by || {};
    const caps = ag.max_per_class || {};
    const syms = ag.universe || [];
    const chips = syms.map(s => {
      const b = map[s];
      const alias = b && b !== s ? ` &rarr; ${b}` : '';
      const why = blocked[s];
      const title = why || (b ? 'broker: ' + b : 'belum dipetakan ke broker');
      return `<span class="uni ${why ? 'off' : ''}" title="${title}"><b>${s}</b>${alias}` +
        (why ? `<span class="why">${why}</span>` : '') + `</span>`;
    }).join('');
    const capTxt = Object.keys(caps).map(k => `${k} maks ${caps[k]}`).join(' · ');
    const note = ag.universe_note || '';
    bar.innerHTML = `<span class="ul">INSTRUMEN BROKER</span>${chips}` +
      (capTxt ? `<span class="uni cap">${capTxt}</span>` : '') +
      (note ? `<span class="ul note">${note}</span>` : '');
  }

  /* ------------------------------------------------ ticker */  function renderTicker(log) {
    if (!log || !log.length) return;
    const items = log.slice(0, 14).map(l => `<span><b>${l.level}</b>${l.msg}</span>`).join('');
    $('ticker').innerHTML = items + items;
  }

  /* ------------------------------------------------ charts */
  function fit(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 300, h = canvas.clientHeight || 150;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function drawChart(eq, pnl) {
    const canvas = $('pnlChart');
    const { ctx, w, h } = fit(canvas);
    const padL = 8, padR = 56, padT = 12, padB = 18;
    const iw = w - padL - padR, ih = h - padT - padB;

    ctx.strokeStyle = 'rgba(70,220,205,.08)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = padT + ih / 4 * i;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + iw, y); ctx.stroke();
    }

    const pts = (eq || []).map(p => p[1]);
    if (pts.length < 2) {
      ctx.fillStyle = 'rgba(111,130,152,.9)';
      ctx.font = '11px ui-monospace, monospace';
      ctx.fillText('menunggu data pasar…', padL + 8, padT + ih / 2);
      return;
    }
    let min = Math.min(...pts), max = Math.max(...pts);
    if (max - min < 1) { const m = (max + min) / 2; min = m - 40; max = m + 40; }
    const pad = (max - min) * 0.18;
    min -= pad; max += pad;
    const x = i => padL + iw * (i / (pts.length - 1));
    const y = v => padT + ih - ih * ((v - min) / (max - min));

    // pnl line (secondary, 0-centred)
    const ps = (pnl || []).map(p => p[1]);
    if (ps.length > 1) {
      const pmax = Math.max(1, ...ps.map(Math.abs));
      ctx.beginPath();
      ps.forEach((v, i) => {
        const yy = padT + ih / 2 - (v / pmax) * (ih / 2) * 0.8;
        i ? ctx.lineTo(x(i), yy) : ctx.moveTo(x(i), yy);
      });
      ctx.strokeStyle = 'rgba(167,139,250,.55)';
      ctx.lineWidth = 1.1;
      ctx.setLineDash([3, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // equity area
    const grad = ctx.createLinearGradient(0, padT, 0, padT + ih);
    grad.addColorStop(0, 'rgba(46,230,200,.28)');
    grad.addColorStop(1, 'rgba(46,230,200,0)');
    ctx.beginPath();
    pts.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
    ctx.lineTo(x(pts.length - 1), padT + ih);
    ctx.lineTo(x(0), padT + ih);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    pts.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
    ctx.strokeStyle = '#2ee6c8';
    ctx.lineWidth = 1.6;
    ctx.shadowColor = 'rgba(46,230,200,.7)';
    ctx.shadowBlur = 10;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // last point
    const lx = x(pts.length - 1), ly = y(pts[pts.length - 1]);
    ctx.beginPath(); ctx.arc(lx, ly, 3, 0, Math.PI * 2);
    ctx.fillStyle = '#eafffb'; ctx.fill();
    ctx.beginPath(); ctx.arc(lx, ly, 7, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(46,230,200,.4)'; ctx.stroke();

    // axis labels
    ctx.fillStyle = 'rgba(111,130,152,.85)';
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillText(money(max, 0), padL + iw + 6, padT + 8);
    ctx.fillText(money(min, 0), padL + iw + 6, padT + ih);
    ctx.fillText(money(pts[pts.length - 1]), padL + iw + 6, ly + 3);
    ctx.fillStyle = 'rgba(46,230,200,.9)';
    ctx.fillText('equity', padL, h - 4);
  }

  function spark(id, values, rgb) {
    const canvas = $(id);
    if (!canvas) return;
    const { ctx, w, h } = fit(canvas);
    if (!values || values.length < 2) return;
    const min = Math.min(...values), max = Math.max(...values);
    const span = (max - min) || 1;
    ctx.beginPath();
    values.forEach((v, i) => {
      const x = (w * i) / (values.length - 1);
      const y = h - 3 - (h - 6) * ((v - min) / span);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle = `rgba(${rgb},.85)`;
    ctx.lineWidth = 1.2;
    ctx.stroke();
    ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
    ctx.fillStyle = `rgba(${rgb},.10)`;
    ctx.fill();
  }

  /* ------------------------------------------------ modal */
  function openPair(key, symbol) {
    if (!state) return;
    const g = state.trading.strategies.find(x => x.key === key);
    if (!g) return;
    const s = g.symbols.find(x => x.symbol === symbol);
    if (!s) return;
    $('modalTitle').textContent = `${g.label} · ${symbol}`;
    if (!s.checks || !s.checks.length) {
      $('modalBody').innerHTML = `<div class="empty">Belum diuji. Jalankan validasi untuk pasangan ini.</div>`;
    } else {
      const m = s.metrics;
      $('modalBody').innerHTML = `
        <div class="mstats">
          <div class="mstat"><span>STATUS</span><b style="font-size:11px;color:${s.tradeable ? 'var(--green)' : 'var(--amber)'}">${s.status}</b></div>
          <div class="mstat"><span>SKOR GATE</span><b>${Math.round(s.score)}<span style="font-size:9px;color:var(--dim)">/100</span></b></div>
          <div class="mstat"><span>PROFIT FACTOR</span><b>${num(m.pf, 2)}</b></div>
          <div class="mstat"><span>SHARPE</span><b>${num(m.sharpe, 2)}</b></div>
          <div class="mstat"><span>MAX DD</span><b>${num(m.dd, 1)}%</b></div>
          <div class="mstat"><span>TRADES</span><b>${m.trades}</b></div>
          <div class="mstat"><span>WIN RATE</span><b>${num(m.win_rate, 1)}%</b></div>
          <div class="mstat"><span>EXPECTANCY</span><b>${num(m.expectancy, 2)}R</b></div>
        </div>
        <div class="gate-grid">
          ${s.checks.map(c => `<div class="gate ${c.pass ? 'pass' : 'fail'}">
            <span>${c.label}</span>
            <div><b>${c.value}</b> <em>${c.pass ? 'PASS' : 'FAIL'}</em></div>
          </div>`).join('')}
        </div>
        <div class="panel-head" style="padding:12px 0 6px;border:none"><h3>WALK-FORWARD 4 FOLD</h3></div>
        <div class="folds">
          ${(s.folds || []).map(f => `<div class="fold ${f.net_r > 0 && f.profit_factor > 1 ? 'ok' : 'bad'}">
            <b>${f.net_r >= 0 ? '+' : ''}${f.net_r}R</b>fold ${f.fold} · PF ${f.profit_factor}</div>`).join('') ||
            '<div class="fold bad"><b>—</b>data tidak cukup</div>'}
        </div>
        <div class="panel-head" style="padding:12px 0 6px;border:none"><h3>MONTE CARLO</h3></div>
        <div class="mstats" style="grid-template-columns:repeat(4,1fr);margin:0">
          <div class="mstat"><span>P(PROFIT)</span><b>${num((s.mc || {}).prob_profit * 100, 1)}%</b></div>
          <div class="mstat"><span>MEDIAN</span><b>${num((s.mc || {}).median_total_r, 1)}R</b></div>
          <div class="mstat"><span>P05</span><b>${num((s.mc || {}).p05_total_r, 1)}R</b></div>
          <div class="mstat"><span>P95 DD</span><b>${num((s.mc || {}).p95_max_dd_pct, 1)}%</b></div>
        </div>
        <div style="display:flex;gap:8px;margin-top:14px">
          <button class="btn" id="modalRerun">Uji Ulang Pasangan Ini</button>
          <button class="btn ghost" id="modalClose2">Tutup</button>
        </div>`;
      $('modalRerun').onclick = () => rerun(key, symbol);
      $('modalClose2').onclick = closeModal;
    }
    $('modal').classList.add('open');
  }
  function closeModal() { $('modal').classList.remove('open'); }

  /* ------------------------------------------------ actions */
  async function whoami() {
    try {
      const r = await fetch('/api/whoami');
      if (!r.ok) return null;
      const d = await r.json();
      return d.operator || null;
    } catch (e) { return null; }
  }

  async function post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    let data = {};
    try { data = await r.json(); } catch (e) { }
    if (r.status === 401) {
      // no session: the gate page is the only way in, by design
      location.replace('/');
      throw new Error('sesi berakhir');
    }
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  }

  async function boot() {
    // register the offline shell first so a reload with no server still works
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(() => null);
    }

    const op = await whoami();
    if (!op) {
      // whoami failed: could be no session OR the agent is down.
      // if we have a snapshot, show it stale instead of bouncing to a dead gate.
      const snap = loadSnapshot();
      if (snap) {
        authed = true;
        paintTier((snap.data && snap.data.operator) || { tier: 'OFFLINE', level: 0 });
        apply(snap.data);
        showStale(snap);
        setConn(false, 'OFFLINE');
        return;
      }
      location.replace('/');
      return;
    }
    authed = true;
    paintTier(op);
    connect();
  }

  async function rerun(key, symbol) {
    try {
      toast(`Menjalankan ulang gate ${key}/${symbol}…`);
      const d = await post('/api/validate', { strategy: key, symbol });
      const st = d.result.status;
      toast(`Gate selesai: ${st} (skor ${Math.round(d.result.score)})`,
        st === 'SIM-VALIDATED' || st === 'LIVE-ELIGIBLE' ? '' : 'gold');
    } catch (e) { toast('Gagal: ' + e.message, 'bad'); }
  }

  function toast(msg, kind) {
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    $('toasts').appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .4s'; }, 3800);
    setTimeout(() => el.remove(), 4300);
  }

  /* ------------------------------------------------ wire up */
  const out = $('btnLogout');
  if (out) out.onclick = async () => {
    try { await fetch('/api/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
    location.replace('/');
  };

  $('btnAttack').onclick = async () => {
    const kind = $('attackKind').value;
    try {
      const d = await post('/api/security/attack', { kind });
      const r = d.result;
      toast(`${kind.toUpperCase()} → ${r.decision.reason}`, r.decision.blocked ? 'bad' : 'gold');
      if (r.decision.blocked) {
        document.body.classList.add('shake');
        setTimeout(() => document.body.classList.remove('shake'), 480);
      }
    } catch (e) { toast('Ditolak: ' + e.message, 'bad'); }
  };

  $('btnPause').onclick = async () => {
    const paused = state && state.trading.agent.paused;
    try {
      await post('/api/trading/control', { action: paused ? 'resume' : 'pause' });
      toast(paused ? 'Trading dilanjutkan' : 'Trading dijeda');
    } catch (e) { toast('Ditolak: ' + e.message, 'bad'); }
  };

  $('btnKill').onclick = async () => {
    if (!confirm('KILL SWITCH: tutup semua posisi dan blokir entry baru?')) return;
    try {
      await post('/api/trading/control', { action: 'kill' });
      toast('KILL SWITCH aktif — semua posisi ditutup', 'bad');
    } catch (e) { toast('Ditolak: ' + e.message, 'bad'); }
  };

  $('btnRevalidate').onclick = async () => {
    try {
      await post('/api/trading/control', { action: 'revalidate' });
      toast('Validasi ulang seluruh pasangan dijalankan');
    } catch (e) { toast('Ditolak: ' + e.message, 'bad'); }
  };

  $('matrix').addEventListener('click', (ev) => {
    const cell = ev.target.closest('.cell');
    if (cell) openPair(cell.dataset.k, cell.dataset.s);
  });
  $('modalClose').onclick = closeModal;
  $('modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

  setInterval(() => {
    const d = new Date();
    $('clock').textContent = d.toLocaleTimeString('id-ID', { hour12: false });
  }, 1000);

  window.addEventListener('resize', () => {
    if (!state) return;
    drawChart(state.trading.equity, state.trading.pnl);
    spark('sparkEquity', (state.trading.equity || []).map(p => p[1]), '46,230,200');
    spark('sparkPnl', (state.trading.pnl || []).map(p => p[1]), '167,139,250');
  });

  /* ---------------- tier-based panels ---------------- */
  function setPanelVisibility(op) {
    const tier = op ? op.tier : 'BIASA';
    const level = op ? op.level : 1;
    $('panelDirector').hidden = tier !== 'DIREKTUR';
    $('panelAppeal').hidden = tier !== 'BIASA';
    $('panelAppeals').hidden = level < 2;
    // BIASA: hide trading data
    const kpis = document.querySelector('.kpis');
    const chartPanel = document.querySelector('.chart-panel');
    const posPanel = document.getElementById('posTable').closest('.panel');
    const sigPanel = document.getElementById('signals').closest('.panel');
    if (tier === 'BIASA') {
      if (kpis) kpis.style.display = 'none';
      if (chartPanel) chartPanel.style.display = 'none';
      if (posPanel) posPanel.style.display = 'none';
      if (sigPanel) sigPanel.style.display = 'none';
      document.getElementById('secAgent').style.display = 'none';
    } else {
      if (kpis) kpis.style.display = '';
      if (chartPanel) chartPanel.style.display = '';
      if (posPanel) posPanel.style.display = '';
      if (sigPanel) sigPanel.style.display = '';
      document.getElementById('secAgent').style.display = '';
    }
  }

  /* ---------------- director controls ---------------- */
  async function setLot(klass, inputId) {
    const v = parseFloat($(inputId).value);
    try {
      const d = await post('/api/trading/lot', { klass, lots: isNaN(v) || v <= 0 ? null : v });
      toast(`Lot ${klass} = ${d.result.lots || 'auto'}`);
    } catch (e) { toast('Gagal: ' + e.message, 'bad'); }
  }
  async function setTf(klass, selectId) {
    const v = $(selectId).value;
    try {
      const d = await post('/api/trading/timeframe', { klass, timeframe: v });
      toast(`Timeframe ${klass} = ${d.result.timeframe || 'default'}`);
    } catch (e) { toast('Gagal: ' + e.message, 'bad'); }
  }
  $('btnLotForex').onclick = () => setLot('FOREX', 'lotForex');
  $('btnLotCommodity').onclick = () => setLot('COMMODITY', 'lotCommodity');
  $('btnTfForex').onclick = () => setTf('FOREX', 'tfForex');
  $('btnTfCommodity').onclick = () => setTf('COMMODITY', 'tfCommodity');

  /* ---------------- appeal ---------------- */
  $('btnAppeal').onclick = async () => {
    const target = $('appealTarget').value;
    const reason = $('appealReason').value;
    try {
      const d = await post('/api/appeal', { target_tier: target, reason });
      $('appealStatus').textContent = 'Banding terkirim! ID: ' + d.appeal.id;
      toast('Banding terkirim ke ' + target);
    } catch (e) { toast('Gagal: ' + e.message, 'bad'); }
  };

  async function loadAppeals() {
    if (!state || !state.operator || state.operator.level < 2) return;
    try {
      const d = await (await fetch('/api/appeal/list')).json();
      if (!d.ok) return;
      $('appealCount').textContent = d.appeals.length;
      $('appealsList').innerHTML = d.appeals.map(a => `
        <div class="appeal-row">
          <div class="appeal-info">${a.eid} → ${a.to_tier}<br><small>${a.reason}</small></div>
          <div class="appeal-actions">
            <button class="btn" data-aid="${a.id}" data-act="1">Setuju</button>
            <button class="btn ghost" data-aid="${a.id}" data-act="0">Tolak</button>
          </div>
        </div>`).join('');
      document.querySelectorAll('#appealsList .btn[data-aid]').forEach(b => {
        b.onclick = async () => {
          const aid = b.dataset.aid, act = b.dataset.act === '1';
          try {
            await post('/api/appeal/approve', { id: aid, approve: act });
            toast(act ? 'Banding disetujui' : 'Banding ditolak');
            loadAppeals();
          } catch (e) { toast('Gagal: ' + e.message, 'bad'); }
        };
      });
    } catch (e) { /* ignore */ }
  }

  /* ---------------- 3-dot menu in security ---------------- */
  function attachDotMenus() {
    document.querySelectorAll('.dot-menu-btn').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const list = btn.nextElementSibling;
        document.querySelectorAll('.dot-menu-list').forEach(l => { if (l !== list) l.classList.remove('open'); });
        list.classList.toggle('open');
      };
    });
  }
  document.addEventListener('click', () => {
    document.querySelectorAll('.dot-menu-list').forEach(l => l.classList.remove('open'));
  });

  /* ---------------- override apply for tier panels ---------------- */
  const _origApply = apply;
  apply = function(s) {
    _origApply(s);
    setPanelVisibility(s.operator);
    if (s.operator && s.operator.level >= 2) loadAppeals();
  };

  boot();
})();
