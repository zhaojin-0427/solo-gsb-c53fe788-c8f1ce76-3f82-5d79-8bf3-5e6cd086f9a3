/* 审计 JSON 的浏览器独立复算：无任何网络依赖。 */
'use strict';

const $ = (id) => document.getElementById(id);

function showErr(msg) {
  const el = $('load-err');
  if (!msg) { el.hidden = true; el.textContent = ''; }
  else { el.hidden = false; el.textContent = msg; }
}

$('f-file').addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => { $('f-paste').value = reader.result; showErr(null); };
  reader.onerror = () => showErr('读取文件失败');
  reader.readAsText(file);
});

$('btn-verify').addEventListener('click', () => runVerify($('f-paste').value));

$('btn-demo').addEventListener('click', async () => {
  $('f-paste').value = JSON.stringify(await buildDemoAudit(), null, 2);
  showErr(null);
});

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
}

async function runVerify(text) {
  results.length = 0;
  showErr(null);
  let audit;
  try {
    audit = JSON.parse(text);
  } catch {
    return showErr('JSON 解析失败');
  }
  if (!audit || audit.format !== 'fair-draw-room/audit/v1' || !audit.config) {
    return showErr('不是 fair-draw-room/audit/v1 审计文件（缺少 format 或 config）');
  }

  const cfg0 = audit.config;
  const cfg = {
    round_id: cfg0.round_id,
    title: cfg0.title,
    members: cfg0.members,
    prizes: cfg0.prizes,
    commit_deadline: cfg0.commit_deadline,
    reveal_deadline: cfg0.reveal_deadline,
  };
  const n = cfg.members.length, pn = cfg.prizes.length;
  let settledRecalc = null;

  // 1. 结构一致性
  check('结构：成员 2–100 人、奖项 1..成员数、成员/奖项无重复',
    n >= 2 && n <= 100 && pn >= 1 && pn <= n &&
    new Set(cfg.members).size === n && new Set(cfg.prizes).size === pn,
    `${n} 名成员 / ${pn} 个奖项`);

  // 2. 承诺校验：每条承诺 = H(轮次,位次,姓名,秘密)
  const secretsByIndex = new Map();
  for (const s of audit.secrets || []) {
    if (s.member_index < 0 || s.member_index >= n) {
      check(`秘密 #${s.member_index} 越界`, false, '位次超出成员范围');
      continue;
    }
    if (secretsByIndex.has(s.member_index)) {
      check(`成员 #${s.member_index} 秘密重复出现`, false, '同一成员有多条秘密');
      continue;
    }
    secretsByIndex.set(s.member_index, s.secret);
    const comm = (audit.commitments || []).find(
      (c) => c.member_index === s.member_index);
    const expect = await FairCrypto.computeCommitment(
      cfg.round_id, s.member_index, cfg.members[s.member_index], s.secret);
    check(
      `承诺核对：成员 #${s.member_index}（${cfg.members[s.member_index]}）`,
      !!comm && comm.commitment === expect,
      comm
        ? (comm.commitment === expect
          ? `SHA-256 一致：${expect.slice(0, 16)}…`
          : `不符！库中 ${comm.commitment.slice(0, 16)}… ≠ 重算 ${expect.slice(0, 16)}…`)
        : '审计文件缺少该成员的承诺');
  }

  // 3. 状态相关检查
  const settled = audit.status === 'settled';
  const allSecrets = [...secretsByIndex.keys()].sort((a, b) => a - b);
  const complete = allSecrets.length === n &&
    allSecrets.every((v, i) => v === i);

  if (settled) {
    check('已结算轮次：全员秘密齐备且按固定位次排列', complete,
      complete ? `收到 ${n}/${n} 条秘密` : `仅 ${allSecrets.length}/${n} 条或位次不连续`);

    // 4. 种子复算
    const seedDigest = await FairCrypto.computeSeed(
      cfg, cfg.members.map((_, i) => secretsByIndex.get(i)));
    const seedHex = FairCrypto.toHex(seedDigest);
    check('种子复算：SHA-256(配置 ‖ 全员秘密) 与记录一致',
      !!audit.result && seedHex === audit.result.seed,
      audit.result
        ? `重算 ${seedHex.slice(0, 32)}… / 记录 ${String(audit.result.seed).slice(0, 32)}…`
        : '审计文件缺少 result');

    // 5. 洗牌复算（带拒绝轨迹）
    const recalc = await FairCrypto.draw(
      cfg, cfg.members.map((_, i) => secretsByIndex.get(i)), true);
    const res = audit.result || {};
    const permOk = JSON.stringify(recalc.permutation) ===
      JSON.stringify(res.permutation);
    check('Fisher-Yates 排列复现一致', permOk,
      `重算 [${recalc.permutation.join(',')}] / 记录 [${(res.permutation || []).join(',')}]`);
    const winOk = JSON.stringify(recalc.winners) === JSON.stringify(res.winners);
    check('奖项归属复现一致', winOk, winOk
      ? recalc.winners.map((w) => `${w.prize}→${w.member}`).join('；')
      : '重算结果与记录的 winners 不一致');

    // 6. 记录的拒绝轨迹自检（若提供）
    if (Array.isArray(res.trace) && res.trace.length) {
      const traceOk = JSON.stringify(
        recalc.trace.map((t) => ({ ...t }))) === JSON.stringify(res.trace);
      check('拒绝采样轨迹（trace）逐步一致', traceOk,
        traceOk ? `${res.trace.length} 个 HMAC 抽样点全部吻合`
          : '轨迹与独立重算不一致');
    } else {
      check('拒绝采样轨迹（trace）可用', false, '记录中未提供 trace（不影响主结论）');
    }

    renderDetail(audit, cfg, recalc, seedHex);
    settledRecalc = recalc;
  } else if (audit.status === 'voided') {
    check('作废轮次：存在作废原因', typeof audit.void_reason === 'string' &&
      audit.void_reason.length > 0, `void_reason=${audit.void_reason}`);
    // 作废轮次没有结果，不应出现 result
    check('作废轮次：不包含结算结果', !audit.result,
      audit.result ? '异常：作废轮次携带 result' : '无 result，符合预期');
  } else {
    check('轮次未结束', false,
      `status=${audit.status}，无法得到最终结果；可先核对已揭示成员的承诺`);
  }

  // 7. 事件序列与提交数据一致性
  const events = audit.events || [];
  const commitEvts = events.filter((e) => e.type === 'committed');
  check('事件序列：committed 事件数与承诺记录数相符',
    commitEvts.length === (audit.commitments || []).length,
    `事件 ${commitEvts.length} 条 / 承诺 ${(audit.commitments || []).length} 条`);
  const revealEvts = events.filter((e) => e.type === 'revealed');
  check('事件序列：revealed 事件数与秘密记录数相符',
    revealEvts.length === (audit.secrets || []).length,
    `事件 ${revealEvts.length} 条 / 秘密 ${(audit.secrets || []).length} 条`);
  const settledEvt = events.filter((e) => e.type === 'settled');
  check('事件序列：结算事件与状态一致',
    settled ? settledEvt.length === 1 : settledEvt.length === 0,
    settled ? '存在唯一 settled 事件' : `settled 事件 ${settledEvt.length} 条`);
  // seq 是「轮次内」序号：每个轮次的审计独立从 1 连续递增、无重复。
  const seqOk = events.length > 0 && events.every((e, i) =>
    Number.isInteger(e.seq) && e.seq === i + 1);
  const dupSeq = events.length === new Set(events.map((e) => e.seq)).size;
  check('事件序列：seq 轮次内从 1 连续递增、无重复', seqOk && dupSeq,
    seqOk && dupSeq
      ? `#1 … #${events.length}（每轮独立编号）`
      : (!dupSeq ? '存在重复序号' : '存在断号或非正整数序号'));
  const startsWithCreated = events.length > 0 && events[0].type === 'created';
  check('事件序列：首条事件为 created', startsWithCreated,
    startsWithCreated ? `created #${events[0].seq}` : '首条事件不是 created');

  const kind = settled ? 'settled' : (audit.status === 'voided' ? 'voided' : 'open');
  renderVerdict(kind, settledRecalc);
  renderChecks();
}

function renderChecks() {
  $('checks').innerHTML = results.map((r) => `
    <div class="ev" style="display:flex;gap:8px;padding:5px 0;border-bottom:1px dashed var(--border)">
      <span>${r.ok ? '✅' : '❌'}</span>
      <div><div>${escapeHtml(r.name)}</div>
      <div class="muted small">${escapeHtml(r.detail || '')}</div></div>
    </div>`).join('');
}

function renderVerdict(kind, recalc) {
  $('report').hidden = false;
  const failed = results.filter((r) => !r.ok);
  // “未提供 trace”仅为信息性提示，不算复算失败
  const hardFails = failed.filter((r) => r.name !== '拒绝采样轨迹（trace）可用');
  const box = $('verdict');
  if (kind === 'settled') {
    if (hardFails.length === 0) {
      box.innerHTML = `<div class="banner ok" style="font-size:15px">
        ✅ 复算通过：所有承诺有效，种子与洗牌结果可独立重现。奖项归属：
        ${recalc.winners.map((w) => `<strong>${escapeHtml(w.prize)} → ${escapeHtml(w.member)}</strong>`).join('；')}
      </div>`;
    } else {
      box.innerHTML = `<div class="banner error" style="font-size:15px">
        ❌ 复算失败：${hardFails.length} 项硬性检查未通过（见下方明细）。该审计结果不可信。</div>`;
    }
  } else if (kind === 'voided') {
    box.innerHTML = `<div class="banner warn" style="font-size:15px">
      ⚠️ 该轮次已作废，不存在可复现的中奖结果；已对已有数据完成一致性检查
      （${hardFails.length === 0 ? '全部通过' : `${hardFails.length} 项未通过`}）。</div>`;
  } else {
    box.innerHTML = `<div class="banner warn" style="font-size:15px">
      ⚠️ 轮次尚未结束（无最终结果）；已对已揭示成员完成承诺核对。</div>`;
  }
}

function renderDetail(audit, cfg, recalc, seedHex) {
  $('detail-card').hidden = false;
  $('detail').innerHTML = `
    <h3>配置（参与哈希的全部字段）</h3>
    <div class="code-block">${escapeHtml(JSON.stringify({
      ...cfg,
      secrets: cfg.members.map((_, i) =>
        (audit.secrets.find((s) => s.member_index === i) || {}).secret),
    }, null, 2))}</div>
    <h3>种子（32 字节，hex）</h3>
    <div class="code-block">${escapeHtml(seedHex)}</div>
    <h3>排列与奖项</h3>
    <div class="code-block">${escapeHtml(JSON.stringify({
      permutation: recalc.permutation,
      winners: recalc.winners,
    }, null, 2))}</div>
    <h3>拒绝采样轨迹（前 20 步）</h3>
    <div class="code-block">${escapeHtml(JSON.stringify(recalc.trace.slice(0, 20), null, 2))}</div>`;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// 生成一份完全合法的合成审计，便于演示/自测
async function buildDemoAudit() {
  const cfg = {
    round_id: 'r_demo00000000000000',
    title: '演示轮次',
    members: ['Alice', 'Bob', 'Carol', 'Dave'],
    prizes: ['一等奖', '二等奖'],
    commit_deadline: '2026-01-01T00:00:00Z',
    reveal_deadline: '2026-01-01T01:00:00Z',
  };
  const rawSecrets = ['11111111111111111111111111111111',
    '22222222222222222222222222222222',
    '33333333333333333333333333333333',
    '44444444444444444444444444444444'];
  const commitments = [];
  for (let i = 0; i < cfg.members.length; i++) {
    commitments.push({
      member_index: i, member: cfg.members[i],
      commitment: await FairCrypto.computeCommitment(
        cfg.round_id, i, cfg.members[i], rawSecrets[i]),
    });
  }
  const recalc = await FairCrypto.draw(cfg, rawSecrets, true);
  const now = '2026-01-01T01:05:00Z';
  const events = [
    { seq: 1, type: 'created', member_index: null, version: 1, at: '2026-01-01T00:10:00Z', payload: null },
    ...commitments.map((c, i) => ({
      seq: i + 2, type: 'committed', member_index: c.member_index,
      version: i + 2, at: '2026-01-01T00:20:00Z',
      payload: { commitment: c.commitment },
    })),
    { seq: 6, type: 'stage', member_index: null, version: 5, at: '2026-01-01T00:20:00Z',
      payload: { status: 'revealing', reason: 'all_committed' } },
    ...rawSecrets.map((secret, i) => ({
      seq: i + 7, type: 'revealed', member_index: i,
      version: 6 + i, at: '2026-01-01T00:30:00Z', payload: null,
    })),
    { seq: 11, type: 'settled', member_index: null, version: 10, at: now,
      payload: { seed: recalc.seed, winners: recalc.winners } },
  ];
  return {
    format: 'fair-draw-room/audit/v1',
    exported_at: now,
    config: { ...cfg, created_at: '2026-01-01T00:10:00Z' },
    commitments,
    secrets: rawSecrets.map((secret, i) => ({
      member_index: i, member: cfg.members[i], secret })),
    events,
    status: 'settled',
    void_reason: null,
    version: 10,
    result: {
      algorithm: { name: 'demo' },
      seed: recalc.seed,
      permutation: recalc.permutation,
      winners: recalc.winners,
      trace: recalc.trace,
    },
  };
}
