#!/usr/bin/env python3
"""跌倒检测全流程演示 —— 两条路径对照。

    python scripts/demo_fall.py

路径 A：真跌倒，用户没响应 -> 二次确认超时 -> 才通知家属 -> 升级网格员
路径 B：真跌倒，用户说「我没事」-> 立即取消 -> 绝不外呼

不需要起服务器，也不需要 API key。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.rules.fall import FallMachine, FallSignal  # noqa: E402

T0 = 1_758_326_400_000
CONFIRM_MS = 15_000


def banner(text: str) -> None:
    print(f"\n\033[1m{'=' * 66}\n{text}\n{'=' * 66}\033[0m")


def show(anns, indent="  ") -> None:
    for a in anns:
        state = a.detail.get("state", "?")
        print(f"{indent}\033[32m▶\033[0m [{state}] {a.text}")
        if "notified" in a.detail:
            print(f"{indent}    已通知: {'、'.join(a.detail['notified'])}")


def fall_signal(**kw) -> FallSignal:
    base = dict(peak_g=3.2, free_fall_ms=120, posture="lying",
                post_impact_still_ms=5000, movement_class="still")
    base.update(kw)
    return FallSignal(**base)


def main() -> int:
    # ------------------------------------------------------------------
    banner("路径 A：真跌倒，用户没有响应")

    m = FallMachine(confirm_ms=CONFIRM_MS)
    print(f"  t=0s   加速度计检测到自由落体 + 冲击 ({3.2}g)，姿态变为 lying")

    anns = m.on_signal(fall_signal(), "evt_A", T0)
    show(anns)
    print(f"  t=0s   \033[33m状态: suspected —— 只询问，绝不外呼\033[0m")

    print(f"\n  t=8s   用户仍未响应……")
    print(f"         {m.tick(T0 + 8_000) and '外呼了！' or '无动作 —— 沉默不等于安全，但也不能提前外呼'}")

    print(f"\n  t=15s  二次确认窗口归零")
    print(f"  t=15s  \033[31m状态: confirmed —— 现在才通知\033[0m")
    show(m.tick(T0 + 15_001))

    # ------------------------------------------------------------------
    banner("路径 B：真跌倒，用户说「我没事」")

    m2 = FallMachine(confirm_ms=CONFIRM_MS)
    anns = m2.on_signal(fall_signal(), "evt_B", T0)
    show(anns)

    print(f"\n  t=4.2s 用户摇一摇手机取消")
    ann = m2.cancel("evt_B", T0 + 4_200, method="shake")
    show([ann])

    print(f"\n  t=15s  窗口归零，但用户已显式取消")
    late = m2.tick(T0 + 15_001)
    print(f"         外呼: {'有（错！）' if late else '\033[32m无 —— 正确\033[0m'}")

    # ------------------------------------------------------------------
    banner("对照：步行节律（最常见假阳性）")

    m3 = FallMachine()
    anns = m3.on_signal(fall_signal(movement_class="walking"), "evt_C", T0)
    print(f"  手机在手里被甩了一下，但步态显示人在正常走路")
    print(f"  播报: {anns if anns else '\033[32m无 —— 直接判为误报，连问都不问\033[0m'}")
    print(f"  状态: {m3.state_of('evt_C')}")

    print("\n\033[32m演示结束\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
