"""播报措辞规则。"""

from app.contracts import RISK_DANGER, RISK_WARNING
from app.core.rules import phrasing


def obs(t="step_down", d=2.0, risk=RISK_DANGER, conf=0.9):
    return {"type": t, "distance_m": d, "risk": risk, "confidence": conf}


# --------------------------------------------------------------------------
# 时长估算
# --------------------------------------------------------------------------


def test_speech_estimate_is_chars_times_rate():
    assert phrasing.speech_est_ms("台阶") == 2 * phrasing.MS_PER_CHAR


def test_ensure_ttl_extends_when_too_short():
    """★ 这是评审里发现的「自我矛盾信封」问题。

    中文 TTS 约 250ms/字，一条 14 字预警要 3.5 秒。ttl 若设成 2000ms，
    消息在播完之前就过期了，用户听到半句被打断，两句话都没听懂。
    """
    text = "前方几米有向下的台阶，注意脚下"  # 15 字
    ttl, ok = phrasing.ensure_ttl(2000, text)
    assert ok is False
    assert ttl >= phrasing.speech_est_ms(text)


def test_ensure_ttl_passes_when_long_enough():
    ttl, ok = phrasing.ensure_ttl(9999, "台阶")
    assert ok is True
    assert ttl == 9999


# --------------------------------------------------------------------------
# 距离措辞：★ 不播数字
# --------------------------------------------------------------------------


def test_obstacle_phrase_never_contains_a_number():
    """对障碍物播报具体距离是有害的。

    单目深度估计误差 30–50%，系统说「2 米」实际 0.8 米时，
    用户会按 2 米的心理预期迈步 —— 误差往低估的方向直接致害。
    """
    for d in (0.3, 1.0, 2.0, 4.0, 8.0):
        text = phrasing.obstacle_phrase("step_down", d)
        assert not any(ch.isdigit() for ch in text), f"{d}m 的措辞里出现了数字: {text}"


def test_distance_words_cover_all_buckets():
    from app.contracts import (
        DISTANCE_CLOSE, DISTANCE_FAR, DISTANCE_MEDIUM, DISTANCE_NEAR, DISTANCE_TOUCH,
    )
    for b in (DISTANCE_TOUCH, DISTANCE_NEAR, DISTANCE_CLOSE, DISTANCE_MEDIUM, DISTANCE_FAR):
        assert b in phrasing.DISTANCE_WORDS


# --------------------------------------------------------------------------
# 置信度对冲
# --------------------------------------------------------------------------


def test_low_confidence_gets_hedged():
    """VLM 会幻觉。低置信时措辞必须传达不确定性。"""
    assert phrasing.hedge("前方有车", 0.4).startswith("可能")


def test_high_confidence_is_not_hedged():
    assert phrasing.hedge("前方有车", 0.9) == "前方有车"


def test_obstacle_phrase_hedges_low_confidence():
    assert "可能" in phrasing.obstacle_phrase("vehicle", 2.0, confidence=0.3)


# --------------------------------------------------------------------------
# 最短形式 —— 近距离说不完完整句子
# --------------------------------------------------------------------------


def test_terse_is_shorter_than_full():
    terse = phrasing.terse_text([obs()])
    full = phrasing.safety_text([obs()])
    assert len(terse) < len(full)


def test_fit_text_full_when_window_is_roomy():
    text, complete = phrasing.fit_text([obs(d=8.0)], window_ms=5000)
    assert complete is True
    assert "注意脚下" in text


def test_fit_text_falls_back_to_terse_when_window_is_tight():
    """★ 用户以 1.1 m/s 走，2 米外的台阶只有 1.8 秒，
    而完整句子要 3.7 秒 —— 物理上说不完，必须用最短形式。"""
    text, complete = phrasing.fit_text([obs(d=2.0)], window_ms=1800)
    assert complete is False
    assert phrasing.speech_est_ms(text) <= 1800


def test_fit_text_still_returns_something_when_impossible():
    """连最短版都说不完（障碍物已贴脸）—— 报总比不报强。"""
    text, complete = phrasing.fit_text([obs(d=0.3)], window_ms=300)
    assert text != ""
    assert complete is False


# --------------------------------------------------------------------------
# 多障碍物排序
# --------------------------------------------------------------------------


def test_most_urgent_obstacle_comes_first():
    obstacles = [
        obs(t="person", d=1.0, risk=RISK_WARNING),
        obs(t="step_down", d=2.0, risk=RISK_DANGER),
    ]
    text = phrasing.safety_text(obstacles)
    assert text.index("台阶") < text.index("人")


def test_safety_text_caps_the_number_of_items():
    """一次说五件事等于一件都没说"""
    obstacles = [obs(t=f"t{i}", d=1.0 + i) for i in range(5)]
    assert phrasing.safety_text(obstacles, max_items=2).count("；") <= 1


def test_empty_input_produces_empty_text():
    assert phrasing.safety_text([]) == ""
    assert phrasing.terse_text([]) == ""


# --------------------------------------------------------------------------
# 震动
# --------------------------------------------------------------------------


def test_danger_gets_stronger_haptic_than_warning():
    assert phrasing.haptic_for(RISK_DANGER) == "double"
    assert phrasing.haptic_for(RISK_WARNING) == "short"
