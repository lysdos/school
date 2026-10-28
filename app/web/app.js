'use strict';

const state = {
  page: 1, size: 20, total: 0,
  beepOn: false, audioCtx: null,
  // 出校语音播报：默认开。浏览器不支持 Web Speech 时静默回退,
  // 因为预生成音频需要后端 TTS 缓存,这一版未做。
  ttsOn: true,
};

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

// 出校语音播报:姓名 + 班级 + 出校。
// 优先用浏览器自带的 SpeechSynthesisUtterance,零依赖;
// 不支持时静默回退(走 AudioContext 短促的双音提示,听感上差不多)。
//
// 不要拿未登记卡的 EPC 拼语音,听感差也会暴露卡号;
function announceOut(event) {
  if (!state.ttsOn) return;
  if (event.direction !== 'out' || event.status !== 'ok') return;
  const who = event.name ? `${event.name}${event.class_name ? `,${event.class_name}` : ''}` : '未知学生';
  const text = `${who},出校`;

  if (typeof window.speechSynthesis !== 'undefined' &&
      typeof window.SpeechSynthesisUtterance === 'function') {
    try {
      window.speechSynthesis.cancel();  // 切到下一个再说,避免长队列
      const u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN';
      u.rate = 1.05;
      u.volume = 0.9;
      window.speechSynthesis.speak(u);
      return;
    } catch (_e) { /* 静默回退 */ }
  }
  // 兜底:出校用一个上行的双音模拟一下
  if (state.audioCtx) {
    const ctx = state.audioCtx;
    const o1 = ctx.createOscillator();
    const o2 = ctx.createOscillator();
    const gain = ctx.createGain();
    o1.frequency.value = 660;
    o2.frequency.value = 880;
    o1.connect(gain); o2.connect(gain);
    gain.connect(ctx.destination);
    gain.gain.value = 0.15;
    o1.start(); o2.start(ctx.currentTime + 0.12);
    o1.stop(ctx.currentTime + 0.18);
    o2.stop(ctx.currentTime + 0.3);
  }
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
    announceOut(event);
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

// 后台未启用时 /api/admin/whoami 返回 404 —— 那就隐藏「管理」入口，
// 免得点了进去只得到一个登录框。
async function probeAdmin() {
  const link = document.getElementById('admin-link');
  try {
    const res = await fetch('/api/admin/whoami', { credentials: 'same-origin' });
    link.hidden = !res.ok;
  } catch (err) {
    link.hidden = true;
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

async function resetData() {
  const confirmed = window.confirm(
    '确定清空所有数据吗？\n\n' +
    '· 所有进出记录会被删除\n' +
    '· 所有学生状态复位为「离校」\n' +
    '· 学生名单保留\n\n' +
    '此操作不可撤销。'
  );
  if (!confirmed) return;

  try {
    const res = await fetch('/api/reset', { method: 'POST' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const result = await res.json();

    document.getElementById('live').innerHTML = '<li class="empty">等待刷卡…</li>';
    state.page = 1;
    await Promise.all([refreshStats(), loadHistory()]);
    window.alert('已清空：删除 ' + result.cleared_swipes + ' 条记录，复位 ' +
                 result.reset_students + ' 名学生。');
  } catch (err) {
    window.alert('清空失败：' + err.message);
  }
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
    state.ttsOn = state.beepOn;        // 两个开关同步,运维更简单
    btn.textContent = state.beepOn ? '关闭提示音+语音' : '开启提示音+语音';
    if (state.beepOn) {
      beep(true);
      // 顺手播一句示例:用最常见的姓名(避免暴露任何真实数据)
      announceOut({ direction: 'out', status: 'ok',
                    name: '示例同学', class_name: '三年级一班' });
    }
  });
}

function init() {
  initBeep();
  connectStream();
  refreshStats();
  loadHistory();
  pollHealth();
  probeAdmin();
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
  document.getElementById('reset-data').addEventListener('click', resetData);
}

document.addEventListener('DOMContentLoaded', init);
