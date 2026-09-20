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
    POST /v1/frame                 ★ 统一帧入口（multipart 上传图像）
    GET  /v1/health                健康检查 + 降级状态
    WS   /v1/stream                统一播报下发
    GET  /                         前端调试台
    GET  /data/*                   测试素材（demo.mp4 / frames）

`/v1/frame` 与其余路由信封完全一致，区别只是**图像走 multipart 上传**而不是
`image_ref` 指一个服务端已有的路径。端侧（摄像头 / 视频抽帧 / 图片文件）
只需要这一个「发图」接口。

启动：
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from app import config
from app.contracts import (
    PRIORITY_IMPORTANT,
    SOURCE_PERCEPTION,
    SOURCE_SAFETY,
    SOURCE_SYSTEM,
    Announcement,
    Frame,
    system_detail,
)
from app.core import registry
from app.core.arbiter import Arbiter

LAYER_NAMES = ("perception", "safety", "navigation", "emergency")

#: `/v1/frame` 允许的 source —— 只有这两层吃图像。
_UPLOAD_SOURCES = frozenset({SOURCE_PERCEPTION, SOURCE_SAFETY})

#: 仓库根目录。前端和测试素材都按它定位，避免依赖启动时的 cwd。
BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"

#: 上传帧的临时落盘目录。
#: `image_ref` 的语义是「服务端路径」（`layers/perception.py` 用
#: `os.path.isfile` 找它），所以上传的字节必须先落地，下游才读得到。
UPLOAD_DIR = DATA_DIR / "uploads"


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

    # 上传帧要落盘，目录得先存在（fresh clone 时 data/ 整个都不在）
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------

    async def publish(anns: list[Announcement], now: int) -> list[Announcement]:
        """过闸门，放行的推给前端。返回真正发出去的。

        排序 / 打断 / 积压 / 到期不补播都在端侧 —— 服务端观察不到播放状态，
        在这里模拟只会自找麻烦（契约里 ttl_ms 本来就写「从端收到起算」）。
        """
        sent: list[Announcement] = []
        for ann in anns:
            if arbiter.submit(ann, now) != "sent":
                continue
            sent.append(ann)
            await hub.broadcast({"type": "announcement", "data": ann.to_dict()})
        return sent

    async def _respond(layer, frame: Frame) -> JSONResponse:
        """跑一层，把能播的推给前端，并按统一信封返回。

        JSON 路由和 `/v1/frame` 的 multipart 路由共用这一条尾巴 ——
        「入 Frame，出 Announcement」的信封只有这一处实现。
        """
        anns: list[Announcement] = await layer.handle(frame)
        await publish(anns, frame.ts)

        return JSONResponse({
            "frame": frame.to_dict(),
            "announcements": [a.to_dict() for a in anns],
            "arbiter": {
                "sent": sorted(arbiter.sent_ids),
                "dropped": [{"id": a.id, "reason": r} for a, r in arbiter.dropped[-5:]],
            },
        })

    def make_handler(layer, source: str, default_extra: dict | None = None):
        defaults = default_extra or {}

        async def handler(request):
            try:
                body = await request.json()
            except Exception:
                body = {}

            return await _respond(layer, _frame_from_body(body, source, defaults))

        return handler

    # ------------------------------------------------------------------
    # ★ 统一帧入口 —— 真实端侧（摄像头 / 视频抽帧 / 图片文件）走这条
    # ------------------------------------------------------------------

    def _bad(msg: str) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=400)

    async def upload_frame(request):
        """multipart 收一帧图像，按 `source` 分发到对应层。

        这是端侧唯一需要的「发图」接口：摄像头、视频抽帧、单张图片都走它，
        差别只在帧源。上传字节先落到临时文件，好让 `image_ref` 保持
        「服务端路径」的语义不变 —— 下游的 `read_image()` 和未来的真实
        检测器一行都不用改。
        """
        try:
            form = await request.form()
        except Exception as e:
            return _bad(f"multipart 解析失败: {e}")

        upload = form.get("image")
        if upload is None or isinstance(upload, str):
            return _bad("缺少 image 字段（multipart 文件）")

        source = (form.get("source") or SOURCE_PERCEPTION).strip()
        if source not in _UPLOAD_SOURCES:
            return _bad(f"source 只能是 {sorted(_UPLOAD_SOURCES)}，收到 {source!r}")

        raw = await upload.read()
        if not raw:
            return _bad("image 是空文件")

        try:
            extra = json.loads(form.get("extra") or "{}")
        except json.JSONDecodeError as e:
            return _bad(f"extra 不是合法 JSON: {e}")
        if not isinstance(extra, dict):
            return _bad("extra 必须是 JSON 对象")

        try:
            ts = int(form.get("ts") or now_ms())
        except ValueError:
            return _bad("ts 必须是毫秒整数")

        suffix = os.path.splitext(upload.filename or "")[1] or ".jpg"
        with tempfile.NamedTemporaryFile(
            dir=UPLOAD_DIR, prefix="frame_", suffix=suffix, delete=False
        ) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name

        try:
            frame = Frame(
                frame_id=form.get("frame_id") or f"f{now_ms()}",
                ts=ts,
                image_ref=tmp_path,
                source=source,
                extra=extra,
            )
            return await _respond(layers[source], frame)
        finally:
            # 下游在返回前已把字节同步读进内存，这里删掉安全
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

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
        sent = await publish(anns, now)
        return JSONResponse({
            "now_ms": now,
            "announcements": [a.to_dict() for a in anns],
            "sent": [a.id for a in sent],
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
    # 前端调试台
    # ------------------------------------------------------------------

    async def homepage(request):
        """单文件调试台。没有构建步骤，直接读盘返回。"""
        index = WEB_DIR / "index.html"
        if not index.is_file():
            return JSONResponse(
                {"error": "前端页面缺失", "expected": str(index)}, status_code=404)
        return FileResponse(index)

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
        Route("/v1/frame", upload_frame, methods=["POST"]),
        Route("/v1/health", health, methods=["GET"]),
        WebSocketRoute("/v1/stream", stream),
        Route("/", homepage, methods=["GET"]),
        # 测试素材给前端 <video> 用。fresh clone 时 data/ 还不存在，
        # 所以别让 StaticFiles 在构造期就因为目录缺失把整个 app 拖垮。
        Mount("/data", StaticFiles(directory=DATA_DIR, check_dir=False), name="data"),
    ]

    app = Starlette(routes=routes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in config.CORS_ORIGINS.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.hub = hub
    app.state.arbiter = arbiter
    app.state.layers = layers
    app.state.broadcast_degraded = broadcast_degraded
    return app


app = create_app()
