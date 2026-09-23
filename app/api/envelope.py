"""统一信封的出口：入 `Frame`，出 `Announcement`。

九条路由（含 `/v1/frame` 的 multipart 入口）共用**这一处**实现。
信封只有一种形状，多一条路由不该多一份拼 JSON 的代码 ——
否则迟早有一条路由悄悄长歪，而端侧只准备了一套解析。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from app.contracts import Announcement, Frame

if TYPE_CHECKING:  # 只为标注：真 import 会和 hub 成环
    from app.api.hub import Hub
    from app.core.arbiter import Arbiter


def now_ms() -> int:
    """当前毫秒时间戳。契约里所有 `ts` / `now_ms` 都是这个单位。"""
    return int(time.time() * 1000)


def frame_from_body(
    body: dict[str, Any], source: str, default_extra: dict | None = None
) -> Frame:
    """从请求体拼一个 `Frame`。

    路由级默认值打底，请求体覆盖 —— `/v1/safety/fall` 的
    `kind=fall_signal` 就是这么塞进去的，端侧不必知道自己调的是哪条路由。
    """
    return Frame(
        frame_id=body.get("frame_id") or f"f{now_ms()}",
        ts=body.get("ts") or now_ms(),
        image_ref=body.get("image_ref"),
        source=source,
        extra={**(default_extra or {}), **(body.get("extra") or {})},
    )


class Envelope:
    """跑完一层之后要做的两件事：过闸门推播报、按统一信封回包。"""

    def __init__(self, hub: "Hub", arbiter: "Arbiter") -> None:
        self.hub = hub
        self.arbiter = arbiter

    async def publish(self, anns: list[Announcement], now: int) -> list[Announcement]:
        """过闸门，放行的推给前端。返回真正发出去的。

        排序 / 打断 / 积压 / 到期不补播都在端侧 —— 服务端观察不到播放状态，
        在这里模拟只会自找麻烦（契约里 `ttl_ms` 本来就写「从端收到起算」）。
        """
        sent: list[Announcement] = []
        for ann in anns:
            if self.arbiter.submit(ann, now) != "sent":
                continue
            sent.append(ann)
            await self.hub.broadcast({"type": "announcement", "data": ann.to_dict()})
        return sent

    async def respond(self, layer, frame: Frame) -> JSONResponse:
        """跑一层，把能播的推给前端，并按统一信封返回。

        JSON 路由和 `/v1/frame` 复用这条尾巴，所以两者的回包结构
        （`frame` / `announcements` / `arbiter`）永远一致。
        """
        anns: list[Announcement] = await layer.handle(frame)
        await self.publish(anns, frame.ts)

        return JSONResponse({
            "frame": frame.to_dict(),
            "announcements": [a.to_dict() for a in anns],
            "arbiter": {
                "sent": sorted(self.arbiter.sent_ids),
                "dropped": [{"id": a.id, "reason": r} for a, r in self.arbiter.dropped[-5:]],
            },
        })

    def handler(self, layer, source: str, default_extra: dict | None = None):
        """给纯 JSON 的 POST 路由做一个瘦包装。

        请求体不是合法 JSON 时按空对象处理 —— 空 `Frame` 也是合法输入，
        这条链路的每一层都允许「什么也没看见」。
        """
        defaults = default_extra or {}

        async def handler(request):
            try:
                body = await request.json()
            except Exception:
                body = {}

            return await self.respond(layer, frame_from_body(body, source, defaults))

        return handler
