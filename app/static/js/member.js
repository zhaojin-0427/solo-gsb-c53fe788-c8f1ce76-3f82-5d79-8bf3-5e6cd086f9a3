'use strict';

const $ = (id) => document.getElementById(id);

const roundId = new URLSearchParams(location.search).get('r');
const stored = roundId && localStorage.getItem(App.LS_MEMBER(roundId));
let session = null;
try { session = stored ? JSON.parse(stored) : null; } catch { session = null; }

let latest = null;
let generatedSecret = null;
let mySecret = null;
let currentCommitment = null;

function getCurrentSecret() {
  // 自定义框非空时以其为准，否则使用本机随机秘密
  const custom = $('secret-custom').value.trim();
  return custom || generatedSecret;
}

function fatal(msg) {
  const el = $('fatal');
  el.hidden = false;
  el.innerHTML = msg;
}

if (!roundId || !session?.token) {
  fatal('未找到你的成员会话。请使用一次性邀请码加入（邀请码只能使用一次，会话保存在加入时的浏览器中）。<br>' +
    '<a href="/">返回首页</a>');
} else {
  init();
}

async function init() {
  // 恢复本机随机秘密；没有则生成一个
  generatedSecret = localStorage.getItem(App.LS_SECRET(roundId, session.member_index));
  if (!generatedSecret) {
    generatedSecret = FairCrypto.randomSecret();
    localStorage.setItem(App.LS_SECRET(roundId, session.member_index), generatedSecret);
  }
  mySecret = generatedSecret;
  $('secret-value').textContent = mySecret;

  $('btn-regen').addEventListener('click', async () => {
    if (latest && myState()?.committed) {
      alert('你已经提交过承诺，不能更换秘密！');
      return;
    }
    generatedSecret = FairCrypto.randomSecret();
    localStorage.setItem(App.LS_SECRET(roundId, session.member_index), generatedSecret);
    $('secret-custom').value = '';
    $('secret-warn').hidden = true;
    await syncSecret();
  });
  $('secret-custom').addEventListener('input', syncSecret);
  $('btn-copy-secret').addEventListener('click', () =>
    copyText($('secret-value').textContent, $('btn-copy-secret')));
  $('btn-commit').addEventListener('click', doCommit);
  $('btn-reveal').addEventListener('click', doReveal);

  const socket = new App.RoomSocket(roundId, null, session.token, onSnapshot, onStatus);
  socket.connect();
  setInterval(tickCountdowns, 1000);
}

async function syncSecret() {
  // 自定义框非空时以其为准，否则使用本机随机秘密
  const custom = $('secret-custom').value.trim();
  mySecret = custom || generatedSecret;
  $('secret-value').textContent = mySecret;
  await refreshCommitmentPreview();
  const me = myState();
  $('secret-warn').hidden = !(custom && me?.committed && me.commitment &&
    currentCommitment && currentCommitment !== me.commitment);
}

function onStatus(st) {
  const map = { connecting: '连接中…', live: '● 实时连接', reconnecting: '断线重连中…' };
  $('ws-status').textContent = map[st] || st;
}

function myState() {
  return latest?.members.find((m) => m.index === session.member_index);
}

async function onSnapshot(snap) {
  latest = snap;
  App.noteServerTime(snap.server_time);
  $('room').hidden = false;
  await render();
}

async function currentLabel() {
  const me = myState();
  return me ? me.label : session.member || '';
}

async function refreshCommitmentPreview() {
  const label = await currentLabel();
  if (!label) return;
  currentCommitment = await FairCrypto.computeCommitment(
    roundId, session.member_index, label, mySecret);
  const el = $('commitment-value');
  el.textContent = currentCommitment;
  el.classList.remove('muted');
  checkRevealConsistency();
}

function checkRevealConsistency() {
  const me = myState();
  const warn = $('reveal-mismatch-warn');
  if (me?.committed && me.commitment && currentCommitment &&
      latest?.status === 'revealing') {
    warn.hidden = currentCommitment === me.commitment;
  } else {
    warn.hidden = true;
  }
}

const STATUS_TEXT = {
  committing: '承诺阶段', revealing: '揭示阶段',
  settled: '已结算', voided: '已作废',
};

async function render() {
  const s = latest;
  $('r-title').textContent = s.title;
  $('r-id').textContent = s.round_id;
  $('me-label').textContent = session.member || myState()?.label || '';
  $('me-index').textContent = session.member_index;
  const pill = $('r-status');
  pill.textContent = STATUS_TEXT[s.status] || s.status;
  pill.className = `pill ${s.status}`;
  $('r-commit-dl').textContent = App.fmtTime(s.commit_deadline);
  $('r-reveal-dl').textContent = App.fmtTime(s.reveal_deadline);

  const me = myState();
  await refreshCommitmentPreview();

  const voidBox = $('void-box');
  if (s.status === 'voided') {
    const reasons = {
      commit_timeout: '⛔ 承诺阶段超时：未在截止时间前收齐全部承诺，整轮作废。',
      reveal_timeout: '⛔ 揭示阶段超时：未在截止时间前收齐全部有效揭示，整轮作废。',
      commitment_mismatch: '⛔ 有成员揭示的秘密与其承诺不符，整轮作废。',
      conflicting_reveal: '⛔ 有成员先后揭示了不同的秘密，整轮作废。',
      missing_commitment: '⛔ 内部错误：缺少承诺，整轮作废。',
    };
    voidBox.hidden = false;
    voidBox.textContent = reasons[s.void_reason] || `整轮作废：${s.void_reason}`;
  } else {
    voidBox.hidden = true;
  }

  // 步骤可见性
  $('step-secret').style.opacity = me?.committed ? '0.75' : '1';
  $('step-commit').style.display =
    s.status === 'committing' || me?.committed ? '' : 'none';
  $('commit-done').hidden = !me?.committed;
  $('btn-commit').disabled = !!me?.committed || s.status !== 'committing';

  const inReveal = s.status === 'revealing' || s.status === 'settled';
  $('step-reveal').hidden = !inReveal;
  $('reveal-done').hidden = !me?.revealed;
  $('btn-reveal').disabled = !!me?.revealed || s.status !== 'revealing';
  $('reveal-secret').textContent = mySecret;

  $('step-result').hidden = s.status !== 'settled';
  if (s.status === 'settled' && s.result) renderResult(s);

  renderMembers(s);
  renderEvents(s);
  checkRevealConsistency();
  tickCountdowns();
}

function renderResult(s) {
  const rows = s.result.winners.map((w) => `
    <tr><td>${App.esc(w.prize)}</td>
    <td class="${w.member_index === session.member_index ? 'tag-ok' : ''}">
      <strong>${App.esc(w.member)}</strong>${w.member_index === session.member_index ? ' 🎉 恭喜你！' : ''}
    </td></tr>`).join('');
  $('result-box').innerHTML = `
    <table><thead><tr><th>奖项</th><th>中奖成员</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="muted small" style="margin-top:8px">
      完整排列 <span class="mono">[${s.result.permutation.join(', ')}]</span><br>
      种子 <span class="mono">${s.result.seed}</span>
    </div>`;
}

function renderMembers(s) {
  $('member-tbody').innerHTML = s.members.map((m) => `
    <tr${m.index === session.member_index ? ' style="background:#1c2740"' : ''}>
      <td class="mono">${m.index}${m.index === session.member_index ? '（你）' : ''}</td>
      <td>${App.esc(m.label)}</td>
      <td>${m.committed ? '<span class="tag-ok">✓</span>' : '<span class="tag-no">…</span>'}</td>
      <td>${m.revealed ? '<span class="tag-ok">✓</span>' : '<span class="tag-no">…</span>'}</td>
    </tr>`).join('');
}

function renderEvents(s) {
  const labels = {
    created: '创建轮次', joined: '成员加入', committed: '提交承诺',
    revealed: '揭示秘密', stage: '阶段切换', settled: '完成结算',
  };
  $('event-list').innerHTML = s.events.map((e) => {
    let detail = '';
    if (e.member_index !== null && e.member_index !== undefined) detail = `成员 #${e.member_index}`;
    if (e.type === 'stage') {
      detail = String(e.payload?.reason || '').endsWith('_timeout')
        ? `整轮作废：${e.payload.reason}`
        : `→ ${e.payload?.status || ''}（${e.payload?.reason || ''}）`;
    }
    if (e.type === 'settled') detail = `种子 ${(e.payload?.seed || '').slice(0, 20)}…`;
    return `<div class="ev"><span class="t">#${e.seq} ${App.fmtTime(e.at)}</span>
      <span>${labels[e.type] || e.type} ${App.esc(detail)}</span></div>`;
  }).reverse().join('');
}

function tickCountdowns() {
  if (!latest) return;
  const s = latest;
  if (s.status === 'committing') {
    $('r-commit-cd').innerHTML = `剩余 <strong>${
      App.fmtCountdown(App.deadlineIn(s.commit_deadline, s.server_time))}</strong>`;
    $('r-reveal-cd').textContent = '等待全员承诺';
  } else if (s.status === 'revealing') {
    $('r-commit-cd').textContent = '承诺阶段已结束';
    $('r-reveal-cd').innerHTML = `剩余 <strong>${
      App.fmtCountdown(App.deadlineIn(s.reveal_deadline, s.server_time))}</strong>`;
  } else {
    $('r-commit-cd').textContent = '';
    $('r-reveal-cd').textContent = '';
  }
}

// ---- 提交动作 ----
async function doCommit() {
  const errEl = $('commit-err');
  errEl.hidden = true;
  const btn = $('btn-commit');
  btn.disabled = true;
  try {
    await App.api('POST', `/api/rounds/${roundId}/commitments`,
      { commitment: currentCommitment },
      App.authHeaders(null, session.token));
  } catch (e) {
    errEl.hidden = false;
    errEl.textContent = e.message;
    btn.disabled = false;
  }
  // 快照由 WebSocket 广播驱动；连接异常时 REST 返回里也带快照
}

async function doReveal() {
  const errEl = $('reveal-err');
  errEl.hidden = true;
  const me = myState();
  if (me?.committed && currentCommitment !== me.commitment) {
    errEl.hidden = false;
    errEl.textContent = '本机自检不通过：当前秘密与已提交承诺不匹配。请换回生成承诺时使用的秘密。';
    return;
  }
  const btn = $('btn-reveal');
  btn.disabled = true;
  try {
    await App.api('POST', `/api/rounds/${roundId}/reveals`,
      { secret: mySecret },
      App.authHeaders(null, session.token));
  } catch (e) {
    errEl.hidden = false;
    errEl.textContent = e.code === 'commitment_mismatch'
      ? '服务器核对失败：秘密与承诺不符，整轮已作废。'
      : e.message;
    btn.disabled = false;
  }
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    if (btn) {
      const old = btn.textContent;
      btn.textContent = '已复制';
      setTimeout(() => { btn.textContent = old; }, 1200);
    }
  }).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  });
}
