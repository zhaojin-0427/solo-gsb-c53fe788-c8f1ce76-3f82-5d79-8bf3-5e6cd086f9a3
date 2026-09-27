"""多轮次事件序号回归测试（仅标准库）。

复现缺陷场景：同一持久化数据库中先完成轮次 1，再创建并推进轮次 2 并导出
审计 JSON——两个轮次的 events.seq 都必须各自从 1 连续递增，互不影响。

另覆盖：跨轮并发写入下每轮序号不重不漏；轮次间存在作废轮不影响编号。
"""
import json
import os
import sys
import tempfile
import threading
from datetime import timedelta

_tmp = tempfile.mkdtemp()
os.environ["DRAW_DB_PATH"] = os.path.join(_tmp, "multiround.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crypto, service  # noqa: E402
from app import db  # noqa: E402
from app.timeutil import to_iso, utcnow  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(name)


def run_full_round(title, members, prizes):
    """创建 -> 全员加入/承诺/揭示 -> settled，返回 (round_id, audit)。"""
    t = utcnow()
    created = service.create_round(
        title, members, prizes,
        to_iso(t + timedelta(minutes=10)), to_iso(t + timedelta(minutes=30)))
    rid = created["round_id"]
    secrets = [f"{title}-secret-{i}" for i in range(len(members))]
    for i in range(len(members)):
        service.join_round(created["invites"][i]["code"])
        service.submit_commitment(
            rid, i, crypto.compute_commitment(rid, i, members[i], secrets[i]))
    for i in range(len(members)):
        service.submit_reveal(rid, i, secrets[i])
    audit = service.build_audit(rid)
    return rid, audit


def expect_per_round_seqs(audit):
    """与 verify.html 同款校验：seq 为正整数且 1..N 连续、无重复、首条 created。"""
    events = audit["events"]
    seq = [e["seq"] for e in events]
    continuous = (len(events) > 0 and
                  all(isinstance(s, int) and s == i + 1 for i, s in enumerate(seq)))
    no_dup = len(set(seq)) == len(seq)
    created_first = bool(events) and events[0]["type"] == "created"
    return continuous and no_dup and created_first, seq


# ---- 缺陷主场景：轮次 1 先结算，轮次 2 再结算并导出审计 -------------------
members1 = ["Alice", "Bob", "Carol"]
rid1, audit1 = run_full_round("第一轮", members1, ["金奖", "银奖"])
check("轮次 1 已结算", audit1["status"] == "settled")

members2 = ["张三", "李四", "王五", "赵六"]
rid2, audit2 = run_full_round("第二轮", members2, ["一等奖", "二等奖", "三等奖"])
check("轮次 2 已结算", audit2["status"] == "settled")

ok1, seq1 = expect_per_round_seqs(audit1)
ok2, seq2 = expect_per_round_seqs(audit2)
check("轮次 1 审计 seq 轮次内 1..N 连续", ok1, str(seq1))
check("轮次 2 审计 seq 也从 1 开始连续（核心缺陷回归）",
      ok2 and seq2[0] == 1, str(seq2))

# 同一数据库：两个轮次都用到 seq=1（全局 AUTOINCREMENT 下不可能）
check("两轮各自拥有 seq=1（按轮独立计数）",
      audit1["events"][0]["seq"] == 1 and audit2["events"][0]["seq"] == 1)

# created/joined/committed/stage/revealed/settled 的数量与类型
def event_types(audit):
    return [e["type"] for e in audit["events"]]

types2 = event_types(audit2)
check("轮次 2 事件构成正确",
      types2[0] == "created" and
      types2.count("joined") == 4 and
      types2.count("committed") == 4 and
      types2.count("revealed") == 4 and
      types2.count("stage") == 1 and
      types2.count("settled") == 1,
      str(types2))
check("轮次 2 seq 总数与事件一致",
      len(seq2) == len(audit2["events"]) == 1 + 4 + 4 + 1 + 4 + 1,
      str(seq2))

# 事件顺序（按 seq）严格保留迁移/插入先后：created 先于一切，settled 最后
check("轮次 2 created 在最前、settled 在最后",
      types2[0] == "created" and types2[-1] == "settled")

# ---- 库内直接核验：(round_id, seq) 复合唯一，跨轮存在相同 seq ----------
conn = db.connect()
rows = conn.execute("SELECT round_id, seq FROM events ORDER BY round_id, seq").fetchall()
per_round: dict[str, list[int]] = {}
for r in rows:
    per_round.setdefault(r["round_id"], []).append(r["seq"])
for rid, seqs in per_round.items():
    check(f"库内轮次 {rid[:10]}… seq=1..N 无重无漏",
          seqs == list(range(1, len(seqs) + 1)), str(seqs))
# 复合主键本身保证无重复行
dups = conn.execute(
    "SELECT round_id, seq, COUNT(*) c FROM events"
    " GROUP BY round_id, seq HAVING c > 1").fetchall()
check("库内不存在 (round_id, seq) 重复行", len(dups) == 0)

# ---- 中间插入一个作废轮：不影响相邻轮次的独立编号 -------------------------
t = utcnow()
rv = service.create_round(
    "作废轮", ["x", "y"], ["p"],
    to_iso(t + timedelta(minutes=10)), to_iso(t + timedelta(minutes=30)))
ridv = rv["round_id"]
for i in range(2):
    service.join_round(rv["invites"][i]["code"])
cv = [crypto.compute_commitment(ridv, i, ["x", "y"][i], f"real-{i}") for i in range(2)]
for i in range(2):
    service.submit_commitment(ridv, i, cv[i])
try:
    service.submit_reveal(ridv, 0, "wrong-secret")
except service.ServiceError:
    pass
auditv = service.build_audit(ridv)
okv, seqv = expect_per_round_seqs(auditv)
check("作废轮自身 seq 仍从 1 连续", okv, str(seqv))
check("作废事件落在该轮连续序列末尾",
      auditv["events"][-1]["type"] == "voided" and
      auditv["events"][-1]["seq"] == len(auditv["events"]),
      str([(e["seq"], e["type"]) for e in auditv["events"]]))

# 再建一轮，确认跨作废轮后新轮依旧从 1 开始
members3 = ["丁一", "丁二"]
rid3, audit3 = run_full_round("第三轮", members3, ["参与奖"])
ok3, seq3 = expect_per_round_seqs(audit3)
check("作废轮之后的轮次 3 seq 仍从 1 开始", ok3 and seq3[0] == 1, str(seq3))

# 前两轮快照（重新读取）序号不受后续写入影响
snap1 = service.build_snapshot(rid1, role="host")
check("轮次 1 快照序号不受后续轮次影响",
      [e["seq"] for e in snap1["events"]] == seq1)

# ---- 跨轮并发写入：每轮序号不重不漏 --------------------------------------
t = utcnow()
ra = service.create_round(
    "并发A", ["a0", "a1", "a2", "a3"], ["p"],
    to_iso(t + timedelta(minutes=30)), to_iso(t + timedelta(minutes=60)))
rb = service.create_round(
    "并发B", ["b0", "b1", "b2"], ["q"],
    to_iso(t + timedelta(minutes=30)), to_iso(t + timedelta(minutes=60)))
for i in range(4):
    service.join_round(ra["invites"][i]["code"])
for i in range(3):
    service.join_round(rb["invites"][i]["code"])

jobs = []
for i in range(4):
    c = crypto.compute_commitment(ra["round_id"], i, f"a{i}", f"sa{i}")
    jobs.append(("A", ra["round_id"], i, c))
for i in range(3):
    c = crypto.compute_commitment(rb["round_id"], i, f"b{i}", f"sb{i}")
    jobs.append(("B", rb["round_id"], i, c))

errors = []
barrier = threading.Barrier(len(jobs))


def fire(tag, rid, idx, commitment):
    barrier.wait()
    try:
        service.submit_commitment(rid, idx, commitment)
    except service.ServiceError as e:
        errors.append((tag, idx, e.code))


threads = [threading.Thread(target=fire, args=j) for j in jobs]
for th in threads:
    th.start()
for th in threads:
    th.join()
check("跨轮并发承诺全部成功", errors == [], str(errors))

for tag, created_doc, n in (("A", ra, 4), ("B", rb, 3)):
    snap = service.build_snapshot(created_doc["round_id"], role="host")
    seqs = [e["seq"] for e in snap["events"]]
    # created + n joined + n committed + 1 stage = 2n+2
    expected = list(range(1, 2 * n + 3))
    check(f"并发轮 {tag}：created+{n} joined+{n} committed+stage 共 {2*n+2} 条且 1..N 连续",
          seqs == expected, str(seqs))
    check(f"并发轮 {tag}：阶段切换事件唯一",
          len([e for e in snap["events"] if e["type"] == "stage"]) == 1)
    check(f"并发轮 {tag}：joined/committed 事件数齐全",
          len([e for e in snap["events"] if e["type"] == "joined"]) == n
          and len([e for e in snap["events"] if e["type"] == "committed"]) == n)

# ---- 导出的审计 JSON 可被独立序列化且 seq 语义保持 -----------------------
for name, audit in (("轮1", audit1), ("轮2", audit2), ("轮3", audit3)):
    reparsed = json.loads(json.dumps(audit, ensure_ascii=False))
    ok, seqs = expect_per_round_seqs(reparsed)
    check(f"{name} 审计 JSON 往返后 seq 校验通过", ok, str(seqs))

print()
print(f"{len(fails)} FAILURES" if fails else "ALL MULTI-ROUND SEQ TESTS PASSED")
sys.exit(1 if fails else 0)
