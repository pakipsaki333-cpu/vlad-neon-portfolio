/* NeonMiner.exe — клиентская часть (зашита внутрь exe).
 * Общается только с локальным сервером 127.0.0.1 и read-only API пулов.
 */
'use strict';
const $ = id => document.getElementById(id);
const state = { catalog: [], pool: 'c3pool', wallet: '', view: null, calc: null, config: null, running: false, starting: false };

async function api(path, options = {}, timeoutMs = 30000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const opts = { method: options.method || 'GET', signal: ctrl.signal, headers: { Accept: 'application/json' } };
  if (options.body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(options.body); }
  try {
    const res = await fetch(path, opts);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  } finally { clearTimeout(timer); }
}
function toast(t) { const e = $('toast'); e.textContent = t; e.classList.add('show'); clearTimeout(e._t); e._t = setTimeout(() => e.classList.remove('show'), 2400); }
async function copy(t, l) { try { await navigator.clipboard.writeText(t); toast(l + ' скопировано'); } catch (e) { toast('Выдели текст вручную'); } }
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, d = 6) => (Number.isFinite(+v) ? (+v).toFixed(d) : '0.' + '0'.repeat(d));
function fmtHash(h) { const v = +h || 0; return v >= 1e9 ? (v / 1e9).toFixed(2) + ' GH/s' : v >= 1e6 ? (v / 1e6).toFixed(2) + ' MH/s' : v >= 1e3 ? (v / 1e3).toFixed(2) + ' kH/s' : v.toFixed(0) + ' H/s'; }
function fmtEur(v) { return (+v || 0).toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' €'; }
function fmtDate(ts) { if (!ts) return '—'; const d = new Date(ts * 1000); return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' }); }
function ago(ts) { if (!ts) return 'никогда'; const s = Math.max(0, Math.floor(Date.now() / 1000) - ts); return s < 60 ? `${s} с назад` : s < 3600 ? `${Math.floor(s / 60)} мин назад` : s < 86400 ? `${Math.floor(s / 3600)} ч назад` : `${Math.floor(s / 86400)} дн назад`; }
function fmtDays(d) { if (!Number.isFinite(d) || d <= 0) return '—'; return d < 1 ? `${Math.round(d * 24)} ч` : d < 60 ? `${d.toFixed(1)} дн` : d < 730 ? `${(d / 30).toFixed(1)} мес` : `${(d / 365).toFixed(1)} лет`; }

/* ----------------------------- каталог ----------------------------- */
function curPool() { return state.catalog.find(p => p.id === state.pool) || null; }
function renderPoolList() {
  $('poolList').innerHTML = state.catalog.map(p => `<div class="pool-item ${p.id === state.pool ? 'active' : ''}" data-pool="${p.id}">
    <div class="pool-top"><span class="pool-name">${esc(p.name)}${p.recommended ? '<span class="badge-rec">КАПАЕТ БЫСТРЕЕ</span>' : ''}</span><span class="pool-fee">${p.fee_pct.toFixed(2)}%</span></div>
    <div class="pool-tag">${esc(p.tagline)}</div></div>`).join('');
  document.querySelectorAll('.pool-item').forEach(el => el.addEventListener('click', () => { state.pool = el.dataset.pool; $('port').value = ''; renderPoolList(); renderPorts(); genConfig(); refreshStats(); }));
}
function renderPorts() {
  const p = curPool(); if (!p) return;
  const defaults = { c3pool: 13333, hashvault: 443, supportxmr: 3333, moneroocean: 10128 };
  $('port').innerHTML = p.ports.map(x => `<option value="${x.port}">${x.port}${x.tls ? ' · TLS' : ''} — ${esc(x.label)}</option>`).join('');
  $('port').value = p.ports.some(x => x.port === defaults[p.id]) ? defaults[p.id] : p.ports[0].port;
  $('poolNotes').innerHTML = `<ul>${(p.notes || []).map(n => `<li>${esc(n)}</li>`).join('')}</ul>`;
}

/* ----------------------------- кошелёк ----------------------------- */
let vTimer = null;
async function onWallet() {
  clearTimeout(vTimer);
  const raw = $('wallet').value.trim(); state.wallet = '';
  const box = $('validator'); box.className = 'validator';
  $('validatorText').textContent = 'Проверяю…';
  $('startBtn').disabled = true;
  if (!raw) { $('validatorText').textContent = 'Жду адрес'; persist(''); genConfig(); refreshStats(); return; }
  vTimer = setTimeout(async () => {
    try {
      const i = await api('/api/validate?address=' + encodeURIComponent(raw));
      if (i.valid) {
        state.wallet = i.address; box.className = 'validator ok';
        $('validatorText').textContent = `✓ валиден · ${i.kind} · mainnet`;
        $('walletHint').textContent = i.warning || '';
        persist(i.address); genConfig(); refreshStats(); syncStartButton();
      } else {
        box.className = 'validator bad'; $('validatorText').textContent = '✗ ' + i.reason;
        $('walletHint').textContent = 'Исправь адрес — иначе пул не начислит монеты.';
        $('configBadge').textContent = 'адрес невалиден'; genConfig(); refreshStats();
      }
    } catch (e) { box.className = 'validator bad'; $('validatorText').textContent = 'Ошибка: ' + e.message; }
  }, 250);
}
function persist(v) { try { v && $('remember').checked ? localStorage.setItem('nm.wallet', v) : localStorage.removeItem('nm.wallet'); } catch (e) {} }

/* ----------------------------- статистика ----------------------------- */
const PICO = 1e12;
const nz = v => (v === false || v === null || v === undefined) ? 0 : (+v || 0);
function normPayments(poolId, raw) {
  if (!Array.isArray(raw)) return [];
  return raw.filter(x => x && typeof x === 'object').map(x => ({
    ts: +nz(x.ts || x.timestamp), amount_xmr: nz(x.amount) / PICO,
    txid: String(x.txnHash || x.txn_hash || '')
  })).sort((a, b) => b.ts - a.ts).slice(0, 25);
}
function normalize(pool, s, pays, ps) {
  s = s || {}; ps = ps || {};
  const v = {
    pool: pool.id, pool_name: pool.name, fee_pct: pool.fee_pct,
    threshold_xmr: pool.min_payout_xmr, auto_threshold_xmr: pool.default_payout_xmr,
    hashrate_hs: 0, hashrate_24h: null, balance_xmr: 0, paid_xmr: 0,
    valid_shares: 0, invalid_shares: 0, total_hashes: 0, last_share_ts: null,
    payments: normPayments(pool.id, pays), price_usd: null, price_eur: null, price_rub: null,
    network_hashrate_hs: null, block_reward_xmr: null, pool_hashrate_hs: null, demo: false
  };
  if (pool.api_kind === 'hashvault') {
    const c = s.collective || {}, r = s.revenue || {};
    v.hashrate_hs = nz(c.hashRate); v.hashrate_24h = nz(c.avg24hashRate) || null;
    v.balance_xmr = nz(r.confirmedBalance) / PICO; v.paid_xmr = nz(r.totalPaid) / PICO;
    v.valid_shares = nz(c.validShares); v.invalid_shares = nz(c.invalidShares);
    v.total_hashes = nz(c.totalHashes);
    v.last_share_ts = nz(c.lastShare) ? Math.floor(nz(c.lastShare) / 1000) : null;
    if (r.payoutThreshold) v.threshold_xmr = nz(r.payoutThreshold) / PICO;
    if (ps.pool_statistics && ps.pool_statistics.collective) v.pool_hashrate_hs = nz(ps.pool_statistics.collective.hashRate);
    const nw = ps.network_statistics || {};
    if (nw.difficulty) v.network_hashrate_hs = nz(nw.difficulty) / 120;
    if (nw.value) v.block_reward_xmr = nz(nw.value) / PICO;
    const m = ps.market || {};
    v.price_usd = m.price_usd || null; v.price_eur = m.price_eur || null; v.price_rub = m.price_rub || null;
  } else {
    v.hashrate_hs = nz(s.hash); v.balance_xmr = nz(s.amtDue) / PICO; v.paid_xmr = nz(s.amtPaid) / PICO;
    v.valid_shares = nz(s.validShares); v.invalid_shares = nz(s.invalidShares);
    v.total_hashes = nz(s.totalHashes); v.last_share_ts = nz(s.lastHash) || null;
    const st = ps.pool_statistics || {};
    v.pool_hashrate_hs = nz(st.hashRate) || null;
    const pr = ps.price || {}; v.price_usd = pr.usd || null; v.price_eur = pr.eur || null;
  }
  v.total_xmr = v.balance_xmr + v.paid_xmr;
  v.progress_pct = Math.min(100, 100 * v.balance_xmr / Math.max(v.threshold_xmr, 1e-9));
  return v;
}
async function raw(poolId, kind, wallet) {
  const r = await api(`/api/raw?pool=${encodeURIComponent(poolId)}&kind=${kind}&wallet=${encodeURIComponent(wallet || '')}`, {}, 20000);
  if (!r.ok) throw new Error(r.error || 'proxy error');
  return typeof r.data === 'string' ? JSON.parse(r.data) : r.data;
}
async function direct(url, ms = 7000) {
  const c = new AbortController(); const t = setTimeout(() => c.abort(), ms);
  try { const r = await fetch(url, { signal: c.signal, headers: { Accept: 'application/json' } }); if (!r.ok) throw new Error('HTTP ' + r.status); return await r.json(); }
  finally { clearTimeout(t); }
}
function directUrls(pool, wallet) {
  const w = encodeURIComponent(wallet), b = pool.api_base;
  return { stats: b + pool.stats_path.replace('{wallet}', w), payments: b + pool.payments_path.replace('{wallet}', w), pool: b + pool.pool_path };
}
async function refreshStats() {
  const pool = curPool(); if (!pool) return;
  $('liveState').textContent = 'загружаю…';
  let v = null, mode = '';
  if (state.wallet) {
    // 1) локальный прокси exe (WinHTTP) — нет проблем с CORS
    try {
      const [s, p, ps, mkt] = await Promise.all([
        raw(pool.id, 'stats', state.wallet),
        raw(pool.id, 'payments', state.wallet).catch(() => []),
        raw(pool.id, 'pool', state.wallet).catch(() => ({})),
        pool.id === 'hashvault' ? Promise.resolve(ps_fallback()) : raw('hashvault', 'market').catch(() => ({}))
      ]);
      v = normalize(pool, s, p, ps); enrich(v, mkt); mode = 'сервер → пул';
    } catch (e) {
      // 2) напрямую из браузера
      try {
        const u = directUrls(pool, state.wallet);
        const [s, p, ps] = await Promise.all([direct(u.stats), direct(u.payments).catch(() => []), direct(u.pool).catch(() => ({}))]);
        v = normalize(pool, s, p, ps); mode = 'браузер → пул';
      } catch (e2) { v = null; }
    }
  }
  if (!v) { v = await api(`/api/stats?pool=${pool.id}&demo=1`).catch(() => null); mode = 'демо'; }
  if (!v) return;
  v.source = mode; state.view = v; render(); await computeCalc();
}
function ps_fallback() { return {}; }
function enrich(v, mkt) {
  if (!mkt || !mkt.market) return;
  v.price_eur = v.price_eur || mkt.market.price_eur || null;
  v.price_usd = v.price_usd || mkt.market.price_usd || null;
  v.price_rub = mkt.market.price_rub || null;
  const nw = mkt.network_statistics || {};
  if (!v.network_hashrate_hs && nw.difficulty) v.network_hashrate_hs = nw.difficulty / 120;
  if (!v.block_reward_xmr && nw.value) v.block_reward_xmr = nw.value / 1e12;
}
function render() {
  const v = state.view, p = curPool(); if (!v) return;
  $('dashPool').textContent = `${v.pool_name} · ${p ? p.api_base : ''}`;
  $('liveState').textContent = v.demo ? 'демо' : `LIVE · ${v.source} · ${new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`;
  $('demoBanner').classList.toggle('hidden', !v.demo);
  if (v.demo) $('demoText').textContent = v.demo_note || 'Нет связи с API пула — показаны вымышленные данные.';
  $('xmrPrice').textContent = v.price_eur ? fmtEur(v.price_eur) : '—';
  $('priceNote').textContent = v.price_rub ? `≈ ${Math.round(v.price_rub).toLocaleString('ru-RU')} ₽` : 'курс пула';
  $('netHash').textContent = v.network_hashrate_hs ? fmtHash(v.network_hashrate_hs) : '~6.8 GH/s';
  $('mBalance').textContent = num(v.balance_xmr, 6);
  $('mBalanceEur').textContent = '≈ ' + (v.price_eur ? fmtEur(v.balance_xmr * v.price_eur) : '— €');
  $('mHash').textContent = fmtHash(v.hashrate_hs);
  $('mHashSub').textContent = v.hashrate_24h ? `24ч: ${fmtHash(v.hashrate_24h)}` : 'H/s сейчас';
  $('mPaid').textContent = num(v.paid_xmr, 6);
  $('mTxCount').textContent = v.payments.length;
  $('mLast').textContent = ago(v.last_share_ts);
  $('mShares').textContent = `шар: ${v.valid_shares.toLocaleString('ru-RU')} ✓ / ${v.invalid_shares.toLocaleString('ru-RU')} ✗`;
  const thr = v.threshold_xmr || 0.001, left = Math.max(0, thr - v.balance_xmr);
  $('progressLabel').textContent = `${num(v.balance_xmr, 6)} / ${num(thr, 4)} XMR`;
  $('progressBar').style.width = Math.min(100, v.progress_pct || 0) + '%';
  $('payoutEta').textContent = v.auto_threshold_xmr > thr
    ? `До минимальной выплаты ${num(left, 6)} XMR. Автопорог пула — ${num(v.auto_threshold_xmr, 4)} XMR.`
    : `Осталось ${num(left, 6)} XMR до выплаты.`;
  const rows = v.payments.slice(0, 12);
  $('paymentsBody').innerHTML = rows.length ? rows.map(q => {
    const hex = /^[0-9a-f]{64}$/i.test(q.txid);
    const tx = hex ? `<a href="https://xmrchain.net/tx/${esc(q.txid)}" target="_blank" rel="noopener noreferrer">${esc(q.txid.slice(0, 10))}…</a>` : esc((q.txid || '—').slice(0, 12)) + '…';
    return `<tr><td>${fmtDate(q.ts)}</td><td style="color:var(--cyan)">+${num(q.amount_xmr, 6)} XMR</td><td>${v.price_eur ? fmtEur(q.amount_xmr * v.price_eur) : '—'}</td><td>${tx}</td></tr>`;
  }).join('') : '<tr><td colspan="4" class="empty">Выплат пока нет</td></tr>';
  renderCompare();
}

/* ----------------------------- калькулятор ----------------------------- */
async function computeCalc() {
  const v = state.view || {}, p = curPool() || {};
  try {
    state.calc = await api('/api/calc', { method: 'POST', body: {
      hashrate_hs: +$('calcHash').value || 0,
      network_hashrate_hs: v.network_hashrate_hs || 0,
      block_reward_xmr: v.block_reward_xmr || 0,
      fee_pct: p.fee_pct || 0, threshold_xmr: v.threshold_xmr || 0.001,
      price_eur: v.price_eur || 0, power_watts: +$('calcWatts').value || 0,
      eur_per_kwh: $('calcKwh').value === '' ? 0.36 : +$('calcKwh').value
    } });
  } catch (e) { $('calcResult').textContent = 'Ошибка: ' + e.message; return; }
  const c = state.calc, cls = c.profitable ? 'pos' : 'neg';
  $('calcResult').innerHTML = `
    <div><span class="k">Добыча:</span> ${num(c.xmr_per_day, 8)} XMR/сутки = <b>${fmtEur(c.eur_per_day)}</b>/сутки</div>
    <div><span class="k">Электричество:</span> ${c.power_kwh_per_day.toFixed(2)} кВт·ч → −${fmtEur(c.power_cost_eur_per_day)}/сутки</div>
    <div><span class="k">До первой выплаты:</span> ~${fmtDays(c.days_to_payout)}</div>
    <div class="big ${cls}" style="margin-top:7px">${c.net_eur_per_day >= 0 ? '+' : '−'}${fmtEur(Math.abs(c.net_eur_per_day))}/сутки</div>
    <div class="k">${c.profitable ? 'В плюс — редкость для CPU.' : 'В минус: электричество дороже добычи.'}</div>`;
  renderCompare();
}
function renderCompare() {
  if (!state.catalog.length) return;
  const gross = state.calc ? state.calc.gross_xmr_per_day : 0, h = +$('calcHash').value || 0;
  $('cmpHashLbl').textContent = h >= 1000 ? (h / 1000).toFixed(1) + ' kH/s' : h + ' H/s';
  $('poolCompare').innerHTML = state.catalog.map(p => {
    const daily = gross * (1 - p.fee_pct / 100), d = daily > 0 ? p.min_payout_xmr / daily : Infinity;
    return `<tr class="${p.recommended ? 'best' : ''}"><td>${esc(p.name)}${p.recommended ? ' ★' : ''}</td><td>${p.fee_pct.toFixed(2)}%</td><td>${num(p.min_payout_xmr, 4)} XMR</td><td>${num(p.default_payout_xmr, 4)} XMR</td><td style="color:${d < 30 ? 'var(--cyan)' : d < 400 ? 'var(--amber)' : 'var(--pink)'}">${fmtDays(d)}</td></tr>`;
  }).join('');
  $('compareNote').textContent = '«1-я выплата» = минимальный порог пула / твой суточный доход. У SupportXMR порог 0.1 XMR — при 5 kH/s ждать очень долго.';
}

/* ----------------------------- конфиг ----------------------------- */
async function genConfig() {
  const demo = !state.wallet;
  try {
    state.config = await api('/api/config', { method: 'POST', body: {
      wallet: state.wallet, pool: state.pool, port: +$('port').value || 0,
      worker: $('worker').value, threads_pct: +$('threads').value || 75,
      donate_level: +$('donate').value, fixed_difficulty: +$('difficulty').value || 0, demo: demo
    } });
    $('configBadge').textContent = demo ? 'ПРИМЕР · введи свой адрес' : `${state.config.host}${state.config.tls ? ' · TLS' : ''}`;
    $('configCode').textContent = state.config.config_json;
  } catch (e) { $('configCode').textContent = 'Ошибка: ' + e.message; }
}

/* ----------------------------- пульт ----------------------------- */
function syncStartButton() {
  $('startBtn').disabled = !state.wallet || !$('consent').checked || state.running || state.starting;
  $('stopBtn').disabled = !state.running;
  const pill = $('minerPill');
  pill.textContent = state.starting ? '● подготовка…' : state.running ? '● майнер работает' : '● майнер остановлен';
  pill.className = 'pill' + (state.running || state.starting ? ' mining' : '');
}
function setStatus(msg, cls) { const e = $('startStatus'); e.textContent = msg; e.className = 'progress-line ' + (cls || ''); }
function payload() {
  return { wallet: state.wallet, pool: state.pool, port: +$('port').value || 0,
    worker: $('worker').value, threads_pct: +$('threads').value || 75,
    donate_level: +$('donate').value, fixed_difficulty: +$('difficulty').value || 0, consent: $('consent').checked };
}
async function startMining() {
  if (!state.wallet) { toast('Сначала введи валидный XMR-адрес'); return; }
  if (!$('consent').checked) { toast('Поставь галочку подтверждения'); return; }
  state.starting = true; syncStartButton();
  setStatus('Готовлю: валидация → config.json → загрузка XMRig → проверка SHA256 → запуск. Это может занять до минуты…', 'busy');
  try {
    const r = await api('/api/start', { method: 'POST', body: payload() }, 240000);
    if (!r.ok) throw new Error(r.error || 'не удалось запустить');
    setStatus(r.message || 'Майнер запущен. Нажми «Обновить» через пару минут — появятся шары.', 'ok');
    toast('Майнер запущен');
    await pollStatus(); refreshStats();
  } catch (e) { setStatus('Ошибка: ' + e.message, 'err'); toast('Ошибка запуска'); }
  finally { state.starting = false; syncStartButton(); }
}
async function stopMining() {
  try { const r = await api('/api/stop', { method: 'POST', body: {} }); setStatus(r.ok ? 'Майнер остановлен.' : 'Не удалось остановить', r.ok ? '' : 'err'); }
  catch (e) { setStatus('Ошибка: ' + e.message, 'err'); }
  await pollStatus();
}
async function pollStatus() {
  try {
    const s = await api('/api/status', {}, 8000);
    state.running = !!s.running;
    if (s.workdir) $('workdirHint').textContent = 'Рабочая папка: ' + s.workdir;
    $('simNote').classList.toggle('hidden', !s.simulated);
    if (s.simulated) $('simNote').textContent = 'ПРЕВЬЮ: это Linux-тестовая сборка для проверки интерфейса — настоящий майнер здесь не запускается. В Windows-версии NeonMiner.exe всё работает по-настоящему.';
    if (s.error && !s.running) { /* показываем только в логе */ }
    syncStartButton();
    const l = await api('/api/log', {}, 8000).catch(() => null);
    if (l && l.lines) { const el = $('minerLog'); el.textContent = l.lines; el.scrollTop = el.scrollHeight; }
    if (s.progress && (state.starting || !state.running)) setStatus(s.progress, s.error ? 'err' : 'busy');
  } catch (e) { /* сервер ещё не ответил */ }
}

/* ----------------------------- init ----------------------------- */
async function init() {
  try {
    const c = await api('/api/catalog'); state.catalog = c.pools;
    if (c.default_pool) state.pool = c.default_pool;
    renderPoolList(); renderPorts();
  } catch (e) { document.body.innerHTML = '<h2 style="color:#fff;padding:40px">Не удалось загрузить каталог пулов: ' + esc(e.message) + '</h2>'; return; }
  try { const h = await api('/api/health'); $('footerVersion').textContent = 'v' + h.version + ' · ' + h.platform; } catch (e) {}
  try { const r = await api('/api/release'); if (r.ok) $('workdirHint').textContent = 'Будет скачан официальный XMRig ' + r.version + ' (' + r.asset + ')'; } catch (e) {}
  try { const w = localStorage.getItem('nm.wallet'); if (w) $('wallet').value = w; } catch (e) {}

  $('wallet').addEventListener('input', onWallet);
  $('consent').addEventListener('change', syncStartButton);
  $('startBtn').addEventListener('click', startMining);
  $('stopBtn').addEventListener('click', stopMining);
  $('refresh').addEventListener('click', refreshStats);
  $('demoHide').addEventListener('click', () => $('demoBanner').classList.add('hidden'));
  $('forgetWallet').addEventListener('click', () => { try { localStorage.removeItem('nm.wallet'); } catch (e) {} $('wallet').value = ''; onWallet(); toast('Кошелёк удалён'); });
  $('genConfig').addEventListener('click', () => { genConfig(); toast('config.json обновлён'); });
  ['port', 'worker', 'threads', 'donate', 'difficulty'].forEach(id => $(id).addEventListener('change', genConfig));
  ['calcHash', 'calcWatts', 'calcKwh'].forEach(id => $(id).addEventListener('input', () => { clearTimeout(window._ct); window._ct = setTimeout(computeCalc, 220); }));
  $('copyConfig').addEventListener('click', () => copy($('configCode').textContent, 'Конфиг'));
  $('downloadConfig').addEventListener('click', () => {
    if (!state.config) return;
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([state.config.config_json], { type: 'application/json' }));
    a.download = 'config.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000); toast('config.json скачан');
  });

  if ($('wallet').value.trim()) await onWallet(); else { await genConfig(); await refreshStats(); }
  await pollStatus();
  setInterval(pollStatus, 5000);
  setInterval(() => { if (state.wallet && !state.starting) refreshStats(); }, 60000);
}
document.addEventListener('DOMContentLoaded', init);
