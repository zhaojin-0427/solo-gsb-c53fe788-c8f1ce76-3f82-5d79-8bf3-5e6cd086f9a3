"""确定性密码学原语。

编码规则（浏览器端必须逐字节复现）：

1. ``frame(b)`` 长度前缀编码：
   ``b'U' + uint32_be(len(b)) + b``，其中 ``U`` (0x55) 是 “UTF-8/字节帧”
   的域分隔标签。
2. ``nested(items)``：先对每个元素 frame，再把所有 frame 拼接后整体再 frame
   一次。嵌套层级由结构本身决定，杜绝拼接歧义。

承诺与种子使用不同的域标签，因此承诺不能被误当作种子的一部分。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Sequence

FRAME_TAG = b"U"
COMMITMENT_DOMAIN = b"fair-draw-commitment-v1"
SEED_DOMAIN = b"fair-draw-seed-v1"


def frame(data: bytes) -> bytes:
    """单段字节 -> 长度前缀帧。"""
    return FRAME_TAG + len(data).to_bytes(4, "big") + data


def nested(items: Sequence[bytes]) -> bytes:
    """对一组帧元素做嵌套长度前缀编码。"""
    return frame(b"".join(frame(item) for item in items))


def _u(data: str | bytes) -> bytes:
    return data.encode("utf-8") if isinstance(data, str) else data


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 成员承诺：bind(round_id, member_index, member_label, secret)
# ---------------------------------------------------------------------------

def commitment_input(
    round_id: str,
    member_index: int,
    member_label: str,
    secret: str,
) -> bytes:
    """承诺的预映像，浏览器端以完全相同的字节序列求 SHA-256。"""
    parts = [
        _u(round_id),
        _u(str(member_index)),
        _u(member_label),
        _u(secret),
    ]
    return nested([COMMITMENT_DOMAIN, nested(parts)])


def compute_commitment(
    round_id: str,
    member_index: int,
    member_label: str,
    secret: str,
) -> str:
    return sha256_hex(commitment_input(round_id, member_index, member_label, secret))


def verify_reveal(
    commitment: str,
    round_id: str,
    member_index: int,
    member_label: str,
    secret: str,
) -> bool:
    expected = compute_commitment(round_id, member_index, member_label, secret)
    return secrets.compare_digest(expected, commitment.strip().lower())


# ---------------------------------------------------------------------------
# 轮次种子：绑定不可变配置 + 揭示阶段收集到的全部秘密（按固定成员顺序）
# ---------------------------------------------------------------------------

def seed_input(
    round_id: str,
    title: str,
    member_labels: Sequence[str],
    prize_labels: Sequence[str],
    commit_deadline: str,
    reveal_deadline: str,
    revealed_secrets: Sequence[str],
) -> bytes:
    payload = nested(
        [
            _u(round_id),
            _u(title),
            nested([_u(m) for m in member_labels]),
            nested([_u(p) for p in prize_labels]),
            _u(commit_deadline),
            _u(reveal_deadline),
            nested([_u(s) for s in revealed_secrets]),
        ]
    )
    return nested([SEED_DOMAIN, payload])


def compute_seed(*args, **kwargs) -> bytes:
    return hashlib.sha256(seed_input(*args, **kwargs)).digest()


# ---------------------------------------------------------------------------
# 拒绝采样 + Fisher-Yates（以 HMAC-SHA256 作为确定性随机源，避免模偏差）
# ---------------------------------------------------------------------------

@dataclass
class DrawTrace:
    """记录每一次抽取，便于审计方逐步复算。"""

    i: int
    upper: int
    counter: int
    candidate: int
    accepted: bool
    value: int


@dataclass
class DrawResult:
    permutation: list[int]
    winners: list[dict]
    trace: list[DrawTrace] = field(default_factory=list)


def uniform_integer(seed: bytes, counter: int, upper: int,
                    trace: list[DrawTrace] | None, i: int = -1) -> tuple[int, int]:
    """从 [0, upper) 抽取均匀整数（拒绝采样）。

    返回 ``(value, next_counter)``。256 位摘要映射到 [0, 2^256)，
    仅接受 ``< limit`` 的候选，保证无模偏差。
    """
    limit = (1 << 256) - ((1 << 256) % upper)
    while True:
        candidate = int.from_bytes(
            hmac.new(seed, counter.to_bytes(8, "big"),
                     hashlib.sha256).digest(),
            "big",
        )
        accepted = candidate < limit
        if trace is not None:
            trace.append(DrawTrace(
                i=i, upper=upper, counter=counter,
                candidate=candidate % upper,
                accepted=accepted,
                value=(candidate % upper) if accepted else -1,
            ))
        counter += 1
        if accepted:
            return candidate % upper, counter


def fair_permutation(seed: bytes, n: int) -> tuple[list[int], list[DrawTrace]]:
    """确定性 Fisher-Yates。counter 在所有步骤/拒绝之间单调递增。"""
    arr = list(range(n))
    trace: list[DrawTrace] = []
    counter = 0
    for i in range(n - 1, 0, -1):
        j, counter = uniform_integer(seed, counter, i + 1, trace, i)
        arr[i], arr[j] = arr[j], arr[i]
    return arr, trace


def draw(
    seed: bytes,
    member_labels: Sequence[str],
    prize_labels: Sequence[str],
) -> DrawResult:
    perm, trace = fair_permutation(seed, len(member_labels))
    winners = [
        {
            "prize_index": p,
            "prize": prize_labels[p],
            "member_index": perm[p],
            "member": member_labels[perm[p]],
        }
        for p in range(len(prize_labels))
    ]
    return DrawResult(permutation=perm, winners=winners, trace=trace)


def new_secret(nbytes: int = 16) -> str:
    """服务端不使用；仅供本地测试。浏览器使用 crypto.getRandomValues。"""
    return secrets.token_hex(nbytes)
