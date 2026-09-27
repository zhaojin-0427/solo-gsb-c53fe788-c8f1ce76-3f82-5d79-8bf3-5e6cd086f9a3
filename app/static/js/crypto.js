/* 与 app/crypto.py 逐字节对应的编码与抽签逻辑（纯 Web Crypto，无依赖）。 */
'use strict';

const FairCrypto = (() => {
  const FRAME_TAG = 0x55; // 'U'
  const COMMITMENT_DOMAIN = new TextEncoder().encode('fair-draw-commitment-v1');
  const SEED_DOMAIN = new TextEncoder().encode('fair-draw-seed-v1');
  const U32 = new Uint8Array(4);
  const U32V = new DataView(U32.buffer);

  function frame(data) {
    U32V.setUint32(0, data.length, false); // big-endian
    const out = new Uint8Array(1 + 4 + data.length);
    out[0] = FRAME_TAG;
    out.set(U32, 1);
    out.set(data, 5);
    return out;
  }

  function concat(parts) {
    const len = parts.reduce((n, p) => n + p.length, 0);
    const out = new Uint8Array(len);
    let off = 0;
    for (const p of parts) { out.set(p, off); off += p.length; }
    return out;
  }

  // 逐元素 frame 后拼接，再整体 frame（对应 Python nested()）
  function nested(items) {
    return frame(concat(items.map((x) => frame(x))));
  }

  function bytes(s) {
    return typeof s === 'string' ? new TextEncoder().encode(s) : s;
  }

  async function sha256(data) {
    const digest = await crypto.subtle.digest('SHA-256', data);
    return new Uint8Array(digest);
  }

  function toHex(u8) {
    return Array.from(u8, (b) => b.toString(16).padStart(2, '0')).join('');
  }

  async function hmacSha256(key, msg) {
    const cryptoKey = await crypto.subtle.importKey(
      'raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    const sig = await crypto.subtle.sign('HMAC', cryptoKey, msg);
    return new Uint8Array(sig);
  }

  // ---- 成员承诺：nested([domain, nested([round_id, idx, label, secret])]) ----
  function commitmentInput(roundId, memberIndex, memberLabel, secret) {
    const inner = nested([
      bytes(roundId),
      bytes(String(memberIndex)),
      bytes(memberLabel),
      bytes(secret),
    ]);
    return nested([COMMITMENT_DOMAIN, inner]);
  }

  async function computeCommitment(roundId, memberIndex, memberLabel, secret) {
    return toHex(await sha256(commitmentInput(roundId, memberIndex, memberLabel, secret)));
  }

  // ---- 轮次种子：绑定不可变配置 + 按固定顺序排列的全部秘密 ----
  function seedInput(cfg, secretsInOrder) {
    const payload = nested([
      bytes(cfg.round_id),
      bytes(cfg.title),
      nested(cfg.members.map(bytes)),
      nested(cfg.prizes.map(bytes)),
      bytes(cfg.commit_deadline),
      bytes(cfg.reveal_deadline),
      nested(secretsInOrder.map(bytes)),
    ]);
    return nested([SEED_DOMAIN, payload]);
  }

  async function computeSeed(cfg, secretsInOrder) {
    return sha256(seedInput(cfg, secretsInOrder));
  }

  const TWO256 = (() => {
    // 1n << 256n
    let x = 1n;
    for (let i = 0; i < 256; i++) x <<= 1n;
    return x;
  })();

  function bytesToBigInt(u8) {
    let n = 0n;
    for (const b of u8) n = (n << 8n) | BigInt(b);
    return n;
  }

  function u64be(counter) {
    const a = new Uint8Array(8);
    new DataView(a.buffer).setBigUint64(0, BigInt(counter), false);
    return a;
  }

  async function uniformInteger(seed, counter, upper, trace, i) {
    const upperN = BigInt(upper);
    const limit = TWO256 - (TWO256 % upperN);
    let candidate;
    for (;;) {
      const digest = await hmacSha256(seed, u64be(counter));
      candidate = bytesToBigInt(digest);
      const accepted = candidate < limit;
      if (trace) {
        trace.push({
          i, upper, counter,
          candidate_mod: Number(candidate % upperN),
          accepted,
          value: accepted ? Number(candidate % upperN) : -1,
        });
      }
      counter += 1;
      if (accepted) return [Number(candidate % upperN), counter];
    }
  }

  async function fairPermutation(seed, n, trace) {
    const arr = Array.from({ length: n }, (_, k) => k);
    let counter = 0;
    for (let i = n - 1; i >= 1; i--) {
      const [j, next] = await uniformInteger(seed, counter, i + 1, trace, i);
      counter = next;
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
  }

  async function draw(cfg, secretsInOrder, wantTrace) {
    const seed = await computeSeed(cfg, secretsInOrder);
    const trace = wantTrace ? [] : null;
    const permutation = await fairPermutation(seed, cfg.members.length, trace);
    const winners = cfg.prizes.map((prize, p) => ({
      prize_index: p,
      prize,
      member_index: permutation[p],
      member: cfg.members[permutation[p]],
    }));
    return { seed: toHex(seed), permutation, winners, trace: trace || [] };
  }

  function randomSecret() {
    const a = new Uint8Array(16);
    crypto.getRandomValues(a);
    return toHex(a);
  }

  return {
    frame, nested, computeCommitment, computeSeed, draw,
    fairPermutation, uniformInteger, randomSecret, toHex,
  };
})();
