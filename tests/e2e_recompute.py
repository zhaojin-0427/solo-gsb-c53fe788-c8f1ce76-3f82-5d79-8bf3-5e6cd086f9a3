"""端到端：Python 服务层真实跑两轮 -> 导出审计 JSON -> Node(verify.js 同款逻辑) 复算。

多轮次回归：两轮在同一 SQLite 持久化库中先后创建并推进，Node 端必须看到
两个审计的 events.seq 都各自从 1 连续递增（与 /verify.html 的校验一致）。
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta

_tmp = tempfile.mkdtemp()
os.environ["DRAW_DB_PATH"] = os.path.join(_tmp, "e2e.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crypto, service  # noqa: E402
from app.timeutil import to_iso, utcnow  # noqa: E402


def play_round(title, members, prizes, secret_factory):
    t = utcnow()
    created = service.create_round(
        title, members, prizes,
        to_iso(t + timedelta(minutes=10)), to_iso(t + timedelta(minutes=30)))
    rid = created["round_id"]
    secrets = []
    for i in range(len(members)):
        service.join_round(created["invites"][i]["code"])
        s = secret_factory(i)
        secrets.append(s)
        service.submit_commitment(
            rid, i, crypto.compute_commitment(rid, i, members[i], s))
    for i in range(len(members)):
        service.submit_reveal(rid, i, secrets[i])
    audit = service.build_audit(rid)
    assert audit["status"] == "settled", audit["status"]
    return audit


audit1 = play_round(
    "年会抽奖", ["张三", "李四", "王五", "赵六"],
    ["一等奖", "二等奖", "三等奖"], lambda _i: crypto.new_secret(16))
# 第二轮：与第一轮在同一持久化库；其事件 seq 必须重新从 1 开始
audit2 = play_round(
    "第二轮抽奖", ["甲", "乙", "丙"], ["头奖"],
    lambda i: f"round2-secret-{i}-padding")

for name, audit in (("round1", audit1), ("round2", audit2)):
    with open(f"/tmp/e2e_audit_{name}.json", "w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False)

# 兼容旧的单文件路径
with open("/tmp/e2e_audit.json", "w", encoding="utf-8") as f:
    json.dump(audit1, f, ensure_ascii=False)

print("Python 侧结果（第一轮）：")
for w in audit1["result"]["winners"]:
    print(f"  {w['prize']} -> {w['member']} (#{w['member_index']})")
print(f"  seed = {audit1['result']['seed'][:24]}…")
print(f"  第一轮事件数 {len(audit1['events'])} / 第二轮事件数 {len(audit2['events'])}")

# Node 侧：加载浏览器 crypto.js，独立重算并对比审计；
# 并对两个轮次执行与 verify.html 相同的 seq 轮次内 1..N 校验。
node_script = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync('app/static/js/crypto.js', 'utf8');
const sandbox = { crypto: globalThis.crypto, TextEncoder, Uint8Array, Array };
vm.createContext(sandbox);
vm.runInContext(src + '\nthis.FairCrypto = FairCrypto;', sandbox);
const FC = sandbox.FairCrypto;

// 与 verify.js 中事件序列检查完全一致
function checkSeq(audit) {
  const events = audit.events || [];
  const continuous = events.length > 0 &&
    events.every((e, i) => Number.isInteger(e.seq) && e.seq === i + 1);
  const noDup = new Set(events.map((e) => e.seq)).size === events.length;
  const createdFirst = events.length > 0 && events[0].type === 'created';
  return continuous && noDup && createdFirst;
}

(async () => {
  let fail = 0;
  const ok = (n, cond, d='') => { console.log((cond?'PASS  ':'FAIL  ')+n+(d?`  [${d}]`:'')); if(!cond) fail++; };

  for (const name of ['round1', 'round2']) {
    const audit = JSON.parse(fs.readFileSync(`/tmp/e2e_audit_${name}.json`, 'utf8'));
    const c = audit.config;
    const secrets = c.members.map((_, i) =>
      audit.secrets.find((s) => s.member_index === i).secret);
    for (const cm of audit.commitments) {
      const h = await FC.computeCommitment(c.round_id, cm.member_index,
        c.members[cm.member_index], secrets[cm.member_index]);
      ok(`${name} 承诺 #${cm.member_index} 浏览器复算`, h === cm.commitment);
    }
    const recalc = await FC.draw({
      round_id: c.round_id, title: c.title, members: c.members, prizes: c.prizes,
      commit_deadline: c.commit_deadline, reveal_deadline: c.reveal_deadline,
    }, secrets, true);
    ok(`${name} 种子一致`, recalc.seed === audit.result.seed);
    ok(`${name} 排列一致`, JSON.stringify(recalc.permutation) === JSON.stringify(audit.result.permutation));
    ok(`${name} 奖项一致`, JSON.stringify(recalc.winners) === JSON.stringify(audit.result.winners));
    ok(`${name} 拒绝轨迹一致`, JSON.stringify(recalc.trace) === JSON.stringify(audit.result.trace),
       `${recalc.trace.length} 个抽样点`);
    ok(`${name} events.seq 轮次内 1..N 连续（verify.html 规则）`,
       checkSeq(audit), audit.events.map((e) => e.seq).join(','));
    ok(`${name} 首事件 seq=1/type=created`,
       audit.events[0].seq === 1 && audit.events[0].type === 'created');
  }
  const a1 = JSON.parse(fs.readFileSync('/tmp/e2e_audit_round1.json', 'utf8'));
  const a2 = JSON.parse(fs.readFileSync('/tmp/e2e_audit_round2.json', 'utf8'));
  ok('两轮审计都拥有 seq=1（按轮独立编号，非全局 AUTOINCREMENT）',
     a1.events[0].seq === 1 && a2.events[0].seq === 1);
  ok('两轮事件序列互不影响（长度不同且各自连续）',
     checkSeq(a1) && checkSeq(a2) && a1.events.length !== a2.events.length,
     `${a1.events.length} vs ${a2.events.length}`);
  process.exit(fail ? 1 : 0);
})();
"""
r = subprocess.run(["node", "-e", node_script], cwd=os.getcwd())
sys.exit(r.returncode)
