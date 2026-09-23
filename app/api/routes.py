"""路由表，以及那几个不值得各自成文件的小 handler。

`/v1/frame`（multipart）在 `uploads.py`，WS 在 `hub.py`，
信封共用部分在 `envelope.py` —— 这里只剩「薄薄的几条」。
"""

from __future__ import annotations

from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from app import config
from app.api.envelope import Envelope, now_ms
from app.api.hub import Hub
from app.api.speech import make_transcribe
from app.api.uploads import make_upload_frame
from app.contracts import (
    PRIORITY_IMPORTANT,
    SOURCE_SYSTEM,
    Announcement,
    system_detail,
)
from app.core import registry
from app.paths import DATA_DIR, WEB_DIR


def make_degraded_announcer(hub: Hub):
    """降级时显式通告，别让系统静默。

    ★「沉默 ≠ 安全」：用户若不知道系统哑了，会把「没出声」理解成
      「环境安全」。这是这类系统最危险的失效模式。

    真实降级来自 VLM / 检测器不可用；此时应该主动播一条，而不是等
    用户自己发现画面不再更新。
    """

    async def announce_degraded(reason: str) -> None:
        ann = Announcement(
            text="系统部分功能暂时不可用",
            ttl_ms=10_000,
            dedup_key=f"system:degraded:{reason}",
            source=SOURCE_SYSTEM,
            priority=PRIORITY_IMPORTANT,
            detail=system_detail(reason),
        )
        await hub.broadcast({"type": "announcement", "data": ann.to_dict()})

    return announce_degraded


async def tick(request, envelope: Envelope, layers: dict) -> JSONResponse:
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
    sent = await envelope.publish(anns, now)
    return JSONResponse({
        "now_ms": now,
        "announcements": [a.to_dict() for a in anns],
        "sent": [a.id for a in sent],
    })


async def health(request) -> JSONResponse:
    """★ 沉默不能有歧义。

    用户若不知道系统哑了，会把「没出声」理解为「环境安全」。
    所以这里不只报 ok/false，还把**哪个实现不可用**一起吐出来。
    """
    degraded: list[dict] = []
    for kind, name in (
        ("vlm", config.VLM_PROVIDER),
        ("detector", config.DETECTOR),
        # ★ router 也要报：ROUTER=baidu 而 AK 没配时，用户听到的其实是
        #   内置演示路网 —— 不说出来的话，「没出声」和「系统哑了」就分不开了。
        ("router", config.ROUTER),
    ):
        try:
            if not await registry.get(kind, name).health():
                degraded.append({"reason": f"{kind}_unavailable", "impl": name})
        except KeyError:
            # ★ 名字根本没注册（典型：`ROUTER=amap` 但还没写这个实现，或
            #   照抄 .env.example 时把 `ROUTER=` 留空）。这与「服务不可用」
            #   是两件事，混成一个原因码会让人去查网络而不是查配置。
            degraded.append({
                "reason": f"{kind}_not_registered",
                "impl": name,
                "known": registry.names(kind),
            })
        except Exception as e:
            degraded.append({"reason": f"{kind}_error", "impl": name, "detail": str(e)})

    return JSONResponse({
        "ok": not degraded,
        "degraded": degraded,
        # ★ asr 只出现在这份**信息性**清单里，不进上面的降级列表：语音识别不是
        #   四层核心链路（目的地还能打字），缺了它会在按下的那一刻由端侧如实
        #   说出来；挂进降级列表只会让「降级」这枚徽章长期亮着，把 VLM /
        #   检测器 / 地图那三个真的降级淹掉。见 app/core/asr/__init__.py。
        "impls": {k: registry.names(k) for k in
                  ("vlm", "detector", "layer", "framesource", "router", "asr")},
        "config": {
            "VLM_PROVIDER": config.VLM_PROVIDER,
            "DETECTOR": config.DETECTOR,
            "FRAME_SOURCE": config.FRAME_SOURCE,
            "ROUTER": config.ROUTER,
        },
    })


async def frontend_config(request) -> JSONResponse:
    """调试台启动时要的那点配置。

    ★ 浏览器端 AK **不能写进 `web/` 里的文件** —— 那是静态托管目录，
      写死等于把它提交进仓库。浏览器 AK 本身是公开的（靠 Referer 白名单
      保护），但**每个人的 AK 不同**，不该让别人的 AK 成为仓库的一部分
      —— 与服务端 AK 走 `.env` 是同一套逻辑。

    ★ 这不是「入 Frame 出 Announcement」的路由，与 `/v1/health` 同类：
      它是给前端自己用的配置，不是业务接口。
    """
    return JSONResponse({
        "baidu_browser_ak": config.BAIDU_BROWSER_AK,
    })


async def homepage(request) -> FileResponse | JSONResponse:
    """前端调试台。零构建：四个静态文件，没有打包步骤。"""
    index = WEB_DIR / "index.html"
    if not index.is_file():
        return JSONResponse(
            {"error": "前端页面缺失", "expected": str(index)}, status_code=404)
    return FileResponse(index)


def make_tick(envelope: Envelope, layers: dict):
    """同上：把状态闭包进去，路由表只认 `request`。"""

    async def endpoint(request):
        return await tick(request, envelope, layers)

    return endpoint


def build_routes(hub: Hub, envelope: Envelope, layers: dict) -> list:
    """九条业务路由 + WS + 调试台 + 素材托管。

    ★ 每一条都是「入 Frame，出 Announcement」，差异只在 `source` 和
      `detail` 的形状上。端侧拿到播报只播 `text`、执行 `haptic`，
      另外读 `priority` / `interrupt` / `ttl_ms` 做排序与到期（详见
      docs/design.md D11）——不需要理解任何业务结构。
    """
    return [
        Route("/v1/perception/describe",
              envelope.handler(layers["perception"], "perception"), methods=["POST"]),
        Route("/v1/safety/analyze",
              envelope.handler(layers["safety"], "safety"), methods=["POST"]),
        Route("/v1/safety/fall",
              envelope.handler(layers["emergency"], "emergency",
                               {"kind": "fall_signal"}), methods=["POST"]),
        Route("/v1/navigation/route",
              envelope.handler(layers["navigation"], "navigation"), methods=["POST"]),
        Route("/v1/emergency/sos",
              envelope.handler(layers["emergency"], "emergency",
                               {"kind": "sos"}), methods=["POST"]),
        Route("/v1/emergency/cancel",
              envelope.handler(layers["emergency"], "emergency",
                               {"kind": "cancel"}), methods=["POST"]),
        Route("/v1/emergency/tick", make_tick(envelope, layers), methods=["POST"]),
        Route("/v1/frame", make_upload_frame(envelope, layers), methods=["POST"]),
        # ★ 唯一一条不返回 Announcement 的业务入口 —— 识别文本是**数据**
        #   （端侧填进目的地），不是要播出去的话。理由见 api/speech.py 头注释。
        Route("/v1/asr", make_transcribe(), methods=["POST"]),
        Route("/v1/health", health, methods=["GET"]),
        # 调试台地图要的浏览器端 AK，从 .env 下发（不写进 web/ 里的文件）。
        Route("/v1/frontend-config", frontend_config, methods=["GET"]),
        WebSocketRoute("/v1/stream", hub.stream),
        Route("/", homepage, methods=["GET"]),
        # 调试台的 css / js。单文件拆成三个后必须挂出来，
        # 否则页面只有骨架没有样式。
        Mount("/static", StaticFiles(directory=WEB_DIR, check_dir=False), name="static"),
        # 测试素材给前端 <video> 用。fresh clone 时 data/ 还不存在，
        # 所以别让 StaticFiles 在构造期就因为目录缺失把整个 app 拖垮。
        Mount("/data", StaticFiles(directory=DATA_DIR, check_dir=False), name="data"),
    ]
