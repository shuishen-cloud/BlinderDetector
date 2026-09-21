"""配置 —— 全部从 .env 读，不填也能跑（全走 mock）。"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# 实现选择（见 app/core/registry.py）
VLM_PROVIDER: str = os.getenv("VLM_PROVIDER", "mock")
DETECTOR: str = os.getenv("DETECTOR", "mock")
FRAME_SOURCE: str = os.getenv("FRAME_SOURCE", "video")

# 云端 VLM（本期不接，仅占位。三家都提供 OpenAI 兼容端点）
VLM_BASE_URL: str = os.getenv("VLM_BASE_URL", "")
VLM_API_KEY: str = os.getenv("VLM_API_KEY", "")
VLM_MODEL: str = os.getenv("VLM_MODEL", "")
VLM_TIMEOUT_MS: int = int(os.getenv("VLM_TIMEOUT_MS", "8000"))

# 第三层：路线数据源（见 app/core/routers/）
#   builtin = 内置假路网，不依赖网络；baidu = 百度地图步行路线规划
# ★ 默认 builtin：演示与离线开发不该被网络拖垮。
#   换成 baidu 需要 BAIDU_AK，且失败会自动降级回 builtin 并如实播报。
ROUTER: str = os.getenv("ROUTER", "builtin")
BAIDU_AK: str = os.getenv("BAIDU_AK", "")
BAIDU_TIMEOUT_MS: int = int(os.getenv("BAIDU_TIMEOUT_MS", "5000"))
#: 开发开关：配了就从这个本地 JSON 读响应代替 HTTP 请求。
#: 没有 AK 也能把「真实响应 → 解析 → 警告 → 降级」整条链跑通。
BAIDU_FIXTURE: str = os.getenv("BAIDU_FIXTURE", "")

# 浏览器端 AK —— 给调试台的地图用（百度 JSAPI GL）。
# ★ 与服务端 AK 是**两个不同的东西**：类型不同（浏览器端 vs 服务端）、
#   要开的服务也不同（JavaScript API GL vs 步行路线规划（轻量）），不能混用。
# 由 `GET /v1/frontend-config` 下发给前端，**不写进 web/ 里的文件**
# —— 那是托管目录，写死等于提交进仓库。
BAIDU_BROWSER_AK: str = os.getenv("BAIDU_BROWSER_AK", "")

# 服务
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

# 跨源。默认 * 只适合开发 —— 前端由本服务同源托管时其实用不到，
# 但允许 file:// 直接打开调试台，以及将来 App 端跨源访问。
CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "*")

# 素材路径
FRAMES_DIR: str = os.getenv("FRAMES_DIR", "data/frames")
