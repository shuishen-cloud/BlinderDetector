"""
灵眸伴途 —— Starlette 应用装配。

七条路由全部「入 Frame，出 Announcement」：
    POST /v1/perception/describe
    POST /v1/safety/analyze
    POST /v1/safety/fall
    POST /v1/navigation/route
    POST /v1/emergency/sos
    GET  /v1/health
    WS   /v1/stream

启动：
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import time
from typing import Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from app import config
from app.contracts import (
    PRIORITY_IMPORTANT,
    SOURCE_SYSTEM,
    Announcement,
    Frame,
    system_detail,
)
from app.core import registry
from app.core.arbiter import Arbiter


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# WebSocket 广播
# --------------------------------------------------------------------------


class Hub:
    """把播报推给所有连接的前端。"""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:
                self.disconnect(ws)


hub = Hub()
arbiter = Arbiter()


# --------------------------------------------------------------------------
# 路由处理器
# --------------------------------------------------------------------------


def _frame_from_body(body: dict[str, Any], source: str) -> Frame:
    return Frame(
        frame_id=body.get("frame_id") or f"f{now_ms()}",
        ts=body.get("ts") or now_ms(),
        image_ref=body.get("image_ref"),
        source=source,
        extra=body.get("extra") or {},
    )


def make_handler(layer, source: str):
    """layer 在启动时实例化一次 —— 层可能持有状态（比如跌倒检测的活跃事件）。"""

    async def handler(request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        frame = _frame_from_body(body, source)
        anns: list[Announcement] = await layer.handle(frame)

        # 过仲裁器 -> 推给前端
        for ann in anns:
            verdict = arbiter.submit(ann, frame.ts)
            if verdict.startswith("dropped"):
                continue
            await hub.broadcast({"type": "announcement", "data": ann.to_dict()})

        return JSONResponse(
            {
                "frame": frame.to_dict(),
                "announcements": [a.to_dict() for a in anns],
                "arbiter": {
                    "spoken": sorted(arbiter.spoken_ids),
                    "dropped": [
                        {"id": a.id, "reason": r} for a, r in arbiter.dropped[-5:]
                    ],
                },
            }
        )

    return handler


async def health(request):
    """★ 沉默不能有歧义。

    用户若不知道系统哑了，会把「没出声」理解为「环境安全」。
    """
    degraded: list[dict[str, Any]] = []
    try:
        vlm = registry.get("vlm", config.VLM_PROVIDER)
        if not await vlm.health():
            degraded.append({"reason": "vlm_unavailable"})
    except Exception as e:
        degraded.append({"reason": "vlm_error", "detail": str(e)})

    return JSONResponse(
        {
            "ok": not degraded,
            "degraded": degraded,
            "impls": {
                "vlm": registry.names("vlm"),
                "layer": registry.names("layer"),
                "framesource": registry.names("framesource"),
            },
            "config": {
                "VLM_PROVIDER": config.VLM_PROVIDER,
                "FRAME_SOURCE": config.FRAME_SOURCE,
            },
        }
    )


async def stream(ws: WebSocket):
    await hub.connect(ws)
    try:
        await ws.send_json({"type": "hello", "data": {"ts": now_ms()}})
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong", "data": {"ts": now_ms()}})
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(ws)


async def broadcast_degraded(reason: str) -> None:
    """降级时显式通告，别让系统静默。"""
    ann = Announcement(
        text="系统部分功能暂时不可用",
        ttl_ms=10000,
        dedup_key=f"system:degraded:{reason}",
        source=SOURCE_SYSTEM,
        priority=PRIORITY_IMPORTANT,
        detail=system_detail(reason),
    )
    await hub.broadcast({"type": "announcement", "data": ann.to_dict()})


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------


def create_app() -> Starlette:
    registry.load_all()  # 触发各实现模块的 @register

    # 每层实例化一次并复用 —— 有状态的层（跌倒检测）靠这个保住状态
    layers = {
        name: registry.get("layer", name)
        for name in ("perception", "safety", "navigation", "emergency")
    }

    routes = [
        Route("/v1/perception/describe", make_handler(layers["perception"], "perception"), methods=["POST"]),
        Route("/v1/safety/analyze", make_handler(layers["safety"], "safety"), methods=["POST"]),
        Route("/v1/safety/fall", make_handler(layers["emergency"], "emergency"), methods=["POST"]),
        Route("/v1/navigation/route", make_handler(layers["navigation"], "navigation"), methods=["POST"]),
        Route("/v1/emergency/sos", make_handler(layers["emergency"], "emergency"), methods=["POST"]),
        Route("/v1/health", health, methods=["GET"]),
        WebSocketRoute("/v1/stream", stream),
    ]
    return Starlette(routes=routes)


app = create_app()
