"""服务层端到端测试（仅标准库）：状态机/幂等/并发/超时/作废/结算/审计闭环。"""
import json
import os
import sys
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone

_tmp = tempfile.mkdtemp()
os.environ["DRAW_DB_PATH"] = os.path.join(_tmp, "test.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import service  # noqa: E402
from app.db import DB_PATH  # noqa: E402
from app.timeutil import to_iso, utcnow  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(name)


def make_round(n=3, prizes=2, commit_minutes=10, reveal_minutes=20):
    t = utcnow()
    return service.create_round(
        "测试轮", [f"成员{i}" for i in range(n)], [f"奖项{i}" for i in range(prizes)],
        to_iso(t + timedelta(minutes=commit_minutes)),
        to_iso(t + timedelta(minutes=reveal_minutes)),
    )


# 1. 创建 + 快照
created = make_round()
rid = created["round_id"]
check("创建返回 host_token 与 3 个邀请码",
      bool(created["host_token"]) and len(created["invites"]) == 3)
snap0 = created["snapshot"]
check("初始快照 status=committing, version=1",
      snap0["status"] == "committing" and snap0["version"] == 1)
check("主持人快照含邀请码", len(snap0.get("invites", [])) == 3)

# 成员视角看不到邀请码
snap_member = service.build_snapshot(rid, role="member", member_index=0)
check("成员快照不含邀请码", "invites" not in snap_member)

# 2. 加入：一次性码
j0 = service.join_round(created["invites"][0]["code"])
j1 = service.join_round(created["invites"][1]["code"])
j2 = service.join_round(created["invites"][2]["code"])
check("加入返回会话令牌与位次",
      j0["member_index"] == 0 and bool(j0["session_token"]))
try:
    service.join_round(created["invites"][0]["code"])
    check("邀请码不可二次使用", False)
except service.ServiceError as e:
    check("邀请码不可二次使用 (410 code_used)",
          e.status_code == 410 and e.code == "code_used")

# 错误码
try:
    service.join_round("NOTEXIST0000")
    check("无效邀请码报错", False)
except service.ServiceError as e:
    check("无效邀请码 -> 404 invalid_code", e.status_code == 404)

# 3. 承诺阶段：不能揭示
secrets = ["secret-0", "secret-1", "secret-2"]
from app import crypto
commitments = [
    crypto.compute_commitment(rid, i, f"成员{i}", secrets[i]) for i in range(3)
]
try:
    service.submit_reveal(rid, 0, secrets[0])
    check("承诺阶段拒绝揭示", False)
except service.ServiceError as e:
    check("承诺阶段拒绝揭示 (409)", e.status_code == 409)

# 成员不能替别人提交（API 层按会话绑定，这里直接调服务层模拟自己的 idx）
r = service.submit_commitment(rid, 0, commitments[0])
# created=v1；join 不改变轮次版本；首次承诺 -> v2
check("第 1 个承诺后仍在 committing",
      r["snapshot"]["status"] == "committing" and r["snapshot"]["version"] == 2)

# 幂等：重复提交同一承诺
r_idem = service.submit_commitment(rid, 0, commitments[0])
check("重复同一承诺幂等（version 不增）",
      r_idem["idempotent"] and r_idem["snapshot"]["version"] == 2)

# 不同承诺拒绝
try:
    service.submit_commitment(rid, 0, "f" * 64)
    check("不同承诺被拒绝", False)
except service.ServiceError as e:
    check("不同承诺 -> 409 already_committed", e.code == "already_committed")

service.submit_commitment(rid, 1, commitments[1])
r = service.submit_commitment(rid, 2, commitments[2])
check("最后一个承诺原子切换到 revealing",
      r["snapshot"]["status"] == "revealing")
# v1 创建；3 次承诺 -> v4；阶段切换 -> v5
check("阶段切换后 version=5", r["snapshot"]["version"] == 5,
      f"actual {r['snapshot']['version']}")

# revealing 阶段新承诺被拒（但同一承诺幂等）
r_idem2 = service.submit_commitment(rid, 0, commitments[0])
check("revealing 阶段重复同一承诺仍幂等", r_idem2["idempotent"])

# 4. 承诺不符 -> 整轮作废
r2 = make_round()
rid2 = r2["round_id"]
jj = [service.join_round(r2["invites"][i]["code"]) for i in range(3)]
c2 = [crypto.compute_commitment(rid2, i, f"成员{i}", f"real-{i}") for i in range(3)]
for i in range(3):
    service.submit_commitment(rid2, i, c2[i])
try:
    service.submit_reveal(rid2, 1, "wrong-secret")
    check("承诺不符应作废", False)
except service.ServiceError as e:
    check("承诺不符 -> 409 commitment_mismatch", e.code == "commitment_mismatch")
snap_bad = service.build_snapshot(rid2, role="host")
check("不符后整轮 voided/commitment_mismatch",
      snap_bad["status"] == "voided" and snap_bad["void_reason"] == "commitment_mismatch",
      f"{snap_bad['status']}/{snap_bad['void_reason']}")
# 作废后不能继续操作
try:
    service.submit_reveal(rid2, 0, "real-0")
    check("作废轮次拒绝操作", False)
except service.ServiceError as e:
    check("作废轮次拒绝操作 (409)", e.status_code == 409)

# 5. 正常结算 + 审计复算
rr = service.submit_reveal(rid, 0, secrets[0])
check("第 1 个揭示后仍 revealing", rr["snapshot"]["status"] == "revealing")
# 幂等
rr_idem = service.submit_reveal(rid, 0, secrets[0])
check("重复同一揭示幂等", rr_idem["idempotent"])
service.submit_reveal(rid, 1, secrets[1])
rr = service.submit_reveal(rid, 2, secrets[2])
snap_settled = rr["snapshot"]
check("全员揭示后 settled", snap_settled["status"] == "settled")
check("结果含种子/排列/winners/trace",
      all(k in snap_settled["result"] for k in ("seed", "permutation", "winners", "trace")))

# 用 Python 自身（模拟浏览器独立复算）验证种子与结果
members = [f"成员{i}" for i in range(3)]
prizes = ["奖项0", "奖项1"]
seed = crypto.compute_seed(
    rid, "测试轮", members, prizes,
    snap_settled["commit_deadline"], snap_settled["reveal_deadline"], secrets)
check("审计种子可由配置+秘密独立复算", seed.hex() == snap_settled["result"]["seed"])
draw_res = crypto.draw(seed, members, prizes)
check("审计排列可独立复算",
      draw_res.permutation == snap_settled["result"]["permutation"])
check("审计奖项可独立复算",
      draw_res.winners == snap_settled["result"]["winners"])

# settled 后重复揭示幂等
rr2 = service.submit_reveal(rid, 0, secrets[0])
check("结算后重复同一揭示幂等", rr2["idempotent"])
# 不同秘密揭示 -> 作废（冲突揭示）
try:
    service.submit_reveal(rid, 0, "totally-different")
    check("结算后冲突揭示应作废", False)
except service.ServiceError:
    pass
snap_conflict = service.build_snapshot(rid, role="host")
check("冲突秘密揭示 -> conflicting_reveal 作废",
      snap_conflict["status"] == "voided" and
      snap_conflict["void_reason"] == "conflicting_reveal",
      f"{snap_conflict['status']}/{snap_conflict['void_reason']}")

# 6. 审计导出结构（使用已成功结算的轮次，后被冲突揭示作废，秘密仍保留）
audit = service.build_audit(rid)
check("审计格式标识", audit["format"] == "fair-draw-room/audit/v1")
check("审计含 config/commitments/secrets/events/result",
      all(k in audit for k in ("config", "commitments", "secrets", "events", "result")))
check("审计 config 含全部绑定字段",
      all(k in audit["config"] for k in
          ("round_id", "title", "members", "prizes", "commit_deadline", "reveal_deadline")))
# 每条承诺可由审计内秘密独立复算
secrets_by_index = {s["member_index"]: s["secret"] for s in audit["secrets"]}
ok = (len(audit["commitments"]) == 3 and len(audit["secrets"]) == 3 and all(
    crypto.compute_commitment(
        audit["config"]["round_id"], c["member_index"],
        audit["config"]["members"][c["member_index"]],
        secrets_by_index[c["member_index"]]) == c["commitment"]
    for c in audit["commitments"]))
check("审计内全部承诺可用秘密重算", ok)

# 7. 承诺超时作废（创建合法轮次后，把承诺截止时间改到过去，再触发惰性检查）
rt = make_round(commit_minutes=10, reveal_minutes=20)
ridt = rt["round_id"]
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE rounds SET commit_deadline=? WHERE id=?",
             (to_iso(utcnow() - timedelta(minutes=1)), ridt))
conn.commit(); conn.close()
snap_t = service.build_snapshot(ridt, role="host")
check("惰性检查：承诺超时 -> voided",
      snap_t["status"] == "voided" and snap_t["void_reason"] == "commit_timeout")

# 8. 揭示超时作废：全员承诺进入 revealing 后，揭示截止时间流逝
t2 = utcnow()
r4 = service.create_round(
    "揭示超时轮", ["a", "b"], ["p"],
    to_iso(t2 + timedelta(minutes=10)),
    to_iso(t2 + timedelta(minutes=20)))
rid4 = r4["round_id"]
j = [service.join_round(r4["invites"][i]["code"]) for i in range(2)]
for i in range(2):
    c = crypto.compute_commitment(rid4, i, ["a", "b"][i], f"s{i}")
    service.submit_commitment(rid4, i, c)
# 先成功揭示一个；随后把揭示截止时间拨到过去，下一次揭示惰性触发超时作废
service.submit_reveal(rid4, 0, "s0")
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE rounds SET reveal_deadline=? WHERE id=?",
             (to_iso(t2 - timedelta(minutes=1)), rid4))
conn.commit(); conn.close()
try:
    service.submit_reveal(rid4, 1, "s1")
    check("揭示超时后提交 -> 作废", False)
except service.ServiceError as e:
    check("揭示超时 -> round_voided", e.code == "round_voided")
snap_rt = service.build_snapshot(rid4, role="host")
check("整轮 reveal_timeout", snap_rt["void_reason"] == "reveal_timeout")

# 9. 后台清扫
n = service.sweep_due_rounds()
check("sweep 不崩溃且返回整数", isinstance(n, int))

# 10. 并发提交幂等（线程池同时提交同一承诺 / 不同成员最后两个承诺）
t3 = utcnow()
r5 = service.create_round(
    "并发轮", ["a", "b", "c", "d"], ["p"],
    to_iso(t3 + timedelta(minutes=30)), to_iso(t3 + timedelta(minutes=60)))
rid5 = r5["round_id"]
for i in range(4):
    service.join_round(r5["invites"][i]["code"])
# 同一成员同一承诺 10 线程并发
c0 = crypto.compute_commitment(rid5, 0, "a", "sec0")
errors = []
barrier = threading.Barrier(10)


def fire_same():
    barrier.wait()
    try:
        service.submit_commitment(rid5, 0, c0)
    except service.ServiceError as e:
        errors.append(e.code)


threads = [threading.Thread(target=fire_same) for _ in range(10)]
for th in threads:
    th.start()
for th in threads:
    th.join()
check("10 并发重复承诺：无错误", errors == [], str(errors))
cnt = None

# 不同成员并发提交最后几个承诺，阶段切换必须只发生一次
cs = [crypto.compute_commitment(rid5, i, ["a", "b", "c", "d"][i], f"sec{i}")
      for i in range(4)]
# 0 已提交；并发提交 1,2,3
barrier2 = threading.Barrier(3)
errors2 = []


def fire_other(i):
    barrier2.wait()
    try:
        service.submit_commitment(rid5, i, cs[i])
    except service.ServiceError as e:
        errors2.append(e.code)


threads = [threading.Thread(target=fire_other, args=(i,)) for i in (1, 2, 3)]
for th in threads:
    th.start()
for th in threads:
    th.join()
check("并发补齐承诺无错误", errors2 == [], str(errors2))
snap5 = service.build_snapshot(rid5, role="host")
check("并发后恰好进入 revealing 且事件不重复",
      snap5["status"] == "revealing" and
      len([e for e in snap5["events"] if e["type"] == "stage"]) == 1)

# 直接查库验证承诺行数
conn = sqlite3.connect(DB_PATH)
ncomm = conn.execute(
    "SELECT COUNT(*) FROM commitments WHERE round_id=?", (rid5,)).fetchone()[0]
stage_versions = [row[0] for row in conn.execute(
    "SELECT version FROM events WHERE round_id=? AND type='stage'", (rid5,))]
conn.close()
check("库中承诺恰好 4 行", ncomm == 4, str(ncomm))
check("版本号严格递增无跳号重复",
      stage_versions == sorted(set(stage_versions)) and len(stage_versions) == 1)

# 11. 认证（新建独立轮次避免与前面流程耦合）
t4 = utcnow()
ra = service.create_round(
    "认证轮", ["a", "b"], ["p"],
    to_iso(t4 + timedelta(minutes=30)), to_iso(t4 + timedelta(minutes=60)))
ja = service.join_round(ra["invites"][0]["code"])

try:
    service.authenticate(ra["round_id"], "badtoken", None)
    check("错误主持人令牌 -> 403", False)
except service.ServiceError as e:
    check("错误主持人令牌 -> 403", e.status_code == 403)

try:
    service.authenticate(ra["round_id"], created["host_token"], None)
    check("其他轮次的主持人令牌 -> 403", False)
except service.ServiceError as e:
    check("其他轮次的主持人令牌 -> 403", e.status_code == 403)

role, idx = service.authenticate(ra["round_id"], ra["host_token"], None)
check("正确主持人令牌 -> host", role == "host")

role, idx = service.authenticate(ra["round_id"], None, ja["session_token"])
check("会话令牌 -> member 且绑定位次", role == "member" and idx == 0)

try:
    service.authenticate(ra["round_id"], None, "nonexistent-token")
    check("无效会话令牌 -> 403", False)
except service.ServiceError as e:
    check("无效会话令牌 -> 403", e.status_code == 403)

try:
    service.authenticate(ra["round_id"], None, None)
    check("缺少凭证 -> 401", False)
except service.ServiceError as e:
    check("缺少凭证 -> 401", e.status_code == 401)

try:
    service.authenticate("r_does_not_exist", ra["host_token"], None)
    check("不存在的轮次 -> 404", False)
except service.ServiceError as e:
    check("不存在的轮次 -> 404", e.status_code == 404)

print()
print(f"{len(fails)} FAILURES" if fails else "ALL SERVICE TESTS PASSED")
sys.exit(1 if fails else 0)
