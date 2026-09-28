# 灵眸伴途 —— 应用镜像
#
# ★ 单进程跑，**故意不加 `--workers`**：紧急状态机与 `arbiter` 的「已发 id」
#   都在进程内存里，多 worker 会把状态劈成互不相干的好几份 —— 客户端连到
#   哪个 worker 就只看到哪一份，而且不会报错，只是行为变得诡异。
#   要扩容得先把状态外置，不是加个参数的事。
#
# ★ HTTPS 不在这里做，交给前面的 nginx（见 deploy/nginx.conf）。但它是
#   **硬要求**：浏览器只在 secure context 下给 `getUserMedia`，站点跑在
#   明文 HTTP 上时麦克风和摄像头直接被拒 —— 而语音是这个项目的入口。
#   这个容器只说 HTTP，只该被 nginx 访问。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# 依赖单独一层：改代码不会触发 pip 重装。
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

COPY app/ ./app/
COPY web/ ./web/

# ★ 只带得走仓库里那两个文件（baidu_walking_sample.json / main_gui.png）。
#   演示素材 data/demo.mp4 与 data/frames/*.png 被 .gitignore 排除，
#   服务器上要另外拷进来 —— 见 deploy/README.md。
COPY data/ ./data/

# 不用 root 跑。uploads/ 是唯一需要写权限的目录；app/main.py 启动时
# 自己也会 mkdir（parents=True, exist_ok=True），这里只是让它先有个属主。
RUN mkdir -p data/uploads \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /srv
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=2).status == 200 else 1)"

# --proxy-headers：让 request.client / request.url.scheme 认 nginx 传来的
#   X-Forwarded-*。少了它，日志里的来源 IP 会全是 nginx 容器那一个。
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-server-header"]
