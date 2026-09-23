"""ASR 抽象接口 + 共享常量。

接口只认 `(audio 字节, 容器格式)`，不认来源：浏览器录音、上传的音频文件、
将来真机 App 录的 m4a，走的都是同一个实现。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class ASRError(RuntimeError):
    """识别**没能进行**（没接实现 / 超时 / 401 / 429 / 返回的形状不对）。

    ★ 与「返回空串」的区别见包注释：空串 = 听到了但没听清；本异常 = 没在听。
      端侧对这两件事的措辞完全不同，所以不能合并。
    """


#: 允许上传的容器格式。
#:
#: ★ `wav` 是**实测过**的那一个（16 kHz 单声道 PCM16，见 `docs/api-contract.md`
#:   §4.2）。其余几个是厂商文档列的常见格式 —— 列出来是为了让「换个客户端录音
#:   实现」不必改服务端，但只有 wav 有实测样本背书。
ALLOWED_FORMATS: frozenset[str] = frozenset({
    "wav", "mp3", "m4a", "aac", "ogg", "opus", "amr", "flac", "webm",
})

#: 单次上传的音量上限。16 kHz 单声道 PCM16 走 10 秒只有 320 KB，
#: 留够余量即可 —— 放开只会让弱网上的失败更慢、更容易超时。
MAX_AUDIO_BYTES: int = 8 * 1024 * 1024


@runtime_checkable
class ASRTool(Protocol):
    name: str

    #: `health()` 为 False 时该报的原因码。
    #:
    #: ★ 两个实现**必须不一样**，因为它们指向完全不同的排查方向：
    #:   `none` 的问题是「本来就没接识别」（端侧该退回浏览器那条），
    #:   而带着凭据的实现多半是**配置没配齐**（少 key / 少地址，端侧该去
    #:   查 `.env`，但功能上同样只能退回浏览器那条）。混成一个码会让人去
    #:   查一个根本没坏的东西 —— 和 `router_not_registered` 与
    #:   `router_unavailable` 分开是同一条规矩。
    unavailable_reason: str

    async def transcribe(self, audio: bytes, fmt: str) -> str:
        """返回识别文本。听不清可以返回空串；**没能识别必须抛 `ASRError`**。"""
        ...

    async def health(self) -> bool:
        """这个实现现在能不能用（凭据 / 地址配好了没有）。

        ★ 用**返回值**表达「没配好」，不要在 `__init__` 里抛异常 ——
          `registry.get()` 就在请求处理路径上，抛出去会直接变成 500，
          降级路径根本来不及触发（这条路 `routers/baidu.py` 已经踩过，
          见那句「没配 AK 时不要在 `__init__` 里抛异常」）。
        """
        ...
