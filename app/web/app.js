'use strict';

const state = { page: 1, size: 20, total: 0, beepOn: false, audioCtx: null };

const DIRECTION_LABEL = { in: '入校', out: '出校', unknown: '—' };
const STATUS_LABEL = { ok: '', unknown_card: '未登记卡', duplicate: '重复刷卡' };
const MAX_LIVE_ITEMS = 50;

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(url + ' → ' + res.status);
  return res.json();
}

function fmtTime(iso) {
  return (iso || '').slice(11, 19);
}

function rowClass(event) {
  if (event.status === 'unknown_card') return 'row-error';
  if (event.status === 'duplicate') return 'row-warn';
  return event.direction === 'in' ? 'row-in' : 'row-out';
}

function labelFor(event) {
  return STATUS_LABEL[event.status] || DIRECTION_LABEL[event.direction];
}

function beep(ok) {
  if (!state.beepOn || !state.audioCtx) return;
  const ctx = state.audioCtx;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = 'sine';
  osc.frequency.value = ok ? 880 : 320;
  gain.gain.value = 0.15;
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.start();
  osc.stop(ctx.currentTime + (ok ? 0.09 : 0.3));
}

function renderStats(stats) {
  document.getElementById('stat-in').textContent = stats.in_count;
  document.getElementById('stat-out').textContent = stats.out_count;
  document.getElementById('stat-incampus').textContent = stats.in_campus_count;
  document.getElementById('stat-total').textContent = stats.total_students;
}

function renderLiveItem(li, event) {
  li.className = 'live-item ' + rowClass(event);
  const who = event.name || ('未登记卡 …' + event.epc.slice(-6));
  li.innerHTML = '<span class="t">' + fmtTime(event.swiped_at) + '</span>' +
                 '<span class="who">' + who + '</span>' +
                 '<span class="what">' + labelFor(event) + '</span>';
}

function findLiveItem(list, epc) {
  // 用遍历而不是 CSS 属性选择器：EPC 来自读卡器，不该拼进选择器字符串
  return Array.prototype.find.call(list.children, (el) => el.dataset.epc === epc);
}

function pushLive(event) {
  const list = document.getElementById('live');
  const empty = list.querySelector('.empty');
  if (empty) empty.remove();

  // 同一个 EPC 只占一行：常驻标签反复上报时就地更新时间并提到顶部，
  // 不再堆叠新行把学生卡刷下去。事件本身仍然照常入库。
  const existing = findLiveItem(list, event.epc);
  if (existing) {
    renderLiveItem(existing, event);
    list.prepend(existing);
  } else {
    const li = document.createElement('li');
    li.dataset.epc = event.epc;
    renderLiveItem(li, event);
    list.prepend(li);
    while (list.children.length > MAX_LIVE_ITEMS) {
      list.lastElementChild.remove();
    }
  }
  beep(event.status === 'ok');
}

function connectStream() {
  const es = new EventSource('/api/events/stream');
  es.addEventListener('swipe', (e) => {
    const event = JSON.parse(e.data);
    pushLive(event);
    refreshStats();
    if (state.page === 1) loadHistory();
  });
  return es;
}

async function refreshStats() {
  try {
    renderStats(await getJSON('/api/stats/today'));
  } catch (err) {
    /* 统计刷新失败不打断看板，健康轮询会反映服务状态 */
  }
}

async function pollHealth() {
  const dot = document.getElementById('conn-dot');
  const text = document.getElementById('conn-text');
  try {
    const h = await getJSON('/api/health');
    dot.className = 'dot ' + (h.reader_connected ? 'dot-ok' : 'dot-bad');
    text.textContent = h.reader_connected ? '读卡器已连接' : '读卡器未连接';
  } catch (err) {
    dot.className = 'dot dot-bad';
    text.textContent = '服务不可用';
  }
}

async function loadHistory() {
  const name = document.getElementById('q-name').value.trim();
  const date = document.getElementById('q-date').value;
  // 历史记录只看已登记学生；未登记卡仍入库（告警要用），只是不在这里展示
  const params = new URLSearchParams({
    page: state.page, size: state.size, registered_only: 'true',
  });
  if (name) params.set('name', name);
  if (date) params.set('date', date);

  let data;
  try {
    data = await getJSON('/api/swipes?' + params.toString());
  } catch (err) {
    return;
  }
  state.total = data.total;

  const tbody = document.getElementById('history');
  tbody.innerHTML = '';
  for (const e of data.items) {
    const tr = document.createElement('tr');
    tr.className = rowClass(e);
    tr.innerHTML = '<td>' + fmtTime(e.swiped_at) + '</td>' +
                   '<td>' + (e.name || '—') + '</td>' +
                   '<td>' + (e.student_no || '—') + '</td>' +
                   '<td>' + labelFor(e) + '</td>';
    tbody.appendChild(tr);
  }

  const pages = Math.max(1, Math.ceil(state.total / state.size));
  document.getElementById('page-info').textContent =
    '第 ' + state.page + ' / ' + pages + ' 页（共 ' + state.total + ' 条）';
  document.getElementById('prev').disabled = state.page <= 1;
  document.getElementById('next').disabled = state.page >= pages;
}

function initBeep() {
  const btn = document.getElementById('beep-toggle');
  btn.addEventListener('click', () => {
    if (!state.audioCtx) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      state.audioCtx = new Ctor();
    }
    state.audioCtx.resume();
    state.beepOn = !state.beepOn;
    btn.textContent = state.beepOn ? '关闭提示音' : '开启提示音';
    if (state.beepOn) beep(true);
  });
}

function init() {
  initBeep();
  connectStream();
  refreshStats();
  loadHistory();
  pollHealth();
  setInterval(pollHealth, 5000);

  document.getElementById('query').addEventListener('submit', (e) => {
    e.preventDefault();
    state.page = 1;
    loadHistory();
  });
  document.getElementById('q-reset').addEventListener('click', () => {
    document.getElementById('q-name').value = '';
    document.getElementById('q-date').value = '';
    state.page = 1;
    loadHistory();
  });
  document.getElementById('prev').addEventListener('click', () => {
    if (state.page > 1) { state.page -= 1; loadHistory(); }
  });
  document.getElementById('next').addEventListener('click', () => {
    state.page += 1;
    loadHistory();
  });
}

document.addEventListener('DOMContentLoaded', init);
