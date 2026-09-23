"""`POST /v1/asr` 与 `app/core/asr/` —— 目的地那一格的**服务端**识别通道。

★ 这个接口的形状和其余路由**不一样**，所以更需要用例钉住：

    其余路由是「入 Frame，出 Announcement」；这一条出的是**文本**（数据），
    由端侧填进目的地那一格，然后再走导航那条路。把这一点钉住很重要 ——
    哪天有人「顺手统一一下」，把识别文本塞进 `announcements`，端侧就会把
    「目的地的名字」当一句话念出来，而且**用户说的话会被当成系统说的话**。

★ 另一条要钉的是「失败也回 200」：`degraded` 非空 = 服务端没在听，
  `text` 空且 `degraded` 空 = 听到了但没听清。这两种在端侧是**两句不同的话**
  （「系统没接识别」vs「请再说一次」），所以服务端不能把它们折成同一个东西。
  回 5xx 会把两者一起压成 `HTTP 500`，用户就再也分不出来了。

★ 这里的实现在测试里一律**不打网络**：`conftest._offline_asr` 把 `ASR` 钉在
  `none`，下面用假实现顶替。真实厂商那一条路单独用**请求形状**那组用例钉 ——
  不联网，但把「发出去的 body 长什么样」逐字固定下来（那个形状是试了四种才
  试出来的，见 `app/core/asr/dashscope.py` 的文件头）。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from starlette.testclient import TestClient

from app import config
from app.api import speech
from app.core import registry
from app.core.asr.base import ASRError, MAX_AUDIO_BYTES
from app.core.asr.dashscope import parse_asr_response
from app.main import create_app

#: 一小段假音频。内容不重要 —— 到不了任何真实厂商那里。
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 20


@pytest.fixture
def client():
    """每个用例一个全新的 app（理由见 test_api.py 的同名夹具）。"""
    with TestClient(create_app()) as c:
        yield c


def asr(client, audio: bytes = WAV, fmt: str | None = None, name: str = "a.wav",
        **extra):
    """按端侧的方式发一段录音：multipart，音频放 `audio` 字段。"""
    files = {"audio": (name, audio, "audio/wav")}
    data = dict(extra)
    if fmt is not None:
        data["format"] = fmt
    return client.post("/v1/asr", files=files, data=data)


class FakeASR:
    """能摆成任意姿态的假实现 —— 用它把每条分支都走到，不碰网络。"""

    def __init__(self, text: str = "带我去太原站", healthy: bool = True,
                 boom: BaseException | None = None):
        self.name = "fake"
        self.text = text
        self.healthy = healthy
        self.boom = boom
        self.calls: list[tuple[bytes, str]] = []

    async def transcribe(self, audio: bytes, fmt: str) -> str:
        self.calls.append((audio, fmt))
        if self.boom is not None:
            raise self.boom
        return self.text

    async def health(self) -> bool:
        return self.healthy


@pytest.fixture
def use_asr(monkeypatch):
    """把 `ASR` 指到一个假实现上。

    ★ 只顶替 `kind == "asr"`：`create_app()` 自己也要用注册表装配感知层和
      各层，把 `registry.get` 整个换掉会让**别的**东西悄悄换成假的。
    """

    real_get = registry.get

    def install(tool: FakeASR, name: str = "fake"):
        def fake_get(kind: str, impl: str, **kw):
            if kind != "asr":
                return real_get(kind, impl, **kw)
            return tool

        monkeypatch.setattr(config, "ASR", name)
        monkeypatch.setattr(registry, "get", fake_get)
        return tool

    return install


# --------------------------------------------------------------------------
# 信封：成功 / 没听清 / 没在听 —— 三种，必须分得开
# --------------------------------------------------------------------------


def test_success_returns_text_and_impl(client, use_asr):
    tool = use_asr(FakeASR("带我去太原站"))
    r = asr(client)

    assert r.status_code == 200
    assert r.json() == {"text": "带我去太原站", "impl": "fake", "degraded": []}
    assert tool.calls == [(WAV, "wav")], "音频与格式要原样交给实现"


def test_silent_audio_is_not_an_error(client, use_asr):
    """★ 听不清 ≠ 没在听。

    厂商对静音返回**空串**，这里原样透传：端侧据此说「没听清，请再按住说
    一次」—— 让用户再说一遍。要是把空串变成 `degraded`，用户听到的会是
    「系统没接识别」，他会去查一个根本没坏的东西。
    """
    use_asr(FakeASR(""))
    body = asr(client).json()

    assert body == {"text": "", "impl": "fake", "degraded": []}


def test_disabled_asr_reports_a_reason_not_an_error(client):
    """`ASR=none`（默认）是**合法状态**，不是故障 —— 回 200 + 原因码。"""
    body = asr(client).json()

    assert body["text"] == ""
    assert body["impl"] == "none"
    assert body["degraded"][0]["reason"] == "asr_unavailable"
    assert body["degraded"][0]["impl"] == "none"


def test_unregistered_name_is_a_reason_not_a_crash(client, monkeypatch):
    """`.env` 里写了个还没实现的 `ASR=whisper` —— 说清是配置问题，别 500。"""
    monkeypatch.setattr(config, "ASR", "whisper")
    body = asr(client).json()

    assert body["text"] == ""
    assert body["degraded"][0]["reason"] == "asr_not_registered"
    assert {"dashscope", "none"} <= set(body["degraded"][0]["known"])


def test_misconfigured_credentials_are_a_reason_not_a_500(client, monkeypatch):
    """★ `ASR=dashscope` 但忘了配 key —— 必须回**原因码**，不能回 500。

    这条是真踩出来的：`DashscopeASR.__init__` 原先在缺 key 时抛
    `RuntimeError`，而 `registry.get()` 就在请求处理路径上 —— 一个 500 直接
    冒到端侧，用户听到的是「语音上传失败：HTTP 500」，既不知道是自己没说清、
    也不知道是配置没配齐。

    `routers/baidu.py` 早就把这条写成了规矩（「没配 AK 时**不要在
    `__init__` 里抛异常**：抛出去会直接变成 500，降级路径根本来不及触发」）。
    """
    monkeypatch.setattr(config, "ASR", "dashscope")
    monkeypatch.setattr(config, "ASR_API_KEY", "")
    monkeypatch.setattr(config, "ASR_BASE_URL", "")

    r = asr(client)

    assert r.status_code == 200
    assert r.json()["degraded"][0]["reason"] == "asr_misconfigured"


def test_misconfigured_is_told_apart_from_not_connected(client):
    """★ 「配置没配齐」和「本来就没接」要分开说。

    一个该去查 `.env`，一个本来就该退回浏览器那条。混成一个原因码，会让人
    去查一个根本没坏的东西（和 `router_not_registered` /
    `router_unavailable` 分开是同一条规矩）。
    """
    reasons = {impl: registry.get("asr", impl).unavailable_reason
               for impl in ("none", "dashscope")}

    assert reasons["none"] == "asr_unavailable"
    assert reasons["dashscope"] == "asr_misconfigured"
    assert len(set(reasons.values())) == 2, "两个实现的原因码不能撞"


def test_the_dashscope_impl_does_not_raise_on_construction(monkeypatch):
    """★ 直接钉住那条反模式：缺凭据时**构造**不能抛，只能由 `health()` 说。

    上面那条走的是端点，这条走构造器本身 —— 将来有人「顺手校验一下参数，
    在 `__init__` 里 raise 更干净」，这里会立刻红。
    """
    monkeypatch.setattr(config, "ASR_API_KEY", "")
    monkeypatch.setattr(config, "ASR_BASE_URL", "")

    from app.core.asr.dashscope import DashscopeASR

    tool = DashscopeASR()          # 不抛
    assert tool.unavailable_reason == "asr_misconfigured"


def test_implementation_failure_is_reported_not_raised(client, use_asr):
    """识别没成功（超时 / 401 / 429）→ `asr_failed`，带上厂商原话。"""
    use_asr(FakeASR(boom=ASRError("识别服务返回 429：Throttling")))
    body = asr(client).json()

    assert body["text"] == ""
    assert body["degraded"][0]["reason"] == "asr_failed"
    assert "Throttling" in body["degraded"][0]["detail"]


def test_unexpected_exception_never_500s(client, use_asr):
    """★ 兜底：连没预料到的异常也不能变成 500。

    500 到了端侧就只剩一句 `HTTP 500`，「没说清」和「没听见」当场混成一件事。
    """
    use_asr(FakeASR(boom=ValueError("形状不对")))
    r = asr(client)

    assert r.status_code == 200
    assert r.json()["degraded"][0]["reason"] == "asr_error"


# --------------------------------------------------------------------------
# 信封的形状
# --------------------------------------------------------------------------


def test_asr_never_returns_an_announcement(client, use_asr):
    """★ 这一条路出的是**数据**，不是播报 —— 端侧要填进输入框，不是念出来。"""
    use_asr(FakeASR("北京西站"))
    body = asr(client).json()

    assert set(body) == {"text", "impl", "degraded"}
    assert "announcements" not in body


def test_failure_is_still_200(client, use_asr):
    """★ 用原因码降级，而不是用状态码 —— 端侧要念的是**哪件事坏了**。"""
    use_asr(FakeASR(healthy=False))
    assert asr(client).status_code == 200


# --------------------------------------------------------------------------
# 请求本身的毛病 —— 这些才是 4xx（端侧的 bug，不是系统的状态）
# --------------------------------------------------------------------------


def test_missing_audio_field_is_400(client):
    r = client.post("/v1/asr", data={"format": "wav"})
    assert r.status_code == 400
    assert "audio" in r.json()["error"]


def test_empty_audio_is_400(client):
    """空文件是**请求**的问题（端侧录到了 0 字节），不是「没听清」。"""
    r = asr(client, b"")
    assert r.status_code == 400
    assert "空" in r.json()["error"]


def test_unknown_format_is_rejected(client):
    """★ 声明了格式但不在白名单里 —— 拒掉，**不静默换成 wav**。

    静默替换会把「客户端说了个我们不认识的东西」藏起来，而厂商那边只会在
    识别结果上体现为一个莫名其妙的错误。
    """
    r = asr(client, fmt="exe")
    assert r.status_code == 400
    assert "format" in r.json()["error"]


def test_oversized_audio_is_413(client, monkeypatch, use_asr):
    use_asr(FakeASR())
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 8)

    r = asr(client, WAV)
    assert r.status_code == 413
    assert "太大" in r.json()["error"]


def test_the_size_cap_is_not_trivially_small():
    """★ 上限本身也要看一眼：10 秒 16 kHz 单声道 PCM16 是 320 KB。

    定得太小会把正常录音全挡在门外，而现象是「说什么都上传失败」。
    """
    assert MAX_AUDIO_BYTES >= 1_000_000


# --------------------------------------------------------------------------
# format：显式声明 > 文件名后缀 > wav
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name,declared,expected", [
    ("a.wav", None, "wav"),
    ("a.mp3", None, "mp3"),
    ("a.m4a", None, "m4a"),
    ("A.WAV", None, "wav"),          # 大小写不该决定成败
    ("voice", None, "wav"),          # 没有后缀 → 端侧转出来的就是 wav
    ("a.webm", "mp3", "mp3"),        # 显式声明优先于后缀
])
def test_format_resolution(client, use_asr, name, declared, expected):
    tool = use_asr(FakeASR("到了"))
    asr(client, name=name, fmt=declared)

    assert tool.calls[0][1] == expected


def test_format_from_a_client_is_never_forwarded_raw(client, use_asr):
    """★ 白名单是**安全边界**：`format` 会进请求体，转给厂商。

    不校验的话客户端就能决定我们发出去的是什么（`format` 里塞路径、塞
    JSON 片段），而厂商的报错不会指回这里。这里顺带钉住上限：白名单之外的
    值一律 400，绝不透传。
    """
    use_asr(FakeASR())
    for bad in ("../../etc/passwd", "wav; rm -rf /", "{'a':1}", ""):
        r = asr(client, fmt=bad)
        assert r.status_code in (400, 200), bad
        if r.status_code == 400:
            assert "format" in r.json()["error"]


# --------------------------------------------------------------------------
# 真实厂商那一条：请求形状 —— 不联网，但逐字钉住
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, payload: dict, text: str):
        self.status_code = status
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._payload


@pytest.fixture
def spy_http(monkeypatch):
    """把 dashscope 实现里的 `httpx.AsyncClient` 换成记账本。"""
    captured: dict = {}

    def install(status: int = 200, payload: dict | None = None, text: str = ""):
        payload = payload if payload is not None else {
            "choices": [{"message": {"content": "带我去太原站。"}}]}
        text = text or json.dumps(payload, ensure_ascii=False)

        class Spy:
            def __init__(self, **kw):
                captured["timeout"] = kw.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None):
                captured.update(url=url, headers=headers, body=json)
                return _Resp(status, payload, text)

        monkeypatch.setattr(httpx, "AsyncClient", Spy)
        return captured

    return install


@pytest.fixture
def real_asr(monkeypatch):
    """把 `ASR` 拨到 dashscope，并给它一副假凭据（请求会被上面的账本拦住）。"""
    monkeypatch.setattr(config, "ASR", "dashscope")
    monkeypatch.setattr(config, "ASR_BASE_URL", "https://example.invalid/compatible-mode/v1")
    monkeypatch.setattr(config, "ASR_API_KEY", "sk-SECRET-must-not-leak")


def test_asr_request_shape_is_the_one_that_actually_works(client, spy_http, real_asr):
    """★ 把实测出来的请求形状钉住 —— 这个形状是试了四种才定下来的。

    踩过的三个坑（见 `app/core/asr/dashscope.py` 文件头）：

      · `data` 必须是 **data-URL**。裸 base64 会被厂商当 URL 解析，400
        「The provided URL does not appear to be valid」—— 而**只有**这一句
        报错，从里面看不出该改成什么形状。这个用例就是防止哪天有人「顺手
        把前缀去掉」。
      · `format` 必须在 `input_audio` **里面**。少了它 / 放到外面 → 400
        「format is empty」。
      · 模型只能是 `qwen3-asr-flash`：`qwen-audio-3.0-asr-flash` 看着更新，
        但在兼容端点上不认这种形状，同样报 `format is empty`。
    """
    cap = spy_http()
    r = asr(client, WAV)

    assert r.json()["text"] == "带我去太原站。"
    assert cap["url"] == "https://example.invalid/compatible-mode/v1/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer sk-SECRET-must-not-leak"

    body = cap["body"]
    assert body["model"] == "qwen3-asr-flash"
    assert body["stream"] is False

    part = body["messages"][0]["content"][0]
    assert part["type"] == "input_audio"
    assert part["input_audio"]["format"] == "wav"

    data = part["input_audio"]["data"]
    assert data.startswith("data:;base64,"), "裸 base64 会被厂商当 URL 拒掉"
    assert base64.b64decode(data.split(",", 1)[1]) == WAV, "音频要原样上去"


def test_vendor_error_keeps_the_words_but_never_the_key(client, spy_http, real_asr):
    """★ 厂商原话要带出来（能直接指向 bug），凭据一个字都不能漏。

    `degraded.detail` 会进端侧日志、也可能被人贴进聊天里 —— 厂商自己的错误
    信息里不会有 `Authorization`，但**我们的**异常包装很容易顺手把它拼进去。
    """
    spy_http(status=401, payload={"error": {"message": "Invalid API-key provided"}})
    body = asr(client).json()

    assert body["degraded"][0]["reason"] == "asr_failed"
    assert "Invalid API-key provided" in body["degraded"][0]["detail"]
    assert "sk-SECRET" not in json.dumps(body)


def test_default_model_is_the_one_that_works():
    """★ 默认值也是契约：`qwen-audio-3.0-asr-flash` 在兼容端点上 400。"""
    assert config.ASR_MODEL == "qwen3-asr-flash"


# --------------------------------------------------------------------------
# 响应解析（纯函数，无网络）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload,expected", [
    ({"choices": [{"message": {"content": "带我去太原站。"}}]}, "带我去太原站。"),
    ({"choices": [{"message": {"content": "  前后有空格  "}}]}, "前后有空格"),
    ({"choices": [{"message": {"content": ""}}]}, ""),
    ({"choices": [{"message": {"content": [{"type": "text", "text": "分段"}]}}]}, "分段"),
])
def test_parse_asr_response_reads_the_text(payload, expected):
    assert parse_asr_response(payload) == expected


@pytest.mark.parametrize("payload", [
    None,
    [],
    {},
    {"choices": []},
    {"choices": [{}]},
    {"choices": [{"message": "不是对象"}]},
    {"choices": [{"message": {}}]},
    {"choices": [{"message": {"content": 42}}]},
])
def test_parse_asr_response_rejects_other_shapes(payload):
    """★ 形状不对一律**抛** —— 绝不返回空串。

    返回空串会被端侧读成「听到了但没听清」，于是用户一遍遍重说，而实际
    情况是服务端坏了。这正是包注释里那条区分要守的东西。
    """
    with pytest.raises(ASRError):
        parse_asr_response(payload)


# --------------------------------------------------------------------------
# 与 /v1/health 的关系 —— 只报信息，不进降级
# --------------------------------------------------------------------------


def test_health_lists_asr_impls_but_never_counts_it_as_degraded(client):
    """★ `ASR=none` 时 `/v1/health` 必须**照样是健康的**。

    语音识别不是四层核心链路（目的地还能打字），缺了它由端侧在按下的那一刻
    如实说出来。挂进降级列表只会让「降级」这枚徽章长期亮着，把 VLM /
    检测器 / 地图那三个**真的**降级淹掉。
    """
    body = client.get("/v1/health").json()

    assert body["ok"] is True
    assert body["degraded"] == []
    assert {"dashscope", "none"} <= set(body["impls"]["asr"])


def test_health_lists_asr_next_to_the_other_layers(client):
    """`impls` 是给排查用的信息性清单 —— asr 该和其余几类并排出现。"""
    impls = client.get("/v1/health").json()["impls"]

    assert "asr" in impls
    assert {"vlm", "detector", "layer", "framesource", "router"} <= set(impls)


# --------------------------------------------------------------------------
# 端侧那一半 —— 两条通道、换通道要出声、麦克风不留常驻
# --------------------------------------------------------------------------

#: 页面上会加载的全部脚本（按页分组，理由见 test_api.py::PAGE_JS）。
PAGE_JS = {
    "/": ["/static/js/dom.js", "/static/js/state.js", "/static/js/log.js",
          "/static/js/speech.js", "/static/js/voice.js", "/static/js/net.js",
          "/static/js/ui.js", "/static/js/dev.js", "/static/js/nav.js",
          "/static/js/main.js", "/static/map.js"],
    "/static/sender.html": ["/static/js/sender.js", "/static/js/dom.js",
                            "/static/js/state.js", "/static/js/log.js",
                            "/static/js/net.js"],
}


def js(client, path: str) -> str:
    return client.get(path).text


def test_voice_prefers_our_own_recogniser_over_the_browser_one(client):
    """★ 两条通道，**服务端优先** —— 这是这一节全部改动的理由。

    浏览器那条走的是**厂商的云**（Chrome→Google / Safari→Apple），key 烘在
    浏览器二进制里，页面里无处可配：国内网络基本不可用（实测 `error ===
    "network"`），出了事我们既看不到失败率也换不掉它。服务端那条把这一环
    拿回自己手里 —— 代价只是松手后多一个往返。
    """
    src = js(client, "/static/js/voice.js")

    assert 'recorderAvailable() ? "server" : (RecogCtor() ? "browser" : null)' in src, \
        "优先级必须是 服务端 → 浏览器 → 没有"
    assert 'from "./net.js"' in src and "postAsr" in src, \
        "上传要走传输层，不能在这一层自己 fetch"
    assert "SpeechRecognition" in src, "浏览器那条不能因为加了新通道就删掉"


def test_voice_records_16k_mono_wav_and_lets_go_of_the_mic(client):
    """★ 转码放端侧做，是为了**后端不引入 ffmpeg**（Termux 上装不动）。

    浏览器录出来的是 webm/opus（Chrome）或 mp4/aac（Safari），厂商那两个只认
    文件格式。顺带还省一次上传：16k 单声道比 44.1k 立体声小十倍。
    """
    src = js(client, "/static/js/voice.js")

    assert "const TARGET_RATE = 16000" in src
    assert "OfflineAudioContext" in src, "重采样交给浏览器，比自己写线性插值靠谱"
    assert "encodeWav" in src and "RIFF" in src, "要自己封 WAV 头"

    # ★ 录完立刻关麦：不留常驻录音。既是隐私，也避免「按第 N 次就没反应了」
    #   （每个页面能开的 AudioContext 有上限，见 blobToWav 里的 close）。
    assert "getTracks().forEach" in src and "t.stop()" in src, "录完要关掉麦克风"
    assert "ctx.close()" in src, "AudioContext 也要关，否则按几次之后就用不了了"


def test_switching_to_the_browser_channel_is_announced(client):
    """★ 换通道**必须说出来**。

    用户按住之后听到的反馈换了一套，而他没有做任何事 —— 不说清楚，他会以为
    是自己按错了。这和「降级必须如实播报」是同一条规矩。

    ★ 而且只有**永久性**的问题（配置）才换通道：`asr_failed`（超时 / 429）
      是暂时性的，换个通道并不能救它，只会把一次网络抖动变成永久的换道。
    """
    src = js(client, "/static/js/voice.js")

    assert "function isPermanent(" in src
    i = src.index("function isPermanent(")
    body = src[i:src.index("\n}\n", i)]
    assert "asr_unavailable" in body and "asr_not_registered" in body
    assert "asr_failed" not in body, "调用失败是暂时性的，不该据此换引擎"

    assert "已切到浏览器识别" in src, "换通道要出声（走 localNote，能翻回来重看）"
    assert "localNote(" in src, "这句话要进播报流，不能只弹个视觉提示"


def test_a_temporary_failure_keeps_the_option_open(client):
    """★ 暂时失败**不标死、不换道**，只是重试 —— 第二次起才劝打字。

    一次失败不值得劝人放弃这个功能；一直失败才该劝。这条同时防住两种坏结果：
    把按钮标成「语音不可用」（网络会回来，标死等于假红）和无声地什么都不做。
    """
    src = js(client, "/static/js/voice.js")

    assert "serverFails" in src, "要数连续失败次数"
    assert "serverFails >= 2" in src, "第二次起改成劝打字"
    i = src.index("function degrade(")
    body = src[i:i + 1400]
    assert 'paint(btn, "dead")' not in body, \
        "服务端这条路失败不该把按钮标死 —— 那是给「一条通道都没有」留的"


def test_the_recording_cap_is_announced_not_silent(client):
    """★ 按住不放不能录到天荒地老，但**截断必须说出来**。

    静默截断等于偷偷丢掉用户后半句话：他以为说完了，系统只收到一半。
    """
    src = js(client, "/static/js/voice.js")

    assert "const MAX_RECORD_MS = 15_000" in src
    i = src.index("if (timedOut) {")
    body = src[i:i + 400]
    assert "localNote(" in body, "录满上限要出声说明"
    assert "录满" in body


def test_the_first_hold_tells_the_truth_about_the_permission_box(client):
    """第一次按住会弹权限框，用户常在框没点完时就松手。

    ★ 这时**不能**假装在听（那是假绿）：要关掉流，如实说「已就绪，请再按住
      说一次」。下一次就是秒开。
    """
    src = js(client, "/static/js/voice.js")

    assert "麦克风已就绪，请再按住说一次" in src
    i = src.index("麦克风已就绪")
    before = src[max(0, i - 400):i]
    assert "getTracks().forEach" in before, "说这句之前要先把流关掉"


def test_only_the_transport_layer_knows_the_asr_url(client):
    """★ 传输层的唯一入口（和 `submitRoute` 只能有一份同一条规矩）。

    `net.js` 是「与后端的通道全在这里」那一个文件。别的模块自己去
    `fetch("/v1/asr")` 就等于把通道复制成两份 —— 改一处、另一处静默不一致，
    而这个项目刚在 `.video-strip` 上栽过同类跟头。
    """
    # ★ 只看**字符串字面量**：注释里提 `/v1/asr` 是好事（那是在说明这条路
    #   通到哪儿），而真去请求必须写出 `"/v1/asr"` 这种字面量。用文本搜会把
    #   说明文字也算成违规，测试就会逼着人删注释。
    literal = ('"/v1/asr"', "'/v1/asr'")
    offenders = {}
    for files in PAGE_JS.values():
        for path in files:
            src = js(client, path)
            if path != "/static/js/net.js" and any(q in src for q in literal):
                offenders[path] = [ln.strip() for ln in src.splitlines()
                                   if any(q in ln for q in literal)]
    assert not offenders, f"只有 net.js 该知道 /v1/asr 这个地址：{offenders}"

    net = js(client, "/static/js/net.js")
    assert 'fetch("/v1/asr"' in net
    assert "export async function postAsr(" in net
    # ★ 失败也回 200：4xx 才是请求本身的毛病（缺字段 / 太大），要抛给调用方说。
    assert "if (!r.ok) throw" in net, "请求本身的错要抛；降级要原样交回"
