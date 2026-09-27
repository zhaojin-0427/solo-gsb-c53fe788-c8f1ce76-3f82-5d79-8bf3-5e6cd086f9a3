"""旧库迁移测试（仅标准库）：全局 AUTOINCREMENT events → 每轮独立连续编号。

手工按「升级前」的旧 schema 构造持久化数据库，两个轮次的事件在全局
AUTOINCREMENT 下交错占用 seq（1,3,5 / 2,4,6），随后让应用在该库上启动：
迁移必须把它们分别重排为每轮 1..N，保留 at 与轮次内先后顺序；迁移后
服务层还能在该轮上继续追加序号并正常结算。
"""
import json
import os
import sqlite3
import sys
import tempfile

_tmp = tempfile.mkdtemp()
DB = os.path.join(_tmp, "legacy.db")
os.environ["DRAW_DB_PATH"] = DB

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crypto  # noqa: E402  # 不触发数据库连接

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------- 构造旧库
LEGACY_SCHEMA = """
CREATE TABLE rounds (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, members_json TEXT NOT NULL,
    prizes_json TEXT NOT NULL, commit_deadline TEXT NOT NULL,
    reveal_deadline TEXT NOT NULL, status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, host_token_hash TEXT NOT NULL,
    void_reason TEXT, result_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE invites (
    code TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES rounds(id),
    member_index INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, UNIQUE(round_id, member_index)
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES rounds(id),
    member_index INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE commitments (
    round_id TEXT NOT NULL REFERENCES rounds(id), member_index INTEGER NOT NULL,
    commitment TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (round_id, member_index)
);
CREATE TABLE reveals (
    round_id TEXT NOT NULL REFERENCES rounds(id), member_index INTEGER NOT NULL,
    secret TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (round_id, member_index)
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, round_id TEXT NOT NULL REFERENCES rounds(id),
    type TEXT NOT NULL, member_index INTEGER, version INTEGER,
    payload_json TEXT, at TEXT NOT NULL
);
CREATE INDEX idx_events_round ON events(round_id, seq);
"""

conn = sqlite3.connect(DB)
conn.executescript(LEGACY_SCHEMA)
# 旧轮 A 停在 revealing（两个承诺已齐、尚未揭示），迁移后可直接揭示至结算。
# 秘密与承诺行须真实匹配，使迁移后推进产生的审计能通过承诺/事件计数核对。
legacy_secrets = ["s-yi", "s-er"]
legacy_members = ["一", "二"]
legacy_commits = [
    crypto.compute_commitment("r_legacy_a", i, legacy_members[i], legacy_secrets[i])
    for i in range(2)
]
conn.execute(
    "INSERT INTO rounds(id,title,members_json,prizes_json,commit_deadline,"
    "reveal_deadline,status,version,host_token_hash,created_at)"
    " VALUES(?,?,?,?,?,?,'revealing',4,?,?)",
    ("r_legacy_a", "旧轮A", json.dumps(legacy_members, ensure_ascii=False),
     json.dumps(["p"], ensure_ascii=False),
     "2099-01-01T00:00:00Z", "2099-01-01T01:00:00Z",
     "x" * 64, "2020-01-01T00:00:00Z"))
for i in range(2):
    conn.execute(
        "INSERT INTO commitments(round_id,member_index,commitment,created_at)"
        " VALUES('r_legacy_a',?,?, '2020-03-01T00:00:05Z')",
        (i, legacy_commits[i]))
conn.execute(
    "INSERT INTO rounds(id,title,members_json,prizes_json,commit_deadline,"
    "reveal_deadline,status,version,host_token_hash,created_at)"
    " VALUES(?,?,?,?,?,?,'revealing',4,?,?)",
    ("r_legacy_b", "旧轮B", json.dumps(["a", "b", "c"], ensure_ascii=False),
     json.dumps(["q"], ensure_ascii=False),
     "2099-01-02T00:00:00Z", "2099-01-02T01:00:00Z",
     "y" * 64, "2020-01-02T00:00:00Z"))
# 两个未使用的邀请码（迁移后用服务层真实加入）
for i, code in enumerate(("CODE00000001", "CODE00000002")):
    conn.execute(
        "INSERT INTO invites(code,round_id,member_index,used,created_at)"
        " VALUES(?,?,?,0,'2020-01-01T00:00:00Z')", (code, "r_legacy_a", i))

# 全局 AUTOINCREMENT 下两轮事件交错：A 占 1/3/5/7，B 占 2/4/6/8
legacy_events = [
    ("r_legacy_a", "created", None, 1, "2020-03-01T00:00:01Z"),
    ("r_legacy_b", "created", None, 1, "2020-03-01T00:00:02Z"),
    ("r_legacy_a", "committed", 0, 2, "2020-03-01T00:00:03Z"),
    ("r_legacy_b", "committed", 0, 2, "2020-03-01T00:00:04Z"),
    ("r_legacy_a", "committed", 1, 3, "2020-03-01T00:00:05Z"),
    ("r_legacy_b", "committed", 1, 3, "2020-03-01T00:00:06Z"),
    ("r_legacy_a", "stage", None, 4, "2020-03-01T00:00:07Z"),
    ("r_legacy_b", "stage", None, 4, "2020-03-01T00:00:08Z"),
]
for rid, etype, mi, ver, at in legacy_events:
    conn.execute(
        "INSERT INTO events(round_id,type,member_index,version,payload_json,at)"
        " VALUES(?,?,?,?,?,?)", (rid, etype, mi, ver, None, at))
conn.commit()
conn.close()

# 模拟 sqlite_sequence 中旧 AUTOINCREMENT 计数器为 6
conn = sqlite3.connect(DB)
conn.execute("INSERT OR REPLACE INTO sqlite_sequence(name,seq) VALUES('events',6)")
conn.commit()
conn.close()

# 记录迁移前的全局序号分布
conn = sqlite3.connect(DB)
before = conn.execute("SELECT seq,round_id,type,at FROM events ORDER BY seq").fetchall()
conn.close()
check("迁移前确为全局交错编号（A=1,3,5,7 / B=2,4,6,8）",
      [r[0] for r in before] == [1, 2, 3, 4, 5, 6, 7, 8])

# ---------------------------------------------------------------- 启动应用
from app import db, service  # noqa: E402

conn = db.connect()

# schema 已变为 WITHOUT ROWID 的复合主键（ALTER RENAME 后表名可能带引号）
ddl = conn.execute(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
).fetchone()[0].lower()
check("迁移后 events 为 (round_id, seq) 复合主键、无 AUTOINCREMENT",
      "autoincrement" not in ddl
      and "primarykey(round_id,seq)" in "".join(ddl.split())
      and "withoutrowid" in "".join(ddl.split()))

a_after = conn.execute(
    "SELECT seq,type,at FROM events WHERE round_id='r_legacy_a' ORDER BY seq").fetchall()
b_after = conn.execute(
    "SELECT seq,type,at FROM events WHERE round_id='r_legacy_b' ORDER BY seq").fetchall()
check("旧轮 A 重排为 1..4", [r[0] for r in a_after] == [1, 2, 3, 4])
check("旧轮 B 重排为 1..4", [r[0] for r in b_after] == [1, 2, 3, 4])

# 轮次内类型与先后顺序保留
check("旧轮 A 顺序 created→committed→committed→stage",
      [r[1] for r in a_after] == ["created", "committed", "committed", "stage"])
check("旧轮 B 顺序 created→committed→committed→stage",
      [r[1] for r in b_after] == ["created", "committed", "committed", "stage"])

# at 时间戳逐条保留（顺序也不变）
check("旧轮 A 的 at 时间原样保留",
      [r[2] for r in a_after] ==
      ["2020-03-01T00:00:01Z", "2020-03-01T00:00:03Z",
       "2020-03-01T00:00:05Z", "2020-03-01T00:00:07Z"])
check("旧轮 B 的 at 时间原样保留",
      [r[2] for r in b_after] ==
      ["2020-03-01T00:00:02Z", "2020-03-01T00:00:04Z",
       "2020-03-01T00:00:06Z", "2020-03-01T00:00:08Z"])

# member_index / version 保留
row = conn.execute(
    "SELECT member_index,version FROM events WHERE round_id='r_legacy_a' AND seq=2"
).fetchone()
check("member_index/version 列保留", tuple(row) == (0, 2))

# 旧 sqlite_sequence 计数器行已清理
seqrow = conn.execute(
    "SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
check("旧 AUTOINCREMENT 计数器已清理", seqrow is None)

# 迁移幂等：再次连接不报错、编号不变
conn2 = db.connect()
again = conn2.execute(
    "SELECT round_id,seq FROM events ORDER BY round_id,seq").fetchall()
check("迁移幂等（重复连接编号不变）",
      [(r[0], r[1]) for r in again] ==
      [("r_legacy_a", i) for i in range(1, 5)] +
      [("r_legacy_b", i) for i in range(1, 5)])

# ------------------------------------------------ 迁移后继续推进旧轮 A
# 旧轮 A 已在 revealing 且两条承诺齐备；加入后直接揭示即可结算
j0 = service.join_round("CODE00000001")
j1 = service.join_round("CODE00000002")
check("迁移后可继续加入旧轮",
      j0["member_index"] == 0 and j1["member_index"] == 1)
# 新 joined 事件必须接在迁移后的 4 条之后：seq=5,6
joined_seqs = [e["seq"] for e in
               service.build_snapshot("r_legacy_a", role="host")["events"]
               if e["type"] == "joined"]
check("迁移后新事件序号紧接最大序号（joined=5,6）", joined_seqs == [5, 6],
      str(joined_seqs))

for i in range(2):
    service.submit_reveal("r_legacy_a", i, legacy_secrets[i])

snap = service.build_snapshot("r_legacy_a", role="host")
# 事件：4 条迁移(created+2 committed+stage) + 2 joined
#       + 2 revealed + 1 settled = 9 条
all_seqs = [e["seq"] for e in snap["events"]]
check("迁移后继续推进至结算：seq=1..9 连续无重",
      all_seqs == list(range(1, 10)), str(all_seqs))
check("旧轮 A 最终 settled", snap["status"] == "settled")
# revealed/settled 事件紧接新序号
types = [e["type"] for e in snap["events"]]
check("settled 事件 seq=9 位于序列末尾",
      types[-1] == "settled" and snap["events"][-1]["seq"] == 9)

# 审计导出：verify.html 同款 1..N 规则；承诺/秘密计数也与事件一致
audit = service.build_audit("r_legacy_a")
ev = audit["events"]
seq_ok = len(ev) > 0 and all(e["seq"] == i + 1 for i, e in enumerate(ev))
check("迁移旧轮的审计导出通过轮次内 1..N 校验", seq_ok,
      str([e["seq"] for e in ev]))
check("迁移旧轮审计：committed/revealed 事件数与数据一致",
      len([e for e in ev if e["type"] == "committed"]) == len(audit["commitments"]) == 2
      and
      len([e for e in ev if e["type"] == "revealed"]) == len(audit["secrets"]) == 2)
# 迁移后的审计仍可独立重算
seed = crypto.compute_seed(
    "r_legacy_a", "旧轮A", legacy_members, ["p"],
    audit["config"]["commit_deadline"], audit["config"]["reveal_deadline"],
    legacy_secrets)
check("迁移旧轮结算种子可独立复算",
      seed.hex() == audit["result"]["seed"])

# 全新轮次在迁移后的库上仍从 1 开始
from datetime import timedelta  # noqa: E402
from app.timeutil import to_iso, utcnow  # noqa: E402
t = utcnow()
rc = service.create_round(
    "迁移后新轮", ["x", "y"], ["p"],
    to_iso(t + timedelta(minutes=10)), to_iso(t + timedelta(minutes=30)))
snap_c = service.build_snapshot(rc["round_id"], role="host")
check("迁移后创建的新轮首事件 seq=1",
      len(snap_c["events"]) == 1 and snap_c["events"][0]["seq"] == 1 and
      snap_c["events"][0]["type"] == "created")

print()
print(f"{len(fails)} FAILURES" if fails else "ALL LEGACY MIGRATION TESTS PASSED")
sys.exit(1 if fails else 0)
