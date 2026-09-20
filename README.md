# 灵眸伴途（简化版）

基于视觉语言模型的视障人士出行辅助系统 —— 后端原型。

手机摄像头看世界，AI 用自然语言说给视障人士听。

> **这是精简版。** 从完整项目（分支 `Lwy`）裁掉了 Web 调试台、测试素材静态托管、
> multipart 上传入口和多余文档，只留核心链路，方便读和跑。
> 完整版见 [设计说明.md](设计说明.md) 末尾「与完整版的差异」。

| 想知道 | 去哪看 |
| :--- | :--- |
| 接口字段 | [docs/api-contract.md](docs/api-contract.md) |
| 为什么这么设计 | [设计说明.md](设计说明.md) |

---

## 快速开始

```bash
pip install -r requirements.txt      # 纯 Python，零编译
cp .env.example .env                 # 不填任何 key 也能完整跑通

bash smoke.sh                        # 一键自检：起服务 -> 打全部路由
```

手动起服务：

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

测试：

```bash
pytest -q                            # 138 项，约 0.2 秒
```

改了 `app/contracts.py` 后重新生成接口契约文档：

```bash
python scripts/export_contract.py    # -> docs/api-contract.md
```

---

## 核心设计

**全系统只有两个数据结构。**

```
Frame         所有接口的输入
Announcement  所有接口的输出
```

九条路由全部「入 `Frame`，出 `Announcement`」，四层的差异只体现在 `source`
字段和 `detail` 的形状上。端侧拿到播报**只播 `text`** 加执行 `haptic` 震动，
不需要理解任何业务结构。

几条关键规则（完整论证见 [设计说明.md](设计说明.md)）：

- **感知与安全是两条解耦的流水线。** 云端 VLM 单次调用 1–3 秒，对避障完全
  不可接受，所以第二层绝不能走 VLM。这是本项目最重要的架构主张。
- **TTL 过期即丢。** 迟到 3 秒的「前方 2 米有台阶」比不播更危险。
- **去重键必须含风险等级和距离档位。** 否则风险升级会被当成「同一物体的
  重复」吞掉 —— 被吞的恰恰是最危险那条。
- **跌倒 `suspected` 态永不自动外呼。** 真跌倒和「把手机扔到床上」在加速度计
  上几乎无法区分。
- **沉默 ≠ 安全。** 网络断、VLM 超时、读不到帧都必须显式通告。

---

## 目录

```
app/
  contracts.py          ★ 接口契约单一真源（只有标准库依赖）
  config.py             读 .env
  main.py               Starlette 装配（九条路由）
  core/
    registry.py         实现注册表
    arbiter.py          ★ 播报仲裁闸门
    detectors/          第二层检测器（mock）
    rules/              ★ 安全规则：风险分级 / 措辞 / 场景 / 跌倒 / 路线 / 求助
    providers/          VLM 实现（mock / dashscope / zhipu / openai）
    layers/             四层编排
    sources/            输入源（video / images）
  mock/fixtures.py      契约样例数据
docs/api-contract.md    ★ 接口契约（生成物，勿手改）
scripts/
  export_contract.py    契约文档生成脚本（纯标准库，无外部依赖）
tests/                  pytest（138 项）
smoke.sh                一键自检
```

**三层分工：** `detectors/` 只回答「看到什么」，`rules/` 回答「怎么判断危险、
怎么说出来」，`layers/` 只做编排。换检测模型时安全策略不用跟着变，
规则层也能脱离 HTTP 单独穷举测试。

---

## 加一种实现

**不要改别人的代码。** 新建文件加个装饰器，靠 `.env` 切换：

```python
# app/core/providers/zhipu_vlm.py
from app.core.registry import register

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

可注册的类别：`vlm` / `detector` / `layer` / `framesource`。
`GET /v1/health` 会列出所有已注册的实现。

---

## 发一帧

路由收 JSON，`image_ref` 指向服务端已有的图片路径：

```bash
curl -X POST http://127.0.0.1:8000/v1/perception/describe \
  -H 'Content-Type: application/json' \
  -d '{"frame_id":"f1","image_ref":"/tmp/a.jpg","extra":{"index":2}}'
```

想看实时推流，用任意 WebSocket 客户端连 `ws://127.0.0.1:8000/v1/stream`
（连接后会先收到一条 `hello`）。简化版没有自带 WS 探针脚本。

`bash smoke.sh` 会自动起服务并把上面这些路由全打一遍。
