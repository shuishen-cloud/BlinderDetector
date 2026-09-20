"""仓库内的固定位置。

单独成一个模块，是为了让 `app/api/*` 和 `app/main.py` 都能取用，
又不必互相 `import`（main 装配 api；api 再回头 import main 就成环了）。

路径一律按**本文件的位置**往上推，不看启动时的 cwd —— 否则从别的目录
`uvicorn app.main:app` 会找不到前端和测试素材。
"""

from __future__ import annotations

from pathlib import Path

#: 仓库根目录。
BASE_DIR = Path(__file__).resolve().parent.parent

#: 前端调试台：`index.html` + `app.css` + `app.js`。
WEB_DIR = BASE_DIR / "web"

#: 测试素材（demo.mp4 / frames）。`GET /data/*` 挂的就是它。
DATA_DIR = BASE_DIR / "data"

#: 上传帧的临时落盘目录。
#: `image_ref` 的语义是「服务端路径」（`layers/perception.py` 用
#: `os.path.isfile` 找它），所以上传的字节必须先落地，下游才读得到。
UPLOAD_DIR = DATA_DIR / "uploads"
