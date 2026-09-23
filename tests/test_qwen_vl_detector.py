"""第二层：用 Qwen-VL 当检测器（`DETECTOR=qwen_vl`）。

★ 这些用例**一个网络请求都不发**，也**不烧任何配额**：

  · `parse_obstacles()` 是纯函数，模型的话用字符串喂给它就行；
  · `detect()` 的用例把 `_ask()` 换成桩，真正要验的是「读图 → 解析 → 失败必抛」
    这条骨架，而不是厂商接口。

  conftest 的 `_offline_vlm` 已经把 `VLM_API_KEY` 清空，所以这里的用例还得
  自己把它设回假值才能构造检测器；下面统一用一个**不可能解析**的 base_url
  （`127.0.0.1:9` 是 discard 端口），万一哪天真发了请求，也是本地立刻失败，
  不会打到厂商那边去。
"""

from __future__ import annotations

import asyncio

import pytest

from app import config
from app.contracts import SOURCE_SYSTEM, Frame
from app.core import registry
from app.core.detectors import qwen_vl
from app.core.detectors.qwen_vl import DetectorError, QwenVlDetector, parse_obstacles
from app.core.layers.safety import SafetyLayer

#: 解析函数不需要任何配置，这里只给个常量方便读。
FAKE_URL = "http://127.0.0.1:9/v1"


@pytest.fixture
def detector(monkeypatch):
    """一个配好假配置的检测器 —— 绝不联网。"""
    monkeypatch.setattr(config, "VLM_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(config, "VLM_BASE_URL", FAKE_URL)
    monkeypatch.setattr(config, "DETECTOR_MODEL", "qwen-vl-max")
    return QwenVlDetector()


def run(coro):
    """跑一个协程。★ 不为这点逻辑引入 pytest-asyncio（同 test_baidu_router.py）。"""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 解析：模型的话 → 契约形状
# --------------------------------------------------------------------------


def test_parses_a_plain_json_array():
    raw = parse_obstacles(
        '[{"type": "step_down", "position": "center", "distance_m": 2.0, "confidence": 0.86}]'
    )
    assert raw == [{
        "type": "step_down",
        "position": "center",
        "distance_m": 2.0,
        # ★ 距离是模型估的，必须带误差；40% 对齐 design.md D3 的「30–50%」
        "distance_sigma_m": 0.8,
        "confidence": 0.86,
        # 一帧量不出接近速度、也追不出 track_id —— 不编，交给规则层按保守处理
        "closing_speed_mps": None,
        "track_id": None,
    }]


@pytest.mark.parametrize("text", [
    '好的，这是结果：[{"type": "bicycle", "distance_m": 3}]',        # 前面带客套话
    '```json\n[{"type": "bicycle", "distance_m": 3}]\n```',           # 包了代码块
    '```\n[{"type": "bicycle", "distance_m": 3}]\n```',               # 没有语言标注
    '  \n[{"type": "bicycle", "distance_m": 3}]\n  \n',              # 前后留白
])
def test_finds_the_array_through_model_chatter(text):
    """模型不总是听话 —— 包代码块、先说一句「好的」都是常态。

    挖不出来就当失败（抛异常），**绝不**当成「前方没有障碍」。
    """
    assert [o["type"] for o in parse_obstacles(text)] == ["bicycle"]


def test_empty_array_means_nothing_in_the_way():
    """真的没有障碍 —— 这时候空列表是**正确**答案，不是失败。"""
    assert parse_obstacles("[]") == []


def test_drops_items_that_are_not_in_the_contract():
    """模型会编类型。编出来的直接丢，不要「修一修再用」。

    修出来的结果会变成莫名奇妙的播报，而按 D3 的态度：误报比漏报更伤 ——
    信任是消耗品。
    """
    raw = parse_obstacles(
        '[{"type": "traffic_cone", "distance_m": 1.0},'
        ' {"type": "stairs", "distance_m": 1.0},'
        ' {"type": "car", "distance_m": 1.0},'          # COCO 叫 car，契约里没有
        ' {"type": "person", "distance_m": 1.0}]'
    )
    assert [o["type"] for o in raw] == ["person"]


def test_drops_impossible_distances():
    """距离不合理（0、负数、字符串、几十米外的「障碍物」）的一律丢。"""
    raw = parse_obstacles(
        '[{"type": "curb", "distance_m": 0},'
        ' {"type": "curb", "distance_m": -3},'
        ' {"type": "curb", "distance_m": "很近"},'
        ' {"type": "curb", "distance_m": null},'
        ' {"type": "curb", "distance_m": 50},'
        ' {"type": "curb", "distance_m": 1.2}]'
    )
    assert [o["distance_m"] for o in raw] == [1.2]


def test_fixes_the_soft_fields():
    """位置不认识就当正前方；置信度缺了就卡在门限上，越界的夹回来。"""
    raw = parse_obstacles(
        '[{"type": "pole", "position": "前方", "distance_m": 2},'
        ' {"type": "pole", "position": "left", "distance_m": 2, "confidence": 7},'
        ' {"type": "pole", "position": "right", "distance_m": 2, "confidence": -1}]'
    )
    # 「前方」不是契约定的三个位置之一 —— 退到 center（宁可说正前方，不要乱指左右）
    assert [o["position"] for o in raw] == ["center", "left", "right"]
    # ★ 缺 confidence 时给 0.50，正好是 rules/risk.py 的门限：
    #   要不要说由规则层定，不在这里替它拍板
    assert [o["confidence"] for o in raw] == [0.50, 1.0, 0.0]


@pytest.mark.parametrize("text", [
    "前方有台阶，请注意。",                      # 纯文本，不是 JSON
    '{"type": "person"}',                        # 是 JSON，但不是数组
    "",                                          # 空响应
])
def test_unusable_replies_raise(text):
    """看不懂的回复 → 抛异常。这条路**不能**返回空列表。"""
    with pytest.raises(DetectorError):
        parse_obstacles(text)


# --------------------------------------------------------------------------
# detect()：读图 → 问模型 → 解析；失败必抛
# --------------------------------------------------------------------------


def test_detect_reads_the_image_and_parses(detector, monkeypatch, tmp_path):
    frame_img = tmp_path / "f.png"
    frame_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    seen = {}

    async def fake_ask(image: bytes) -> str:
        seen["image"] = image
        return '[{"type": "vehicle", "position": "right", "distance_m": 5.0}]'

    monkeypatch.setattr(detector, "_ask", fake_ask)
    frame = Frame(frame_id="f1", ts=1, image_ref=str(frame_img), source="safety")
    raw = run(detector.detect(frame))

    assert seen["image"].startswith(b"\x89PNG")           # 读的确实是那个文件
    assert raw[0]["type"] == "vehicle"
    assert raw[0]["distance_sigma_m"] == 2.0              # 40% × 5.0


def test_detect_propagates_unusable_replies(detector, monkeypatch, tmp_path):
    frame_img = tmp_path / "f.png"
    frame_img.write_bytes(b"\x89PNG\r\n\x1a\n")

    async def fake_ask(image: bytes) -> str:
        return "我觉得前面好像有点东西"

    monkeypatch.setattr(detector, "_ask", fake_ask)
    frame = Frame(frame_id="f1", ts=1, image_ref=str(frame_img), source="safety")
    with pytest.raises(DetectorError):
        run(detector.detect(frame))


def test_missing_image_is_a_failure_not_an_empty_scan(detector):
    """★ 读不到图 → 抛异常，不是「前方没有障碍」。

    这两件事长得一模一样，而用户听不出区别 —— 会把沉默当成安全。
    """
    frame = Frame(frame_id="f1", ts=1, image_ref=None, source="safety")
    with pytest.raises(DetectorError):
        run(detector.detect(frame))


def test_health_does_not_touch_the_network(detector):
    """`health()` 只查配置 —— 前端按秒轮询 /v1/health，打厂商接口等于烧配额。"""
    assert run(detector.health()) is True


def test_registered_and_selectable(detector, monkeypatch):
    """★ 注册表里必须有它，而且 `DETECTOR=qwen_vl` 能选到它。

    `load_all()` 平时由 `create_app()` 触发；单独跑这个文件时得自己叫一次，
    否则注册表里只有本文件 import 进来的东西。
    """
    registry.load_all()
    assert "qwen_vl" in registry.names("detector")
    assert isinstance(registry.get("detector", "qwen_vl"), QwenVlDetector)


def test_missing_key_fails_loudly(monkeypatch):
    """★ 没配 key 就当场报错，**绝不**悄悄退回 mock。

    悄悄退化的后果是「安全层其实没在看」，而页面上一切正常 —— 这正是
    整个项目最不能接受的那类失效。
    """
    monkeypatch.setattr(config, "VLM_API_KEY", "")
    with pytest.raises(RuntimeError, match="VLM_API_KEY"):
        QwenVlDetector()


# --------------------------------------------------------------------------
# 检测器哑了 → 安全层必须**说出声**
# --------------------------------------------------------------------------


class _BoomDetector:
    async def detect(self, frame):
        raise DetectorError("HTTP 429：quota exceeded")

    async def health(self) -> bool:
        return False


def _safety_layer(detector_obj):
    """造一个安全层，再把它的检测器换成桩。

    ★ 用 `__new__` 绕开注册表查名字那一步：这里要验的是**层怎么处理检测器
      失败**，不是「注册表能不能找到 mock」—— 那件事单独跑本文件时也不成立
      （`load_all()` 还没跑）。不污染全局注册表，也不依赖用例顺序。
    """
    layer = SafetyLayer.__new__(SafetyLayer)
    layer.detector = detector_obj
    layer.walk_speed_mps = None
    return layer


def test_detector_failure_becomes_an_audible_degradation():
    """★ 这条是本文件最要紧的用例。

    「这一拍没看见东西」和「这一拍根本没在看」必须能被区分开 ——
    用户听不出区别，把沉默当成安全，是这类系统最危险的失效模式。
    """
    layer = _safety_layer(_BoomDetector())
    frame = Frame(frame_id="f1", ts=123, image_ref=None, source="safety")
    anns = run(layer.handle(frame))

    assert len(anns) == 1, "检测器失败必须产生一条播报，不能是空的"
    ann = anns[0]
    assert ann.source == SOURCE_SYSTEM, "降级通告走 system 通道"
    assert ann.priority == 2, "要压过普通播报，用户得听得见"
    assert "不可用" in ann.text
    # 同一类失败用同一个去重键 —— 否则每秒一拍会刷成满屏
    assert ann.dedup_key == "system:degraded:detector_error:DetectorError"
    # 排查线索留在 detail 里，不塞进播报文本
    assert ann.detail["reason"] == "detector_error:DetectorError"
    assert "429" in ann.detail["message"]


def test_nothing_in_the_way_stays_silent():
    """对照：检测器**正常工作**而且什么都没看见时，是安静的，不是降级。

    两条路必须分得开 —— 这条用例就是用来钉住「别把安静当成哑巴」的反面。
    """

    class _QuietDetector:
        async def detect(self, frame):
            return []

        async def health(self) -> bool:
            return True

    layer = _safety_layer(_QuietDetector())
    frame = Frame(frame_id="f1", ts=1, source="safety")
    assert run(layer.handle(frame)) == []
