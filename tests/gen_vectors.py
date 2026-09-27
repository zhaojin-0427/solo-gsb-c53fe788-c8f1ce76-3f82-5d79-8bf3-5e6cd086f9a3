"""生成协议测试向量：Python 端编码/承诺/种子/洗牌结果。"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crypto

vectors = {}

# 1. frame / nested
vectors["frame_hello"] = crypto.frame(b"hello").hex()
vectors["nested_abc"] = crypto.nested([b"a", b"bb", b""]).hex()
vectors["nested_nested"] = crypto.nested([
    crypto.COMMITMENT_DOMAIN,
    crypto.nested([b"rid", b"3", b"\xe4\xb8\x89", b"topsecret"]),
]).hex()

# 2. commitment
vectors["commitment"] = crypto.compute_commitment("r_abc", 2, "张三", "p@ss 秘密!")

# 3. seed + draw
members = ["Alice", "Bob", "Carol", "Dave", "Eve"]
prizes = ["一等奖", "二等奖"]
secrets = ["s0-secret", "s1-secret", "s2-secret", "s3-secret", "s4-secret"]
seed = crypto.compute_seed(
    "r_abc", "年会", members, prizes,
    "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", secrets)
vectors["seed"] = seed.hex()
res = crypto.draw(seed, members, prizes)
vectors["permutation"] = res.permutation
vectors["winners"] = res.winners
vectors["trace"] = [
    {"i": t.i, "upper": t.upper, "counter": t.counter,
     "candidate_mod": t.candidate, "accepted": t.accepted, "value": t.value}
    for t in res.trace
]

# 4. 手工计算前几个 HMAC 抽样点，供 JS 直接比对
import hashlib
import hmac as hmac_mod
pts = {}
for c in range(6):
    pts[str(c)] = hmac_mod.new(
        seed, c.to_bytes(8, "big"), hashlib.sha256).hexdigest()
vectors["hmac_points"] = pts

print(json.dumps(vectors, ensure_ascii=False, indent=2))
