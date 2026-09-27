// Node 交叉验证：加载浏览器 crypto.js，与 Python 生成的测试向量逐字节比对。
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(
  path.join(__dirname, '..', 'app', 'static', 'js', 'crypto.js'), 'utf8');
const sandbox = { crypto: globalThis.crypto, TextEncoder, Uint8Array, Array };
vm.createContext(sandbox);
vm.runInContext(src + '\nthis.FairCrypto = FairCrypto;', sandbox);
const FC = sandbox.FairCrypto;

const vectors = JSON.parse(fs.readFileSync('/tmp/vectors.json', 'utf8'));

let failures = 0;
function assert(name, actual, expected) {
  const a = typeof actual === 'string' ? actual : JSON.stringify(actual);
  const e = typeof expected === 'string' ? expected : JSON.stringify(expected);
  if (a === e) {
    console.log(`PASS  ${name}`);
  } else {
    failures++;
    console.log(`FAIL  ${name}\n  JS : ${a}\n  PY : ${e}`);
  }
}

(async () => {
  // frame / nested
  assert('frame(hello)', FC.toHex(FC.frame(new TextEncoder().encode('hello'))),
    vectors.frame_hello);
  assert('nested([a,bb,""])',
    FC.toHex(FC.nested([new TextEncoder().encode('a'),
      new TextEncoder().encode('bb'), new Uint8Array(0)])),
    vectors.nested_abc);

  // commitment
  assert('commitment',
    await FC.computeCommitment('r_abc', 2, '张三', 'p@ss 秘密!'),
    vectors.commitment);

  // seed
  const cfg = {
    round_id: 'r_abc',
    title: '年会',
    members: ['Alice', 'Bob', 'Carol', 'Dave', 'Eve'],
    prizes: ['一等奖', '二等奖'],
    commit_deadline: '2026-01-01T00:00:00Z',
    reveal_deadline: '2026-01-01T01:00:00Z',
  };
  const secrets = ['s0-secret', 's1-secret', 's2-secret', 's3-secret', 's4-secret'];
  const seedBytes = await FC.computeSeed(cfg, secrets);
  assert('seed', FC.toHex(seedBytes), vectors.seed);

  // HMAC 抽样点
  for (const [c, hex] of Object.entries(vectors.hmac_points)) {
    const a = new Uint8Array(8);
    new DataView(a.buffer).setBigUint64(0, BigInt(c), false);
    const key = await globalThis.crypto.subtle.importKey(
      'raw', seedBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    const sig = new Uint8Array(await globalThis.crypto.subtle.sign('HMAC', key, a));
    assert(`hmac(${c})`, FC.toHex(sig), hex);
  }

  // 全量 draw（含拒绝轨迹）
  const recalc = await FC.draw(cfg, secrets, true);
  assert('permutation', recalc.permutation, vectors.permutation);
  assert('winners', recalc.winners, vectors.winners);
  assert('trace', recalc.trace, vectors.trace);

  // 拒绝采样统计：30 万样本，任一桶偏差 < 1%（1% ≈ 5σ，正常应远低于此）
  const seed2 = new TextEncoder().encode('bias-test-seed-32-bytes-ok!!!!!!');
  const counts = new Array(6).fill(0);
  let counter = 0;
  const N = 300000;
  for (let k = 0; k < N; k++) {
    const [v, c2] = await FC.uniformInteger(seed2, counter, 6, null, -1);
    counter = c2;
    counts[v]++;
  }
  const maxDev = Math.max(...counts.map((c) => Math.abs(c - N / 6) / (N / 6)));
  console.log(`INFO  rejection sampling max deviation over ${N} draws: ${(maxDev * 100).toFixed(3)}%`);
  if (maxDev > 0.01) { failures++; console.log('FAIL  bias too high'); }

  console.log(failures ? `\n${failures} FAILURES` : '\nALL CHECKS PASSED');
  process.exit(failures ? 1 : 0);
})();
