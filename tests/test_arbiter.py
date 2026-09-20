"""仲裁器测试 —— 时序全部显式构造，不依赖真实时钟。"""

from app.contracts import (
    PRIORITY_BACKGROUND,
    PRIORITY_CRITICAL,
    PRIORITY_IMPORTANT,
    PRIORITY_NORMAL,
    RISK_DANGER,
    RISK_INFO,
    RISK_WARNING,
    SOURCE_PERCEPTION,
    SOURCE_SAFETY,
    Announcement,
    make_dedup_key,
)
from app.core.arbiter import Arbiter

T0 = 1_000_000


def ann(priority=PRIORITY_NORMAL, dedup_key="k", ttl_ms=5000, aid="a", text="t"):
    return Announcement(id=aid, ts=T0, source=SOURCE_SAFETY, priority=priority,
                        text=text, ttl_ms=ttl_ms, dedup_key=dedup_key)


# --------------------------------------------------------------------------
# 规则 3：优先级打断
# --------------------------------------------------------------------------


def test_critical_interrupts_background_scene_description():
    """用户正在听场景描述，检测到台阶 -> 必须立刻打断"""
    arb = Arbiter()
    scene = ann(PRIORITY_BACKGROUND, "vision:scene", aid="scene")
    arb.submit(scene, T0)

    step = ann(PRIORITY_CRITICAL, "obstacle:step:center:danger:close", aid="step")
    result = arb.submit(step, T0 + 1000)  # 已超过 min_play_ms

    assert result == "spoken"
    assert arb.current().id == "step"
    assert "interrupted" in [r for _, r in arb.dropped]


def test_lower_priority_cannot_interrupt():
    arb = Arbiter()
    arb.submit(ann(PRIORITY_CRITICAL, "k1", aid="critical"), T0)
    result = arb.submit(ann(PRIORITY_BACKGROUND, "k2", aid="bg"), T0 + 1000)
    assert result == "queued"
    assert arb.current().id == "critical"


def test_min_play_ms_blocks_barge_in():
    """刚开口就被打断会让用户听到碎片，min_play_ms 期间不允许抢占"""
    arb = Arbiter(min_play_ms=800)
    arb.submit(ann(PRIORITY_BACKGROUND, "k1", aid="bg"), T0)
    result = arb.submit(ann(PRIORITY_CRITICAL, "k2", aid="critical"), T0 + 200)
    assert result == "queued", "min_play_ms 内不该被抢占"


# --------------------------------------------------------------------------
# 规则 2：去重 + ★ 升级必须突破去重
# --------------------------------------------------------------------------


def test_duplicate_within_window_is_dropped():
    arb = Arbiter(dedup_window_ms=3000)
    arb.submit(ann(PRIORITY_IMPORTANT, "same", aid="a1"), T0)
    result = arb.submit(ann(PRIORITY_IMPORTANT, "same", aid="a2"), T0 + 500)

    assert result == "dropped:duplicate"
    assert ("a2" not in arb.spoken_ids)
    assert [a.id for a in arb.spoken] == ["a1"]


def test_escalation_breaks_dedup():
    """★ 最关键的一条。

    台阶从 INFO 升级到 DANGER，绝不能被当成「同一个台阶的重复」吞掉 ——
    被吞掉的恰恰是最危险的那一条。
    """
    arb = Arbiter()
    info = ann(PRIORITY_NORMAL, make_dedup_key("obstacle", "step_down", "center", RISK_INFO, 3.2), aid="info")
    warn = ann(PRIORITY_IMPORTANT, make_dedup_key("obstacle", "step_down", "center", RISK_WARNING, 1.8), aid="warn")
    danger = ann(PRIORITY_CRITICAL, make_dedup_key("obstacle", "step_down", "center", RISK_DANGER, 0.7), aid="danger")

    arb.submit(info, T0)
    arb.submit(warn, T0 + 800)
    arb.submit(danger, T0 + 1600)

    assert "danger" in arb.spoken_ids, "危险升级被去重吞掉了"
    assert arb.current().id == "danger"


def test_same_key_after_window_is_spoken_again():
    arb = Arbiter(dedup_window_ms=3000)
    arb.submit(ann(PRIORITY_IMPORTANT, "same", aid="a1"), T0)
    arb.finish_current(T0 + 1000)
    arb.submit(ann(PRIORITY_IMPORTANT, "same", aid="a2"), T0 + 4000)
    assert "a2" in arb.spoken_ids


# --------------------------------------------------------------------------
# 规则 1：TTL
# --------------------------------------------------------------------------


def test_expired_in_queue_is_dropped_not_replayed():
    """迟到 3 秒的「前方 2 米有台阶」比不播更危险"""
    arb = Arbiter()
    arb.submit(ann(PRIORITY_CRITICAL, "k1", ttl_ms=10000, aid="blocker"), T0)
    arb.submit(ann(PRIORITY_NORMAL, "k2", ttl_ms=2000, aid="stale"), T0)

    # 10 秒后才轮到它 —— 早就过期了
    nxt = arb.finish_current(T0 + 10_000)
    assert "stale" not in arb.spoken_ids
    assert ("stale" in [a.id for a, _ in arb.dropped])
    assert nxt is None


def test_zero_ttl_is_dropped_immediately():
    arb = Arbiter()
    assert arb.submit(ann(ttl_ms=0, aid="dead"), T0) == "dropped:expired"


# --------------------------------------------------------------------------
# 规则 4：积压保护
# --------------------------------------------------------------------------


def test_queue_full_drops_lowest_priority():
    arb = Arbiter(max_queue=3)
    arb.submit(ann(PRIORITY_CRITICAL, "blocker", aid="blocker"), T0)
    for i in range(3):
        arb.submit(ann(PRIORITY_NORMAL, f"k{i}", aid=f"n{i}"), T0)
    # 队列已满，来一条更高优先级的
    result = arb.submit(ann(PRIORITY_IMPORTANT, "high", aid="high"), T0)
    assert result == "queued"
    # 有一条低优先级被挤掉
    assert any(r == "queue_full" for _, r in arb.dropped)


# --------------------------------------------------------------------------
# 时序：播完接上
# --------------------------------------------------------------------------


def test_finish_current_pops_next_by_priority():
    arb = Arbiter()
    arb.submit(ann(PRIORITY_CRITICAL, "k0", aid="blocker"), T0)
    arb.submit(ann(PRIORITY_NORMAL, "k1", aid="low"), T0)
    arb.submit(ann(PRIORITY_IMPORTANT, "k2", aid="high"), T0)

    nxt = arb.finish_current(T0 + 2000)
    assert nxt.id == "high", "应先播优先级更高的"
