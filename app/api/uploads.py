"""★ 统一帧入口 —— `POST /v1/frame`（multipart）。

真实端侧只需要这一个「发图」接口：摄像头、视频抽帧、单张图片都走它，
差别只在帧源。信封与其余九条 JSON 路由**完全一致**，唯一区别是
图像走上传字节而不是 `image_ref` 指一个服务端已有的路径。

★ 字节必须先落到临时文件：`image_ref` 的语义就是「服务端路径」
  （`layers/perception.py` 用 `os.path.isfile` 找它），下游读的是路径。
  落盘的文件在**响应返回前**删掉 —— 跑一天不能把磁盘塞满。
"""

from __future__ import annotations

import json
import os
import tempfile

from starlette.responses import JSONResponse

from app.api.envelope import Envelope, frame_from_body, now_ms
from app.contracts import SOURCE_PERCEPTION, SOURCE_SAFETY
from app.paths import UPLOAD_DIR

#: `/v1/frame` 允许的 source —— 只有这两层吃图像。
_UPLOAD_SOURCES = frozenset({SOURCE_PERCEPTION, SOURCE_SAFETY})


def _bad(msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=400)


async def upload_frame(request, envelope: Envelope, layers: dict) -> JSONResponse:
    """收一帧图像，按 `source` 分发到对应层。"""
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
        # 帧的拼装复用 JSON 路由那一条路径：上传只是「图从哪来」不同，
        # 拼出来的 Frame 必须一模一样，否则两条入口会慢慢长歪。
        frame = frame_from_body(
            {
                "frame_id": form.get("frame_id"),
                "ts": ts,
                "image_ref": tmp_path,
                "extra": extra,
            },
            source,
        )
        return await envelope.respond(layers[source], frame)
    finally:
        # 下游在返回前已把字节同步读进内存，这里删掉安全
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def make_upload_frame(envelope: Envelope, layers: dict):
    """绑好状态，交给路由表。

    Starlette 只会给端点传 `request`，所以依赖得先闭包进来 ——
    这也让 `upload_frame` 本身保持「纯函数」的形状，方便单独调。
    """

    async def endpoint(request):
        return await upload_frame(request, envelope, layers)

    return endpoint
