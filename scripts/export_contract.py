#!/usr/bin/env python3
"""从 app/contracts.py 生成 docs/api-contract.md。

    python scripts/export_contract.py

契约文档是「生成物」，不是手写的 —— 这样它永远不会和代码脱节。
改契约请改 app/contracts.py 然后重跑本脚本。
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import contracts as C  # noqa: E402
from app.mock import fixtures as F  # noqa: E402

OUT = ROOT / "docs" / "api-contract.md"


def field_table(cls) -> str:
    lines = ["| 字段 | 类型 | 默认 | 说明 |", "| :--- | :--- | :--- | :--- |"]
    for f in dataclasses.fields(cls):
        typ = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", str(f.type))
        default = (
            "必填"
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
            else ("" if f.default is dataclasses.MISSING else f"`{f.default}`")
        )
        doc = (f.metadata or {}).get("doc", "")
        lines.append(f"| `{f.name}` | `{typ}` | {default} | {doc} |")
    return "\n".join(lines)


def detail_examples() -> str:
    out = []
    for src in (C.SOURCE_PERCEPTION, C.SOURCE_SAFETY, C.SOURCE_NAVIGATION,
                C.SOURCE_EMERGENCY, C.SOURCE_SYSTEM):
        ann = F.BY_SOURCE[src]
        out.append(f"**`source = {src}`**\n")
        out.append("```json")
        out.append(json.dumps(ann.detail, ensure_ascii=False, indent=2))
        out.append("```\n")
    return "\n".join(out)


DOC = f"""# 灵眸伴途 —— 接口契约

> ⚠️ 本文件由 `python scripts/export_contract.py` 自动生成，**不要手改**。
> 改契约请改 `app/contracts.py`，然后重跑生成脚本。
>
> 本文档讲**是什么**；**为什么这么设计**见 [design.md](design.md)。

## 0. 一句话

全系统只有两个数据结构：**`Frame`（输入）** 和 **`Announcement`（输出）**。
十条路由全部「入 Frame，出 Announcement」（九条 JSON + 一个 multipart 统一
帧入口），四层的差异只体现在 `source`
字段和 `detail` 的形状上。

> 契约版本 1.0　｜　兼容性原则：只加可选字段，不改字段名和类型，不删字段。

---

## 1. `Frame` —— 统一输入

{field_table(C.Frame)}

---

## 2. `Announcement` —— 统一输出

{field_table(C.Announcement)}

**端侧的播报内容只看 `text`**，执行 `haptic` 震动，不需要理解 `detail` 的结构。
但**还要读 `priority` / `interrupt` / `ttl_ms` 三个字段做播放排序与到期判断**
（详见 §5）—— 排序、打断、积压、到期不补播都由端侧负责。
业务逻辑、措辞、优先级判定都在后端。

### 2.1 `source` 取值

| 值 | 含义 |
| :--- | :--- |
| `{C.SOURCE_PERCEPTION}` | 第一层 环境感知 |
| `{C.SOURCE_SAFETY}` | 第二层 安全预警 |
| `{C.SOURCE_NAVIGATION}` | 第三层 智能导航 |
| `{C.SOURCE_EMERGENCY}` | 第四层 紧急求助 |
| `{C.SOURCE_SYSTEM}` | 系统降级通告 |

### 2.2 `priority` 取值

| 值 | 含义 | 能否打断 |
| :--- | :--- | :--- |
| `{C.PRIORITY_BACKGROUND}` | 场景描述 | 随时可被打断 |
| `{C.PRIORITY_NORMAL}` | 用户主动查询的结果 | 可被 2/3 打断 |
| `{C.PRIORITY_IMPORTANT}` | 导航指令、一般障碍 | 可被 3 打断 |
| `{C.PRIORITY_CRITICAL}` | 碰撞风险、紧急求助 | 打断一切 |

### 2.3 `haptic` 取值

`{C.HAPTIC_NONE}` / `{C.HAPTIC_SHORT}` / `{C.HAPTIC_DOUBLE}` / `{C.HAPTIC_LONG}`

> 枚举刻意做小：微信小程序只有「短/长」两档，iOS Web 完全没有震动 API。
> 端拿不到震动时，用 `text` 的语音播报兜底。

---

## 3. `detail` 的四种形状

按 `source` 区分。**注意 `bbox` 是归一化 `[x, y, w, h]`（0–1，原点左上），
不是像素坐标** —— 这是组员之间最容易对不上的地方。

{detail_examples()}

### 3.1 方位约定

`position` ∈ `{C.POSITION_LEFT}` / `{C.POSITION_CENTER}` / `{C.POSITION_RIGHT}`

> ★ **参考系是「相机画面」，不是用户身体。** 手机放兜里或斜拿时，
> 画面左边 ≠ 身体左边。播报措辞要避免会产生方向歧义的表达。

### 3.2 距离约定

单目深度估计误差 30–50%，所以障碍物必须带 `distance_sigma_m`。

> **建议：对障碍物只播粗档（「就在脚前 / 很近 / 几米外 / 远处」），不播数字。**
> 虚假的精确感会让用户按错误预期迈步 —— 误差往低估的方向是有害的。
> 例外：第三层的距离来自地图/GPS，误差量级完全不同，**可以播数字**
> （「前方 300 米右转」）。

| 档位 | 范围 |
| :--- | :--- |
| `{C.DISTANCE_TOUCH}` | < 0.5 m |
| `{C.DISTANCE_NEAR}` | 0.5 – 1.5 m |
| `{C.DISTANCE_CLOSE}` | 1.5 – 3 m |
| `{C.DISTANCE_MEDIUM}` | 3 – 6 m |
| `{C.DISTANCE_FAR}` | > 6 m |

### 3.3 障碍物类型

{", ".join(f"`{t}`" for t in C.OBSTACLE_TYPES)}

### 3.4 跌倒状态机

```
{C.FALL_IDLE} ──冲击+姿态异常──> {C.FALL_SUSPECTED} ──倒计时归零──> {C.FALL_CONFIRMED}
                                      │
                                      └──用户显式取消──> {C.FALL_CANCELLED}
```

**两条硬规则：**

1. **`{C.FALL_SUSPECTED}` 态永不自动外呼。**
   真跌倒和「把手机扔到床上」在加速度计上几乎无法区分。必须等
   `confirm_deadline_ts` 超时、用户没响应，才升到 `{C.FALL_CONFIRMED}`。
2. **取消不能依赖网络。** 用户说「我没事」时如果连接正好断了，取消仍然
   必须生效 —— 端侧本地就能完成，服务端只是通知者。

---

## 4. HTTP 路由

| 方法 | 路径 | 入 | 出 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `POST` | `/v1/perception/describe` | `Frame` | `Announcement` | 第一层 环境感知 |
| `POST` | `/v1/safety/analyze` | `Frame` | `Announcement` | 第二层 安全预警 |
| `POST` | `/v1/safety/fall` | `Frame` | `Announcement` | 第二层 跌倒检测 |
| `POST` | `/v1/navigation/route` | `Frame` | `Announcement` | 第三层 智能导航 |
| `POST` | `/v1/emergency/sos` | `Frame` | `Announcement` | 第四层 一键求助 |
| `POST` | `/v1/emergency/cancel` | `Frame` | `Announcement` | 取消求助 / 取消跌倒确认 |
| `POST` | `/v1/emergency/tick` | `{"now_ms"}` | `Announcement[]` | 推进紧急状态机时钟 |
| `POST` | `/v1/frame` | multipart | `Announcement` | ★ 统一帧入口（上传图像） |
| `GET` | `/v1/health` | — | 降级状态 | |
| `WS` | `/v1/stream` | — | 推 `Announcement` | |

**路由可能返回空的 `announcements` 数组**（比如前方无障碍），这不代表出错。
「没出声」和「系统哑了」的区分靠 `/v1/health` 和降级通告。

### 4.0 统一帧入口 `POST /v1/frame`

端侧（摄像头 / 视频抽帧 / 图片文件）只需要这一个「发图」接口。信封与其余
路由完全一致，区别只是**图像走 multipart 上传**，而不是让 `image_ref` 指一个
服务端已有的路径。

`multipart/form-data` 字段：

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `image` | ★ 是 | 图像文件本体 |
| `source` | 否 | `perception`（默认）\\| `safety` — 只有这两层吃图像 |
| `frame_id` | 否 | 不填则服务端生成 |
| `ts` | 否 | 毫秒时间戳，不填则取当前时间 |
| `extra` | 否 | JSON 对象字符串，如 `{{"index":1}}` |

入参不合法返回 **400** 且响应体为 `{{"error": "..."}}`（缺 `image`、`source`
不在允许集合、`extra` 不是 JSON 对象、`ts` 非整数、空文件）。

> **为什么上传的字节要落盘成临时文件？** 因为 `image_ref` 的语义是
> 「服务端路径」，`layers/perception.py` 用 `os.path.isfile()` 找它。
> 落盘一次可以让下游（含未来的真实检测器）一行都不用改。
> 文件在响应返回前删除。

### 4.1 `Frame.extra` 的约定字段

`Frame` 只有五个字段，各层的差异化入参统一放 `extra`：

| 层 | 字段 | 说明 |
| :--- | :--- | :--- |
| 通用 | `index` | 帧序号，测试素材按它轮换场景 |
| 感知 | — | 只用 `image_ref` |
| 导航 | `destination` | 目的地（自然语言） |
| 导航 | `geo` | `{"lat", "lng"}` 起点坐标 |
| 导航 | `avoid` | 要避开的障碍，默认 `["overpass","underpass","stairs"]` |
| 求助 | `kind` | `fall_signal` \\| `sos` \\| `cancel` |
| 求助 | `signal` | 跌倒传感器窗口，见下 |
| 求助 | `event_id` | 事件标识，不填则由 `frame_id` 推导 |
| 求助 | `idempotency_key` | ★ 幂等键，防止重试导致重复呼叫家属 |
| 求助 | `trigger` | 触发方式，见 §4.2 |
| 求助 | `method` | 取消方式：`voice` \\| `shake` \\| `screen_tap` \\| `hardware_key` |

跌倒传感器窗口 `signal` 的字段：
`peak_g`、`free_fall_ms`、`posture`、`post_impact_still_ms`、
`movement_class`（`still` \\| `walking` \\| `vehicle` \\| `handheld` \\| `unknown`）、
`on_charger`、`screen_on`。

### 4.2 求助触发方式

{", ".join(f"`{t}`" for t in C.TRIGGERS)}

> ★ **「长按手机侧键 3 秒」在微信小程序和 Web 上都没有对应 API。**
> 所以触发方式是**可协商的集合**而不是常量，端上有什么能力就上报什么。
> 冗余触发是安全系统的基本要求 —— 单一触发通道等于单点故障。

---

## 5. 播报闸门规则

四条流水线共用的唯一出口。它只回答一个问题：**这条值不值得发给端侧？**

1. **去重** —— `dedup_key` 相同、且距上次放行不足 3 秒，不发
2. **废数据** —— `ttl_ms <= 0`，不发

就这样。**服务端不管排序、打断、积压，也不管「当前正在播哪条」。**

> ★ **为什么：喇叭在端侧，服务端观察不到播放状态。**
> 早期版本在服务端维护「当前播报」+ 优先级队列来模拟播放，连出两个
> 永久静默的 bug，根因都是**服务端在猜自己看不见的东西**：
> 没人调 `finish_current()` 导致当前播报永不释放、队列涨满；或
> `/v1/emergency/tick` 传的未来时间戳让时钟倒流、当前播报永远退不了场。
> 所以这套状态机被拆掉了。

**排序 / 打断 / 积压 / 到期不补播由端侧负责**，`Announcement` 已经带了
`priority`、`interrupt`、`ttl_ms` 三个字段供端侧决策。这也正是 `ttl_ms`
的定义所要求的 —— 见 §2：**从端「收到」起算**。

### ★ 两条安全关键规则

**一、`dedup_key` 必须包含风险等级和距离档位，不能只用障碍物类型。**

构造去重键请一律用 `contracts.make_dedup_key()`。否则下面这个序列会被
当成「同一个台阶的重复」全部吞掉：

```
台阶(info,   3.2m)  -> 播报
台阶(warning,1.8m)  -> 被去重丢弃
台阶(danger, 0.7m)  -> 被去重丢弃   ← 用户撞上去
```

**被吞掉的恰恰是最危险的那一条**，因为它是「同一个物体的重复」。

**二、`ttl_ms` 必须 ≥ 播报这条文本所需时长。**

中文 TTS 约 250 ms/字，一条 14 字的预警要 3.5 秒。若 `ttl_ms = 2000`，
消息在播完之前就过期了，用户听到半句被打断，两句话都没听懂。

---

## 6. 降级通告

`GET /v1/health` 返回降级状态，WS 也会推 `source = {C.SOURCE_SYSTEM}` 的播报。

> **沉默不能有歧义。** 网络断了、VLM 超时、读不到帧 —— 用户如果不知道，
> 会把「系统没出声」理解为「环境安全」。这是这类系统最危险的失效模式。

---

## 7. 对接方式

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
bash scripts/smoke.sh
```

前端连 `ws://<host>:8000/v1/stream` 收播报，收到就播 `text` + 执行 `haptic`。
"""


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(DOC, encoding="utf-8")
    print(f"已生成 {OUT.relative_to(ROOT)}  ({len(DOC.splitlines())} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
