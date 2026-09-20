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

# 服务
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

# 跨源。默认 * 只适合开发 —— 前端由本服务同源托管时其实用不到，
# 但允许 file:// 直接打开调试台，以及将来 App 端跨源访问。
CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "*")

# 素材路径
FRAMES_DIR: str = os.getenv("FRAMES_DIR", "data/frames")
