"""FastAPI 入口：REST + WebSocket + 静态页面 + 后台超时清扫。"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import service
from .service import ServiceError
from .ws import manager, ws_endpoint

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SWEEP_INTERVAL = float(os.environ.get("DRAW_SWEEP_INTERVAL", "1.0"))


class CreateRoundBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    members: list[str] = Field(min_length=2, max_length=100)
    prizes: list[str] = Field(min_length=1, max_length=100)
    commit_deadline: str
    reveal_deadline: str


class JoinBody(BaseModel):
    code: str


class CommitmentBody(BaseModel):
    commitment: str


class RevealBody(BaseModel):
    secret: str


def _auth(request: Request, round_id: str):
    return service.authenticate(
        round_id,
        request.headers.get("x-host-token"),
        request.headers.get("x-session-token"),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    manager.bind_loop(loop)
    service.register_change_hook(manager.broadcast_threadsafe)

    async def sweeper():
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            try:
                await loop.run_in_executor(None, service.sweep_due_rounds)
            except Exception:
                pass

    task = asyncio.create_task(sweeper())
    yield
    task.cancel()


app = FastAPI(title="多人公平抽签室", version="1.0.0", lifespan=lifespan)


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # 折叠 Pydantic 细节为统一错误结构；保留第一条可读信息
    detail = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in detail.get("loc", []) if x != "body")
    msg = detail.get("msg", "请求参数无效")
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "invalid_input",
            "message": f"{loc + ': ' if loc else ''}{msg}",
        }},
    )


# ----------------------------------------------------------------- REST API

@app.post("/api/rounds", status_code=201)
async def create_round(body: CreateRoundBody):
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: service.create_round(
            body.title, body.members, body.prizes,
            body.commit_deadline, body.reveal_deadline),
    )


@app.post("/api/rounds/join")
async def join_round(body: JoinBody):
    return await asyncio.get_running_loop().run_in_executor(
        None, service.join_round, body.code)


@app.post("/api/rounds/{round_id}/commitments")
async def post_commitment(round_id: str, body: CommitmentBody, request: Request):
    _role, member_index = _auth(request, round_id)
    if member_index is None:
        raise ServiceError("forbidden", "主持人无需提交承诺", 403)
    return await asyncio.get_running_loop().run_in_executor(
        None, service.submit_commitment, round_id, member_index, body.commitment)


@app.post("/api/rounds/{round_id}/reveals")
async def post_reveal(round_id: str, body: RevealBody, request: Request):
    _role, member_index = _auth(request, round_id)
    if member_index is None:
        raise ServiceError("forbidden", "主持人无需揭示秘密", 403)
    return await asyncio.get_running_loop().run_in_executor(
        None, service.submit_reveal, round_id, member_index, body.secret)


@app.get("/api/rounds/{round_id}/snapshot")
async def get_snapshot(round_id: str, request: Request):
    role, member_index = _auth(request, round_id)
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: service.build_snapshot(
            round_id, role=role, member_index=member_index),
    )


@app.get("/api/rounds/{round_id}/audit")
async def get_audit(round_id: str, request: Request):
    role, _idx = _auth(request, round_id)
    if role != "host":
        raise ServiceError("forbidden", "仅主持人可导出审计文件", 403)
    return await asyncio.get_running_loop().run_in_executor(
        None, service.build_audit, round_id)


@app.websocket("/api/ws/{round_id}")
async def websocket_round(websocket: WebSocket, round_id: str):
    await ws_endpoint(websocket, round_id)


@app.get("/api/health")
async def health():
    return {"ok": True}


# ----------------------------------------------------------------- 页面

@app.get("/", include_in_schema=False)
async def index_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/host.html", include_in_schema=False)
async def host_page():
    return FileResponse(STATIC_DIR / "host.html")


@app.get("/member.html", include_in_schema=False)
async def member_page():
    return FileResponse(STATIC_DIR / "member.html")


@app.get("/verify.html", include_in_schema=False)
async def verify_page():
    return FileResponse(STATIC_DIR / "verify.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
