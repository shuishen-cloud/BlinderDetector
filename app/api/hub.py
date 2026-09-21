"""WebSocket 播报通道 —— 端侧唯一的播报来源。

★ 这里只管「发」。排序、打断、积压保护一律不在此处做：
   喇叭在端侧，服务端观察不到播放状态（理由见 `app/core/arbiter.py` 的长注释）。
"""

from __future__ import annotations

from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from app.api.envelope import now_ms


class Hub:
    """维护所有连着的前端，把播报推给每一端。"""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        # 发失败就当对面已经断了。这里绝不能往外抛 ——
        # 一条僵死的连接否则会把整轮广播带走，其他人跟着收不到。
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:
                self.disconnect(ws)

    async def stream(self, ws: WebSocket) -> None:
        """`WS /v1/stream` 端点。

        客户端推 `{"type":"ping"}`，服务端回 `{"type":"pong"}`：
        端侧用它在没有播报的时段确认「通道还活着」，把「没出声」和
        「断线了」分开 —— 对这类系统，静默不能有歧义。
        """
        await self.connect(ws)
        try:
            # hello 是「通道已就绪」的信号。没有它，端侧在头几秒里
            # 分不清「系统哑了」和「暂时没内容」。
            await ws.send_json({"type": "hello", "data": {"ts": now_ms()}})
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "ping":
                    await ws.send_json({"type": "pong", "data": {"ts": now_ms()}})
        except WebSocketDisconnect:
            pass
        finally:
            self.disconnect(ws)
