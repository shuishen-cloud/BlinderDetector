# 灵眸伴途

基于视觉语言模型的视障人士出行辅助系统 —— 后端原型。

手机摄像头看世界，AI 用自然语言说给视障人士听。

> **当前阶段：四层逻辑已实现，外部依赖仍是 mock，各组并行开工。**
>
> 已写实：风险分级、措辞生成、跌倒状态机、求助状态机、无障碍路线过滤。
> 未接入：云端 VLM、检测模型、地图 API、短信/推送通道。
>
> | 想知道 | 去哪看 |
> | :--- | :--- |
> | 怎么跑起来 | 本文档下方 |
> | 接口字段 | [docs/api-contract.md](docs/api-contract.md) |
> | **为什么这么设计** | [docs/design.md](docs/design.md) |
> | 谁做什么、进度 | [工作管理.md](工作管理.md) |
> | 设计中的已知问题 | [文档-设计中的问题.md](文档-设计中的问题.md) |

---

## 快速开始

```bash
pip install -r requirements.txt      # 纯 Python，无编译
cp .env.example .env                 # 不填任何 key 也能完整跑通

bash scripts/smoke.sh                # 一键自检：起服务 -> 打全部路由
```

手动起服务：

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` 是为了让同局域网的其他设备（组员的前端）能连上。

---

## 前端调试台

起服务后直接开 **<http://127.0.0.1:8000/>**：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

单文件、零构建、零 npm（`web/index.html`）。它**不是交付形态**，只用来把
「帧 → 后端 → 播报 → WS 推流」这条链路跑通并肉眼验证契约。

- **帧源 ①视频抽帧** —— 读 `data/demo.mp4`，感知/安全两条流水线**各自独立
  发包**（频率可调），这就是 design.md D2 的两条解耦流水线。
  视频 404 就先跑 `bash scripts/make_test_video.sh`。
- **帧源 ②摄像头** —— 只留接口占位。接入时换成 `getUserMedia` 取流，
  复用同一个 `sendFrame()`，后端不用改。
- **帧源 ③单张图片** —— 拖拽即可，不依赖 demo.mp4。

端侧发图走**统一入口 `POST /v1/frame`**（multipart），字段见
[docs/api-contract.md](docs/api-contract.md) §4.0。

---

## 跑一遍测试素材

先生成测试视频（手写 SVG → PNG → mp4）：

```bash
bash scripts/make_test_video.sh      # 首次会自动装 librsvg / ffmpeg
```

然后喂给后端看产生了哪些播报：

```bash
python scripts/run_video.py data/demo.mp4
python scripts/run_video.py data/frames --source images    # 也可以用图片序列
python scripts/demo_fall.py                                # 跌倒全流程演示
```

另开一个终端看实时推流：

```bash
python scripts/ws_probe.py
```

---

## 核心设计

**全系统只有两个数据结构。**

```
Frame         所有接口的输入
Announcement  所有接口的输出
```

十条路由全部「入 `Frame`，出 `Announcement`」（九条 JSON + 一个 multipart 统一帧入口），四层的差异只体现在
`source` 字段和 `detail` 的形状上。端侧拿到播报**只播 `text`** 加执行
`haptic` 震动，不需要理解 `detail` 的结构 —— 但**要读 `priority` /
`interrupt` / `ttl_ms` 做播放排序和到期判断**（详见 design.md D11）。

几条关键规则（完整推导见 [docs/design.md](docs/design.md)）：

- **感知与安全是两条解耦的流水线。** 云端 VLM 单次调用 1–3 秒，对避障来说
  完全不可接受，所以第二层绝不能走 VLM。这是本项目最重要的架构主张。
- **TTL 过期即丢。** 迟到 3 秒的「前方 2 米有台阶」比不播更危险。
  由**端侧**执行（契约里 `ttl_ms` 就是「从端收到起算」）—— 服务端不排队，
  也就不存在「排到他时已过期」这回事。
- **服务端只做闸门。** 只去重、丢废数据；排序 / 打断 / 积压归端侧。
  详见 [docs/design.md](docs/design.md) D11 —— 那里记着为什么。
- **去重键必须含风险等级和距离档位。** 否则风险升级会被当成「同一物体的
  重复」吞掉 —— 被吞的恰恰是最危险那条。
- **跌倒 `suspected` 态永不自动外呼。** 真跌倒和「把手机扔到床上」在加速度计
  上几乎无法区分。

---

## 目录

```
app/
  contracts.py          ★ 接口契约单一真源（只有标准库依赖）
  config.py             读 .env
  main.py               Starlette 装配
  core/
    registry.py         实现注册表
    arbiter.py          ★ 播报闸门（只做去重 + 废数据过滤）
    rules/              ★ 各层业务规则（可单独测试）
      risk.py             障碍物风险分级（悲观距离、置信度门限、TTL）
      phrasing.py         措辞生成（不播数字、置信度对冲、短句降级）
      scene.py            场景分类（决定去重粒度）
      fall.py             跌倒状态机
      sos.py              求助状态机（幂等、升级链）
      route.py            无障碍路线规划
    layers/             四层编排（薄）
    providers/          VLM 实现（mock / dashscope / zhipu / openai）
    detectors/          障碍物检测器实现
    sources/            输入源（video / images）
  mock/fixtures.py      契约样例数据
web/index.html          ★ 前端调试台（单文件，零构建）
assets/                 测试素材（手写 SVG）
data/                   生成的帧和视频；uploads/ 是上传帧的临时落盘处
scripts/                自检、跑视频、跌倒演示、WS 探针、契约导出
tests/                  pytest
docs/api-contract.md    自动生成的接口契约
```

**规则和编排是分开的**：`detectors/` 只回答「看到什么」，`rules/` 回答
「怎么判断危险、怎么说出来」，`layers/` 只做编排。换检测模型时安全策略
不会跟着变，而且规则层可以脱离框架单独测试。

---

## 加一种实现

**不要改别人的代码。** 新建文件加个装饰器，靠 `.env` 切换。

### 换云端 VLM（第一层）

```python
# app/core/providers/zhipu_vlm.py
from app.core.registry import register
from app.contracts import Frame

@register("vlm", "zhipu")
class ZhipuVLM:
    async def describe_frame(self, frame: Frame, image: bytes) -> dict:
        ...   # 返回 contracts.vision_detail() 的形状

    async def health(self) -> bool:
        return True
```

```bash
VLM_PROVIDER=zhipu
```

`openai_compat.py` 里已经有 DashScope / 智谱 / OpenAI 三个实现，都走
OpenAI 兼容协议，填上 `VLM_BASE_URL` / `VLM_API_KEY` / `VLM_MODEL` 即可。

### 换障碍物检测模型（第二层）

```python
# app/core/detectors/yolo.py
@register("detector", "yolo")
class YoloDetector:
    async def detect(self, frame: Frame) -> list[dict]:
        ...   # 返回原始检测结果，不做分级和措辞
    async def health(self) -> bool:
        return True
```

```bash
DETECTOR=yolo
```

**检测器只回答「看到什么」** —— 置信度过滤、风险分级、措辞全在
`app/core/rules/` 里，所有检测器共用同一套安全策略。所以换模型不会
让安全规则跟着变。

### 其他

可注册的类别：`vlm` / `detector` / `layer` / `framesource`。
`GET /v1/health` 会列出所有已注册的实现。

---

## 测试

```bash
pytest -v
```

| 文件 | 测什么 |
| :--- | :--- |
| `test_contracts.py` | 契约形状、距离档位、去重键 |
| `test_arbiter.py` | 闸门：去重窗口、升级突破去重、废数据 |
| `test_api.py` | 全部路由的形状一致性、`/v1/frame` 上传（含 400 分支）、WebSocket 广播、静态托管 |
| `test_risk.py` | 悲观距离分级、置信度门限、TTL |
| `test_phrasing.py` | 不播数字、置信度对冲、短句降级 |
| `test_scene.py` | 场景分类与去重粒度 |
| `test_fall.py` | 跌倒状态机（覆盖最全） |
| `test_sos.py` | 幂等、升级链、绝不自动拨 120 |
| `test_route.py` | 无障碍过滤、导航播报 |

---

## 环境说明

当前开发环境是 **WSL / Linux（Python 3.13）**；早期开发在 Termux / Android
（Python 3.14）上做，手机端仍是目标运行环境之一 —— 下面的零编译约束来自它。

**为什么不用 FastAPI：** Termux 平台标签是 `android_24_arm64_v8a`（bionic libc），
而 `pydantic-core` 在 PyPI 上只有 `manylinux_2_17_aarch64`（glibc），不兼容，
会退化成源码编译 Rust。改用 Starlette + 标准库 dataclass，整条依赖树都是
纯 Python wheel，零编译。

**装 uvicorn 时不要带 `[standard]`** —— 会拉 httptools / uvloop 等 C 扩展。

**但 WebSocket 库必须单独装**（已写进 `requirements.txt` 的 `wsproto`）。
不带 `[standard]` 的 uvicorn 没有任何 WS 实现，`/v1/stream` 会直接 404。
`wsproto` 是纯 Python，满足上面的零编译约束；`websockets` 是 C 扩展 wheel，不行。
