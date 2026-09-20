"""
契约样例数据 —— 每个 detail 形状一份。

用途：
  1. 组员不装任何依赖就能看到契约长什么样
  2. mock 实现直接返回它，无 API key 也能跑通全链路
  3. 前端拿它造 UI

改 app/contracts.py 后要同步改这里，并跑 pytest tests/test_contracts.py。
"""

from app.contracts import (
    FALL_SUSPECTED,
    HAPTIC_DOUBLE,
    HAPTIC_LONG,
    PRIORITY_BACKGROUND,
    PRIORITY_CRITICAL,
    PRIORITY_IMPORTANT,
    RISK_DANGER,
    RISK_WARNING,
    SOURCE_EMERGENCY,
    SOURCE_PERCEPTION,
    SOURCE_SAFETY,
    SOURCE_SYSTEM,
    Announcement,
    Frame,
    emergency_detail,
    make_dedup_key,
    route_detail,
    safety_detail,
    system_detail,
    vision_detail,
)

NOW = 1758326400000  # 2025-09-20 00:00:00 UTC，固定值方便测试比对


# --------------------------------------------------------------------------
# 输入
# --------------------------------------------------------------------------

SAMPLE_FRAME = Frame(
    frame_id="f0001",
    ts=NOW,
    image_ref="data/frames/01_traffic_light.png",
    source=SOURCE_PERCEPTION,
)


# --------------------------------------------------------------------------
# 第一层：环境感知
# --------------------------------------------------------------------------

PERCEPTION = Announcement(
    text="前方约 3 米有一个红绿灯，现在是红灯",
    ttl_ms=5000,
    dedup_key="vision:scene:traffic_light",
    source=SOURCE_PERCEPTION,
    priority=PRIORITY_BACKGROUND,
    id="ann_perception_001",
    ts=NOW,
    frame_id="f0001",
    detail=vision_detail(
        "前方约 3 米有一个红绿灯，现在是红灯",
        scene_key="traffic_light",
        scene_conf=0.90,
        ocr_results=[
            {"text": "电梯", "bbox": [0.62, 0.31, 0.11, 0.08], "confidence": 0.93,
             "category": "elevator_button"},
        ],
        objects=[
            {"label": "人", "confidence": 0.95, "position": "left",
             "bbox": [0.05, 0.40, 0.15, 0.45],
             "distance_m": 1.8, "distance_sigma_m": 0.6, "track_id": 7,
             "is_known": False, "face_id": None},
            {"label": "红绿灯", "confidence": 0.88, "position": "center",
             "bbox": [0.44, 0.18, 0.06, 0.14],
             "distance_m": 3.0, "distance_sigma_m": 1.1, "track_id": None,
             "is_known": False, "face_id": None},
        ],
    ),
)


# --------------------------------------------------------------------------
# 第二层：安全预警
# --------------------------------------------------------------------------

SAFETY = Announcement(
    text="前方约 2 米有向下的台阶，注意抬脚",
    ttl_ms=6000,
    dedup_key=make_dedup_key("obstacle", "step_down", "center", RISK_DANGER, 2.0),
    source=SOURCE_SAFETY,
    priority=PRIORITY_CRITICAL,
    id="ann_safety_001",
    ts=NOW,
    interrupt=True,
    haptic=HAPTIC_DOUBLE,
    frame_id="f0002",
    detail=safety_detail(
        RISK_DANGER,
        [
            {"type": "step_down", "position": "center",
             "distance_m": 2.0, "distance_sigma_m": 0.7,
             "risk": RISK_DANGER, "confidence": 0.86, "track_id": 3},
        ],
    ),
)


# --------------------------------------------------------------------------
# 第三层：智能导航（stub）
# --------------------------------------------------------------------------

NAVIGATION = Announcement(
    text="沿人行道直行 200 米，然后右转进入建国路",
    ttl_ms=15000,
    dedup_key="nav:step:0",
    source="navigation",
    priority=PRIORITY_IMPORTANT,
    id="ann_nav_001",
    ts=NOW,
    detail=route_detail(
        "r1",
        [{"instruction": "沿人行道直行", "distance_m": 200.0, "maneuver": "straight"}],
        total_distance_m=820.0,
        total_duration_s=600.0,
        warnings=["路线已避开天桥和地下通道"],
    ),
)


# --------------------------------------------------------------------------
# 第四层：紧急求助（stub）
# --------------------------------------------------------------------------

FALL_SUSPECTED_ANN = Announcement(
    text="检测到您可能跌倒了。需要帮助吗？说「我没事」，或按任意键取消。",
    ttl_ms=15000,
    dedup_key="fall:evt_001",
    source=SOURCE_EMERGENCY,
    priority=PRIORITY_CRITICAL,
    id="ann_fall_001",
    ts=NOW,
    interrupt=True,
    haptic=HAPTIC_LONG,
    detail=emergency_detail(
        FALL_SUSPECTED,
        "evt_001",
        confirm_deadline_ts=NOW + 15000,
        contact_scope="family",
        location={"lat": 39.9042, "lng": 116.4074, "accuracy_m": 8.0, "ts": NOW},
    ),
)


# --------------------------------------------------------------------------
# 系统：降级通告
# --------------------------------------------------------------------------

DEGRADED = Announcement(
    text="场景描述功能暂时不可用，避障功能正常",
    ttl_ms=10000,
    dedup_key="system:degraded:vlm_timeout",
    source=SOURCE_SYSTEM,
    priority=PRIORITY_IMPORTANT,
    id="ann_system_001",
    ts=NOW,
    haptic="none",
    detail=system_detail("vlm_timeout", retry_in_ms=30000),
)


# --------------------------------------------------------------------------
# 按 source 索引，供 mock 实现和测试取用
# --------------------------------------------------------------------------

BY_SOURCE = {
    SOURCE_PERCEPTION: PERCEPTION,
    SOURCE_SAFETY: SAFETY,
    "navigation": NAVIGATION,
    SOURCE_EMERGENCY: FALL_SUSPECTED_ANN,
    SOURCE_SYSTEM: DEGRADED,
}


def for_source(source: str) -> Announcement:
    """mock 实现用这个：给什么 source 就返回对应样例。"""
    return BY_SOURCE.get(source, DEGRADED)


# --------------------------------------------------------------------------
# 按帧轮换的场景
#
# 模拟视频里画面在变，否则每一帧产出完全相同的文本，会全被仲裁器
# 当成重复丢掉，演示时看不出东西。真实实现里这里换成模型输出。
# --------------------------------------------------------------------------

PERCEPTION_SCENES = [
    PERCEPTION,  # 前方红绿灯
    Announcement(
        text="左侧一米左右有人经过",
        ttl_ms=4000,
        dedup_key="vision:scene:person_left",
        source=SOURCE_PERCEPTION,
        priority=PRIORITY_BACKGROUND,
        id="ann_perception_002",
        ts=NOW,
        detail=vision_detail(
            "左侧一米左右有人经过",
            scene_key="person_left",
            scene_conf=0.86,
            objects=[
                {"label": "人", "confidence": 0.94, "position": "left",
                 "bbox": [0.07, 0.45, 0.13, 0.42],
                 "distance_m": 1.1, "distance_sigma_m": 0.4, "track_id": 12,
                 "is_known": False, "face_id": None},
            ],
        ),
    ),
    Announcement(
        text="前方人行道畅通",
        ttl_ms=4000,
        dedup_key="vision:scene:clear",
        source=SOURCE_PERCEPTION,
        priority=PRIORITY_BACKGROUND,
        id="ann_perception_003",
        ts=NOW,
        detail=vision_detail("前方人行道畅通", scene_key="clear", scene_conf=0.72),
    ),
]

SAFETY_SCENES = [
    [],  # 无障碍 —— 这一帧不产生任何播报
    [SAFETY],  # 前方台阶
    [
        Announcement(
            text="右前方约 4 米有自行车过来，靠左走",
            ttl_ms=6000,
            dedup_key=make_dedup_key("obstacle", "bicycle", "right", RISK_WARNING, 4.0),
            source=SOURCE_SAFETY,
            priority=PRIORITY_IMPORTANT,
            id="ann_safety_002",
            ts=NOW,
            haptic="short",
            detail=safety_detail(
                RISK_WARNING,
                [
                    {"type": "bicycle", "position": "right",
                     "distance_m": 4.0, "distance_sigma_m": 1.6,
                     "risk": RISK_WARNING, "confidence": 0.79, "track_id": 5},
                ],
            ),
        )
    ],
]


def perception_for_index(i: int) -> Announcement:
    return PERCEPTION_SCENES[i % len(PERCEPTION_SCENES)]


def safety_for_index(i: int) -> list[Announcement]:
    return SAFETY_SCENES[i % len(SAFETY_SCENES)]
