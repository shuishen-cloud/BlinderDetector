"""帧图像的读取与判定。

★ 为什么单独成模块（2026-09-23）：`read_image()` 原本住在
  `app/core/layers/perception.py` —— 那是**第一层**。第二层的检测器要读同一
  份图，就得反过来 import 一个 layer，层次是歪的（`新功能与接口改动.md` §1.2
  的「另一个小问题」写的就是这条）。
  现在它落在 `core` 根上：谁都能用，谁都不欠谁。

★ 这里只有纯标准库 —— 它是热路径（第二层延迟预算 <200ms），
  不该因为读一张图而拉进任何重依赖。
"""

from __future__ import annotations

import os


def read_image(image_ref: str | None) -> bytes:
    """读帧图像。读不到返回空 —— 不因为缺一张图把整条链路打断。

    ★ 返回空串而不是抛异常：调用方要能区分「没有图像」和「图像读坏了」，
      而且两者的处理方式不同（前者通常该报降级，后者通常该记日志）。
    """
    if not image_ref or not os.path.isfile(image_ref):
        return b""
    try:
        with open(image_ref, "rb") as f:
            return f.read()
    except OSError:
        return b""


def image_mime(image: bytes) -> str:
    """按**魔数**判断图片类型。

    ★ 不要按扩展名猜：两个生产者写出来的东西不一样 ——
      `POST /v1/frame` 收的是浏览器 canvas 导出的 JPEG，视频抽帧是 ffmpeg
      写的 PNG，而 `image_ref` 是服务端路径，后缀不一定还在。
      猜错的后果是把 base64 塞进 data URI 时带上错误的 MIME，
      厂商接口会直接 400，而且报的是「图片格式不支持」这种看不出根因的话。
    """
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    # 两个真实生产者之外的东西（webp 等）—— 按 JPEG 交出去，
    # 让厂商去拒；在本地猜格式只会把失败藏得更深。
    return "image/jpeg"
