"""轮次状态机：创建/加入/承诺/揭示/结算/作废。

不变量
------
* 轮次配置（成员、奖项、标题、两个截止时间）创建后不可变；
* 所有承诺落库前不接受任何揭示；
* 任一阶段超时，或揭示的秘密与承诺不符，整轮立即作废；
* 每次状态迁移在 ``BEGIN IMMEDIATE`` 事务中读取-校验-写入，
  并将 ``version`` 加 1；重复提交返回幂等结果，不产生重复事件；
* 结算用的种子同时绑定配置与全部揭示秘密（按固定成员顺序），任何篡改都改变结果。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from typing import Any, Callable

from . import crypto
from .db import CommitOnServiceError, query_all, query_one, read_lock, transaction
from .timeutil import is_past, normalize_iso, parse_iso, utcnow, utcnow_iso

HEX64 = set("0123456789abcdef")

# 状态迁移后触发的广播钩子，由 ws 模块注入（延迟导入避免循环依赖）。
_change_hooks: list[Callable[[str], None]] = []


def register_change_hook(hook: Callable[[str], None]) -> None:
    _change_hooks.append(hook)


def _emit(round_id: str) -> None:
    for hook in list(_change_hooks):
        try:
            hook(round_id)
        except Exception:  # 广播失败绝不影响状态机
            pass


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _void_then_raise(conn: sqlite3.Connection, row: sqlite3.Row, reason: str,
                     code: str, message: str) -> None:
    """作废当前轮（合法状态迁移，需随异常一起提交），随后向客户端报错。"""
    _void(conn, row, reason)
    err = ServiceError(code, message, 409)
    raise CommitOnServiceError(err) from err


# --------------------------------------------------------------------------- 工具

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_id() -> str:
    return "r_" + secrets.token_hex(9)


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _new_invite_code() -> str:
    # 去掉易混字符的大写字母+数字，12 位（约 71 bit）
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _loads(value: str) -> Any:
    return json.loads(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _get_round(conn: sqlite3.Connection, round_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()
    if row is None:
        raise ServiceError("round_not_found", "轮次不存在", 404)
    return row


def _clean_label(value: str, field_name: str, limit: int) -> str:
    v = (value or "").strip()
    if not v:
        raise ServiceError("invalid_input", f"{field_name}不能为空")
    if len(v) > limit:
        raise ServiceError("invalid_input", f"{field_name}过长（最多 {limit} 字符）")
    return v


def _validate_hex_commitment(value: str) -> str:
    v = (value or "").strip().lower()
    if len(v) != 64 or any(c not in HEX64 for c in v):
        raise ServiceError("invalid_commitment", "承诺必须是 64 位十六进制 SHA-256", 422)
    return v


def _clean_secret(value: str) -> str:
    v = (value or "").strip()
    if not v or len(v) > 512:
        raise ServiceError("invalid_secret", "秘密不能为空且长度不超过 512 字符", 422)
    return v


def _add_event(
    conn: sqlite3.Connection,
    round_id: str,
    etype: str,
    version: int,
    member_index: int | None = None,
    payload: Any = None,
) -> None:
    # seq 在每轮内从 1 连续递增。所有状态迁移都在 BEGIN IMMEDIATE 写事务内
    # 串行执行，MAX(seq)+1 的读-分配-插入与复合唯一主键 (round_id,seq) 共同
    # 保证并发下原子分配、绝不产生重复序号或跳号。
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE round_id=?",
        (round_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO events(round_id,seq,type,member_index,version,payload_json,at)"
        " VALUES(?,?,?,?,?,?,?)",
        (round_id, row["next_seq"], etype, member_index, version,
         _dumps(payload) if payload is not None else None, utcnow_iso()),
    )


def _bump_version(conn: sqlite3.Connection, row: sqlite3.Row,
                  status: str | None = None) -> int:
    new_version = row["version"] + 1
    if status is not None:
        conn.execute("UPDATE rounds SET version=?, status=? WHERE id=?",
                     (new_version, status, row["id"]))
    else:
        conn.execute("UPDATE rounds SET version=? WHERE id=?",
                     (new_version, row["id"]))
    return new_version


def _void(conn: sqlite3.Connection, row: sqlite3.Row, reason: str,
          etype: str = "voided") -> int:
    new_version = row["version"] + 1
    conn.execute("UPDATE rounds SET version=?, status='voided', void_reason=? WHERE id=?",
                 (new_version, reason, row["id"]))
    _add_event(conn, row["id"], etype, new_version, payload={"reason": reason})
    return new_version


# --------------------------------------------------------------------------- 超时

def _expire_if_due_locked(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """调用方已持有写事务。超时则作废并返回最新行；否则返回原行。"""
    deadline = None
    if row["status"] == "committing":
        deadline = row["commit_deadline"]
    elif row["status"] == "revealing":
        deadline = row["reveal_deadline"]
    if deadline and is_past(deadline):
        reason = "commit_timeout" if row["status"] == "committing" else "reveal_timeout"
        _void(conn, row, reason, etype="stage")
        return _get_round(conn, row["id"])
    return row


def expire_if_due(round_id: str) -> bool:
    """惰性超时检查（每次读快照/写操作都会走这里）。返回是否发生了作废。"""
    with transaction() as conn:
        row = _get_round(conn, round_id)
        if row["status"] not in ("committing", "revealing"):
            return False
        fresh = _expire_if_due_locked(conn, row)
        changed = fresh["status"] == "voided"
    if changed:
        _emit(round_id)
    return changed


def sweep_due_rounds() -> int:
    """后台定时器调用：把所有已到截止时间的轮次作废。返回作废数量。"""
    now = utcnow_iso()
    rows = query_all(
        "SELECT id,status,commit_deadline,reveal_deadline FROM rounds"
        " WHERE status IN ('committing','revealing')"
    )
    voided = 0
    for r in rows:
        deadline = r["commit_deadline"] if r["status"] == "committing" else r["reveal_deadline"]
        # 规范化 UTC ISO 字符串的字典序比较等价于时间比较
        if deadline <= now:
            with transaction() as conn:
                fresh = _get_round(conn, r["id"])
                if fresh["status"] in ("committing", "revealing"):
                    fresh = _expire_if_due_locked(conn, fresh)
                    if fresh["status"] == "voided":
                        voided += 1
            _emit(r["id"])
    return voided


# --------------------------------------------------------------------------- 创建

def create_round(
    title: str,
    members: list[str],
    prizes: list[str],
    commit_deadline: str,
    reveal_deadline: str,
) -> dict:
    title = _clean_label(title, "标题", 80)

    if not isinstance(members, list) or not (2 <= len(members) <= 100):
        raise ServiceError("invalid_input", "成员数量必须在 2–100 之间")
    if not isinstance(prizes, list) or not (1 <= len(prizes) <= len(members)):
        raise ServiceError("invalid_input", "奖项数量必须在 1 到成员数之间")

    member_labels: list[str] = []
    seen: set[str] = set()
    for m in members:
        label = _clean_label(str(m), "成员名", 60)
        if label in seen:
            raise ServiceError("invalid_input", f"成员名重复：{label}")
        seen.add(label)
        member_labels.append(label)

    prize_labels: list[str] = []
    seen_p: set[str] = set()
    for p in prizes:
        label = _clean_label(str(p), "奖项名", 60)
        if label in seen_p:
            raise ServiceError("invalid_input", f"奖项名重复：{label}")
        seen_p.add(label)
        prize_labels.append(label)

    try:
        cd = normalize_iso(commit_deadline)
        rd = normalize_iso(reveal_deadline)
    except (ValueError, TypeError):
        raise ServiceError("invalid_deadline", "截止时间格式无效（ISO-8601）")

    if parse_iso(cd) <= utcnow():
        raise ServiceError("invalid_deadline", "承诺截止时间必须晚于当前服务器时间")
    if parse_iso(rd) <= parse_iso(cd):
        raise ServiceError("invalid_deadline", "揭示截止时间必须晚于承诺截止时间")

    round_id = _new_id()
    host_token = _new_token()
    now = utcnow_iso()

    invites: list[dict] = []
    for idx, _label in enumerate(member_labels):
        code = _new_invite_code()
        invites.append({"member_index": idx, "member": member_labels[idx], "code": code})

    with transaction() as conn:
        conn.execute(
            "INSERT INTO rounds(id,title,members_json,prizes_json,commit_deadline,"
            "reveal_deadline,status,version,host_token_hash,created_at)"
            " VALUES(?,?,?,?,?,?,'committing',1,?,?)",
            (round_id, title, _dumps(member_labels), _dumps(prize_labels),
             cd, rd, _hash_token(host_token), now),
        )
        for inv in invites:
            conn.execute(
                "INSERT INTO invites(code,round_id,member_index,used,created_at)"
                " VALUES(?,?,?,0,?)",
                (inv["code"], round_id, inv["member_index"], now),
            )
        _add_event(conn, round_id, "created", 1,
                   payload={"members": len(member_labels), "prizes": len(prize_labels)})

    _emit(round_id)
    return {
        "round_id": round_id,
        "host_token": host_token,
        "invites": invites,
        "snapshot": build_snapshot(round_id, role="host", member_index=None),
    }


# --------------------------------------------------------------------------- 加入

def join_round(code: str) -> dict:
    code = (code or "").strip().upper()
    if not code:
        raise ServiceError("invalid_code", "邀请码不能为空", 404)
    with transaction() as conn:
        inv = conn.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
        if inv is None:
            raise ServiceError("invalid_code", "邀请码无效", 404)
        if inv["used"]:
            raise ServiceError("code_used",
                               "该一次性邀请码已被使用，请在最初加入的设备上继续", 410)
        row = _get_round(conn, inv["round_id"])
        row = _expire_if_due_locked(conn, row)
        if row["status"] not in ("committing", "revealing"):
            raise ServiceError("round_closed", "轮次已结束，无法加入", 409)

        token = _new_token()
        members = _loads(row["members_json"])
        conn.execute("UPDATE invites SET used=1 WHERE code=?", (code,))
        conn.execute(
            "INSERT INTO sessions(token_hash,round_id,member_index,created_at)"
            " VALUES(?,?,?,?)",
            (_hash_token(token), row["id"], inv["member_index"], utcnow_iso()),
        )
        _add_event(conn, row["id"], "joined", row["version"],
                   member_index=inv["member_index"])
        round_id = row["id"]
        member_index = inv["member_index"]

    _emit(round_id)
    return {
        "round_id": round_id,
        "member_index": member_index,
        "member": members[member_index],
        "session_token": token,
        "snapshot": build_snapshot(round_id, role="member", member_index=member_index),
    }


# --------------------------------------------------------------------------- 认证

def authenticate(round_id: str, host_token: str | None,
                 session_token: str | None) -> tuple[str, int | None]:
    """返回 (role, member_index)。失败抛 401/403。"""
    row = query_one("SELECT host_token_hash FROM rounds WHERE id=?", (round_id,))
    if row is None:
        raise ServiceError("round_not_found", "轮次不存在", 404)
    if host_token:
        if not secrets.compare_digest(_hash_token(host_token), row["host_token_hash"]):
            raise ServiceError("forbidden", "主持人凭证无效", 403)
        return "host", None
    if session_token:
        s = query_one(
            "SELECT member_index FROM sessions WHERE token_hash=? AND round_id=?",
            (_hash_token(session_token), round_id),
        )
        if s is None:
            raise ServiceError("forbidden", "会话凭证无效", 403)
        return "member", s["member_index"]
    raise ServiceError("unauthorized", "缺少凭证", 401)


# --------------------------------------------------------------------------- 承诺

def submit_commitment(round_id: str, member_index: int, commitment: str) -> dict:
    commitment = _validate_hex_commitment(commitment)
    idem = False
    with transaction() as conn:
        row = _expire_if_due_locked(conn, _get_round(conn, round_id))
        if row["status"] == "voided":
            raise ServiceError("round_voided", "轮次已作废", 409)

        existing = conn.execute(
            "SELECT commitment FROM commitments WHERE round_id=? AND member_index=?",
            (round_id, member_index)).fetchone()
        if existing:
            # 幂等：重复提交同一承诺；不同承诺直接拒绝（承诺不可更改）
            if not secrets.compare_digest(existing["commitment"], commitment):
                raise ServiceError("already_committed",
                                   "该成员已使用不同承诺，承诺不可更改", 409)
            idem = True
        else:
            if row["status"] != "committing":
                raise ServiceError("phase_closed", "承诺阶段已结束", 409)
            conn.execute(
                "INSERT INTO commitments(round_id,member_index,commitment,created_at)"
                " VALUES(?,?,?,?)",
                (round_id, member_index, commitment, utcnow_iso()),
            )
            new_version = _bump_version(conn, row)
            _add_event(conn, round_id, "committed", new_version,
                       member_index=member_index, payload={"commitment": commitment})

            members = _loads(row["members_json"])
            count = conn.execute(
                "SELECT COUNT(*) c FROM commitments WHERE round_id=?",
                (round_id,)).fetchone()["c"]
            if count == len(members):
                # 最后一个承诺原子地把阶段切换为 revealing
                new_version = _bump_version(conn, _get_round(conn, round_id),
                                            status="revealing")
                _add_event(conn, round_id, "stage", new_version,
                           payload={"status": "revealing", "reason": "all_committed"})

    _emit(round_id)
    return {"idempotent": idem,
            "snapshot": build_snapshot(round_id, role="member",
                                       member_index=member_index)}


# --------------------------------------------------------------------------- 揭示

def _commit_tx(fn):
    """运行写事务体；把“作废后报错”的标记异常转换回 ServiceError（作废已提交）。"""
    try:
        with transaction() as conn:
            return fn(conn)
    except CommitOnServiceError as exc:
        raise exc.args[0] from None


def submit_reveal(round_id: str, member_index: int, secret: str) -> dict:
    secret = _clean_secret(secret)
    state = {"idem": False, "changed": True}

    def body(conn):
        row = _expire_if_due_locked(conn, _get_round(conn, round_id))
        if row["status"] == "voided":
            state["changed"] = False
            raise ServiceError("round_voided",
                               f"轮次已作废：{row['void_reason']}", 409)
        if row["status"] not in ("revealing", "settled"):
            state["changed"] = False
            raise ServiceError("phase_closed", "尚未进入揭示阶段", 409)

        members = _loads(row["members_json"])
        member_label = members[member_index]
        comm = conn.execute(
            "SELECT commitment FROM commitments WHERE round_id=? AND member_index=?",
            (round_id, member_index)).fetchone()
        if comm is None:
            _void_then_raise(conn, _get_round(conn, round_id),
                             "missing_commitment", "round_voided", "缺少承诺，整轮作废")

        existing = conn.execute(
            "SELECT secret FROM reveals WHERE round_id=? AND member_index=?",
            (round_id, member_index)).fetchone()
        if existing:
            if not secrets.compare_digest(existing["secret"], secret):
                # 同一成员两次揭示不同秘密 —— 破坏承诺绑定，整轮作废
                _void_then_raise(conn, _get_round(conn, round_id),
                                 "conflicting_reveal", "round_voided",
                                 "重复揭示使用了不同秘密，整轮作废")
            state["idem"] = True
        else:
            if row["status"] == "settled":
                state["changed"] = False
                raise ServiceError("phase_closed", "轮次已结算", 409)
            if not crypto.verify_reveal(comm["commitment"], round_id,
                                        member_index, member_label, secret):
                # 承诺不符 —— 整轮作废（与揭示截止超时同权）
                _void_then_raise(conn, _get_round(conn, round_id),
                                 "commitment_mismatch", "commitment_mismatch",
                                 "秘密与承诺不符，整轮作废")
            conn.execute(
                "INSERT INTO reveals(round_id,member_index,secret,created_at)"
                " VALUES(?,?,?,?)",
                (round_id, member_index, secret, utcnow_iso()),
            )
            new_version = _bump_version(conn, row)
            _add_event(conn, round_id, "revealed", new_version,
                       member_index=member_index)

            fresh = _get_round(conn, round_id)
            reveal_count = conn.execute(
                "SELECT COUNT(*) c FROM reveals WHERE round_id=?",
                (round_id,)).fetchone()["c"]
            if reveal_count == len(members) and fresh["status"] == "revealing":
                _settle_locked(conn, _get_round(conn, round_id))

    try:
        _commit_tx(body)
    except ServiceError:
        if state["changed"]:
            _emit(round_id)  # 作废型错误已经迁移了状态
        raise

    _emit(round_id)
    return {"idempotent": state["idem"],
            "snapshot": build_snapshot(round_id, role="member",
                                       member_index=member_index)}


def _settle_locked(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """调用方已持有写事务；前置：status=revealing 且全员揭示。"""
    members = _loads(row["members_json"])
    prizes = _loads(row["prizes_json"])
    secret_rows = conn.execute(
        "SELECT secret FROM reveals WHERE round_id=? ORDER BY member_index",
        (row["id"],)).fetchall()
    secrets_in_order = [r["secret"] for r in secret_rows]

    seed = crypto.compute_seed(
        row["id"], row["title"], members, prizes,
        row["commit_deadline"], row["reveal_deadline"], secrets_in_order,
    )
    result = crypto.draw(seed, members, prizes)
    seed_hex = seed.hex()
    result_doc = {
        "algorithm": {
            "name": "hmac-sha256 rejection-sampling Fisher-Yates",
            "seed_domain": crypto.SEED_DOMAIN.decode(),
            "commitment_domain": crypto.COMMITMENT_DOMAIN.decode(),
            "frame_tag": "U(0x55) + uint32be(len) + payload",
            "rng": "HMAC-SHA256(key=seed, msg=uint64be(counter)); counter 从 0 单调递增",
            "rejection": "candidate >= 2^256 - (2^256 mod upper) 时拒绝并 counter+1",
            "shuffle": "i 从 n-1 递减到 1，与 [0,i] 均匀抽取的 j 交换；奖项按顺序取排列前 |P| 位",
        },
        "seed": seed_hex,
        "permutation": result.permutation,
        "winners": result.winners,
        "trace": [
            {"i": t.i, "upper": t.upper, "counter": t.counter,
             "candidate_mod": t.candidate, "accepted": t.accepted, "value": t.value}
            for t in result.trace
        ],
    }
    new_version = row["version"] + 1
    conn.execute(
        "UPDATE rounds SET version=?, status='settled', result_json=? WHERE id=?",
        (new_version, _dumps(result_doc), row["id"]),
    )
    _add_event(conn, row["id"], "settled", new_version,
               payload={"seed": seed_hex, "winners": result.winners})
    return new_version


# --------------------------------------------------------------------------- 快照

def _events_locked(conn: sqlite3.Connection, round_id: str) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT seq,type,member_index,version,payload_json,at FROM events"
            " WHERE round_id=? ORDER BY seq", (round_id,)):
        out.append({
            "seq": r["seq"], "type": r["type"],
            "member_index": r["member_index"], "version": r["version"],
            "payload": _loads(r["payload_json"]) if r["payload_json"] else None,
            "at": r["at"],
        })
    return out


def _snapshot_locked(conn: sqlite3.Connection, row: sqlite3.Row, *, role: str,
                     member_index: int | None, include_invites: bool,
                     reveal_secrets: bool) -> dict:
    members = _loads(row["members_json"])
    prizes = _loads(row["prizes_json"])
    committed = {
        r["member_index"]: r["commitment"]
        for r in conn.execute(
            "SELECT member_index,commitment FROM commitments WHERE round_id=?",
            (row["id"],))
    }
    revealed_rows = conn.execute(
        "SELECT member_index,secret FROM reveals WHERE round_id=?",
        (row["id"],)).fetchall()
    revealed = {r["member_index"] for r in revealed_rows}
    secrets_map = {r["member_index"]: r["secret"] for r in revealed_rows}

    member_states = []
    for idx, label in enumerate(members):
        ms: dict[str, Any] = {
            "index": idx,
            "label": label,
            "committed": idx in committed,
            "revealed": idx in revealed,
        }
        # 承诺是公开哈希：任何参与者在任何阶段都可核对（秘密不随承诺泄露）
        if idx in committed:
            ms["commitment"] = committed[idx]
        # 承诺-揭示模型：揭示后秘密对全体公开，以完成公开复算
        if reveal_secrets and idx in secrets_map:
            ms["secret"] = secrets_map[idx]
        member_states.append(ms)

    snap: dict[str, Any] = {
        "round_id": row["id"],
        "title": row["title"],
        "members": member_states,
        "prizes": [{"index": i, "label": p} for i, p in enumerate(prizes)],
        "commit_deadline": row["commit_deadline"],
        "reveal_deadline": row["reveal_deadline"],
        "status": row["status"],
        "void_reason": row["void_reason"],
        "version": row["version"],
        "server_time": utcnow_iso(),
        "role": role,
        "you": member_index,
        "events": _events_locked(conn, row["id"]),
        "encoding": {
            "frame": "U(0x55) + uint32be(len) + payload；列表先逐元素 frame 再整体 frame",
            "commitment_domain": crypto.COMMITMENT_DOMAIN.decode(),
            "seed_domain": crypto.SEED_DOMAIN.decode(),
        },
    }
    if include_invites:
        snap["invites"] = [
            {"member_index": r["member_index"], "code": r["code"], "used": bool(r["used"])}
            for r in conn.execute(
                "SELECT member_index,code,used FROM invites WHERE round_id=?"
                " ORDER BY member_index", (row["id"],))
        ]
    if row["result_json"]:
        snap["result"] = _loads(row["result_json"])
    return snap


def build_snapshot(round_id: str, *, role: str,
                   member_index: int | None = None) -> dict:
    """读取当前快照。任何读取都先惰性执行超时作废，保证重连即最新。"""
    expire_if_due(round_id)
    with read_lock() as conn:
        row = _get_round(conn, round_id)
        is_host = role == "host"
        reveal_secrets = is_host or row["status"] in ("settled", "voided")
        return _snapshot_locked(
            conn, row, role=role, member_index=member_index,
            include_invites=is_host, reveal_secrets=reveal_secrets)


# --------------------------------------------------------------------------- 审计

def build_audit(round_id: str) -> dict:
    """主持人导出：配置 + 承诺 + 秘密 + 事件序列 + 结果，可独立复算。"""
    snap = build_snapshot(round_id, role="host")
    row = query_one("SELECT created_at FROM rounds WHERE id=?", (round_id,))
    return {
        "format": "fair-draw-room/audit/v1",
        "exported_at": utcnow_iso(),
        "config": {
            "round_id": round_id,
            "title": snap["title"],
            "members": [m["label"] for m in snap["members"]],
            "prizes": [p["label"] for p in snap["prizes"]],
            "commit_deadline": snap["commit_deadline"],
            "reveal_deadline": snap["reveal_deadline"],
            "created_at": row["created_at"],
        },
        "commitments": [
            {"member_index": m["index"], "member": m["label"],
             "commitment": m.get("commitment")}
            for m in snap["members"] if m["committed"]
        ],
        "secrets": [
            {"member_index": m["index"], "member": m["label"], "secret": m["secret"]}
            for m in snap["members"] if "secret" in m
        ],
        "events": snap["events"],
        "status": snap["status"],
        "void_reason": snap["void_reason"],
        "version": snap["version"],
        "result": snap.get("result"),
    }
