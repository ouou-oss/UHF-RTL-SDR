'use strict';

const ICON_SUN = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const ICON_MOON = '<svg viewBox="0 0 24 24"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.5 6.5 0 0 0 9.8 9.8z"/></svg>';
const STATE_LABEL = { ok: '正常', warn: '注意', bad: '异常', idle: '静默', init: '启动中' };

const $ = (id) => document.getElementById(id);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const fmtClock = (t) => new Date(t * 1000).toTimeString().slice(0, 8);
const fmtDb = (v) => {
  const n = Math.abs(v) < 0.05 ? 0 : v;
  return (n >= 0 ? '+' : '') + n.toFixed(1);
};

let siteEls = null;
let cards = null;
let chanTrend = null;
let chanRange = 300;
let chanSeq = 0;
let resizeTimer = 0;
let lastState = null;
let detailKey = null;
let detailRange = 3600;
let detailData = null;
let detailSeq = 0;
let detailHover = null;
let detailGeo = null;
let detailTrend = [];
let chIndex = null;
let chFilter = 'all';
let chQuery = '';
let chBuiltSig = '';
const smooth = new Map();

function fmtUp(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h}h${String(m).padStart(2, '0')}` : `${m}m${String(s % 60).padStart(2, '0')}`;
}

function withAlpha(color, a) {
  const m = /^#?([0-9a-f]{6})$/i.exec((color || '').trim());
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const btn = $('themeBtn');
  if (btn) btn.innerHTML = t === 'light' ? ICON_MOON : ICON_SUN;
  try { localStorage.setItem('dsh-theme', t); } catch (e) {}
  drawAllChan();
  drawDetail();
}

/* ── status card ── */
function renderStatus(d) {
  const v = d.verdict || {};
  const card = $('verdictCard');
  card.dataset.state = v.level || 'init';
  $('vState').textContent = STATE_LABEL[v.level] || '—';
  $('vTitle').textContent = v.title || '正在启动';
  $('vSub').textContent = v.sub || '';
  $('vActive').textContent = `${v.active ?? 0}/${v.total ?? 33}`;
  $('vClip').textContent = (d.clip ?? 0).toFixed(2) + '%';
  $('vUp').textContent = fmtUp(d.uptime || 0);
  $('clock').textContent = fmtClock(d.ts || Date.now() / 1000);

  const pill = $('devPill');
  pill.classList.toggle('on', !!d.device);
  pill.classList.toggle('off', !d.device);
  pill.querySelector('b').textContent = d.device ? 'ONLINE' : 'OFFLINE';
}

/* ── site cards ── */
function buildSiteCards(list) {
  const wrap = $('siteCards');
  wrap.innerHTML = '';
  siteEls = new Map();
  for (const s of list) {
    const el = document.createElement('article');
    el.className = 'sc';
    el.dataset.state = 'init';
    el.innerHTML = `
      <div class="sc-hd">
        <span class="sc-name">${s.site.toUpperCase()}</span>
        <span class="sc-led"></span>
        <span class="sc-state">—</span>
      </div>
      <div class="sc-read"><strong>—</strong><span class="u">dB</span></div>
      <div class="sc-meter">
        <div class="sc-track"><i class="sc-fill"></i><b class="sc-needle"></b></div>
        <div class="sc-ticks"><span>−10</span><span>−5</span><span>0</span><span>+5</span><span>+10</span><span>+15</span><span>+20</span></div>
      </div>
      <div class="sc-inst" title="瞬时控制信道电平（0–40 dB）；白色段为最近 1 秒峰值">
        <span class="sc-inst-k">INST</span>
        <div class="sc-inst-bars"></div>
        <span class="sc-inst-v">—</span>
      </div>
      <div class="sc-meta">—</div>
      <div class="sc-foot"><span class="f-base">—</span><span class="f-act">—</span></div>`;
    const bars = el.querySelector('.sc-inst-bars');
    for (let i = 0; i < 24; i++) bars.appendChild(document.createElement('i'));
    wrap.appendChild(el);
    siteEls.set(s.site, {
      el,
      state: el.querySelector('.sc-state'),
      read: el.querySelector('.sc-read strong'),
      fill: el.querySelector('.sc-fill'),
      needle: el.querySelector('.sc-needle'),
      meta: el.querySelector('.sc-meta'),
      base: el.querySelector('.f-base'),
      act: el.querySelector('.f-act'),
      segs: Array.from(bars.children),
      instV: el.querySelector('.sc-inst-v'),
    });
  }
}

function renderSiteCards(d) {
  const list = (d.verdict && d.verdict.sites) || [];
  if (!siteEls || siteEls.size !== list.length) buildSiteCards(list);
  for (const s of list) {
    const k = siteEls.get(s.site);
    if (!k) continue;
    k.el.dataset.state = s.state;
    k.state.textContent = s.text;

    const has = s.dev !== null && s.dev !== undefined;
    let disp = has ? s.dev : null;
    if (has) {
      const prev = smooth.get(s.site);
      disp = prev === undefined ? s.dev : prev * 0.55 + s.dev * 0.45;
      smooth.set(s.site, disp);
    } else {
      smooth.delete(s.site);
    }
    k.read.textContent = has ? fmtDb(disp) : '—';

    const ZERO = 33.3333;
    const v = clamp(has ? disp : 0, -10, 20);
    const pct = ((v + 10) / 30) * 100;
    if (v >= 0) {
      k.fill.style.left = ZERO + '%';
      k.fill.style.width = (pct - ZERO) + '%';
    } else {
      k.fill.style.left = pct + '%';
      k.fill.style.width = (ZERO - pct) + '%';
    }
    k.needle.style.left = pct + '%';

    const inst = s.inst ?? 0, inst1 = s.inst1 ?? 0;
    const nseg = k.segs.length;
    const lit = Math.round(clamp(inst / 40, 0, 1) * nseg);
    const pkIdx = clamp(Math.round((inst1 / 40) * nseg) - 1, 0, nseg - 1);
    k.segs.forEach((el, i) => {
      el.classList.toggle('on', i < lit);
      el.classList.toggle('pk', inst1 > 0.5 && i === pkIdx);
    });
    k.instV.textContent = inst.toFixed(1);

    k.meta.textContent = `最弱 ${s.ctrlMhz ? s.ctrlMhz.toFixed(4) : '—'} · 峰值 ${s.peak.toFixed(1)} dB`;
    k.base.textContent = s.base === null ? '基线建立中' : `基线 ${s.base.toFixed(1)} dB`;
    k.act.textContent = `活跃 ${s.clean}/${s.total}`;
  }
}

/* ── issues（嵌在状态卡里） ── */
function renderIssues(d) {
  const list = (d.verdict && d.verdict.issues) || [];
  const box = $('issues');
  if (!list.length) {
    box.innerHTML = '<div class="st-none">暂无异常</div>';
    return;
  }
  box.innerHTML = list.map((x) => `
    <div class="st-issue ${x.kind}"><i></i><time>${fmtClock(x.t)}</time>
      ${x.site ? `<b>${x.site}</b>` : ''}<span>${x.text}</span></div>`).join('');
}

/* ── 全部信道明细 ── */
function buildMatrix(d) {
  const sites = new Map();
  for (const c of d.channels) {
    if (!sites.has(c.site)) sites.set(c.site, []);
    sites.get(c.site).push(c);
  }
  const wrap = $('sites');
  wrap.innerHTML = '';
  cards = new Map();
  for (const [site, list] of sites) {
    const sec = document.createElement('div');
    sec.className = 'site';
    sec.innerHTML = `<div class="site-hd"><span>${site.toUpperCase()}</span><em></em></div><div class="grid"></div>`;
    const grid = sec.querySelector('.grid');
    for (const c of list) {
      const el = document.createElement('div');
      el.className = 'ch';
      el.innerHTML = `
        <div class="ch-top">
          <span class="freq">${c.mhz.toFixed(4)}</span>
          <span class="badge">${c.ctrl ? '控制' : '业务'}</span>
        </div>
        <div class="ch-spark"><canvas></canvas></div>
        <div class="ch-bot">
          <span class="lvl"><em>当前高于噪底</em><b>—</b><i>dB</i></span>
          <span class="state">空闲</span>
        </div>`;
      el.addEventListener('click', () => {
        detailKey = c.key;
        try { localStorage.setItem('radio-detail-key', c.key); } catch (e) {}
        detailHover = null;
        $('detailEmpty').style.display = 'flex';
        $('detailEmpty').textContent = '正在加载…';
        setTab('channel');
      });
      grid.appendChild(el);
      cards.set(c.key, {
        key: c.key, el, ctrl: c.ctrl,
        cv: el.querySelector('canvas'),
        lvl: el.querySelector('.lvl b'),
        st: el.querySelector('.state'),
        active: false, unrel: false,
      });
    }
    wrap.appendChild(sec);
  }
  $('chCount').textContent = d.channels.length;
  if (chanTrend) drawAllChan();
}

function renderMatrix(d) {
  if (!cards || cards.size !== d.channels.length) buildMatrix(d);
  for (const c of d.channels) {
    const k = cards.get(c.key);
    if (!k) continue;
    k.el.classList.toggle('act', c.active);
    k.el.classList.toggle('unrel', !!c.unreliable);

    k.lvl.textContent = fmtDb(c.level);
    k.st.textContent = c.active
      ? (c.unreliable ? '邻道' : '活跃')
      : (c.level > 2.5 ? '弱载波' : '空闲');

    const wasActive = k.active, wasUnrel = k.unrel;
    k.active = !!c.active;
    k.unrel = !!c.unreliable;
    if (wasActive !== k.active || wasUnrel !== k.unrel) drawCardSpark(k);

    const peak = chanPeak(k.key);
    k.el.title = `${c.site}-${c.rep} · ${c.mhz.toFixed(4)} MHz\n`
      + `当前高于噪底 ${fmtDb(c.level)} dB（频谱噪声底以上 ${c.delta.toFixed(1)} dB）\n`
      + `窗口 ${rangeLabel()} 峰值 ${peak === null ? '—' : fmtDb(peak) + ' dB'}`;
  }
}

function chanPeak(key) {
  const arr = chanTrend && chanTrend.chan_trend && chanTrend.chan_trend[key];
  if (!arr || !arr.length) return null;
  let hi = -Infinity;
  for (const [, v] of arr) if (v > hi) hi = v;
  return hi;
}

function rangeLabel() {
  return chanRange >= 60 ? `${Math.round(chanRange / 60)}m` : `${chanRange}s`;
}

/* ── 小卡片曲线 ── */
function drawCardSpark(k) {
  const cv = k.cv;
  if (!cv) return;
  const wrap = cv.parentElement;
  const w = Math.max(Math.floor(wrap.clientWidth), 40);
  const h = Math.max(Math.floor(wrap.clientHeight), 20);
  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const grid = cssVar('--grid');
  let col = cssVar('--mut');
  if (k.unrel && k.active) col = cssVar('--warn');
  else if (k.active) col = k.ctrl ? cssVar('--acc') : cssVar('--ok');

  const arr = (chanTrend && chanTrend.chan_trend && chanTrend.chan_trend[k.key]) || [];
  if (arr.length < 2) {
    g.strokeStyle = grid;
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(0, h - 2.5);
    g.lineTo(w, h - 2.5);
    g.stroke();
    g.font = '8.5px ui-monospace,SFMono-Regular,Menlo,monospace';
    g.fillStyle = cssVar('--dim');
    g.textAlign = 'center';
    g.fillText('无数据', w / 2, h / 2 + 3);
    return;
  }

  const t0 = chanTrend.from, t1 = chanTrend.to;
  const span = Math.max(t1 - t0, 1);
  let pmin = Infinity, pmax = -Infinity;
  for (const [, v] of arr) {
    if (v < pmin) pmin = v;
    if (v > pmax) pmax = v;
  }
  let lo = Math.min(0, Math.floor(pmin / 5) * 5);
  let hi = Math.max(10, Math.ceil((pmax + 0.001) / 5) * 5);
  if (hi - lo < 10) hi = lo + 10;

  const padT = 2, padB = 2;
  const ih = h - padT - padB;
  const X = (t) => clamp((t - t0) / span, 0, 1) * w;
  const Y = (v) => padT + (1 - (clamp(v, lo, hi) - lo) / (hi - lo)) * ih;

  // 零线（噪声底参考）
  if (lo < 0 && hi > 0) {
    g.strokeStyle = grid;
    g.lineWidth = 1;
    g.setLineDash([2, 3]);
    g.beginPath();
    g.moveTo(0, Math.round(Y(0)) + 0.5);
    g.lineTo(w, Math.round(Y(0)) + 0.5);
    g.stroke();
    g.setLineDash([]);
  }

  const gapLimit = Math.max((span / 140) * 3, 6);
  const grad = g.createLinearGradient(0, padT, 0, h - padB);
  grad.addColorStop(0, withAlpha(col, 0.30));
  grad.addColorStop(1, withAlpha(col, 0.02));

  // 面积
  g.beginPath();
  let pen = false, prevT = 0, lastX = 0;
  for (const [t, v] of arr) {
    if (pen && t - prevT > gapLimit) pen = false;
    const x = X(t), y = Y(v);
    if (!pen) {
      g.moveTo(x, h - padB);
      g.lineTo(x, y);
      pen = true;
    } else {
      g.lineTo(x, y);
    }
    lastX = x;
    prevT = t;
  }
  g.lineTo(lastX, h - padB);
  g.closePath();
  g.fillStyle = grad;
  g.fill();

  // 折线
  g.beginPath();
  pen = false;
  prevT = 0;
  for (const [t, v] of arr) {
    if (pen && t - prevT > gapLimit) pen = false;
    const x = X(t), y = Y(v);
    if (!pen) { g.moveTo(x, y); pen = true; } else g.lineTo(x, y);
    prevT = t;
  }
  g.strokeStyle = col;
  g.lineWidth = 1.4;
  g.lineJoin = 'round';
  g.lineCap = 'round';
  g.stroke();

  // 末端当前值
  const [lt, lv] = arr[arr.length - 1];
  g.fillStyle = col;
  g.beginPath();
  g.arc(X(lt), Y(lv), 1.9, 0, Math.PI * 2);
  g.fill();

  // 窗口峰值（说明纵向自适应缩放的真实数值）
  const label = `峰 ${fmtDb(pmax)} dB`;
  g.font = '8.5px ui-monospace,SFMono-Regular,Menlo,monospace';
  g.textAlign = 'right';
  const tw = g.measureText(label).width;
  g.fillStyle = withAlpha(cssVar('--panel2'), 0.82);
  g.fillRect(w - tw - 4, 0, tw + 4, 11);
  g.fillStyle = cssVar('--dim');
  g.fillText(label, w - 1, 8.5);

}

function drawAllChan() {
  if (!cards || $('viewOverview').hidden) return;
  for (const k of cards.values()) drawCardSpark(k);
}

function updateChanToolbar() {
  document.querySelectorAll('#chanToolbar button[data-range]').forEach((b) => {
    b.classList.toggle('on', Number(b.dataset.range) === chanRange);
  });
}

/* ── 信道分析页 ── */
const VERDICT_LABEL = {
  ok: '信号稳定', warn: '信号偏低', bad: '信号明显下降',
  idle: '近期无发射', insufficient: '样本不足',
};

function median(arr) {
  if (!arr.length) return null;
  const a = arr.slice().sort((x, y) => x - y);
  const m = a.length >> 1;
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
}

function fmtSpan(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec >= 3600) return `${(sec / 3600).toFixed(sec % 3600 ? 1 : 0)} 小时`;
  return `${Math.max(1, Math.round(sec / 60))} 分钟`;
}

function fmtAxis(t, span) {
  const d = new Date(t * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mi = String(d.getMinutes()).padStart(2, '0');
  if (span > 1800) return `${hh}:${mi}`;
  return `${hh}:${mi}:${String(d.getSeconds()).padStart(2, '0')}`;
}

function channelSignature(state) {
  if (!state || !state.channels) return '';
  return state.channels.map((c) => c.key).join(',') + '|' + chFilter + '|' + chQuery;
}

function buildChannelList(state) {
  const box = $('chItems');
  if (!box) return;
  chBuiltSig = channelSignature(state);
  chIndex = new Map();
  if (!state || !state.channels) {
    box.innerHTML = '<div class="ch-empty">正在读取信道…</div>';
    return;
  }
  const groups = new Map();
  for (const c of state.channels) {
    if (chFilter === 'ctrl' && !c.ctrl) continue;
    if (chFilter === 'biz' && c.ctrl) continue;
    const q = chQuery;
    if (q && !(`${c.mhz.toFixed(4)} ${c.site} ${c.rep} ${c.key}`.toLowerCase().includes(q))) continue;
    if (!groups.has(c.site)) groups.set(c.site, []);
    groups.get(c.site).push(c);
  }
  box.innerHTML = '';
  if (!groups.size) {
    box.innerHTML = '<div class="ch-empty">没有匹配的信道</div>';
    return;
  }
  const ordered = [...groups.entries()].sort((a, b) =>
    a[0].localeCompare(b[0], 'en', { numeric: true }));
  for (const [site, list] of ordered) {
    const g = document.createElement('div');
    g.className = 'ch-group';
    g.innerHTML = `<h4>${site.toUpperCase()}（${list.length}）</h4>`;
    for (const c of list) {
      const el = document.createElement('button');
      el.className = 'ch-item';
      el.dataset.key = c.key;
      el.innerHTML = `
        <i class="dot"></i>
        <span class="f">${c.mhz.toFixed(4)}</span>
        <span class="m">${c.ctrl ? '控制' : '业务'} · ${c.rep}</span>
        <span class="v">—</span>`;
      el.addEventListener('click', () => selectChannel(c.key));
      g.appendChild(el);
      chIndex.set(c.key, { el, v: el.querySelector('.v') });
    }
    box.appendChild(g);
  }
  updateChannelValues(state);
}

function updateChannelValues(state) {
  if (!chIndex || !state || !state.channels) return;
  for (const c of state.channels) {
    const k = chIndex.get(c.key);
    if (!k) continue;
    k.v.textContent = fmtDb(c.level) + ' dB';
    k.el.classList.toggle('hot', c.level >= 6);
    k.el.classList.toggle('on', c.key === detailKey);
  }
}

function ensureChannelList(state) {
  if (!state || !state.channels) return;
  if (!chIndex || chBuiltSig !== channelSignature(state)) {
    buildChannelList(state);
  } else {
    updateChannelValues(state);
  }
}

function selectDefaultChannel() {
  if (!lastState || !lastState.channels || !lastState.channels.length) return;
  const list = lastState.channels;
  let key = detailKey || localStorage.getItem('radio-detail-key');
  if (!key || !list.some((c) => c.key === key)) {
    // 首次进入优先选最近最忙的信道，避免打开就是一条空曲线
    const ranked = list.slice().sort((a, b) =>
      (b.active ? 1 : 0) - (a.active ? 1 : 0) || (b.duty || 0) - (a.duty || 0)
      || b.level - a.level);
    key = (ranked[0] || list[0]).key;
  }
  selectChannel(key);
}

function selectChannel(key) {
  detailKey = key;
  try { localStorage.setItem('radio-detail-key', key); } catch (e) {}
  detailHover = null;
  if (chIndex) {
    for (const [k, item] of chIndex) item.el.classList.toggle('on', k === key);
  }
  $('detailEmpty').style.display = 'flex';
  $('detailEmpty').textContent = '正在加载…';
  loadDetail();
}

async function loadDetail() {
  if (!detailKey) { selectDefaultChannel(); return; }
  const seq = ++detailSeq;
  try {
    const r = await fetch(`/api/chan-detail?key=${encodeURIComponent(detailKey)}&range=${detailRange}`,
      { cache: 'no-store' });
    if (!r.ok) throw new Error('http ' + r.status);
    const d = await r.json();
    if (seq !== detailSeq) return;
    detailData = d;
    renderDetail();
  } catch (e) {
    if (seq !== detailSeq) return;
    $('detailEmpty').style.display = 'flex';
    $('detailEmpty').textContent = '读取失败，请稍后再试';
  }
}

function statHtml(label, value, unit, sub, cls, signed) {
  const txt = value === null || value === undefined
    ? '—'
    : (signed ? fmtDb(value) : Number(value).toFixed(1));
  return `<div class="stat"><span>${label}</span><b class="${cls}">${txt}<em>${unit}</em></b><small>${sub}</small></div>`;
}

function renderDetail() {
  const d = detailData;
  if (!d || d.key !== detailKey) return;
  $('detailName').textContent = `${d.site}-${d.rep} · ${d.mhz.toFixed(4)} MHz`;
  $('detailSub').textContent = `${d.ctrl ? '控制信道' : '业务信道'} · 统计 ${fmtSpan(d.to - d.from)}`;

  const lvl = d.level;
  const deltaCls = lvl === 'bad' ? 'bad' : lvl === 'warn' ? 'warn' : lvl === 'ok' ? 'ok' : 'dim';
  const recentLabel = d.recent === null
    ? '无有效发射'
    : `${d.recent_n} 个样本 · 峰值 ${fmtDb(d.recent_peak)} dB`;
  const baseLabel = d.baseline === null ? '样本不足' : `${d.baseline_n} 个发射样本`;
  $('detailStats').innerHTML = [
    statHtml('早先基线（中值）', d.baseline, 'dB', baseLabel, ''),
    statHtml(`最近 ${fmtSpan(d.recent_sec)}（中值）`, d.recent, 'dB', recentLabel, ''),
    statHtml('变化', d.delta, 'dB', '下降 3 dB 提醒 / 6 dB 告警', deltaCls, true),
    statHtml('发射占空比', d.duty, '%', d.last ? `最近发射 ${fmtClock(d.last.t)}` : '窗口内无发射', ''),
  ].join('');

  const box = $('detailVerdict');
  box.className = 'detail-verdict ' + lvl;
  box.innerHTML = `<b>${VERDICT_LABEL[lvl] || '—'}</b><span>${d.text}</span>`;

  const empty = $('detailEmpty');
  if (!d.points.length) {
    empty.style.display = 'flex';
    empty.textContent = lvl === 'idle'
      ? '最近观察窗内没有有效发射，暂时无法比较'
      : '有效发射样本不足，继续积累后再判断';
  } else {
    empty.style.display = 'none';
  }
  drawDetail();
}

function computeTrend(points, span) {
  const win = Math.max(120, span * 0.06);
  const out = [];
  let j = 0;
  for (let i = 0; i < points.length; i++) {
    const t = points[i][0];
    while (points[j][0] < t - win) j++;
    let sum = 0, n = 0;
    const vals = [];
    for (let k = j; k <= i; k++) { vals.push(points[k][1]); n++; }
    out.push([t, vals.length ? median(vals) : points[i][1]]);
  }
  return out;
}

function drawDetail() {
  const cv = $('detailChart');
  if (!cv || $('viewChannel').hidden) return;
  const wrap = cv.parentElement;
  const w = Math.floor(cv.clientWidth || wrap.clientWidth);
  const h = Math.floor(cv.clientHeight);
  if (w < 80 || h < 80) return;
  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const d = detailData;
  if (!d) return;
  const pts = d.points || [];
  const t0 = d.from, t1 = d.to, span = Math.max(t1 - t0, 1);
  const padL = 42, padR = 18, padT = 20, padB = 27;
  const iw = w - padL - padR, ih = h - padT - padB;
  if (iw < 40 || ih < 40) return;

  let pmax = 10;
  for (const p of pts) if (p[1] > pmax) pmax = p[1];
  if (d.baseline !== null) pmax = Math.max(pmax, d.baseline);
  if (d.recent !== null) pmax = Math.max(pmax, d.recent);
  const hi = Math.max(20, Math.ceil((pmax + 2) / 10) * 10);
  const X = (t) => padL + clamp((t - t0) / span, 0, 1) * iw;
  const Y = (v) => padT + (1 - clamp(v, 0, hi) / hi) * ih;
  detailGeo = { padL, padR, padT, padB, iw, ih, w, h, t0, t1, hi };
  detailTrend = computeTrend(pts, span);

  const cssGrid = cssVar('--grid');
  const cssDim = cssVar('--dim');
  const cssMut = cssVar('--mut');
  const cssAcc = cssVar('--acc');
  const cssYellow = cssVar('--yellow');
  const cssWarn = cssVar('--warn');
  const cssErr = cssVar('--err');
  const cssOk = cssVar('--ok');

  // 最近观察窗底色
  const rx = X(Math.max(t0, t1 - d.recent_sec));
  g.fillStyle = withAlpha(cssAcc, 0.055);
  g.fillRect(rx, padT, w - padR - rx, ih);

  // 水平网格 + 纵轴
  g.font = '9.5px ui-monospace,SFMono-Regular,Menlo,monospace';
  g.textBaseline = 'middle';
  g.textAlign = 'right';
  const step = hi <= 20 ? 5 : 10;
  g.strokeStyle = cssGrid;
  g.fillStyle = cssDim;
  g.lineWidth = 1;
  for (let v = 0; v <= hi; v += step) {
    const y = Math.round(Y(v)) + 0.5;
    g.beginPath(); g.moveTo(padL, y); g.lineTo(w - padR, y); g.stroke();
    g.fillText(String(v), padL - 6, y);
  }
  g.textAlign = 'left';
  g.fillText('dB', 4, padT - 12);
  g.fillText(`最近 ${fmtSpan(d.recent_sec)}`, Math.min(rx + 4, w - padR - 58), padT - 8);

  // 时间刻度和竖线
  const ticks = 6;
  g.textAlign = 'center';
  g.textBaseline = 'top';
  for (let k = 0; k <= ticks; k++) {
    const t = t0 + (span * k) / ticks;
    const x = Math.round(X(t)) + 0.5;
    g.strokeStyle = cssGrid;
    g.beginPath(); g.moveTo(x, padT); g.lineTo(x, h - padB); g.stroke();
    const lab = fmtAxis(t, span);
    g.fillStyle = cssDim;
    g.fillText(lab, clamp(x, 22, w - 22), h - padB + 6);
  }

  // 基线
  if (d.baseline !== null) {
    const by = Math.round(Y(d.baseline)) + 0.5;
    g.strokeStyle = withAlpha(cssYellow, 0.75);
    g.setLineDash([5, 4]);
    g.lineWidth = 1;
    g.beginPath(); g.moveTo(padL, by); g.lineTo(w - padR, by); g.stroke();
    g.setLineDash([]);
    g.textAlign = 'right';
    g.textBaseline = 'bottom';
    g.fillStyle = cssYellow;
    g.fillText(`基线 ${fmtDb(d.baseline)} dB`, w - padR - 2, by - 4);
  }

  // 原始发射峰值（点）
  g.fillStyle = withAlpha(cssMut, 0.55);
  for (const [t, v] of pts) {
    g.beginPath();
    g.arc(X(t), Y(v), 1.7, 0, Math.PI * 2);
    g.fill();
  }

  // 趋势线
  if (detailTrend.length) {
    g.beginPath();
    g.moveTo(X(detailTrend[0][0]), Y(detailTrend[0][1]));
    for (let i = 1; i < detailTrend.length; i++) {
      g.lineTo(X(detailTrend[i][0]), Y(detailTrend[i][1]));
    }
    g.strokeStyle = withAlpha(cssAcc, 0.16);
    g.lineWidth = 7;
    g.lineJoin = 'round';
    g.lineCap = 'round';
    g.stroke();
    g.strokeStyle = cssAcc;
    g.lineWidth = 2.1;
    g.stroke();
  }

  // 最近一次有效发射
  if (d.last) {
    g.fillStyle = cssAcc;
    g.beginPath(); g.arc(X(d.last.t), Y(d.last.v), 3, 0, Math.PI * 2); g.fill();
    g.strokeStyle = withAlpha(cssAcc, 0.35);
    g.lineWidth = 5;
    g.beginPath(); g.arc(X(d.last.t), Y(d.last.v), 5.5, 0, Math.PI * 2); g.stroke();
  }

  // 悬停十字与标记
  if (detailHover) {
    const hx = X(detailHover.t);
    g.strokeStyle = withAlpha(cssMut, 0.55);
    g.lineWidth = 1;
    g.setLineDash([2, 3]);
    g.beginPath(); g.moveTo(hx, padT); g.lineTo(hx, h - padB); g.stroke();
    g.setLineDash([]);
    g.fillStyle = cssOk;
    g.beginPath(); g.arc(hx, Y(detailHover.v), 3.2, 0, Math.PI * 2); g.fill();
    g.strokeStyle = cssOk;
    g.lineWidth = 1.5;
    g.beginPath(); g.arc(hx, Y(detailHover.v), 5, 0, Math.PI * 2); g.stroke();
  }
}

function nearestPoint(px) {
  const g = detailGeo, d = detailData;
  if (!g || !d || !d.points.length) return null;
  const t = g.t0 + clamp((px - g.padL) / g.iw, 0, 1) * (g.t1 - g.t0);
  let best = null, dist = Infinity;
  for (let i = 0; i < d.points.length; i++) {
    const dd = Math.abs(d.points[i][0] - t);
    if (dd < dist) { dist = dd; best = i; }
  }
  const p = d.points[best];
  return { i: best, t: p[0], v: p[1], n: p[2] };
}

function showDetailTip(e) {
  const cv = $('detailChart');
  const tip = $('chartTip');
  const p = nearestPoint(e.offsetX);
  if (!p) { tip.hidden = true; return; }
  detailHover = p;
  drawDetail();
  const trend = detailTrend[p.i] ? detailTrend[p.i][1] : null;
  tip.innerHTML = `<b>${fmtClock(p.t)}</b><br>发射峰值 ${fmtDb(p.v)} dB<br>`
    + (trend === null ? '' : `趋势 ${fmtDb(trend)} dB<br>`)
    + `本桶 ${p.n} 个原始样本`;
  tip.hidden = false;
  const wrap = $('detailChartWrap');
  const cr = cv.getBoundingClientRect(), wr = wrap.getBoundingClientRect();
  const px = detailGeo ? detailGeo.padL + clamp((p.t - detailGeo.t0) / (detailGeo.t1 - detailGeo.t0), 0, 1) * detailGeo.iw : e.offsetX;
  let left = cr.left - wr.left + px + 14;
  let top = cr.top - wr.top + 10;
  tip.style.left = '0px';
  tip.style.top = '0px';
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  if (left + tw > wrap.clientWidth - 4) left = cr.left - wr.left + px - tw - 14;
  if (left < 4) left = 4;
  if (top + th > wrap.clientHeight - 4) top = wrap.clientHeight - th - 4;
  tip.style.left = left + 'px';
  tip.style.top = top + 'px';
}

function hideDetailTip() {
  detailHover = null;
  const tip = $('chartTip');
  if (tip) tip.hidden = true;
  drawDetail();
}

function initDetailControls() {
  $('chSearch').addEventListener('input', (e) => {
    chQuery = e.target.value.trim().toLowerCase();
    buildChannelList(lastState);
  });
  $('chFilter').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-filter]');
    if (!b) return;
    chFilter = b.dataset.filter;
    document.querySelectorAll('#chFilter button').forEach((x) =>
      x.classList.toggle('on', x === b));
    buildChannelList(lastState);
  });
  document.querySelectorAll('#detailRange button').forEach((b) => {
    b.addEventListener('click', () => {
      detailRange = Number(b.dataset.range);
      try { localStorage.setItem('radio-detail-range', detailRange); } catch (e) {}
      updateDetailToolbar();
      detailHover = null;
      loadDetail();
    });
  });
  const cv = $('detailChart');
  cv.addEventListener('mousemove', showDetailTip);
  cv.addEventListener('mouseleave', hideDetailTip);
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { drawAllChan(); drawDetail(); }, 150);
  });
}

function updateDetailToolbar() {
  document.querySelectorAll('#detailRange button[data-range]').forEach((b) => {
    b.classList.toggle('on', Number(b.dataset.range) === detailRange);
  });
}

function setTab(name) {
  const isChannel = name === 'channel';
  $('viewOverview').hidden = isChannel;
  $('viewChannel').hidden = !isChannel;
  document.querySelectorAll('#tabs button').forEach((b) =>
    b.classList.toggle('on', b.dataset.tab === name));
  if (isChannel) {
    ensureChannelList(lastState);
    if (!detailKey) selectDefaultChannel();
    else loadDetail();
  } else {
    drawAllChan();
  }
  history.replaceState(null, '', '#' + name);
}

function initTabs() {
  document.querySelectorAll('#tabs button').forEach((b) => {
    b.addEventListener('click', () => setTab(b.dataset.tab));
  });
  window.addEventListener('hashchange', () => {
    setTab(location.hash === '#channel' ? 'channel' : 'overview');
  });
}

/* ── polling ── */
async function pollState() {
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    const d = await r.json();
    // 数据过期检测：采集停了但进程还在时，必须让用户看到"停更"，而不是冻结的数字
    const age = Date.now() / 1000 - (d.ts || 0);
    if (age > 8) {
      d.device = false;
      d.verdict = Object.assign({}, d.verdict, {
        level: 'bad',
        title: '数据停止更新',
        sub: `最后一次采样在 ${Math.round(age)} 秒前，可能设备已断开`,
      });
    }
    lastState = d;
    renderStatus(d);
    renderSiteCards(d);
    renderIssues(d);
    renderMatrix(d);
    if (!$('viewChannel').hidden) {
      ensureChannelList(d);
      if (!detailKey) selectDefaultChannel();
    }
  } catch (e) {
    $('vTitle').textContent = '后端断开';
    $('verdictCard').dataset.state = 'bad';
    $('vSub').textContent = '连接不到采集服务';
  }
}

async function pollChanTrend() {
  const seq = ++chanSeq;
  try {
    const r = await fetch('/api/chan-trend?range=' + chanRange, { cache: 'no-store' });
    const d = await r.json();
    if (seq !== chanSeq) return;
    chanTrend = d;
    drawAllChan();
  } catch (e) { /* 下一轮再试 */ }
}

(function boot() {
  const qp = new URLSearchParams(location.search).get('theme');
  const saved = qp || localStorage.getItem('dsh-theme') || 'dark';
  applyTheme(saved === 'light' ? 'light' : 'dark');
  $('themeBtn').addEventListener('click', () =>
    applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'));

  document.querySelectorAll('#chanToolbar button[data-range]').forEach((b) => {
    b.addEventListener('click', () => {
      chanRange = Number(b.dataset.range);
      updateChanToolbar();
      pollChanTrend();
    });
  });

  try {
    const savedRange = Number(localStorage.getItem('radio-detail-range'));
    if ([900, 1800, 3600, 10800].includes(savedRange)) detailRange = savedRange;
  } catch (e) {}
  updateChanToolbar();
  updateDetailToolbar();
  initTabs();
  initDetailControls();
  setTab(location.hash === '#channel' ? 'channel' : 'overview');
  pollState();
  pollChanTrend();
  setInterval(pollState, 250);
  setInterval(pollChanTrend, 2000);
  setInterval(() => {
    if (!$('viewChannel').hidden && detailKey) loadDetail();
  }, 5000);
})();
