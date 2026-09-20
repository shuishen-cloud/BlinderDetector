"""
播报仲裁器 —— 四条流水线共用的唯一出口。

没有它，场景描述和安全预警会互相打断，或者排队造成预警延迟倒灌。

四条规则：
  1. TTL 过期直接丢，绝不补播
  2. dedup_key 相同且窗口内 -> 丢
  3. 新 priority 严格更高才可打断当前播报，且当前已播够 min_play_ms
  4. 队列超 max_queue 时丢最低优先级

所有方法都要求传 now_ms（而不是内部取 time.time()），这样测试可以
精确构造时序。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from app.contracts import Announcement


@dataclass
class Arbiter:
    dedup_window_ms: int = 3000
    max_queue: int = 8
    min_play_ms: int = 800  # 一旦开口，至少播这么久才允许被打断

    # 观测用（测试和调试）
    spoken: list[Announcement] = field(default_factory=list)
    dropped: list[tuple[Announcement, str]] = field(default_factory=list)

    _queue: list = field(default_factory=list, repr=False)  # [(-priority, seq, ann)]
    _dedup: dict[str, int] = field(default_factory=dict, repr=False)  # key -> 上次播出时刻
    _enqueued: dict[str, int] = field(default_factory=dict, repr=False)  # ann.id -> 入队时刻
    _current: Announcement | None = field(default=None, repr=False)
    _current_started: int = field(default=0, repr=False)
    _seq: int = field(default=0, repr=False)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def submit(self, ann: Announcement, now_ms: int) -> str:
        """提交一条播报。返回 'spoken' | 'queued' | 'dropped:<原因>'"""
        # 规则 2：去重
        # ★ 依赖 dedup_key 里含 risk 和距离档位（见 contracts.make_dedup_key）。
        #   否则风险升级会被当成「同一物体的重复」而吞掉。
        last = self._dedup.get(ann.dedup_key)
        if last is not None and (now_ms - last) < self.dedup_window_ms:
            return self._drop(ann, "duplicate")

        # 规则 1：TTL
        if ann.ttl_ms <= 0:
            return self._drop(ann, "expired")

        self._enqueued[ann.id] = now_ms

        # 没有在播 -> 直接播
        if self._current is None:
            return self._speak(ann, now_ms)

        # 规则 3：抢占
        played = now_ms - self._current_started
        if ann.priority > self._current.priority and played >= self.min_play_ms:
            self._drop(self._current, "interrupted")
            self._current = None
            return self._speak(ann, now_ms)

        # 规则 4：入队 / 挤掉最低优先级
        if len(self._queue) >= self.max_queue:
            lowest = min(self._queue, key=lambda t: t[0])  # -priority 最小 == 优先级最低
            if ann.priority <= -lowest[0]:
                return self._drop(ann, "queue_full")
            self._queue.remove(lowest)
            heapq.heapify(self._queue)
            self._drop(lowest[2], "queue_full")

        self._seq += 1
        heapq.heappush(self._queue, (-ann.priority, self._seq, ann))
        return "queued"

    def finish_current(self, now_ms: int) -> Announcement | None:
        """当前播报播完了。返回下一条该播的，没有则 None。

        这里做 TTL 检查 —— 排队期间过期的消息直接丢，绝不补播。
        迟到 3 秒的「前方 2 米有台阶」比不播更危险。
        """
        self._current = None
        while self._queue:
            _, _, ann = heapq.heappop(self._queue)
            enq = self._enqueued.pop(ann.id, now_ms)
            if (now_ms - enq) > ann.ttl_ms:
                self._drop(ann, "expired_in_queue")
                continue
            self._speak(ann, now_ms)
            return ann
        return None

    def is_speaking(self) -> bool:
        return self._current is not None

    def current(self) -> Announcement | None:
        return self._current

    @property
    def spoken_ids(self) -> set[str]:
        return {a.id for a in self.spoken}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _speak(self, ann: Announcement, now_ms: int) -> str:
        self._current = ann
        self._current_started = now_ms
        self._dedup[ann.dedup_key] = now_ms
        self.spoken.append(ann)
        return "spoken"

    def _drop(self, ann: Announcement, reason: str) -> str:
        self.dropped.append((ann, reason))
        return f"dropped:{reason}"

    def drop_reason(self, ann_id: str) -> str | None:
        """某条播报为什么没播出来。调试和测试用。"""
        for a, r in self.dropped:
            if a.id == ann_id:
                return r
        return None
