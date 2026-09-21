"""灵眸伴途 —— Starlette 应用装配。

这个文件**只做装配**：建状态、连出口、挂路由。实现分别在：

    app/api/routes.py     路由表 + 小 handler（tick / health / 调试台）
    app/api/uploads.py    ★ 统一帧入口 POST /v1/frame（multipart）
    app/api/envelope.py   统一信封：入 Frame，出 Announcement
    app/api/hub.py        WS 播报通道
    app/core/             业务逻辑（各层编排、规则、provider）

全部路由都是「入 `Frame`，出 `Announcement`」：

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
    GET  /static/*                 调试台的 css / js
    GET  /data/*                   测试素材（demo.mp4 / frames）

启动：
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware

from app import config
from app.api.envelope import Envelope
from app.api.hub import Hub
from app.api.routes import build_routes, make_degraded_announcer
from app.core import registry
from app.core.arbiter import Arbiter

# 路径常量住在 app/paths.py（api 层也要用），这里转出来是为了让
# `from app.main import UPLOAD_DIR` 这种老写法继续可用 —— 测试在用。
# noqa 因为确实没有任何一行代码「读」它们。
from app.paths import BASE_DIR, DATA_DIR, UPLOAD_DIR, WEB_DIR  # noqa: F401

LAYER_NAMES = ("perception", "safety", "navigation", "emergency")


def create_app() -> Starlette:
    """装配一个新的应用实例。

    hub / arbiter / 各层实例都在这里创建 —— 它们都持有状态，
    不能做成模块级单例，否则多个 app 实例（主要是测试）会互相污染。
    """
    registry.load_all()  # 触发各实现模块的 @register

    hub = Hub()
    arbiter = Arbiter()
    envelope = Envelope(hub, arbiter)
    layers = {name: registry.get("layer", name) for name in LAYER_NAMES}

    # 上传帧要落盘，目录得先存在（fresh clone 时 data/ 整个都不在）
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    app = Starlette(routes=build_routes(hub, envelope, layers))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in config.CORS_ORIGINS.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.hub = hub
    app.state.arbiter = arbiter
    app.state.layers = layers
    app.state.broadcast_degraded = make_degraded_announcer(hub)
    return app


app = create_app()
