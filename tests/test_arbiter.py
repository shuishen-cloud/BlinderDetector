"""闸门测试 —— 时序全部显式构造，不依赖真实时钟。

闸门只回答「这条值不值得发给端侧」：去重 + 废数据。
排序 / 打断 / 积压 / 到期不补播都在端侧，不在这里测。
"""

from app.contracts import (
    PRIORITY_BACKGROUND,
    PRIORITY_CRITICAL,
    PRIORITY_IMPORTANT,
    PRIORITY_NORMAL,
    RISK_DANGER,
    RISK_INFO,
    RISK_WARNING,
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
# 放行
# --------------------------------------------------------------------------


def test_new_key_is_sent():
    arb = Arbiter()
    assert arb.submit(ann(dedup_key="k1", aid="a1"), T0) == "sent"
    assert [a.id for a in arb.sent] == ["a1"]


def test_priority_does_not_gate_anything():
    """★ 闸门不看优先级 —— 判断「该不该打断」是端侧的事。

    服务端把高优先级和低优先级都发出去，端侧按 priority 决定怎么播。
    """
    arb = Arbiter()
    assert arb.submit(ann(PRIORITY_BACKGROUND, "k1", aid="bg"), T0) == "sent"
    assert arb.submit(ann(PRIORITY_CRITICAL, "k2", aid="crit"), T0 + 100) == "sent"
    assert {a.id for a in arb.sent} == {"bg", "crit"}


# --------------------------------------------------------------------------
# 去重
# --------------------------------------------------------------------------


def test_duplicate_within_window_is_dropped():
    arb = Arbiter(dedup_window_ms=3000)
    arb.submit(ann(PRIORITY_IMPORTANT, "same", aid="a1"), T0)
    result = arb.submit(ann(PRIORITY_IMPORTANT, "same", aid="a2"), T0 + 500)

    assert result == "dropped:duplicate"
    assert "a2" not in arb.sent_ids
    assert [a.id for a in arb.sent] == ["a1"]


def test_same_key_after_window_is_sent_again():
    arb = Arbiter(dedup_window_ms=3000)
    arb.submit(ann(PRIORITY_IMPORTANT, "same", aid="a1"), T0)
    assert arb.submit(ann(PRIORITY_IMPORTANT, "same", aid="a2"), T0 + 4000) == "sent"
    assert "a2" in arb.sent_ids


def test_escalation_breaks_dedup():
    """★ 最关键的一条安全规则。

    台阶从 INFO 升级到 DANGER，绝不能被当成「同一个台阶的重复」吞掉 ——
    被吞掉的恰恰是最危险的那一条。

    靠的是 dedup_key 里含 risk 和距离档位（见 contracts.make_dedup_key）。
    """
    arb = Arbiter()
    info = ann(PRIORITY_NORMAL, make_dedup_key("obstacle", "step_down", "center", RISK_INFO, 3.2), aid="info")
    warn = ann(PRIORITY_IMPORTANT, make_dedup_key("obstacle", "step_down", "center", RISK_WARNING, 1.8), aid="warn")
    danger = ann(PRIORITY_CRITICAL, make_dedup_key("obstacle", "step_down", "center", RISK_DANGER, 0.7), aid="danger")

    arb.submit(info, T0)
    arb.submit(warn, T0 + 800)
    arb.submit(danger, T0 + 1600)

    assert "danger" in arb.sent_ids, "危险升级被去重吞掉了"


def test_dedup_survives_a_backwards_clock():
    """★ 时钟倒流不该把某个 key 永久锁死。

    /v1/emergency/tick 会传 now_ms=9999999999999（公元 2286 年）。
    若去重用 (now - last) 判断，这个未来时刻之后所有真实时间戳的差值都是负数、
    恒小于窗口 —— 该 key 会被永久判为重复。

    障碍物的 dedup_key 不带事件号（同一种台阶永远同一个 key），
    一旦锁死就再也播不出来，所以必须取绝对值。
    """
    arb = Arbiter(dedup_window_ms=3000)
    arb.submit(ann(dedup_key="obstacle:step", aid="a1"), 9_999_999_999_999)

    # 回到真实量级的时间戳，同 key 必须还能发出去
    assert arb.submit(ann(dedup_key="obstacle:step", aid="a2"), T0) == "sent"


# --------------------------------------------------------------------------
# 废数据
# --------------------------------------------------------------------------


def test_zero_ttl_is_dropped_immediately():
    arb = Arbiter()
    assert arb.submit(ann(ttl_ms=0, aid="dead"), T0) == "dropped:expired"
    assert arb.sent == []


# --------------------------------------------------------------------------
# 观测
# --------------------------------------------------------------------------


def test_drop_reason_lookup():
    arb = Arbiter(dedup_window_ms=3000)
    arb.submit(ann(dedup_key="k", aid="a1"), T0)
    dup = ann(dedup_key="k", aid="a2")
    arb.submit(dup, T0 + 100)
    assert arb.drop_reason("a2") == "duplicate"
    assert arb.drop_reason("a1") is None
