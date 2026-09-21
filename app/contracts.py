"""
灵眸伴途 —— 接口契约单一真源

全系统只有两个数据结构：
    Frame         所有接口的输入
    Announcement  所有接口的输出

四层的差异只体现在 Announcement.source 和 Announcement.detail 的形状上。

★ 改这个文件前请先读 工作管理.md §三「改契约的流程」。
★ 本文件只用标准库，不依赖任何第三方包（Termux 装不了 pydantic）。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# 常量（用字符串而非 Enum，方便直接 JSON 序列化和组员对照）
# --------------------------------------------------------------------------

# Announcement.priority
PRIORITY_BACKGROUND = 0  # 场景描述，随时可被打断
PRIORITY_NORMAL = 1  # 用户主动查询的结果
PRIORITY_IMPORTANT = 2  # 导航指令、一般障碍
PRIORITY_CRITICAL = 3  # 碰撞风险、紧急求助

# Announcement.source
SOURCE_PERCEPTION = "perception"
SOURCE_SAFETY = "safety"
SOURCE_NAVIGATION = "navigation"
SOURCE_EMERGENCY = "emergency"
SOURCE_SYSTEM = "system"  # 降级通告

# Announcement.haptic
HAPTIC_NONE = "none"
HAPTIC_SHORT = "short"
HAPTIC_DOUBLE = "double"
HAPTIC_LONG = "long"

# 方位。★ 参考系是「相机画面」，不是用户身体。
POSITION_LEFT = "left"
POSITION_CENTER = "center"
POSITION_RIGHT = "right"

# 风险等级
RISK_INFO = "info"
RISK_WARNING = "warning"
RISK_DANGER = "danger"

# 跌倒状态机（唯一的状态机，安全关键）
FALL_IDLE = "idle"
FALL_SUSPECTED = "suspected"  # 检测到冲击，语音询问中 —— ★ 此态永不自动外呼
FALL_CONFIRMED = "confirmed"  # 用户未在截止前响应，确认跌倒
FALL_CANCELLED = "cancelled"  # 用户显式取消（★ 端侧本地完成，不依赖网络）

# 求助触发方式。
# ★ 「长按手机侧键 3 秒」在微信小程序和 Web 上都没有对应 API，
#   所以触发方式是**可协商的集合**，不是写死的常量。端上有什么能力
#   就上报什么，契约不绑定具体触发通道。冗余触发是安全系统的基本要求 ——
#   单一触发通道等于单点故障。
TRIGGER_HARDWARE_KEY = "hardware_key_long"  # 仅 Android 原生
TRIGGER_SCREEN_LONG = "screen_long_press"  # 全端可用（需屏幕）
TRIGGER_SHAKE = "shake_pattern"  # 需 IMU
TRIGGER_VOICE = "voice_trigger"  # 需麦克风
TRIGGER_FALL = "fall_confirmed"  # 由跌倒状态机产生

TRIGGERS = (
    TRIGGER_HARDWARE_KEY,
    TRIGGER_SCREEN_LONG,
    TRIGGER_SHAKE,
    TRIGGER_VOICE,
    TRIGGER_FALL,
)

# 求助通知范围
SCOPE_FAMILY = "family"
SCOPE_GRID_WORKER = "grid_worker"
SCOPE_BOTH = "both"
SCOPE_NONE = "none"

# 障碍物类型
OBSTACLE_TYPES = (
    "step_up",
    "step_down",
    "pothole",
    "curb",
    "vehicle",
    "bicycle",
    "pole",
    "person",
    "other",
)

# 距离档位 —— 用于去重键，也建议用于播报措辞
DISTANCE_TOUCH = "touch"  # <0.5m  伸手可及
DISTANCE_NEAR = "near"  # 0.5-1.5m
DISTANCE_CLOSE = "close"  # 1.5-3m
DISTANCE_MEDIUM = "medium"  # 3-6m
DISTANCE_FAR = "far"  # >6m


# --------------------------------------------------------------------------
# 两个信封
# --------------------------------------------------------------------------


@dataclass
class Frame:
    """统一输入。四层都消费它，差异靠 source 区分。"""

    frame_id: str = field(metadata={"doc": "帧唯一标识"})
    ts: int = field(metadata={"doc": "毫秒时间戳"})
    image_ref: str | None = field(default=None, metadata={"doc": "图片路径或视频帧引用"})
    source: str = field(default=SOURCE_PERCEPTION,
                        metadata={"doc": "哪一层在消费这个输入"})
    extra: dict[str, Any] = field(default_factory=dict,
                                  metadata={"doc": "上下文；图片序列会填 index/total"})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Announcement:
    """统一输出。端侧唯一的消费对象 —— 只播 text + 执行震动，不懂业务。"""

    text: str = field(metadata={"doc": "★ 端侧唯一消费字段"})
    ttl_ms: int = field(metadata={"doc": "★ 从端「收到」起算，过期即丢，绝不补播"})
    dedup_key: str = field(metadata={"doc": "去重键，★ 构造请用 make_dedup_key()"})
    source: str = field(default=SOURCE_SYSTEM, metadata={"doc": "哪个层产生的"})
    priority: int = field(default=PRIORITY_NORMAL, metadata={"doc": "0-3，越高越能打断别人"})
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12],
                    metadata={"doc": "播报唯一标识"})
    ts: int = field(default_factory=lambda: int(time.time() * 1000),
                    metadata={"doc": "毫秒时间戳"})
    interrupt: bool = field(default=False, metadata={"doc": "这条是否允许打断当前播报"})
    haptic: str = field(default=HAPTIC_NONE, metadata={"doc": "震动模式"})
    frame_id: str | None = field(default=None, metadata={"doc": "关联的输入帧"})
    detail: dict[str, Any] = field(default_factory=dict,
                                   metadata={"doc": "结构化细节，形状见 §3"})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# --------------------------------------------------------------------------
# 辅助构造函数
# --------------------------------------------------------------------------


def distance_bucket(distance_m: float) -> str:
    """把米数映射成粗档。用于去重键，也建议用于播报措辞。"""
    if distance_m < 0.5:
        return DISTANCE_TOUCH
    if distance_m < 1.5:
        return DISTANCE_NEAR
    if distance_m < 3.0:
        return DISTANCE_CLOSE
    if distance_m < 6.0:
        return DISTANCE_MEDIUM
    return DISTANCE_FAR


def make_dedup_key(
    kind: str,
    obstacle_type: str,
    position: str,
    risk: str,
    distance_m: float,
) -> str:
    """构造障碍物去重键。

    ★ 必须包含 risk 和距离档位，不能只用障碍物类型。

    否则下面这个序列会被当成「同一个台阶的重复」全部吞掉：

        台阶(info,   3.2m)  -> 播报
        台阶(warning,1.8m)  -> 被去重丢弃
        台阶(danger, 0.7m)  -> 被去重丢弃   ← 用户撞上去

    被吞掉的恰恰是最危险的那一条，因为它是「同一个物体的重复」。
    """
    return f"{kind}:{obstacle_type}:{position}:{risk}:{distance_bucket(distance_m)}"


def vision_dedup_key(detail: dict[str, Any]) -> str:
    """第一层的去重键 —— 按场景分类，同一个场景不重播，场景变了才播。"""
    return f"vision:scene:{detail.get('scene_key', 'default')}"


def route_dedup_key(route_id: str, kind: str, key: str | int) -> str:
    """第三层的去重键 —— 集中在这里，别在各处手拼 f-string。

    `kind` ∈ `step`（分步指令）/ `warn`（无障碍警告）/ `notice`（降级、无路线）。

    为什么要有这个函数：第三层的键格式原本散落在 `rules/route.py` 和
    `mock/fixtures.py` 里各写一遍，而它们已经漂了 —— fixture 写的是
    `nav:step:0`，真实产出是 `nav:{route_id}:step:{i}`。
    格式一旦集中，就不会再有第二份「看起来对」的版本。
    """
    return f"nav:{route_id}:{kind}:{key}"


def announcement(source: str, priority: int, text: str, ttl_ms: int, dedup_key: str, **kw) -> Announcement:
    """按 source 自动配好震动模式的便捷构造器。"""
    haptic = kw.pop("haptic", None)
    if haptic is None:
        if priority >= PRIORITY_CRITICAL:
            haptic = HAPTIC_DOUBLE
        elif priority >= PRIORITY_IMPORTANT:
            haptic = HAPTIC_SHORT
        else:
            haptic = HAPTIC_NONE
    return Announcement(
        source=source, priority=priority, text=text,
        ttl_ms=ttl_ms, dedup_key=dedup_key, haptic=haptic, **kw,
    )


# --------------------------------------------------------------------------
# detail 各层的构造器 —— 保证四层产出的形状一致
# --------------------------------------------------------------------------


def vision_detail(
    scene_description: str,
    *,
    scene_key: str = "default",
    scene_conf: float = 1.0,
    ocr_results: list[dict] | None = None,
    objects: list[dict] | None = None,
    degraded: bool = False,
) -> dict[str, Any]:
    """第一层 detail。

    scene_key 是场景的粗分类（traffic_light / person_left / clear ...），
    用来构造去重键 —— 同一个场景不用每帧重播，场景变了才播。

    objects[] 每条：

        {"label": str, "confidence": float, "position": "left|center|right",
         "bbox": [x, y, w, h],            # 归一化 0-1，原点左上
         "distance_m": float | None,
         "distance_sigma_m": float | None, # ★ 单目深度误差范围
         "track_id": int | None,
         "is_known": bool,                 # 本期恒 false
         "face_id": str | None}            # 占位
    """
    return {
        "kind": "vision",
        "scene_key": scene_key,
        "scene_description": scene_description,
        "scene_conf": scene_conf,
        "ocr_results": ocr_results or [],
        "objects": objects or [],
        "degraded": degraded,
    }


def safety_detail(
    risk: str,
    obstacles: list[dict],
    *,
    degraded: bool = False,
) -> dict[str, Any]:
    """第二层 detail。obstacles[] 每条：

        {"type": str, "position": str,
         "distance_m": float, "distance_sigma_m": float,  # ★ 必须带误差
         "risk": str, "confidence": float, "track_id": int | None}
    """
    return {"kind": "safety", "risk": risk, "obstacles": obstacles, "degraded": degraded}


def route_detail(
    route_id: str,
    steps: list[dict],
    total_distance_m: float,
    total_duration_s: float,
    *,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """第三层 detail。steps[] 每条：{"instruction", "distance_m", "maneuver"}"""
    return {
        "kind": "route",
        "route_id": route_id,
        "steps": steps,
        "total_distance_m": total_distance_m,
        "total_duration_s": total_duration_s,
        "warnings": warnings or [],
    }


def emergency_detail(
    state: str,
    event_id: str,
    *,
    confirm_deadline_ts: int | None = None,
    contact_scope: str = "family",
    location: dict | None = None,
) -> dict[str, Any]:
    """第四层 detail。

    state 走跌倒状态机：idle -> suspected -> confirmed | cancelled。
    idempotency_key 用 event_id，防止重试导致重复呼叫家属。
    """
    d: dict[str, Any] = {
        "kind": "emergency",
        "state": state,
        "event_id": event_id,
        "idempotency_key": event_id,
        "contact_scope": contact_scope,
    }
    if confirm_deadline_ts is not None:
        d["confirm_deadline_ts"] = confirm_deadline_ts
    if location is not None:
        d["location"] = location
    return d


def system_detail(reason: str, **kw) -> dict[str, Any]:
    """降级通告。

    ★ 沉默不能有歧义 —— 网络断、VLM 超时、读不到帧都必须显式通告。
    用户若不知道系统哑了，会把「没出声」理解为「环境安全」。
    """
    return {"kind": "system", "reason": reason, **kw}
