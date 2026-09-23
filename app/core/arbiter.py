"""
播报闸门 —— 四条流水线共用的唯一出口。

★ 它只回答一个问题：**这条值不值得发给端侧？**

    dedup_key 在窗口内出现过 -> 不发（重复）
    ttl_ms <= 0              -> 不发（废数据）
    其余                     -> 发

就这样，没有别的。

★ 为什么没有队列、没有「当前正在播的那条」、没有打断、没有积压保护：

   **喇叭在端侧，服务端观察不到播放状态。** 早期版本在服务端维护
   `_current` + 优先级队列来模拟播放，结果连出两个 bug，根因相同 ——
   服务端在猜自己看不见的东西：

     1) 服务端从来没人调 `finish_current()`，`_current` 一旦被占住就
        永不释放，队列涨到 max_queue 后每条都被 queue_full 丢掉，
        播报流**永久静默**；
     2) `/v1/emergency/tick`（和 smoke.sh）会传 `now_ms=9999999999999`
        （公元 2286 年）来强行触发升级链。这个未来时刻被记成
        `_current_started` 后，之后所有真实时间戳都成了「过去」，
        永远退不了场 —— 同样永久静默。

   排序、打断、积压、到期不补播，本来就该由端侧做：
   `Announcement` 已经带了 `priority` / `interrupt` / `ttl_ms`，
   而契约里 `ttl_ms` 的定义就是「**从端「收到」起算**」（见
   docs/api-contract.md §2）—— 到期与否本来就该端侧算。
   服务端只要别把明显重复的、废的数据发出去就够了。

   `docs/design.md` D11 讲的「端侧需要一个单一有序通道」仍然成立，
   那由 WebSocket 保证；但它**不需要**服务端也跟着模拟播放。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts import Announcement


@dataclass
class Arbiter:
    dedup_window_ms: int = 3000

    # 观测用（测试和调试台）
    sent: list[Announcement] = field(default_factory=list)
    dropped: list[tuple[Announcement, str]] = field(default_factory=list)

    _dedup: dict[str, int] = field(default_factory=dict, repr=False)  # key -> 上次放行时刻

    # ------------------------------------------------------------------

    def submit(self, ann: Announcement, now_ms: int) -> str:
        """过闸门。返回 'sent' | 'dropped:<原因>'"""
        last = self._dedup.get(ann.dedup_key)
        # ★ 用 abs 而不是 (now - last)：时间戳不保证单调 ——
        #   /v1/emergency/tick 会传未来时间，客户端也能在 Frame.ts 里塞任意值。
        #   若不取绝对值，一个「未来时刻」会把该 key 永久判为重复，
        #   而障碍物的 dedup_key 不带事件号（同一种台阶永远是同一个 key），
        #   一旦被永久锁死就再也播不出来了。
        if last is not None and abs(now_ms - last) < self.dedup_window_ms:
            return self._drop(ann, "duplicate")

        if ann.ttl_ms <= 0:
            return self._drop(ann, "expired")

        self._dedup[ann.dedup_key] = now_ms
        self.sent.append(ann)
        return "sent"

    @property
    def sent_ids(self) -> set[str]:
        return {a.id for a in self.sent}

    def drop_reason(self, ann_id: str) -> str | None:
        """某条播报为什么没发出去。调试和测试用。"""
        for a, r in self.dropped:
            if a.id == ann_id:
                return r
        return None

    def _drop(self, ann: Announcement, reason: str) -> str:
        self.dropped.append((ann, reason))
        return f"dropped:{reason}"
