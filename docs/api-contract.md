# 灵眸伴途 —— 接口契约

> ⚠️ 本文件由 `python scripts/export_contract.py` 自动生成，**不要手改**。
> 改契约请改 `app/contracts.py`，然后重跑生成脚本。
>
> 本文档讲**是什么**；**为什么这么设计**见 [design.md](design.md)。

## 0. 一句话

全系统只有两个数据结构：**`Frame`（输入）** 和 **`Announcement`（输出）**。
九条路由全部「入 Frame，出 Announcement」，四层的差异只体现在 `source`
字段和 `detail` 的形状上。

> 契约版本 1.0　｜　兼容性原则：只加可选字段，不改字段名和类型，不删字段。

---

## 1. `Frame` —— 统一输入

| 字段 | 类型 | 默认 | 说明 |
| :--- | :--- | :--- | :--- |
| `frame_id` | `str` | 必填 | 帧唯一标识 |
| `ts` | `int` | 必填 | 毫秒时间戳 |
| `image_ref` | `str | None` | `None` | 图片路径或视频帧引用 |
| `source` | `str` | `perception` | 哪一层在消费这个输入 |
| `extra` | `dict[str, Any]` |  | 上下文；图片序列会填 index/total |

---

## 2. `Announcement` —— 统一输出

| 字段 | 类型 | 默认 | 说明 |
| :--- | :--- | :--- | :--- |
| `text` | `str` | 必填 | ★ 端侧唯一消费字段 |
| `ttl_ms` | `int` | 必填 | ★ 从端「收到」起算，过期即丢，绝不补播 |
| `dedup_key` | `str` | 必填 | 去重键，★ 构造请用 make_dedup_key() |
| `source` | `str` | `system` | 哪个层产生的 |
| `priority` | `int` | `1` | 0-3，越高越能打断别人 |
| `id` | `str` |  | 播报唯一标识 |
| `ts` | `int` |  | 毫秒时间戳 |
| `interrupt` | `bool` | `False` | 这条是否允许打断当前播报 |
| `haptic` | `str` | `none` | 震动模式 |
| `frame_id` | `str | None` | `None` | 关联的输入帧 |
| `detail` | `dict[str, Any]` |  | 结构化细节，形状见 §3 |

**端侧只消费 `text` 字段** + 执行 `haptic` 震动，不需要理解 `detail`。
业务逻辑、措辞、优先级全部集中在后端。

### 2.1 `source` 取值

| 值 | 含义 |
| :--- | :--- |
| `perception` | 第一层 环境感知 |
| `safety` | 第二层 安全预警 |
| `navigation` | 第三层 智能导航 |
| `emergency` | 第四层 紧急求助 |
| `system` | 系统降级通告 |

### 2.2 `priority` 取值

| 值 | 含义 | 能否打断 |
| :--- | :--- | :--- |
| `0` | 场景描述 | 随时可被打断 |
| `1` | 用户主动查询的结果 | 可被 2/3 打断 |
| `2` | 导航指令、一般障碍 | 可被 3 打断 |
| `3` | 碰撞风险、紧急求助 | 打断一切 |

### 2.3 `haptic` 取值

`none` / `short` / `double` / `long`

> 枚举刻意做小：微信小程序只有「短/长」两档，iOS Web 完全没有震动 API。
> 端拿不到震动时，用 `text` 的语音播报兜底。

---

## 3. `detail` 的四种形状

按 `source` 区分。**注意 `bbox` 是归一化 `[x, y, w, h]`（0–1，原点左上），
不是像素坐标** —— 这是组员之间最容易对不上的地方。

**`source = perception`**

```json
{
  "kind": "vision",
  "scene_key": "traffic_light",
  "scene_description": "前方约 3 米有一个红绿灯，现在是红灯",
  "scene_conf": 0.9,
  "ocr_results": [
    {
      "text": "电梯",
      "bbox": [
        0.62,
        0.31,
        0.11,
        0.08
      ],
      "confidence": 0.93,
      "category": "elevator_button"
    }
  ],
  "objects": [
    {
      "label": "人",
      "confidence": 0.95,
      "position": "left",
      "bbox": [
        0.05,
        0.4,
        0.15,
        0.45
      ],
      "distance_m": 1.8,
      "distance_sigma_m": 0.6,
      "track_id": 7,
      "is_known": false,
      "face_id": null
    },
    {
      "label": "红绿灯",
      "confidence": 0.88,
      "position": "center",
      "bbox": [
        0.44,
        0.18,
        0.06,
        0.14
      ],
      "distance_m": 3.0,
      "distance_sigma_m": 1.1,
      "track_id": null,
      "is_known": false,
      "face_id": null
    }
  ],
  "degraded": false
}
```

**`source = safety`**

```json
{
  "kind": "safety",
  "risk": "danger",
  "obstacles": [
    {
      "type": "step_down",
      "position": "center",
      "distance_m": 2.0,
      "distance_sigma_m": 0.7,
      "risk": "danger",
      "confidence": 0.86,
      "track_id": 3
    }
  ],
  "degraded": false
}
```

**`source = navigation`**

```json
{
  "kind": "route",
  "route_id": "r1",
  "steps": [
    {
      "instruction": "沿人行道直行",
      "distance_m": 200.0,
      "maneuver": "straight"
    }
  ],
  "total_distance_m": 820.0,
  "total_duration_s": 600.0,
  "warnings": [
    "路线已避开天桥和地下通道"
  ]
}
```

**`source = emergency`**

```json
{
  "kind": "emergency",
  "state": "suspected",
  "event_id": "evt_001",
  "idempotency_key": "evt_001",
  "contact_scope": "family",
  "confirm_deadline_ts": 1758326415000,
  "location": {
    "lat": 39.9042,
    "lng": 116.4074,
    "accuracy_m": 8.0,
    "ts": 1758326400000
  }
}
```

**`source = system`**

```json
{
  "kind": "system",
  "reason": "vlm_timeout",
  "retry_in_ms": 30000
}
```


### 3.1 方位约定

`position` ∈ `left` / `center` / `right`

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
| `touch` | < 0.5 m |
| `near` | 0.5 – 1.5 m |
| `close` | 1.5 – 3 m |
| `medium` | 3 – 6 m |
| `far` | > 6 m |

### 3.3 障碍物类型

`step_up`, `step_down`, `pothole`, `curb`, `vehicle`, `bicycle`, `pole`, `person`, `other`

### 3.4 跌倒状态机

```
idle ──冲击+姿态异常──> suspected ──倒计时归零──> confirmed
                                      │
                                      └──用户显式取消──> cancelled
```

**两条硬规则：**

1. **`suspected` 态永不自动外呼。**
   真跌倒和「把手机扔到床上」在加速度计上几乎无法区分。必须等
   `confirm_deadline_ts` 超时、用户没响应，才升到 `confirmed`。
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
| `POST` | `/v1/emergency/tick` | `now_ms` | `Announcement[]` | 推进紧急状态机时钟 |
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
| `source` | 否 | `perception`（默认）\| `safety` — 只有这两层吃图像 |
| `frame_id` | 否 | 不填则服务端生成 |
| `ts` | 否 | 毫秒时间戳，不填则取当前时间 |
| `extra` | 否 | JSON 对象字符串，如 `{"index":1}` |

入参不合法返回 **400** 且响应体为 `{"error": "..."}`（缺 `image`、`source`
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
| 导航 | `geo` | `('lat', 'lng')` 起点坐标 |
| 导航 | `avoid` | 要避开的障碍，默认 `["overpass","underpass","stairs"]` |
| 求助 | `kind` | `fall_signal` \| `sos` \| `cancel` |
| 求助 | `signal` | 跌倒传感器窗口，见下 |
| 求助 | `event_id` | 事件标识，不填则由 `frame_id` 推导 |
| 求助 | `idempotency_key` | ★ 幂等键，防止重试导致重复呼叫家属 |
| 求助 | `trigger` | 触发方式，见 §4.2 |
| 求助 | `method` | 取消方式：`voice` \| `shake` \| `screen_tap` \| `hardware_key` |

跌倒传感器窗口 `signal` 的字段：
`peak_g`、`free_fall_ms`、`posture`、`post_impact_still_ms`、
`movement_class`（`still` \| `walking` \| `vehicle` \| `handheld` \| `unknown`）、
`on_charger`、`screen_on`。

### 4.2 求助触发方式

`hardware_key_long`, `screen_long_press`, `shake_pattern`, `voice_trigger`, `fall_confirmed`

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

`GET /v1/health` 返回降级状态，WS 也会推 `source = system` 的播报。

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
