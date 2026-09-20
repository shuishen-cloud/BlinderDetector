#!/usr/bin/env python3
"""生成测试素材：assets/*.svg -> data/frames/*.png -> data/demo.mp4

    python scripts/make_test_video.py
    python scripts/make_test_video.py out.mp4 --hold 3

和 `make_test_video.sh` 做的是同一件事，但**不依赖 Unix 工具链**：
.sh 里自动装依赖那几行写的是 `pkg install`，只有 Termux 认得，
Windows 上直接跑会失败。本脚本按可用性自动挑工具：

    SVG -> PNG   rsvg-convert（Termux / Linux）或 Chrome/Edge headless（桌面）
    PNG -> MP4   系统 ffmpeg，或 imageio-ffmpeg 自带的静态二进制

矢量素材是手写的，改 assets/*.svg 就能改测试场景。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ★ Windows 控制台默认 GBK，中文提示会撑爆 UnicodeEncodeError。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.sources.video import ffmpeg_exe  # noqa: E402

#: 与 assets/*.svg 的 viewBox 一致
W, H = 640, 480


def find_browser() -> str | None:
    """桌面端渲染 SVG 的退路 —— 浏览器 headless 截图，零安装。"""
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        exe = shutil.which(name)
        if exe:
            return exe

    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates = []

    for c in candidates:
        if Path(c).is_file():
            return c
    return None


def render_png(svg: Path, out: Path) -> str:
    """SVG -> PNG（640x480）。返回实际用的渲染器名字。"""
    out.parent.mkdir(parents=True, exist_ok=True)

    rsvg = shutil.which("rsvg-convert")
    if rsvg:
        subprocess.run(
            [rsvg, "-w", str(W), "-h", str(H), str(svg), "-o", str(out)],
            check=True,
        )
        return "rsvg-convert"

    browser = find_browser()
    if browser is None:
        raise RuntimeError(
            "没有可用的 SVG 渲染器：装 librsvg（Termux: pkg install librsvg）"
            "或 Chrome / Edge / Chromium"
        )
    subprocess.run(
        [
            browser,
            "--headless", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", f"--window-size={W},{H}",
            # 等渲染完成再截图，否则偶发空图
            "--virtual-time-budget=2000",
            f"--screenshot={out.resolve()}",
            svg.resolve().as_uri(),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Chrome 截图失败时是静默的（退出码仍然 0），必须自己验产物
    if not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError(f"截图没落盘: {svg} -> {out}")
    return "Chrome/Edge headless"


def encode(pngs: list[Path], out: Path, hold: float) -> None:
    """PNG 序列 -> MP4。参数和 make_test_video.sh 保持一致。"""
    exe = ffmpeg_exe()
    if exe is None:
        raise RuntimeError("需要 ffmpeg：装系统 ffmpeg，或 pip install imageio-ffmpeg")

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        listfile = Path(fh.name)
        for p in pngs:
            fh.write(f"file '{p.resolve().as_posix()}'\n")
            fh.write(f"duration {hold:g}\n")
        # concat demuxer 要求末帧再写一遍，否则最后一帧被吞
        fh.write(f"file '{pngs[-1].resolve().as_posix()}'\n")

    try:
        subprocess.run(
            [
                exe, "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(listfile),
                "-vf", "fps=1,format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast",
                str(out),
            ],
            check=True,
        )
    finally:
        listfile.unlink(missing_ok=True)


def main() -> int:
    p = argparse.ArgumentParser(description="生成测试素材（跨平台）")
    p.add_argument("out", nargs="?", default="data/demo.mp4", help="输出视频路径")
    p.add_argument("--hold", type=float, default=2.0, help="每张图停留秒数")
    args = p.parse_args()

    svgs = sorted((ROOT / "assets").glob("*.svg"))
    if not svgs:
        print(f"没有素材: {ROOT / 'assets'}/*.svg", file=sys.stderr)
        return 1

    frames_dir = ROOT / "data" / "frames"
    print("==> 转换矢量素材")
    renderer = ""
    for svg in svgs:
        out = frames_dir / f"{svg.stem}.png"
        renderer = render_png(svg, out)
        print(f"    {svg.relative_to(ROOT).as_posix():<32} -> "
              f"{out.relative_to(ROOT).as_posix()}")
    print(f"    （渲染器: {renderer}）")

    pngs = sorted(frames_dir.glob("*.png"))
    print(f"==> 拼接视频（每张停留 {args.hold:g}s，共 {len(pngs)} 张）")
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    encode(pngs, out, args.hold)

    print(f"==> 完成: {out}  ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
