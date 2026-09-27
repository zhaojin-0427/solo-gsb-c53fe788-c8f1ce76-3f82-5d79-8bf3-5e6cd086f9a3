"""复算的防篡改测试：对真实审计做各种篡改，JS 端必须检测出来。"""
import json
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from datetime import timedelta

_tmp = tempfile.mkdtemp()
os.environ["DRAW_DB_PATH"] = os.path.join(_tmp, "tamper.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crypto, service  # noqa: E402
from app.timeutil import to_iso, utcnow  # noqa: E402

t = utcnow()
created = service.create_round(
    "篡改测试", ["甲", "乙", "丙"], ["大奖"],
    to_iso(t + timedelta(minutes=10)), to_iso(t + timedelta(minutes=30)))
rid = created["round_id"]
members = ["甲", "乙", "丙"]
secrets = []
for i in range(3):
    service.join_round(created["invites"][i]["code"])
    s = f"devsecret-{i}-xyz"
    secrets.append(s)
    service.submit_commitment(
        rid, i, crypto.compute_commitment(rid, i, members[i], s))
for i in range(3):
    service.submit_reveal(rid, i, secrets[i])
good = service.build_audit(rid)

NODE_VERIFY = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync('app/static/js/crypto.js', 'utf8');
const sandbox = { crypto: globalThis.crypto, TextEncoder, Uint8Array, Array };
vm.createContext(sandbox);
vm.runInContext(src + '\nthis.FairCrypto = FairCrypto;', sandbox);
const FC = sandbox.FairCrypto;
const audit = JSON.parse(fs.readFileSync(process.env.AUDIT, 'utf8'));
(async () => {
  const c = audit.config;
  const sec = c.members.map((_, i) =>
    (audit.secrets.find((s) => s.member_index === i) || {}).secret);
  let failures = 0;
  if (audit.status !== 'settled') { console.log('NOT_SETTLED'); process.exit(2); }
  // 承诺核对
  for (const cm of audit.commitments) {
    const h = await FC.computeCommitment(c.round_id, cm.member_index,
      c.members[cm.member_index], sec[cm.member_index]);
    if (h !== cm.commitment) { failures++; console.log(`COMMITMENT_MISMATCH #${cm.member_index}`); }
  }
  // 种子
  if (sec.some((s) => s === undefined)) { failures++; console.log('MISSING_SECRET'); }
  else {
    const recalc = await FC.draw({
      round_id: c.round_id, title: c.title, members: c.members, prizes: c.prizes,
      commit_deadline: c.commit_deadline, reveal_deadline: c.reveal_deadline,
    }, sec, true);
    if (recalc.seed !== audit.result.seed) { failures++; console.log('SEED_MISMATCH'); }
    if (JSON.stringify(recalc.permutation) !== JSON.stringify(audit.result.permutation))
      { failures++; console.log('PERM_MISMATCH'); }
    if (JSON.stringify(recalc.winners) !== JSON.stringify(audit.result.winners))
      { failures++; console.log('WINNERS_MISMATCH'); }
  }
  console.log(failures === 0 ? 'VERIFY_OK' : `VERIFY_FAIL(${failures})`);
  process.exit(failures === 0 ? 0 : 1);
})();
"""


def run_js(audit, expect_rc):
    path = "/tmp/tamper_case.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False)
    env = {**os.environ, "AUDIT": path}
    r = subprocess.run(["node", "-e", NODE_VERIFY], env=env,
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    ok = (r.returncode == 0) == (expect_rc == 0)
    print(("PASS  " if ok else "FAIL  ") +
          f"篡改场景「{case_name}」: rc={r.returncode} out={out[:80]}")
    return ok


results = []

# 正常
case_name = "原始审计（应通过）"
results.append(run_js(good, 0))

# 1. 改一个秘密
a = deepcopy(good); a["secrets"][1]["secret"] = "tampered-secret"
case_name = "替换某成员的秘密"
results.append(run_js(a, 1))

# 2. 改承诺
a = deepcopy(good); a["commitments"][0]["commitment"] = "0" * 64
case_name = "伪造某成员承诺"
results.append(run_js(a, 1))

# 3. 改成员名（顺序绑定）
a = deepcopy(good); a["config"]["members"][0], a["config"]["members"][1] = \
    a["config"]["members"][1], a["config"]["members"][0]
case_name = "交换成员顺序"
results.append(run_js(a, 1))

# 4. 改标题
a = deepcopy(good); a["config"]["title"] = "另一个标题"
case_name = "篡改轮次标题"
results.append(run_js(a, 1))

# 5. 改截止时间
a = deepcopy(good); a["config"]["commit_deadline"] = "2030-01-01T00:00:00Z"
case_name = "篡改承诺截止时间"
results.append(run_js(a, 1))

# 6. 只改结果中的种子（与秘密脱节）
a = deepcopy(good); a["result"]["seed"] = "f" * 64
case_name = "伪造结果种子"
results.append(run_js(a, 1))

# 7. 只改 winners
a = deepcopy(good); a["result"]["winners"][0]["member"] = "内定者"
case_name = "内定中奖者"
results.append(run_js(a, 1))

# 8. 截断一个秘密
a = deepcopy(good); a["secrets"][2]["secret"] = a["secrets"][2]["secret"][:-2]
case_name = "截断秘密"
results.append(run_js(a, 1))

print()
sys.exit(0 if all(results) else 1)
