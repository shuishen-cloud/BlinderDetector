# 灵眸伴途

基于视觉语言模型的视障人士出行辅助系统 —— 后端原型。

手机摄像头看世界，AI 用自然语言说给视障人士听。

> **当前阶段：接口契约冻结，各层并行开发。**
> 本期只有契约和骨架，四层全是 mock 实现，不接真实模型。
> 任务分工与进度见 [工作管理.md](工作管理.md)，接口字段见 [docs/api-contract.md](docs/api-contract.md)。

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

## 跑一遍测试素材

先生成测试视频（手写 SVG → PNG → mp4）：

```bash
bash scripts/make_test_video.sh      # 首次会自动装 librsvg / ffmpeg
```

然后喂给后端看产生了哪些播报：

```bash
python scripts/run_video.py data/demo.mp4
python scripts/run_video.py data/frames --source images    # 也可以用图片序列
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

七条路由全部「入 `Frame`，出 `Announcement`」，四层的差异只体现在
`source` 字段和 `detail` 的形状上。端侧拿到播报**只播 `text`** 加执行
`haptic` 震动，不需要理解任何业务结构。

几条关键规则（详见 [docs/api-contract.md](docs/api-contract.md)）：

- **感知与安全是两条解耦的流水线。** 云端 VLM 单次调用 1–3 秒，对避障来说
  完全不可接受，所以第二层绝不能走 VLM。这是本项目最重要的架构主张。
- **TTL 过期即丢。** 迟到 3 秒的「前方 2 米有台阶」比不播更危险。
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
    arbiter.py          ★ 播报仲裁器
    providers/          VLM 实现（mock / dashscope / zhipu / openai）
    layers/             四层实现
    sources/            输入源（video / images）
  mock/fixtures.py      契约样例数据
assets/                 测试素材（手写 SVG）
data/                   生成的帧和视频
scripts/                自检、跑视频、WS 探针、契约导出
tests/                  pytest
docs/api-contract.md    自动生成的接口契约
```

---

## 加一种实现

**不要改别人的代码。** 新建文件加个装饰器，靠 `.env` 切换：

```python
# app/core/providers/zhipu_vlm.py
from app.core.registry import register
from app.contracts import Frame

@register("vlm", "zhipu")
class ZhipuVLM:
    async def describe_frame(self, frame: Frame, image: bytes) -> dict:
        ...   # 返回 detail 里 vision 的形状

    async def health(self) -> bool:
        return True
```

```bash
# .env
VLM_PROVIDER=zhipu
```

可注册的类别：`vlm` / `layer` / `framesource`。
`GET /v1/health` 会列出所有已注册的实现。

---

## 测试

```bash
pytest -v
```

| 文件 | 测什么 |
| :--- | :--- |
| `test_contracts.py` | 契约形状、距离档位、去重键 |
| `test_arbiter.py` | 仲裁四条规则（打断 / 去重 / TTL / 积压） |
| `test_api.py` | 七条路由的形状一致性 + WebSocket 广播 |

---

## 环境说明

当前开发环境是 Termux / Android（Python 3.14）。

**为什么不用 FastAPI：** Termux 平台标签是 `android_24_arm64_v8a`（bionic libc），
而 `pydantic-core` 在 PyPI 上只有 `manylinux_2_17_aarch64`（glibc），不兼容，
会退化成源码编译 Rust。改用 Starlette + 标准库 dataclass，整条依赖树都是
纯 Python wheel，零编译。

**装 uvicorn 时不要带 `[standard]`** —— 会拉 httptools / uvloop 等 C 扩展。
