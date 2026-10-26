/* NeonMiner — фронтенд локального клиента.
 * Никаких внешних библиотек и CDN. Кошелёк не уходит никуда, кроме
 * публичного read-only API выбранного пула.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  catalog: [],
  pool: 'c3pool',
  wallet: '',
  view: null,
  calc: null,
  config: null,
  tab: 'config',
  validator: null,
  busy: false,
};

async function api(path, options = {}) {
  const opts = { method: options.method || 'GET', headers: { 'Accept': 'application/json' } };
  if (options.body) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, opts);
  let data;
  try { data = await res.json(); } catch (e) { throw new Error(`Сервер вернул не JSON (${res.status})`); }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(text) {
  const el = $('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 2200);
}

async function copy(text, label) {
  try { await navigator.clipboard.writeText(text); toast(`${label} скопировано`); }
  catch (e) { toast('Не удалось скопировать — выдели текст вручную'); }
}

/* --------------------------- форматирование --------------------------- */
const num = (v, d = 6) => (Number.isFinite(+v) ? (+v).toFixed(d) : '0.' + '0'.repeat(d));
function fmtHash(hs) {
  const v = +hs || 0;
  if (v >= 1e9) return (v / 1e9).toFixed(2) + ' GH/s';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + ' MH/s';
  if (v >= 1e3) return (v / 1e3).toFixed(2) + ' kH/s';
  return v.toFixed(0) + ' H/s';
}
function fmtEur(v) { return (+v || 0).toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' €'; }
function fmtDate(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' });
}
function ago(ts) {
  if (!ts) return 'никогда';
  const sec = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (sec < 60) return `${sec} с назад`;
  if (sec < 3600) return `${Math.floor(sec / 60)} мин назад`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} ч назад`;
  return `${Math.floor(sec / 86400)} дн назад`;
}
function fmtDays(days) {
  if (!Number.isFinite(days) || days <= 0) return '—';
  if (days < 1) return `${Math.round(days * 24)} ч`;
  if (days < 60) return `${days.toFixed(1)} дн`;
  if (days < 730) return `${(days / 30).toFixed(1)} мес`;
  return `${(days / 365).toFixed(1)} лет`;
}
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* --------------------------- каталог пулов --------------------------- */
function currentPool() { return state.catalog.find(p => p.id === state.pool) || null; }

function poolUrls(pool, wallet) {
  const w = encodeURIComponent(wallet);
  const join = (p) => pool.api_base + p.replace('{wallet}', w);
  return { stats: join(pool.stats_path), payments: join(pool.payments_path), pool: pool.api_base + pool.pool_path };
}

function renderPoolList() {
  $('poolList').innerHTML = state.catalog.map(p => `
    <div class="pool-item ${p.id === state.pool ? 'active' : ''}" data-pool="${p.id}">
      <div class="pool-top">
        <span class="pool-name">${esc(p.name)}${p.recommended ? '<span class="badge-rec">КАПАЕТ БЫСТРЕЕ</span>' : ''}</span>
        <span class="pool-fee">${p.fee_pct.toFixed(2)}%</span>
      </div>
      <div class="pool-tag">${esc(p.tagline)}</div>
    </div>`).join('');
  document.querySelectorAll('.pool-item').forEach(el => el.addEventListener('click', () => selectPool(el.dataset.pool)));
}

function renderPorts() {
  const p = currentPool();
  if (!p) return;
  const defaults = { c3pool: 13333, hashvault: 443, supportxmr: 3333, moneroocean: 10128 };
  const keep = Number($('port').value);
  $('port').innerHTML = p.ports.map(x => `<option value="${x.port}">${x.port}${x.tls ? ' · TLS' : ''} — ${esc(x.label)}</option>`).join('');
  const allowed = p.ports.map(x => x.port);
  $('port').value = allowed.includes(keep) ? keep : (defaults[p.id] || allowed[0]);
  $('poolNotes').innerHTML = `<ul>${(p.notes || []).map(n => `<li>${esc(n)}</li>`).join('')}</ul>`;
}

function selectPool(id) {
  state.pool = id;
  $('port').value = '';
  renderPoolList();
  renderPorts();
  refreshStats();
  generateConfig();
}

/* --------------------------- валидация кошелька --------------------------- */
let validateTimer = null;
async function onWalletInput() {
  clearTimeout(validateTimer);
  const raw = $('wallet').value.trim();
  state.wallet = '';
  const box = $('validator');
  box.className = 'validator';
  $('validatorText').textContent = 'Проверяю…';
  if (!raw) {
    $('validatorText').textContent = 'Жду адрес';
    $('walletHint').textContent = 'Проверяем контрольную сумму Keccak — опечатка = потеря монет.';
    $('configBadge').textContent = 'введи кошелёк';
    persistWallet('');
    refreshStats(); generateConfig();
    return;
  }
  validateTimer = setTimeout(async () => {
    try {
      const info = await api('/api/validate?address=' + encodeURIComponent(raw));
      state.validator = info;
      if (info.valid) {
        state.wallet = info.address;
        box.className = 'validator ok';
        $('validatorText').textContent = `✓ валиден · ${info.kind === 'subaddress' ? 'субадрес' : info.kind === 'integrated' ? 'интегрированный' : 'основной'} · mainnet`;
        $('walletHint').textContent = info.warning || '';
        persistWallet(info.address);
        refreshStats();
        generateConfig();
      } else {
        box.className = 'validator bad';
        $('validatorText').textContent = '✗ ' + info.reason;
        $('walletHint').textContent = 'Исправь адрес — с неверным пул не начислит монеты.';
        $('configBadge').textContent = 'адрес невалиден';
        // Показываем демо-панель и пример конфига, чтобы интерфейс не пустовал.
        refreshStats();
        generateConfig();
      }
    } catch (e) {
      box.className = 'validator bad';
      $('validatorText').textContent = 'Ошибка проверки: ' + e.message;
    }
  }, 260);
}

function persistWallet(value) {
  try {
    if ($('remember').checked) {
      if (value) localStorage.setItem('neonminer.wallet', value);
      else localStorage.removeItem('neonminer.wallet');
    } else {
      localStorage.removeItem('neonminer.wallet');
    }
  } catch (e) { /* приватный режим — просто не сохраняем */ }
}

/* --------------------------- статистика --------------------------- */
async function fetchDirect(url, timeout = 7000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const res = await fetch(url, { signal: ctrl.signal, headers: { 'Accept': 'application/json' } });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } finally { clearTimeout(timer); }
}

async function refreshStats() {
  if (state.busy) return;
  state.busy = true;
  $('liveState').textContent = 'загружаю…';
  const pool = currentPool();
  try {
    let view;
    if (!state.wallet) {
      view = await api(`/api/stats?pool=${state.pool}&demo=1`);
      view.source = 'demo';
    } else {
      const urls = poolUrls(pool, state.wallet);
      try {
        // Сначала пробуем из браузера напрямую: работает даже если у бэкенда нет сети.
        const [stats, payments, poolStats] = await Promise.all([
          fetchDirect(urls.stats),
          fetchDirect(urls.payments).catch(() => []),
          fetchDirect(urls.pool).catch(() => ({})),
        ]);
        view = await api('/api/normalize', {
          method: 'POST',
          body: { pool: state.pool, stats, payments, pool_stats: poolStats },
        });
        view.source = 'browser';
      } catch (e) {
        // CORS/офлайн — просим бэкенд (у него есть кэш и демо-режим).
        view = await api(`/api/stats?pool=${state.pool}&wallet=${encodeURIComponent(state.wallet)}`);
        view.source = view.demo ? 'demo' : 'server';
      }
    }
    state.view = view;
    render();
    await computeCalc();
  } catch (e) {
    $('liveState').textContent = 'ошибка: ' + e.message;
    toast('Не удалось получить статистику: ' + e.message);
  } finally { state.busy = false; }
}

function render() {
  const v = state.view;
  if (!v) return;
  const pool = currentPool();
  const demo = !!v.demo;
  $('dashPool').textContent = `${v.pool_name} · ${pool ? pool.api_base : ''}`;
  $('sourcePill').textContent = demo ? '● ДЕМО-ДАННЫЕ' : (v.source === 'browser' ? '● LIVE · браузер → пул' : '● LIVE · сервер → пул');
  $('sourcePill').className = 'pill ' + (demo ? 'live-demo' : 'live-ok');
  $('liveState').textContent = demo ? 'демо' : 'обновлено ' + new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  $('demoBanner').classList.toggle('hidden', !demo);
  if (demo) $('demoText').textContent = v.demo_note || 'Демо-данные.';

  $('xmrPrice').textContent = v.price_eur ? fmtEur(v.price_eur) : '—';
  $('priceNote').textContent = v.price_rub ? `≈ ${Math.round(v.price_rub).toLocaleString('ru-RU')} ₽` : 'цена пула';
  $('netHash').textContent = v.network_hashrate_hs ? fmtHash(v.network_hashrate_hs) : '~6.8 GH/s';

  $('mBalance').textContent = num(v.balance_xmr, 6);
  $('mBalanceEur').textContent = '≈ ' + (v.price_eur ? fmtEur(v.balance_xmr * v.price_eur) : '— €');
  $('mHash').textContent = fmtHash(v.hashrate_hs);
  $('mHashSub').textContent = v.hashrate_24h ? `24ч: ${fmtHash(v.hashrate_24h)}` : 'H/s сейчас';
  $('mPaid').textContent = num(v.paid_xmr, 6);
  $('mTxCount').textContent = v.payments ? v.payments.length : (v.txn_count || 0);
  $('mLast').textContent = ago(v.last_share_ts);
  $('mShares').textContent = `шар: ${(v.valid_shares || 0).toLocaleString('ru-RU')} ✓ / ${(v.invalid_shares || 0).toLocaleString('ru-RU')} ✗`;

  const thr = v.threshold_xmr || 0.001;
  $('progressLabel').textContent = `${num(v.balance_xmr, 6)} / ${num(thr, 4)} XMR`;
  $('progressBar').style.width = Math.min(100, v.progress_pct || 0) + '%';
  const left = Math.max(0, thr - v.balance_xmr);
  $('payoutEta').textContent = v.auto_threshold_xmr && v.auto_threshold_xmr > thr
    ? `До минимальной выплаты ${num(left, 6)} XMR. Автовыплата у пула по умолчанию — от ${num(v.auto_threshold_xmr, 4)} XMR (порог меняется в настройках пула).`
    : `Осталось ${num(left, 6)} XMR до порога выплаты.`;

  renderPayments(v);
  renderCompare();
  renderStartCommand();
}

function renderPayments(v) {
  const rows = (v.payments || []).slice(0, 12);
  if (!rows.length) { $('paymentsBody').innerHTML = '<tr><td colspan="4" class="empty">Выплат пока нет — майнер ещё не накапал до порога</td></tr>'; return; }
  $('paymentsBody').innerHTML = rows.map(p => {
    const isHex = /^[0-9a-f]{64}$/i.test(p.txid || '');
    const tx = isHex
      ? `<a href="https://xmrchain.net/tx/${esc(p.txid)}" target="_blank" rel="noopener noreferrer">${esc(p.txid.slice(0, 10))}…</a>`
      : esc((p.txid || '—').slice(0, 12)) + '…';
    return `<tr><td>${fmtDate(p.ts)}</td><td style="color:var(--cyan)">+${num(p.amount_xmr, 6)} XMR</td>
      <td>${v.price_eur ? fmtEur(p.amount_xmr * v.price_eur) : '—'}</td><td>${tx}</td></tr>`;
  }).join('');
}

/* --------------------------- экономика --------------------------- */
async function computeCalc() {
  const v = state.view || {};
  const payload = {
    hashrate_hs: Number($('calcHash').value) || 0,
    network_hashrate_hs: v.network_hashrate_hs || null,
    block_reward_xmr: v.block_reward_xmr || null,
    fee_pct: currentPool() ? currentPool().fee_pct : 0,
    threshold_xmr: v.threshold_xmr || 0.001,
    price_eur: v.price_eur || null,
    power_watts: Number($('calcWatts').value) || 0,
    eur_per_kwh: $('calcKwh').value === '' ? null : Number($('calcKwh').value),
  };
  try { state.calc = await api('/api/calc', { method: 'POST', body: payload }); }
  catch (e) { $('calcResult').innerHTML = '<span class="neg">Ошибка расчёта: ' + esc(e.message) + '</span>'; return; }
  renderCalc();
  renderCompare();
}

function renderCalc() {
  const c = state.calc;
  if (!c) return;
  const cls = c.profitable ? 'pos' : 'neg';
  $('calcResult').innerHTML = `
    <div><span class="k">Добыча:</span> ${num(c.xmr_per_day, 8)} XMR/сутки = <b>${fmtEur(c.eur_per_day)}</b>/сутки</div>
    <div><span class="k">Электричество:</span> ${c.power_kwh_per_day.toFixed(2)} кВт·ч → −${fmtEur(c.power_cost_eur_per_day)}/сутки</div>
    <div><span class="k">До первой выплаты:</span> ~${fmtDays(c.days_to_payout)} при пороге ${num(c.threshold_xmr, 4)} XMR</div>
    <div class="big ${cls}" style="margin-top:7px">${c.net_eur_per_day >= 0 ? '+' : '−'}${fmtEur(Math.abs(c.net_eur_per_day))}/сутки · ${c.net_eur_per_month >= 0 ? '+' : '−'}${fmtEur(Math.abs(c.net_eur_per_month))}/мес</div>
    <div class="k">${c.profitable ? 'В плюс — редкость для CPU-майнинга.' : 'В минус: электричество дороже добычи. Майни ради эксперимента, не ради прибыли.'}</div>`;
}

function renderCompare() {
  if (!state.catalog.length) return;
  const gross = state.calc ? state.calc.gross_xmr_per_day : 0;
  const hash = Number($('calcHash').value) || 0;
  $('cmpHashLbl').textContent = hash >= 1000 ? (hash / 1000).toFixed(1) + ' kH/s' : hash + ' H/s';
  $('poolCompare').innerHTML = state.catalog.map(p => {
    const daily = gross * (1 - p.fee_pct / 100);
    const days = daily > 0 ? p.min_payout_xmr / daily : Infinity;
    const best = p.id === 'c3pool';
    return `<tr class="${best ? 'best' : ''}"><td>${esc(p.name)}${p.recommended ? ' ★' : ''}</td>
      <td>${p.fee_pct.toFixed(2)}%</td><td>${num(p.min_payout_xmr, 4)} XMR</td>
      <td>${num(p.default_payout_xmr, 4)} XMR</td><td style="color:${days < 30 ? 'var(--cyan)' : days < 400 ? 'var(--amber)' : 'var(--pink)'}">${fmtDays(days)}</td></tr>`;
  }).join('');
  $('compareNote').textContent = '«1-я выплата» = минимальный порог пула / твой суточный доход. '
    + 'SupportXMR стабильнее всех, но при 5 kH/s до его 0.1 XMR ждать годы — поэтому для «капельницы» выбран C3Pool.';
}

/* --------------------------- конфиг --------------------------- */
async function generateConfig() {
  const demoMode = !state.wallet;
  const payload = {
    wallet: state.wallet,
    pool: state.pool,
    port: Number($('port').value) || undefined,
    worker: $('worker').value,
    threads_pct: Number($('threads').value) || 75,
    donate_level: Number($('donate').value),
    fixed_difficulty: Number($('difficulty').value) || null,
    demo: demoMode,
  };
  try { state.config = await api('/api/config', { method: 'POST', body: payload }); }
  catch (e) { $('configCode').textContent = 'Ошибка: ' + e.message; return; }
  $('configBadge').textContent = demoMode ? 'ПРИМЕР · подставь свой адрес' : `${state.config.pool_name} → ${state.config.host}`;
  renderConfigTab();
  renderStartCommand();
}

function renderConfigTab() {
  if (!state.config) return;
  const c = state.config;
  const sources = {
    config: c.config_json,
    cmd_linux: c.cmd_linux + '\n\n# Или с конфигом:\n./xmrig --config=config.json',
    cmd_windows: c.cmd_windows + '\n\nREM Или с конфигом:\nxmrig.exe --config=config.json',
    systemd_unit: c.systemd_unit,
    windows_bat: c.windows_bat,
  };
  $('configCode').textContent = sources[state.tab] || c.config_json;
  document.querySelectorAll('#configTabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === state.tab));
}

function renderStartCommand() {
  const wallet = state.wallet || 'ТВОЙ_XMR_АДРЕС';
  $('startCmd').textContent = `python3 -m neon_miner start --wallet ${wallet} --pool ${state.pool} --download --yes`;
}

/* --------------------------- init --------------------------- */
async function init() {
  try {
    const data = await api('/api/catalog');
    state.catalog = data.pools;
    renderPoolList();
    renderPorts();
  } catch (e) { toast('Не удалось загрузить каталог пулов: ' + e.message); return; }

  try {
    const h = await api('/api/health');
    $('appVersion').textContent = 'v' + h.version;
  } catch (e) { /* не критично */ }

  try {
    const r = await api('/api/release');
    $('releaseVersion').textContent = r.ok ? `последний релиз ${r.version} · ${r.asset}` : 'релиз недоступен офлайн';
  } catch (e) { /* не критично */ }

  try {
    const saved = localStorage.getItem('neonminer.wallet');
    if (saved) { $('wallet').value = saved; }
  } catch (e) { /* приватный режим */ }

  // Слушатели
  $('wallet').addEventListener('input', onWalletInput);
  $('port').addEventListener('change', generateConfig);
  ['worker', 'threads', 'donate', 'difficulty'].forEach(id => $(id).addEventListener('change', generateConfig));
  ['calcHash', 'calcWatts', 'calcKwh'].forEach(id => $(id).addEventListener('input', () => {
    clearTimeout(window._calcT);
    window._calcT = setTimeout(computeCalc, 220);
  }));
  $('genConfig').addEventListener('click', () => { generateConfig(); toast('Конфиг обновлён'); });
  $('refresh').addEventListener('click', refreshStats);
  $('demoHide').addEventListener('click', () => $('demoBanner').classList.add('hidden'));
  $('forgetWallet').addEventListener('click', () => {
    try { localStorage.removeItem('neonminer.wallet'); } catch (e) {}
    $('wallet').value = '';
    onWalletInput();
    toast('Кошелёк удалён из браузера');
  });
  $('remember').addEventListener('change', () => persistWallet(state.wallet));
  document.querySelectorAll('#configTabs button').forEach(b => b.addEventListener('click', () => {
    state.tab = b.dataset.tab; renderConfigTab();
  }));
  $('copyConfig').addEventListener('click', () => copy($('configCode').textContent, 'Конфиг'));
  $('copyStart').addEventListener('click', () => copy($('startCmd').textContent, 'Команда'));
  $('downloadConfig').addEventListener('click', () => {
    if (!state.config) return;
    const blob = new Blob([state.config.config_json], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'config.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast('config.json скачан');
  });

  if ($('wallet').value.trim()) await onWalletInput();
  else { await refreshStats(); await generateConfig(); }
  setInterval(() => { if (state.wallet && !state.busy) refreshStats(); }, 60000);
}

document.addEventListener('DOMContentLoaded', init);
