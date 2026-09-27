"""时间工具：内部统一使用 UTC、秒精度、以 ``Z`` 结尾的 ISO-8601 字符串。

字典序比较即时间先后比较，方便 SQLite 直接与截止时间比较。
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return to_iso(utcnow())


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """解析 ISO-8601；裸时间视为 UTC；规范化为秒精度。"""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def normalize_iso(value: str) -> str:
    return to_iso(parse_iso(value))


def is_past(deadline_iso: str, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return parse_iso(deadline_iso) <= now
