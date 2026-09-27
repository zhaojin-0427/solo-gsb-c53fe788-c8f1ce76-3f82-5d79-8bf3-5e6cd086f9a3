'use strict';

const $ = (id) => document.getElementById(id);

const roundId = new URLSearchParams(location.search).get('r');
const hostToken = roundId && localStorage.getItem(App.LS_HOST(roundId));

let latest = null;
let auditCache = null;

function fatal(msg) {
  const el = $('fatal');
  el.hidden = false;
  el.innerHTML = msg;
}

if (!roundId || !hostToken) {
  fatal('未找到主持人凭证。请使用创建轮次时保存的主持人链接（令牌保存在该浏览器的 localStorage 中）。<br>' +
    '<a href="/">返回首页</a>');
} else {
  init();
}

function init() {
  $('host-url').textContent = `${location.origin}/host.html?r=${roundId}`;
  $('btn-copy-host').addEventListener('click', () =>
    copyText($('host-url').textContent, $('btn-copy-host')));
  $('btn-audit').addEventListener('click', downloadAudit);
  $('btn-audit-view').addEventListener('click', viewAudit);

  const socket = new App.RoomSocket(roundId, hostToken, null, onSnapshot, onStatus);
  socket.connect();

  setInterval(tickCountdowns, 1000);
}

function onStatus(s) {
  const map = { connecting: '连接中…', live: '● 实时连接', reconnecting: '断线重连中…' };
  $('ws-status').textContent = map[s] || s;
}

function onSnapshot(snap) {
  latest = snap;
  App.noteServerTime(snap.server_time);
  $('room').hidden = false;
  render();
}

const STATUS_TEXT = {
  committing: '承诺阶段', revealing: '揭示阶段',
  settled: '已结算', voided: '已作废',
};

function render() {
  const s = latest;
  $('r-title').textContent = s.title;
  $('r-id').textContent = s.round_id;
  const pill = $('r-status');
  pill.textContent = STATUS_TEXT[s.status] || s.status;
  pill.className = `pill ${s.status}`;
  $('r-commit-dl').textContent = App.fmtTime(s.commit_deadline);
  $('r-reveal-dl').textContent = App.fmtTime(s.reveal_deadline);

  $('prize-list').textContent = s.prizes.map((p) => p.label).join('、');

  const voidBox = $('void-box');
  if (s.status === 'voided') {
    const reasons = {
      commit_timeout: '承诺阶段超时（未在截止时间前收齐全部承诺），整轮作废。',
      reveal_timeout: '揭示阶段超时（未在截止时间前收齐全部有效揭示），整轮作废。',
      commitment_mismatch: '有成员揭示的秘密与其承诺不符，整轮作废。',
      conflicting_reveal: '有成员先后揭示了不同的秘密，整轮作废。',
      missing_commitment: '内部错误：缺少承诺，整轮作废。',
    };
    voidBox.hidden = false;
    voidBox.textContent = `⛔ ${reasons[s.void_reason] || s.void_reason}`;
  } else {
    voidBox.hidden = true;
  }

  renderResults(s);
  renderMembers(s);
  renderEvents(s);
  tickCountdowns();
}

function renderResults(s) {
  const box = $('results-box');
  if (s.status === 'settled' && s.result) {
    const rows = s.result.winners.map((w) => `
      <tr><td>${App.esc(w.prize)}</td>
      <td><strong>${App.esc(w.member)}</strong> <span class="muted small">(位次 #${w.member_index})</span></td></tr>`).join('');
    box.innerHTML = `
      <table><thead><tr><th>奖项</th><th>中奖成员</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="muted small" style="margin-top:8px">
        完整排列 permutation = <span class="mono">[${s.result.permutation.join(', ')}]</span><br>
        种子 SHA-256 = <span class="mono">${s.result.seed}</span>
      </div>`;
  } else {
    const committed = s.members.filter((m) => m.committed).length;
    const revealed = s.members.filter((m) => m.revealed).length;
    const n = s.members.length;
    box.innerHTML = `<div class="muted small">
      承诺进度 ${committed}/${n} ｜ 揭示进度 ${revealed}/${n}
      ${s.status === 'committing' ? '（全员承诺后自动进入揭示阶段）' : ''}
      ${s.status === 'revealing' ? '（全员揭示后自动结算）' : ''}
    </div>`;
  }
}

function renderMembers(s) {
  const inviteMap = Object.fromEntries((s.invites || []).map((i) => [i.member_index, i]));
  $('member-tbody').innerHTML = s.members.map((m) => {
    const inv = inviteMap[m.index];
    const link = inv
      ? `${location.origin}/#join=${inv.code}`
      : '';
    const used = inv?.used
      ? '<span class="tag-ok small">已加入</span>'
      : '<span class="muted small">未加入</span>';
    return `<tr>
      <td class="mono">${m.index}</td>
      <td>${App.esc(m.label)}</td>
      <td>${m.committed
        ? `<span class="tag-ok">✓ 已承诺</span><div class="mono muted small">${m.commitment || ''}</div>`
        : '<span class="tag-no">待承诺</span>'}</td>
      <td>${m.revealed
        ? `<span class="tag-ok">✓ 已揭示</span>${m.secret ? `<div class="mono muted small">${App.esc(m.secret)}</div>` : ''}`
        : '<span class="tag-no">待揭示</span>'}</td>
      <td>
        ${inv ? `<div class="invite-link">
          <input readonly value="${App.esc(link)}" id="inv-${m.index}">
          <button class="ghost small" onclick="copyInvite(${m.index})">复制</button>
        </div>
        <div class="mono muted small">码 ${App.esc(inv.code)} · ${used}</div>` : ''}
      </td>
    </tr>`;
  }).join('');
}

window.copyInvite = function (idx) {
  const input = $(`inv-${idx}`);
  if (input) copyText(input.value, null);
};

function renderEvents(s) {
  const labels = {
    created: '创建轮次', joined: '成员加入', committed: '提交承诺',
    revealed: '揭示秘密', stage: '阶段切换', settled: '完成结算',
  };
  $('event-list').innerHTML = s.events.map((e) => {
    let detail = '';
    if (e.type === 'committed') detail = `成员 #${e.member_index}`;
    else if (e.type === 'revealed' || e.type === 'joined') detail = `成员 #${e.member_index}`;
    else if (e.type === 'stage') {
      if (String(e.payload?.reason || '').endsWith('_timeout')) {
        detail = `整轮作废：${e.payload.reason}`;
      } else {
        detail = `→ ${e.payload?.status || ''}（${e.payload?.reason || ''}）`;
      }
    }
    else if (e.type === 'settled') detail = `种子 ${(e.payload?.seed || '').slice(0, 20)}…`;
    return `<div class="ev"><span class="t">#${e.seq} ${App.fmtTime(e.at)}</span>
      <span>${labels[e.type] || e.type} ${App.esc(detail)}</span>
      <span class="muted small">v${e.version}</span></div>`;
  }).reverse().join('');
}

function tickCountdowns() {
  if (!latest) return;
  const s = latest;
  const c1 = $('r-commit-cd');
  const c2 = $('r-reveal-cd');
  if (s.status === 'committing') {
    c1.innerHTML = `剩余 <strong class="${App.deadlineIn(s.commit_deadline, s.server_time) < 60000 ? 'tag-err' : ''}">${
      App.fmtCountdown(App.deadlineIn(s.commit_deadline, s.server_time))}</strong>`;
    c2.textContent = '';
  } else if (s.status === 'revealing') {
    c1.textContent = '承诺阶段已结束';
    c2.innerHTML = `剩余 <strong class="${App.deadlineIn(s.reveal_deadline, s.server_time) < 60000 ? 'tag-err' : ''}">${
      App.fmtCountdown(App.deadlineIn(s.reveal_deadline, s.server_time))}</strong>`;
  } else {
    c1.textContent = '';
    c2.textContent = '';
  }
}

// ---- 审计 ----
async function fetchAudit() {
  if (!auditCache) {
    auditCache = await App.api('GET', `/api/rounds/${roundId}/audit`,
      undefined, App.authHeaders(hostToken, null));
  }
  return auditCache;
}

async function downloadAudit() {
  try {
    const audit = await fetchAudit();
    const blob = new Blob([JSON.stringify(audit, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `audit-${roundId}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { alert(`导出失败：${e.message}`); }
}

async function viewAudit() {
  try {
    const audit = await fetchAudit();
    const w = window.open('', '_blank');
    w.document.write(`<pre style="font-family:monospace;font-size:12px;white-space:pre-wrap">${
      App.esc(JSON.stringify(audit, null, 2))}</pre>`);
  } catch (e) { alert(`导出失败：${e.message}`); }
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
