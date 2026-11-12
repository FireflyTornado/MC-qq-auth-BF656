import asyncio
import logging
import secrets
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, Header, HTTPException, Response, WebSocket, WebSocketDisconnect

from app_config import (
    ConfigError,
    MAX_TOKEN_LENGTH,
    is_bearer_token,
    load_config,
    require_token as config_token,
    target_group_id,
)
from database import DB_PATH

config = load_config()
API_TOKEN = config_token(config, "MC_API_TOKEN").encode("ascii")
NOTIFY_TOKEN = config_token(config, "MC_NOTIFY_TOKEN").encode("ascii")
if secrets.compare_digest(API_TOKEN, NOTIFY_TOKEN):
    raise ConfigError("MC_API_TOKEN 与 MC_NOTIFY_TOKEN 必须使用不同的值")
TARGET_GROUP_ID = str(target_group_id(config))
del config

MAX_AUTHORIZATION_LENGTH = MAX_TOKEN_LENGTH + 128
WS_SEND_TIMEOUT = 3
WS_CLOSE_TIMEOUT = 3
RECONCILE_INTERVAL = 60
logger = logging.getLogger("mc_qq_auth")


# 不对外暴露自动生成的 API 文档
app = FastAPI(
    title="MC QQ Authentication API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def verify_bearer(authorization: list[str] | None, expected_token: bytes) -> bool:
    if not authorization or len(authorization) != 1:
        return False
    header = authorization[0]
    if len(header) > MAX_AUTHORIZATION_LENGTH:
        return False

    scheme, _, token = header.strip(" \t").partition(" ")
    token = token.lstrip(" ")
    return (
        scheme.lower() == "bearer"
        and is_bearer_token(token)
        and secrets.compare_digest(token.encode("ascii"), expected_token)
    )


def require_bearer(authorization: list[str] | None, expected_token: bytes) -> None:
    if not verify_bearer(authorization, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_token(authorization: list[str] | None = Header(default=None)) -> None:
    require_bearer(authorization, API_TOKEN)


@app.get("/internal/health")
async def health():
    return {"status": "ok", "minecraft_connections": len(connections)}


@app.get(
    "/internal/mc/snapshot",
    dependencies=[Depends(require_token)],
)
def get_snapshot(response: Response):
    """读取当前有效授权名单，不修改数据库。"""

    response.headers["Cache-Control"] = "no-store"

    # 以 SQLite 只读模式打开现有数据库
    db_uri = f"file:{DB_PATH}?mode=ro"

    try:
        with closing(
            sqlite3.connect(
                db_uri,
                uri=True,
                timeout=5,
            )
        ) as conn:

            # 同一事务内读取版本号和绑定数据
            conn.execute("BEGIN")

            row = conn.execute(
                "SELECT version FROM auth_state WHERE id = 1"
            ).fetchone()

            if row is None:
                raise RuntimeError("授权版本记录不存在")

            version = row[0]

            rows = conn.execute(
                """
                SELECT mc_name_key
                FROM offline_bindings
                WHERE group_id = ?
                  AND status = 'ACTIVE'
                ORDER BY mc_name_key
                """,
                (TARGET_GROUP_ID,),
            ).fetchall()

            active_names = [row[0] for row in rows]

            return {
                "version": version,
                "active_names": active_names,
            }

    except sqlite3.Error:
        raise HTTPException(
            status_code=503,
            detail="Authorization database unavailable",
        )


@dataclass
class BroadcastConnection:
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    failed: asyncio.Event = field(default_factory=asyncio.Event)


# 每个连接的路由负责关闭，广播只标记失败。
connections: dict[WebSocket, BroadcastConnection] = {}

# 避免多个通知同时广播
broadcast_lock = asyncio.Lock()

# 上一次广播的授权时间戳
last_broadcast_version: int | None = None


def read_auth_version() -> int:
    """从 SQLite 读取最新授权版本。"""

    uri = f"file:{DB_PATH}?mode=ro"

    with closing(
        sqlite3.connect(uri, uri=True, timeout=5)
    ) as conn:
        row = conn.execute(
            "SELECT version FROM auth_state WHERE id = 1"
        ).fetchone()

        if row is None or type(row[0]) is not int or row[0] < 0:
            raise RuntimeError("授权版本记录无效")
        return row[0]


async def send_update(websocket: WebSocket, connection: BroadcastConnection, message: dict) -> bool:
    try:
        async with asyncio.timeout(WS_SEND_TIMEOUT), connection.send_lock:
            if websocket not in connections:
                return False
            await websocket.send_json(message)
        return True
    except Exception as exc:
        connections.pop(websocket, None)
        connection.failed.set()
        logger.warning("授权 WebSocket 广播失败，正在关闭连接（%s）", type(exc).__name__)
        return False


async def broadcast_latest():
    """串行发布递增版本，每个版本并发向各连接发送。"""
    global last_broadcast_version
    async with broadcast_lock:
        version = await asyncio.to_thread(read_auth_version)
        if version == last_broadcast_version:
            return version, 0
        if last_broadcast_version is not None and version < last_broadcast_version:
            raise RuntimeError("数据库授权版本回退")

        message = {"type": "auth_updated", "version": version}
        delivered = await asyncio.gather(*(
            send_update(websocket, connection, message)
            for websocket, connection in tuple(connections.items())
        ))
        last_broadcast_version = version
        return version, sum(delivered)


# ----------------------------------------
# 内部 HTTP POST 通知接口
# ----------------------------------------

@app.post("/internal/mc/notify")
async def notify_auth_change(
    authorization: list[str] | None = Header(default=None),
):
    require_bearer(authorization, NOTIFY_TOKEN)

    try:
        version, delivered = await broadcast_latest()
    except (sqlite3.Error, RuntimeError):
        logger.exception("授权版本读取或推送失败")
        raise HTTPException(
            status_code=503,
            detail="Authorization update unavailable",
        )

    return {
        "success": True,
        "version": version,
        "delivered": delivered,
    }


# ----------------------------------------
# Minecraft WebSocket 接口
# ----------------------------------------

@app.websocket("/internal/mc/ws")
async def mc_websocket(websocket: WebSocket):

    if not verify_bearer(websocket.headers.getlist("authorization"), API_TOKEN):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    connection = BroadcastConnection()

    try:
        # Register before the greeting so the initial snapshot and broadcasts overlap.
        async with connection.send_lock:
            connections[websocket] = connection
            try:
                async with asyncio.timeout(WS_SEND_TIMEOUT):
                    await websocket.send_json({
                        "type": "connected",
                        "message": "Minecraft authentication connected",
                    })
            except Exception:
                connections.pop(websocket, None)
                await close_failed_websocket(websocket)
                return

        disconnected = asyncio.create_task(wait_for_disconnect(websocket))
        failed = asyncio.create_task(connection.failed.wait())
        try:
            done, _ = await asyncio.wait((disconnected, failed), return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                disconnected.result()
            else:
                await close_failed_websocket(websocket)
        finally:
            disconnected.cancel()
            failed.cancel()
            await asyncio.gather(disconnected, failed, return_exceptions=True)
    finally:
        connections.pop(websocket, None)


async def wait_for_disconnect(websocket: WebSocket) -> None:
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass


async def close_failed_websocket(websocket: WebSocket) -> None:
    try:
        async with asyncio.timeout(WS_CLOSE_TIMEOUT):
            await websocket.close(code=1011)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # The deployed Uvicorn closes the transport when the ASGI scope returns.
        logger.warning("授权 WebSocket 关闭帧发送失败，结束连接处理（%s）", type(exc).__name__)


# ----------------------------------------
# 内部补偿检查
# ----------------------------------------

async def reconcile_versions():
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL)

        try:
            await broadcast_latest()
        except Exception:
            logger.exception("授权版本补偿检查失败")


@app.on_event("startup")
async def start_reconciliation():
    app.state.reconcile_task = asyncio.create_task(
        reconcile_versions()
    )


@app.on_event("shutdown")
async def stop_reconciliation():
    task = app.state.reconcile_task
    task.cancel()

    with suppress(asyncio.CancelledError):
        await task
