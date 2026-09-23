"""`POST /v1/asr` —— 把一段录音转成文本（multipart）。

★ 这是**唯一一条不返回 `Announcement` 的业务入口**：识别出的文本是**数据**
  （端侧要把它填进目的地那一格，再走 `nav.js::submitRoute()`），不是一句要
  播出去的话。把它硬塞进 `announcements` 会让端侧把「目的地的名字」和
  「系统要说的话」混成一条 —— 而这两件事的去处完全不同：
  前者进输入框，后者进播报流。

  同类的还有 `/v1/health` 与 `/v1/frontend-config`（见 routes.py 的说明），
  它们也是「不是入 Frame 出 Announcement」的路由。

★ 形状（`{"text", "impl", "degraded"}`）是照 `/v1/frame` 那套「信封 + 原因码」
  的规矩定的：**失败也回 200**，把原因放进 `degraded` 让端侧如实说出来。
  回 5xx 只会让端侧拿到一句 `HTTP 500`，而用户听到「发送失败：HTTP 500」
  仍然不知道**是自己没说清还是系统没听见** —— 那正是本项目最忌讳的歧义。

    · `degraded` 为空 + `text` 非空  → 识别成功
    · `degraded` 为空 + `text` 为空  → 听到了，但没听清（「请再说一次」）
    · `degraded` 非空                → **没在听**，原因在 `reason` 里（「系统哑了」）

  第三种与前两种的区分是整个接口存在的理由，端侧的措辞完全不同。
"""

from __future__ import annotations

import os
from typing import Any

from starlette.responses import JSONResponse

from app import config
from app.core import registry
from app.core.asr.base import ALLOWED_FORMATS, MAX_AUDIO_BYTES, ASRError

#: 上传时若没带 `format`，按扩展名猜；再猜不到就按 wav（端侧转出来的就是这个）。
_FALLBACK_FORMAT = "wav"


def _bad(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _ok(text: str, impl: str, degraded: list[dict[str, Any]] | None = None) -> JSONResponse:
    return JSONResponse({"text": text, "impl": impl, "degraded": degraded or []})


def _guess_format(filename: str | None, declared: str | None) -> str:
    """`format` 以**显式声明**优先，其次看文件名后缀，最后按 wav。

    ★ 只认白名单里的值：把任意的 `format` 透传给厂商，等于让客户端决定我们
      发什么请求 —— 一个 `format: "../../etc/passwd"` 就能把请求体变成别的东西。
    """
    for raw in (declared, os.path.splitext(filename or "")[1].lstrip(".")):
        if not raw:
            continue
        fmt = raw.strip().lower()
        if fmt in ALLOWED_FORMATS:
            return fmt
        return ""      # 声明了但不在白名单 → 让调用方拒掉，别静默替换
    return _FALLBACK_FORMAT


async def transcribe(request) -> JSONResponse:
    """收一段音频，交给 `ASR` 选的实现，回文本 + 原因。"""
    try:
        form = await request.form()
    except Exception as e:
        return _bad(f"multipart 解析失败: {e}")

    upload = form.get("audio")
    if upload is None or isinstance(upload, str):
        return _bad("缺少 audio 字段（multipart 文件）")

    fmt = _guess_format(upload.filename, form.get("format"))
    if not fmt:
        return _bad(f"format 只支持 {sorted(ALLOWED_FORMATS)}")

    raw = await upload.read()
    if not raw:
        return _bad("audio 是空文件")
    if len(raw) > MAX_AUDIO_BYTES:
        return _bad(f"audio 太大（{len(raw)} 字节 > {MAX_AUDIO_BYTES}）", 413)

    name = config.ASR
    try:
        tool = registry.get("asr", name)
    except KeyError:
        # 名字没注册（`ASR=whisper` 但还没写这个实现）。**不能**在这里把服务
        # 拖垮，也不能静默 —— 端侧要拿到一个能念出来的原因。
        return _ok("", name, [{
            "reason": "asr_not_registered",
            "impl": name,
            "known": registry.names("asr"),
        }])

    try:
        if not await tool.health():
            # ★ 原因码由实现自己给（`none` = 本来没接 / `dashscope` = 配置没配齐）。
            #   这两件事的排查方向完全不同，端侧的措辞也不同。
            return _ok("", name, [{
                "reason": getattr(tool, "unavailable_reason", "asr_unavailable"),
                "impl": name,
            }])
        text = await tool.transcribe(raw, fmt)
    except ASRError as e:
        # 厂商原话带出来（「format is empty」这种能直接指向 bug），
        # 但**不带**凭据：ASRError 的消息里只有状态码与响应正文片段。
        return _ok("", name, [{"reason": "asr_failed", "impl": name, "detail": str(e)}])
    except Exception as e:                     # 兜底：绝不把 500 抛给端侧
        return _ok("", name, [{"reason": "asr_error", "impl": name, "detail": str(e)}])

    return _ok(text, name)


def make_transcribe():
    """绑成 Starlette 认得的端点。放在这里而不是直接挂 `transcribe`，
    是为了和 `uploads.py::make_upload_frame` 一个写法 —— 那条路将来若要
    闭包进状态（比如连接复用），改这里就够了。"""

    async def endpoint(request):
        return await transcribe(request)

    return endpoint
