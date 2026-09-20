"""
播报文本生成 —— 第一层和第二层共用。

三条硬规则：

1. **对障碍物不播具体距离数字。** 单目深度估计误差 30–50%，
   「前方 2.3 米」这种虚假精度会让用户按错误预期迈步，误差往低估的
   方向直接致害。改用粗档：「就在脚前 / 很近 / 几米外 / 远处」。
   第三层的 GPS 距离不受此限 —— 误差量级完全不同，可以播数字。

2. **置信度低要说「可能」。** VLM 幻觉出「前方有车」是危险的，
   低置信时措辞必须传达不确定性。

3. **ttl_ms 必须 ≥ 播报耗时。** 中文 TTS 约 250ms/字，一条 14 字的预警
   要 3.5 秒。若 ttl 设成 2000ms，消息在播完之前就过期了，用户听到
   半句被打断，两句话都没听懂。
"""

from __future__ import annotations

from app.contracts import (
    DISTANCE_CLOSE,
    DISTANCE_FAR,
    DISTANCE_MEDIUM,
    DISTANCE_NEAR,
    DISTANCE_TOUCH,
    HAPTIC_DOUBLE,
    HAPTIC_LONG,
    HAPTIC_NONE,
    HAPTIC_SHORT,
    RISK_DANGER,
    RISK_WARNING,
    distance_bucket,
)

#: 中文 TTS 语速（毫秒/字）。用来估算一条播报要占多久。
MS_PER_CHAR = 250

#: 低于这个置信度，措辞要加「可能」之类的对冲词
HEDGE_CONFIDENCE = 0.65


DISTANCE_WORDS: dict[str, str] = {
    DISTANCE_TOUCH: "就在脚前",
    DISTANCE_NEAR: "很近",
    DISTANCE_CLOSE: "前方几米",
    DISTANCE_MEDIUM: "前方不远处",
    DISTANCE_FAR: "前方远处",
}

OBSTACLE_WORDS: dict[str, str] = {
    "step_up": "有向上的台阶",
    "step_down": "有向下的台阶",
    "pothole": "有坑洼",
    "curb": "有路缘",
    "vehicle": "有车",
    "bicycle": "有自行车",
    "pole": "有立柱",
    "person": "有人",
    "glass_door": "可能是玻璃门",
    "other": "有障碍物",
}

#: 最短形式的名词。近距离预警用这个 —— 见 `safety_text` 的说明。
OBSTACLE_SHORT_WORDS: dict[str, str] = {
    "step_up": "台阶",
    "step_down": "台阶",
    "pothole": "坑",
    "curb": "路缘",
    "vehicle": "车",
    "bicycle": "自行车",
    "pole": "立柱",
    "person": "有人",
    "glass_door": "玻璃门",
    "other": "障碍",
}

#: 不同障碍对的行动建议。没有对应建议时只报不加建议。
OBSTACLE_ACTIONS: dict[str, str] = {
    "step_up": "注意抬脚",
    "step_down": "注意脚下",
    "pothole": "注意绕开",
    "curb": "注意脚下",
    "vehicle": "请停下等待",
    "bicycle": "靠边走",
    "pole": "注意避让",
    "glass_door": "请用手杖确认",
}

HAPTIC_FOR_RISK: dict[str, str] = {
    RISK_DANGER: HAPTIC_DOUBLE,
    RISK_WARNING: HAPTIC_SHORT,
    "info": HAPTIC_NONE,
}


# --------------------------------------------------------------------------
# 时长估算
# --------------------------------------------------------------------------


def speech_est_ms(text: str) -> int:
    """这条文本播出来大约要多久。"""
    return max(1, len(text)) * MS_PER_CHAR


def ensure_ttl(ttl_ms: int, text: str) -> tuple[int, bool]:
    """保证 TTL 不小于播报耗时。

    返回 (修正后的 ttl, 是否原本就够)。调用方在返回 False 时可以
    改用更短的文本，或干脆丢弃这条 —— 但绝不能让它「播到一半过期」。
    """
    need = speech_est_ms(text)
    if ttl_ms >= need:
        return ttl_ms, True
    return need, False


# --------------------------------------------------------------------------
# 文本
# --------------------------------------------------------------------------


def hedge(text: str, confidence: float) -> str:
    """置信度不够时加对冲词。"""
    if confidence >= HEDGE_CONFIDENCE:
        return text
    return f"可能{text}"


def obstacle_phrase(
    obstacle_type: str,
    distance_m: float,
    *,
    confidence: float = 1.0,
    position_word: str = "",
) -> str:
    """单条障碍物的播报短语。例：『前方几米有向下的台阶，注意脚下』"""
    where = DISTANCE_WORDS.get(distance_bucket(distance_m), "前方")
    what = OBSTACLE_WORDS.get(obstacle_type, "有障碍物")
    if position_word:
        what = f"{what}" if position_word in what else f"{position_word}{what}"

    phrase = f"{where}{what}"
    action = OBSTACLE_ACTIONS.get(obstacle_type)
    if action:
        phrase = f"{phrase}，{action}"
    return hedge(phrase, confidence) if confidence < HEDGE_CONFIDENCE else phrase


def safety_text(obstacles: list[dict], *, max_items: int = 2) -> str:
    """一组障碍物 -> 一句话。

    只播最紧急的前 max_items 条 —— 一次说五件事等于一件都没说。
    """
    if not obstacles:
        return ""
    ranked = _rank(obstacles)
    phrases = [
        obstacle_phrase(
            o["type"],
            o.get("distance_m", 0.0),
            confidence=o.get("confidence", 1.0),
        )
        for o in ranked[:max_items]
    ]
    return "；".join(phrases)


def terse_phrase(obstacle: dict) -> str:
    """最短形式：「台阶，注意脚下」。

    近距离预警必须用这个。用户以 1.1 m/s 走，2 米外的台阶只有 1.8 秒，
    而「前方几米有向下的台阶，注意脚下」要 3.7 秒才播得完 ——
    完整句子在物理上就说不完。**宁可不完整，也不能说不完。**
    """
    t = obstacle.get("type", "other")
    what = OBSTACLE_SHORT_WORDS.get(t, "障碍")
    action = OBSTACLE_ACTIONS.get(t)
    return f"{what}，{action}" if action else what


def terse_text(obstacles: list[dict], *, max_items: int = 1) -> str:
    """最短形式的一组障碍物。默认只留最紧急的一条。"""
    if not obstacles:
        return ""
    return "；".join(terse_phrase(o) for o in _rank(obstacles)[:max_items])


def _rank(obstacles: list[dict]) -> list[dict]:
    return sorted(
        obstacles,
        key=lambda o: (_risk_rank(o.get("risk")), -o.get("distance_m", 999.0)),
        reverse=True,
    )


def fit_text(obstacles: list[dict], window_ms: int) -> tuple[str, bool]:
    """在给定时间窗内挑一条播得完的文本。

    返回 (文本, 是否说了完整版)。

    优先级：完整版 -> 最短版 -> 最紧急一条的最短版。
    """
    full = safety_text(obstacles)
    if full and speech_est_ms(full) <= window_ms:
        return full, True

    short = terse_text(obstacles)
    if short and speech_est_ms(short) <= window_ms:
        return short, False

    # 连最短版都说不完 —— 说明障碍物已经贴脸了。
    # 这时候报总比不报强，走最短版并接受它可能播不完。
    return short or full, False


def _risk_rank(risk: str | None) -> int:
    return {RISK_DANGER: 2, RISK_WARNING: 1}.get(risk or "", 0)


def scene_prefix(scene_conf: float) -> str:
    """场景描述的整体置信度前缀。"""
    if scene_conf >= HEDGE_CONFIDENCE:
        return ""
    return "可能"


def haptic_for(risk: str) -> str:
    return HAPTIC_FOR_RISK.get(risk, HAPTIC_NONE)


def fall_confirm_text(seconds: int) -> str:
    """跌倒二次确认的询问语。"""
    return (
        f"检测到您可能跌倒了。需要帮助吗？"
        f"说「我没事」，或按任意键取消。{seconds} 秒后将通知家人。"
    )
