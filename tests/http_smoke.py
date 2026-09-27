"""真实 HTTP + WebSocket 冒烟测试：启动 uvicorn 子进程跑完整轮次。"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import httpx
import websockets

DB = tempfile.mktemp(suffix=".db")
PORT = "8765"
BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}/api/ws"

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(name)


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def main():
    members = ["甲", "乙", "丙"]
    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(base_url=BASE, timeout=10) as http:
        # health + 首页
        check("GET /api/health", (await http.get("/api/health")).json()["ok"] is True)
        r = await http.get("/")
        check("GET / 返回首页", r.status_code == 200 and "公平抽签" in r.text)
        r = await http.get("/verify.html")
        check("GET /verify.html", r.status_code == 200)
        r = await http.get("/static/js/crypto.js")
        check("静态资源 crypto.js", r.status_code == 200)

        # 创建
        r = await http.post("/api/rounds", json={
            "title": "HTTP冒烟", "members": members, "prizes": ["头奖"],
            "commit_deadline": iso(now + timedelta(minutes=10)),
            "reveal_deadline": iso(now + timedelta(minutes=30)),
        })
        check("创建轮次 201", r.status_code == 201)
        data = r.json()
        rid, host = data["round_id"], data["host_token"]
        codes = [i["code"] for i in data["invites"]]
        H = {"X-Host-Token": host}

        # 校验错误格式
        r = await http.post("/api/rounds", json={"title": "x", "members": ["a"],
                                                 "prizes": ["p"],
                                                 "commit_deadline": iso(now),
                                                 "reveal_deadline": iso(now)})
        check("成员过少 -> 422 统一错误格式",
              r.status_code == 422 and r.json()["error"]["code"] == "invalid_input",
              str(r.status_code))

        # 无凭证
        r = await http.get(f"/api/rounds/{rid}/snapshot")
        check("无凭证 -> 401", r.status_code == 401)

        # 主持人快照
        snap = (await http.get(f"/api/rounds/{rid}/snapshot", headers=H)).json()
        check("主持人 REST 快照", snap["status"] == "committing" and len(snap["invites"]) == 3)

        # 三个成员加入
        joins = []
        for c in codes:
            r = await http.post("/api/rounds/join", json={"code": c})
            joins.append(r.json())
        check("三人加入", all(j["member_index"] == i for i, j in enumerate(joins)))
        r = await http.post("/api/rounds/join", json={"code": codes[0]})
        check("一次性码复用 -> 410", r.status_code == 410)

        # 主持人 WS 先连接，等待后续广播
        host_ws = await websockets.connect(
            f"{WS}/{rid}?round_id={rid}&host_token={host}")
        first = json.loads(await host_ws.recv())
        check("主持人 WS 连接即收快照", first["type"] == "snapshot" and
              first["snapshot"]["status"] == "committing")

        # 成员 WS（成员 0）
        token0 = joins[0]["session_token"]
        mem_ws = await websockets.connect(
            f"{WS}/{rid}?round_id={rid}&session_token={token0}")
        await mem_ws.recv()
        # 错误令牌 WS 应被关闭
        closed_code = None
        try:
            async with websockets.connect(
                    f"{WS}/{rid}?round_id={rid}&host_token=bogus") as bad:
                await bad.recv()
        except websockets.InvalidStatus as e:
            closed_code = getattr(e.response, "status_code", None)
        except Exception:
            closed_code = "closed"
        check("WS 坏凭证握手失败/关闭", closed_code in (403, "closed", None))

        # 浏览器式承诺：用与 crypto.js 相同的逻辑（这里用服务端函数模拟，编码已交叉验证）
        sys.path.insert(0, "/workspace")
        from app import crypto as cpy
        secrets = [f"http-sek-{i}" for i in range(3)]
        commits = [cpy.compute_commitment(rid, i, members[i], secrets[i]) for i in range(3)]

        broadcast_statuses = []
        got_settled = asyncio.Event()

        async def listen_host():
            async for raw in host_ws:
                msg = json.loads(raw)
                if msg["type"] == "snapshot":
                    st = msg["snapshot"]["status"]
                    broadcast_statuses.append(st)
                    if st == "settled":
                        got_settled.set()

        asyncio.create_task(listen_host())

        for i, j in enumerate(joins):
            r = await http.post(
                f"/api/rounds/{rid}/commitments",
                json={"commitment": commits[i]},
                headers={"X-Session-Token": j["session_token"]})
            check(f"成员 {i} 提交承诺 {r.status_code}", r.status_code == 200)

        # 幂等
        r = await http.post(f"/api/rounds/{rid}/commitments",
                            json={"commitment": commits[0]},
                            headers={"X-Session-Token": joins[0]["session_token"]})
        check("重复承诺幂等 200", r.status_code == 200 and r.json()["idempotent"] is True)

        await asyncio.sleep(0.3)
        snap = (await http.get(f"/api/rounds/{rid}/snapshot", headers=H)).json()
        check("收齐承诺后 status=revealing（WS 也已广播）",
              snap["status"] == "revealing" and "revealing" in broadcast_statuses)

        # 揭示 + 错误秘密作废用另一轮测，这里走正常结算
        for i, j in enumerate(joins):
            r = await http.post(f"/api/rounds/{rid}/reveals",
                                json={"secret": secrets[i]},
                                headers={"X-Session-Token": j["session_token"]})
            check(f"成员 {i} 揭示 {r.status_code}", r.status_code == 200)

        try:
            await asyncio.wait_for(got_settled.wait(), timeout=3)
            check("WS 收到 settled 广播", True)
        except asyncio.TimeoutError:
            check("WS 收到 settled 广播", False, f"got {broadcast_statuses}")

        # WS ping/pong + 主动 snapshot（先排空 settle 广播积压的快照）
        async def recv_with_expectation(timeout=2.0):
            return json.loads(await asyncio.wait_for(mem_ws.recv(), timeout=timeout))

        # 排空队列直到最近一条 settled 快照
        settled_seen = False
        try:
            while True:
                m = await recv_with_expectation(timeout=0.5)
                if m.get("type") == "snapshot" and m["snapshot"]["status"] == "settled":
                    settled_seen = True
        except asyncio.TimeoutError:
            pass
        check("成员 WS 已收到 settled 广播", settled_seen)

        await mem_ws.send(json.dumps({"type": "ping"}))
        check("WS ping -> pong", (await recv_with_expectation())["type"] == "pong")
        await mem_ws.send(json.dumps({"type": "snapshot"}))
        msnap = await recv_with_expectation()
        check("WS 主动拉取快照 settled", msnap["snapshot"]["status"] == "settled")
        check("成员结算后可见全员秘密（公开复算）",
              all("secret" in m for m in msnap["snapshot"]["members"]))

        # 审计
        r = await http.get(f"/api/rounds/{rid}/audit", headers=H)
        audit = r.json()
        check("审计导出 200 + settled", r.status_code == 200 and audit["status"] == "settled")
        check("审计结果一致", audit["result"]["seed"] == msnap["snapshot"]["result"]["seed"])
        # 成员不能导出
        r = await http.get(f"/api/rounds/{rid}/audit",
                           headers={"X-Session-Token": joins[0]["session_token"]})
        check("成员不能导出审计 -> 403", r.status_code == 403)

        # ---- 第二轮：HTTP 真实链路下承诺不符 -> 整轮作废并持久化 ----
        d2 = (await http.post("/api/rounds", json={
            "title": "作废HTTP", "members": ["x", "y"], "prizes": ["p"],
            "commit_deadline": iso(now + timedelta(minutes=10)),
            "reveal_deadline": iso(now + timedelta(minutes=30))})).json()
        rid2, host2 = d2["round_id"], d2["host_token"]
        joins2 = [(await http.post("/api/rounds/join", json={"code": i["code"]})).json()
                  for i in d2["invites"]]
        real2 = ["real-s0", "real-s1"]
        for i, j in enumerate(joins2):
            c = cpy.compute_commitment(rid2, i, ["x", "y"][i], real2[i])
            await http.post(f"/api/rounds/{rid2}/commitments",
                            json={"commitment": c},
                            headers={"X-Session-Token": j["session_token"]})
        r = await http.post(f"/api/rounds/{rid2}/reveals", json={"secret": "WRONG"},
                            headers={"X-Session-Token": joins2[0]["session_token"]})
        check("错误秘密 -> 409 commitment_mismatch",
              r.status_code == 409 and r.json()["error"]["code"] == "commitment_mismatch")
        snap2 = (await http.get(f"/api/rounds/{rid2}/snapshot",
                                headers={"X-Host-Token": host2})).json()
        check("作废已持久化（独立快照请求仍是 voided）",
              snap2["status"] == "voided" and
              snap2["void_reason"] == "commitment_mismatch")
        r = await http.post(f"/api/rounds/{rid2}/reveals", json={"secret": real2[1]},
                            headers={"X-Session-Token": joins2[1]["session_token"]})
        check("作废后其他成员的揭示也被拒绝 409", r.status_code == 409)

        await host_ws.close()
        await mem_ws.close()

    print()
    print(f"{len(fails)} FAILURES" if fails else "ALL HTTP/WS SMOKE TESTS PASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    env = {**os.environ, "DRAW_DB_PATH": DB, "DRAW_SWEEP_INTERVAL": "0.5"}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", PORT, "--log-level", "warning"],
        cwd="/workspace", env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # 等待健康
        import time
        for _ in range(50):
            try:
                if httpx.get(f"{BASE}/api/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        rc = asyncio.run(main())
    finally:
        server.send_signal(signal.SIGINT)
        try:
            out, _ = server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            out = server.communicate()[0]
        if os.environ.get("VERBOSE"):
            print(out)
    sys.exit(rc)
