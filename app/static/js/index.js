'use strict';

const $ = (id) => document.getElementById(id);

function showErr(el, msg) {
  if (!msg) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.textContent = msg;
}

// ---- 选项卡 ----
const tabCreate = $('tab-create'), tabJoin = $('tab-join');
function setTab(name) {
  const create = name === 'create';
  tabCreate.classList.toggle('active', create);
  tabJoin.classList.toggle('active', !create);
  $('create-panel').hidden = !create;
  $('join-panel').hidden = create;
  history.replaceState(null, '', create ? '/' : '/#join');
}
tabCreate.addEventListener('click', (e) => { e.preventDefault(); setTab('create'); });
tabJoin.addEventListener('click', (e) => { e.preventDefault(); setTab('join'); });
if (location.hash === '#join') setTab('join');

// 预填邀请码（主持人页面链接形如 /#join=CODE）
if (location.hash.startsWith('#join=')) {
  $('f-code').value = location.hash.slice(6).toUpperCase();
  setTab('join');
}

// ---- 默认截止时间：承诺 5 分钟后，揭示 15 分钟后 ----
function pad(n) { return String(n).padStart(2, '0'); }
function toLocalInput(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
$('f-commit-deadline').value = toLocalInput(new Date(Date.now() + 5 * 60e3));
$('f-reveal-deadline').value = toLocalInput(new Date(Date.now() + 15 * 60e3));

// ---- 创建轮次 ----
$('btn-create').addEventListener('click', async () => {
  const errEl = $('create-err');
  showErr(errEl, null);
  const title = $('f-title').value.trim();
  const members = $('f-members').value.split('\n').map((s) => s.trim()).filter(Boolean);
  const prizes = $('f-prizes').value.split('\n').map((s) => s.trim()).filter(Boolean);
  if (!title) return showErr(errEl, '请填写标题');
  if (members.length < 2) return showErr(errEl, '至少需要 2 名成员');
  if (new Set(members).size !== members.length) return showErr(errEl, '成员名不能重复');
  if (prizes.length < 1) return showErr(errEl, '至少需要 1 个奖项');
  if (new Set(prizes).size !== prizes.length) return showErr(errEl, '奖项名不能重复');
  if (prizes.length > members.length) return showErr(errEl, '奖项不能多于成员');

  let commitDeadline, revealDeadline;
  try {
    commitDeadline = App.localToIso($('f-commit-deadline').value);
    revealDeadline = App.localToIso($('f-reveal-deadline').value);
  } catch {
    return showErr(errEl, '截止时间无效');
  }

  const btn = $('btn-create');
  btn.disabled = true;
  try {
    const data = await App.api('POST', '/api/rounds', {
      title, members, prizes,
      commit_deadline: commitDeadline,
      reveal_deadline: revealDeadline,
    });
    localStorage.setItem(App.LS_HOST(data.round_id), data.host_token);
    location.href = `/host.html?r=${encodeURIComponent(data.round_id)}`;
  } catch (e) {
    showErr(errEl, e.message);
    btn.disabled = false;
  }
});

// ---- 加入轮次 ----
$('btn-join').addEventListener('click', async () => {
  const errEl = $('join-err');
  showErr(errEl, null);
  const code = $('f-code').value.trim().toUpperCase();
  if (!code) return showErr(errEl, '请输入邀请码');
  const btn = $('btn-join');
  btn.disabled = true;
  try {
    const data = await App.api('POST', '/api/rounds/join', { code });
    localStorage.setItem(App.LS_MEMBER(data.round_id), JSON.stringify({
      token: data.session_token,
      member_index: data.member_index,
      member: data.member,
    }));
    location.href = `/member.html?r=${encodeURIComponent(data.round_id)}`;
  } catch (e) {
    showErr(errEl, e.code === 'code_used'
      ? '该一次性邀请码已被使用。请在最初加入的设备/浏览器上打开成员页（会话令牌保存在那里）。'
      : e.message);
    btn.disabled = false;
  }
});

$('f-code').addEventListener('input', (e) => {
  e.target.value = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 12);
});
