"""
灵眸伴途 —— Starlette 应用装配。

所有路由统一「入 Frame，出 Announcement」：

    POST /v1/perception/describe   第一层 环境感知
    POST /v1/safety/analyze        第二层 安全预警
    POST /v1/safety/fall           第二层 跌倒检测
    POST /v1/navigation/route      第三层 智能导航
    POST /v1/emergency/sos         第四层 一键求助
    POST /v1/emergency/cancel      取消求助 / 取消跌倒确认
    POST /v1/emergency/tick        推进紧急状态机时钟
    GET  /v1/health                健康检查 + 降级状态
    WS   /v1/stream                统一播报下发

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

LAYER_NAMES = ("perception", "safety", "navigation", "emergency")


def now_ms() -> int:
    return int(time.time() * 1000)


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


def _frame_from_body(body: dict[str, Any], source: str, default_extra: dict) -> Frame:
    return Frame(
        frame_id=body.get("frame_id") or f"f{now_ms()}",
        ts=body.get("ts") or now_ms(),
        image_ref=body.get("image_ref"),
        source=source,
        # 路由级默认值打底，请求体覆盖
        extra={**default_extra, **(body.get("extra") or {})},
    )


def create_app() -> Starlette:
    """装配一个新的应用实例。

    hub / arbiter / 各层实例都在这里创建 —— 它们都持有状态，
    不能做成模块级单例，否则多个 app 实例（主要是测试）会互相污染。
    """
    registry.load_all()  # 触发各实现模块的 @register

    hub = Hub()
    arbiter = Arbiter()
    layers = {name: registry.get("layer", name) for name in LAYER_NAMES}

    # ------------------------------------------------------------------

    async def publish(anns: list[Announcement], now: int) -> list[Announcement]:
        """过仲裁器，能播的推给前端。返回真正播出去的。"""
        spoken: list[Announcement] = []
        for ann in anns:
            if arbiter.submit(ann, now).startswith("dropped"):
                continue
            spoken.append(ann)
            await hub.broadcast({"type": "announcement", "data": ann.to_dict()})
        return spoken

    def make_handler(layer, source: str, default_extra: dict | None = None):
        defaults = default_extra or {}

        async def handler(request):
            try:
                body = await request.json()
            except Exception:
                body = {}

            frame = _frame_from_body(body, source, defaults)
            anns: list[Announcement] = await layer.handle(frame)
            await publish(anns, frame.ts)

            return JSONResponse({
                "frame": frame.to_dict(),
                "announcements": [a.to_dict() for a in anns],
                "arbiter": {
                    "spoken": sorted(arbiter.spoken_ids),
                    "dropped": [{"id": a.id, "reason": r} for a, r in arbiter.dropped[-5:]],
                },
            })

        return handler

    # ------------------------------------------------------------------
    # 紧急状态机
    # ------------------------------------------------------------------

    async def tick(request):
        """推进紧急状态机的时钟。

        真实部署里这由后台定时器驱动；做成接口是为了方便测试升级链。

        ★ 端侧的倒计时是**本地自减**的，不依赖这个接口 —— 断网时倒计时
          仍然要走完。这是跌倒检测不做成纯服务端状态机的原因。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        now = body.get("now_ms") or now_ms()

        anns = layers["emergency"].tick(now)
        spoken = await publish(anns, now)
        return JSONResponse({
            "now_ms": now,
            "announcements": [a.to_dict() for a in anns],
            "spoken": [a.id for a in spoken],
        })

    # ------------------------------------------------------------------

    async def health(request):
        """★ 沉默不能有歧义。

        用户若不知道系统哑了，会把「没出声」理解为「环境安全」。
        这是这类系统最危险的失效模式。
        """
        degraded: list[dict[str, Any]] = []
        for kind, name in (("vlm", config.VLM_PROVIDER), ("detector", config.DETECTOR)):
            try:
                if not await registry.get(kind, name).health():
                    degraded.append({"reason": f"{kind}_unavailable", "impl": name})
            except Exception as e:
                degraded.append({"reason": f"{kind}_error", "impl": name, "detail": str(e)})

        return JSONResponse({
            "ok": not degraded,
            "degraded": degraded,
            "impls": {k: registry.names(k) for k in
                      ("vlm", "detector", "layer", "framesource")},
            "config": {
                "VLM_PROVIDER": config.VLM_PROVIDER,
                "DETECTOR": config.DETECTOR,
                "FRAME_SOURCE": config.FRAME_SOURCE,
            },
        })

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
            ttl_ms=10_000,
            dedup_key=f"system:degraded:{reason}",
            source=SOURCE_SYSTEM,
            priority=PRIORITY_IMPORTANT,
            detail=system_detail(reason),
        )
        await hub.broadcast({"type": "announcement", "data": ann.to_dict()})

    # ------------------------------------------------------------------

    routes = [
        Route("/v1/perception/describe",
              make_handler(layers["perception"], "perception"), methods=["POST"]),
        Route("/v1/safety/analyze",
              make_handler(layers["safety"], "safety"), methods=["POST"]),
        Route("/v1/safety/fall",
              make_handler(layers["emergency"], "emergency",
                           {"kind": "fall_signal"}), methods=["POST"]),
        Route("/v1/navigation/route",
              make_handler(layers["navigation"], "navigation"), methods=["POST"]),
        Route("/v1/emergency/sos",
              make_handler(layers["emergency"], "emergency",
                           {"kind": "sos"}), methods=["POST"]),
        Route("/v1/emergency/cancel",
              make_handler(layers["emergency"], "emergency",
                           {"kind": "cancel"}), methods=["POST"]),
        Route("/v1/emergency/tick", tick, methods=["POST"]),
        Route("/v1/health", health, methods=["GET"]),
        WebSocketRoute("/v1/stream", stream),
    ]

    app = Starlette(routes=routes)
    app.state.hub = hub
    app.state.arbiter = arbiter
    app.state.layers = layers
    app.state.broadcast_degraded = broadcast_degraded
    return app


app = create_app()
