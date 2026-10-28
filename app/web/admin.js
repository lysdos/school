'use strict';

/* 后台管理前端。
 *
 * 只做三件事:登录态判断、学生 CRUD、刷卡记录查询/修订/删除、上传配置展示与重传。
 * 所有请求都带 Cookie(默认 same-origin 会自动带上),不需要手动处理 token。
 */

const $ = (id) => document.getElementById(id);

const DIRECTION_TAG = {
  in: ['tag-in', '入校'],
  out: ['tag-out', '出校'],
  unknown: ['tag-unknown', '未知'],
};
const STATUS_TAG = {
  ok: ['tag-ok', '正常'],
  duplicate: ['tag-dup', '重复刷卡'],
  unknown_card: ['tag-err', '未登记卡'],
};

const swipeState = { page: 1, size: 20, total: 0 };
let editingEpc = null;   // null = 新增模式，否则为编辑模式下的 EPC


// ---------- 通用请求 ----------

async function api(path, options = {}) {
  const opts = Object.assign({ credentials: 'same-origin' }, options);
  if (opts.body && typeof opts.body !== 'string') {
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers);
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    // 会话失效：回到登录页，不做任何其他处理
    showLogin();
    throw new Error('未登录');
  }
  if (!res.ok) {
    let detail = 'HTTP ' + res.status;
    try {
      const data = await res.json();
      if (data && data.detail) {
        detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
    } catch (_e) { /* 响应体不是 JSON，保留 HTTP 码 */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}


// ---------- 界面切换 ----------

let toastTimer = null;

function toast(msg, isError) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast' + (isError ? ' err' : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

function showLogin() {
  $('login').hidden = false;
  $('app').hidden = true;
  $('login-pwd').value = '';
  $('login-pwd').focus();
}

function showApp() {
  $('login').hidden = true;
  $('app').hidden = false;
  switchTab('students');
}

function switchTab(name) {
  for (const tab of document.querySelectorAll('.tab')) {
    tab.classList.toggle('active', tab.dataset.tab === name);
  }
  for (const pane of document.querySelectorAll('.pane')) {
    pane.hidden = pane.id !== 'pane-' + name;
  }
  if (name === 'students') loadStudents();
  else if (name === 'swipes') loadSwipes();
  else if (name === 'upload') loadUpload();
}


// ---------- 登录 ----------

$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('login-err');
  err.hidden = true;
  try {
    await api('/api/admin/login', { method: 'POST', body: { password: $('login-pwd').value } });
    showApp();
    toast('登录成功');
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  }
});

$('logout').addEventListener('click', async () => {
  try {
    await api('/api/admin/logout', { method: 'POST' });
  } catch (_e) { /* 已经失效也无所谓 */ }
  showLogin();
});


// ---------- 学生 ----------

async function loadStudents() {
  const q = $('stu-filter').value.trim().toLowerCase();
  let rows;
  try {
    rows = await api('/api/admin/students');
  } catch (ex) {
    if (ex.message !== '未登录') toast(ex.message, true);
    return;
  }

  if (q) {
    rows = rows.filter((r) =>
      (r.student_no || '').toLowerCase().includes(q) ||
      (r.name || '').toLowerCase().includes(q) ||
      (r.epc || '').toLowerCase().includes(q));
  }

  const tbody = $('stu-body');
  tbody.innerHTML = '';
  $('stu-empty').hidden = rows.length > 0;

  for (const r of rows) {
    const tr = document.createElement('tr');

    const epcTd = document.createElement('td');
    epcTd.className = 'epc-cell';
    epcTd.textContent = r.epc;

    const noTd = document.createElement('td');
    noTd.textContent = r.student_no;

    const nameTd = document.createElement('td');
    nameTd.textContent = r.name;

    const classTd = document.createElement('td');
    classTd.textContent = r.class_name || '—';

    const stateTd = document.createElement('td');
    const tag = document.createElement('span');
    tag.className = 'tag ' + (r.in_campus ? 'tag-in' : 'tag-unknown');
    tag.textContent = r.in_campus ? '在校' : '离校';
    stateTd.appendChild(tag);

    const lastTd = document.createElement('td');
    lastTd.textContent = r.last_swipe_at ? r.last_swipe_at.replace('T', ' ') : '—';

    const actTd = document.createElement('td');
    const actions = document.createElement('div');
    actions.className = 'row-actions';
    actions.appendChild(btn('编辑', () => openStuModal(r)));
    actions.appendChild(btn('删除', () => deleteStudent(r), true));
    actTd.appendChild(actions);

    tr.append(epcTd, noTd, nameTd, classTd, stateTd, lastTd, actTd);
    tbody.appendChild(tr);
  }
}

function btn(text, onClick, danger) {
  const b = document.createElement('button');
  b.className = 'btn-xs' + (danger ? ' danger' : '');
  b.textContent = text;
  b.type = 'button';
  b.addEventListener('click', onClick);
  return b;
}

$('stu-filter').addEventListener('input', loadStudents);

$('stu-add').addEventListener('click', () => openStuModal(null));

function openStuModal(row) {
  editingEpc = row ? row.epc : null;
  $('modal-title').textContent = row ? '编辑学生' : '新增学生';
  $('f-epc').value = row ? row.epc : '';
  // EPC 是主键，不允许改；要换卡得删了重加
  $('f-epc').disabled = !!row;
  $('f-no').value = row ? row.student_no : '';
  $('f-name').value = row ? row.name : '';
  $('f-class').value = row ? (row.class_name || '') : '';
  $('stu-err').hidden = true;
  $('modal').hidden = false;
  (row ? $('f-no') : $('f-epc')).focus();
}

$('modal-cancel').addEventListener('click', () => { $('modal').hidden = true; });

$('modal').addEventListener('click', (e) => {
  if (e.target === $('modal')) $('modal').hidden = true;
});

$('stu-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('stu-err');
  err.hidden = true;

  const payload = {
    student_no: $('f-no').value.trim(),
    name: $('f-name').value.trim(),
    class_name: $('f-class').value.trim() || null,
  };
  if (!payload.student_no || !payload.name) {
    err.textContent = '学号和姓名必填';
    err.hidden = false;
    return;
  }

  try {
    if (editingEpc) {
      await api('/api/admin/students/' + encodeURIComponent(editingEpc),
        { method: 'PATCH', body: payload });
      toast('已保存');
    } else {
      payload.epc = $('f-epc').value.trim();
      await api('/api/admin/students', { method: 'POST', body: payload });
      toast('已新增');
    }
    $('modal').hidden = true;
    await loadStudents();
  } catch (ex) {
    if (ex.message !== '未登录') {
      err.textContent = ex.message;
      err.hidden = false;
    }
  }
});

async function deleteStudent(row) {
  if (!window.confirm(
    '确定删除「' + row.name + '」?\n\n' +
    '· 该 EPC 将从名单中移除\n' +
    '· 历史刷卡记录会保留（便于审计）\n\n' +
    '此操作不可撤销。')) {
    return;
  }
  try {
    await api('/api/admin/students/' + encodeURIComponent(row.epc), { method: 'DELETE' });
    toast('已删除 ' + row.name);
    await loadStudents();
  } catch (ex) {
    if (ex.message !== '未登录') toast(ex.message, true);
  }
}


// ---------- 刷卡记录 ----------

async function loadSwipes() {
  const params = new URLSearchParams({
    page: swipeState.page, size: swipeState.size,
  });
  const add = (k, v) => { if (v) params.set(k, v); };
  add('name', $('sw-name').value.trim());
  add('date', $('sw-date').value);
  add('direction', $('sw-direction').value);
  add('status_filter', $('sw-status').value);
  add('uploaded', $('sw-uploaded').value);

  let data;
  try {
    data = await api('/api/admin/swipes?' + params.toString());
  } catch (ex) {
    if (ex.message !== '未登录') toast(ex.message, true);
    return;
  }

  swipeState.total = data.total;

  const tbody = $('sw-body');
  tbody.innerHTML = '';
  $('sw-empty').hidden = data.items.length > 0;

  for (const e of data.items) {
    const tr = document.createElement('tr');

    const timeTd = document.createElement('td');
    timeTd.textContent = (e.swiped_at || '').replace('T', ' ');

    const nameTd = document.createElement('td');
    nameTd.textContent = e.name || '—';

    const noTd = document.createElement('td');
    noTd.textContent = e.student_no || '—';

    const classTd = document.createElement('td');
    classTd.textContent = e.class_name || '—';

    const epcTd = document.createElement('td');
    epcTd.className = 'epc-cell';
    epcTd.textContent = e.epc;

    const dirTd = document.createElement('td');
    dirTd.appendChild(mkTag(DIRECTION_TAG[e.direction]));

    const stTd = document.createElement('td');
    stTd.appendChild(mkTag(STATUS_TAG[e.status]));

    const upTd = document.createElement('td');
    upTd.appendChild(mkTag(e.uploaded_at
      ? ['tag-sent', '已上传']
      : ['tag-pending', '未上传']));

    const actTd = document.createElement('td');
    const actions = document.createElement('div');
    actions.className = 'row-actions';
    actions.appendChild(btn('修正', () => editSwipe(e)));
    actions.appendChild(btn('删除', () => deleteSwipe(e), true));
    actTd.appendChild(actions);

    tr.append(timeTd, nameTd, noTd, classTd, epcTd, dirTd, stTd, upTd, actTd);
    tbody.appendChild(tr);
  }

  const pages = Math.max(1, Math.ceil(data.total / swipeState.size));
  $('sw-page').textContent =
    '第 ' + swipeState.page + ' / ' + pages + ' 页（共 ' + data.total + ' 条）';
  $('sw-prev').disabled = swipeState.page <= 1;
  $('sw-next').disabled = swipeState.page >= pages;
}

function mkTag(pair) {
  const span = document.createElement('span');
  if (pair) {
    span.className = 'tag ' + pair[0];
    span.textContent = pair[1];
  } else {
    span.className = 'tag tag-unknown';
    span.textContent = '—';
  }
  return span;
}

$('sw-search').addEventListener('click', () => {
  swipeState.page = 1;
  loadSwipes();
});

$('sw-reset').addEventListener('click', () => {
  $('sw-name').value = '';
  $('sw-date').value = '';
  $('sw-direction').value = '';
  $('sw-status').value = '';
  $('sw-uploaded').value = '';
  swipeState.page = 1;
  loadSwipes();
});

$('sw-prev').addEventListener('click', () => {
  if (swipeState.page > 1) { swipeState.page -= 1; loadSwipes(); }
});

$('sw-next').addEventListener('click', () => {
  swipeState.page += 1;
  loadSwipes();
});

async function editSwipe(e) {
  const nextDir = window.prompt(
    '修正方向（in=入校 / out=出校 / unknown=未知）\n当前：' + e.direction,
    e.direction);
  if (nextDir === null) return;

  const nextStatus = window.prompt(
    '修正状态（ok=正常 / duplicate=重复刷卡 / unknown_card=未登记卡）\n当前：' + e.status,
    e.status);
  if (nextStatus === null) return;

  try {
    await api('/api/admin/swipes/' + e.id, {
      method: 'PATCH',
      body: { direction: nextDir, status: nextStatus },
    });
    toast('已修正记录 #' + e.id);
    await loadSwipes();
  } catch (ex) {
    if (ex.message !== '未登录') toast(ex.message, true);
  }
}

async function deleteSwipe(e) {
  if (!window.confirm('确定删除这条刷卡记录？\n\n' +
      (e.name || e.epc) + ' ' + (e.swiped_at || '').replace('T', ' ') +
      '\n\n此操作不可撤销。')) {
    return;
  }
  try {
    await api('/api/admin/swipes/' + e.id, { method: 'DELETE' });
    toast('已删除记录 #' + e.id);
    await loadSwipes();
  } catch (ex) {
    if (ex.message !== '未登录') toast(ex.message, true);
  }
}


// ---------- 上传 ----------

async function loadUpload() {
  let cfg;
  try {
    cfg = await api('/api/admin/upload/config');
  } catch (ex) {
    if (ex.message !== '未登录') toast(ex.message, true);
    return;
  }

  const rows = [
    ['启用状态', cfg.enabled ? '已启用' : '未启用'],
    ['接收地址', cfg.url || '（未配置）'],
    ['Bearer Token', cfg.token ? '已设置' : '（未设置）'],
    ['超时', cfg.timeout + ' 秒'],
    ['轮询间隔', cfg.interval + ' 秒'],
    ['单批上限', cfg.batch_size + ' 条'],
  ];

  const dl = $('upload-cfg');
  dl.innerHTML = '';
  for (const [k, v] of rows) {
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = v;
    dl.append(dt, dd);
  }
}

$('reupload').addEventListener('click', async () => {
  const out = $('reupload-result');
  out.textContent = '推送中…';
  try {
    const r = await api('/api/admin/reupload', { method: 'POST' });
    // flush_once 用 reason='disabled' 表示没配 URL —— 对运维来说这是配置问题，
    // 直接把英文 reason 甩给人看没意义，这里翻译成可执行的下一步。
    if (r.reason === 'disabled') {
      out.textContent = '未配置接收地址，无法上传。请先在 .env 里设置 '
        + 'UPLOAD_ENABLED=1 和 UPLOAD_URL，再执行 docker compose up -d。';
    } else if (!r.ok) {
      out.textContent = '失败：' + (r.reason || '未知原因');
    } else if (r.sent === 0) {
      out.textContent = '没有待上传的记录。';
    } else {
      out.textContent = '已推送 ' + r.sent + ' 条，成功标记 ' + r.ok_count + ' 条。';
    }
  } catch (ex) {
    if (ex.message !== '未登录') out.textContent = '失败：' + ex.message;
  }
});


// ---------- 页签 ----------

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => switchTab(tab.dataset.tab));
}


// ---------- 启动 ----------

(async function init() {
  // 后台禁用时 whoami 会 404 —— 那就当成「后台没开」给个提示，而不是死等
  let authed = false;
  try {
    const res = await fetch('/api/admin/whoami', { credentials: 'same-origin' });
    if (res.ok) {
      const data = await res.json();
      authed = !!data.authenticated;
    }
  } catch (_e) { /* 网络异常，等用户点登录 */ }

  if (authed) showApp();
  else showLogin();
})();