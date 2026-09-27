"""WebSocket 连接管理。

* 主持人 / 成员凭各自的令牌连接；
* 连接后立即推送当前快照（重连恢复）；
* 每次状态迁移向该轮次所有连接者广播 ``snapshot``；
* 客户端可发 ``{"type":"snapshot"}`` 主动拉取，``ping`` 心跳保活。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect

from . import service
from .service import ServiceError


class ConnectionManager:
    def __init__(self) -> None:
        # round_id -> set[WebSocket]
        self._rooms: dict[str, set[WebSocket]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket, round_id: str) -> None:
        await ws.accept()
        self._rooms.setdefault(round_id, set()).add(ws)

    def disconnect(self, ws: WebSocket, round_id: str) -> None:
        room = self._rooms.get(round_id)
        if room:
            room.discard(ws)
            if not room:
                self._rooms.pop(round_id, None)

    @staticmethod
    async def _safe_send(ws: WebSocket, message: dict) -> None:
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
        except Exception:
            pass

    async def push_to(self, ws: WebSocket, message: dict) -> None:
        await self._safe_send(ws, message)

    def broadcast_threadsafe(self, round_id: str) -> None:
        """供同步的服务层线程 / 后台定时器调用。"""
        if self._loop is None or round_id not in self._rooms:
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(round_id), self._loop)

    @staticmethod
    def _snapshot_sync(round_id: str, host_token: str | None,
                       session_token: str | None) -> dict:
        role, member_index = service.authenticate(
            round_id, host_token, session_token)
        return service.build_snapshot(
            round_id, role=role, member_index=member_index)

    async def broadcast(self, round_id: str) -> None:
        room = list(self._rooms.get(round_id, ()))
        if not room:
            return
        loop = asyncio.get_running_loop()

        async def snapshot_for(ws: WebSocket):
            qp = ws.query_params
            # 同步 SQLite 调用放入执行器，避免阻塞事件循环；
            # 每连接独立快照构建（SQLite 读锁串行），可并发等待。
            return await loop.run_in_executor(
                None, self._snapshot_sync, round_id,
                qp.get("host_token"), qp.get("session_token"))

        results = await asyncio.gather(*[snapshot_for(ws) for ws in room],
                                       return_exceptions=True)
        for ws, result in zip(room, results):
            if isinstance(result, Exception):
                await self._safe_send(ws, {
                    "type": "error", "code": "snapshot_failed",
                    "message": "快照获取失败，请重连",
                })
            else:
                await self._safe_send(ws, {"type": "snapshot", "snapshot": result})


manager = ConnectionManager()


async def ws_endpoint(websocket: WebSocket, round_id: str) -> None:
    qp = websocket.query_params
    loop = asyncio.get_running_loop()
    try:
        role, member_index = await loop.run_in_executor(
            None,
            lambda: service.authenticate(round_id, qp.get("host_token"),
                                          qp.get("session_token")))
    except ServiceError:
        await websocket.close(code=4401)
        return

    await manager.connect(websocket, round_id)
    try:
        snapshot = await loop.run_in_executor(
            None, lambda: service.build_snapshot(
                round_id, role=role, member_index=member_index))
        await manager.push_to(websocket, {"type": "snapshot", "snapshot": snapshot})

        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                await manager.push_to(websocket, {
                    "type": "error", "code": "bad_message", "message": "消息必须是 JSON"})
                continue
            mtype = msg.get("type")
            if mtype == "ping":
                await manager.push_to(websocket, {"type": "pong"})
            elif mtype == "snapshot":
                snap = await loop.run_in_executor(
                    None, lambda: service.build_snapshot(
                        round_id, role=role, member_index=member_index))
                await manager.push_to(websocket, {"type": "snapshot", "snapshot": snap})
            else:
                await manager.push_to(websocket, {
                    "type": "error", "code": "unknown_type",
                    "message": f"未知消息类型：{mtype}"})
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, round_id)
