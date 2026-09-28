# 部署

`docker compose up -d --build` 之前，有五件事必须先做。**第 1 件不做，服务就是不可用的**。

## 1. 域名 + HTTPS（唯一的硬前置）

浏览器只在 secure context 下暴露 `getUserMedia`，明文 HTTP 上麦克风和摄像头会被直接拒掉。
语音是这个项目的入口，所以**没有 HTTPS 就没有服务** —— 不是体验差一点。

```bash
# 证书走 webroot，用 nginx 已经挂出来的那个目录
certbot certonly --webroot -w ./deploy/certbot-www -d your.domain

sudo cp /etc/letsencrypt/live/your.domain/fullchain.pem deploy/certs/
sudo cp /etc/letsencrypt/live/your.domain/privkey.pem   deploy/certs/
```

`deploy/certs/` 已在 `.gitignore` 里，**私钥绝不要提交**。只想先试通链路的话，自签一张也行，
浏览器会警告，但 secure context 成立、麦克风可用：

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout deploy/certs/privkey.pem -out deploy/certs/fullchain.pem -subj "/CN=your.domain"
```

## 2. 密钥

```bash
cp .env.example .env && chmod 600 .env
```

要填的：`VLM_API_KEY`（dashscope，VLM 与 ASR 共用这一把）、
`VLM_PROVIDER=dashscope`、`DETECTOR=qwen_vl`、`ASR=dashscope`。
路线想走真实地图再加 `ROUTER=baidu` + `BAIDU_AK`。

★ **`CORS_ORIGINS` 必须从 `*` 改成你的域名**。前端由本服务同源托管，本来就用不到 CORS，
留着 `*` 只是让任何站点都能带着用户的浏览器打你的接口。

## 3. 演示素材

`data/demo.mp4` 与 `data/frames/*.png` 被 `.gitignore` 排除，**服务器上 clone 下来是没有的**。
不拷的话 `web/sender.html` 里的 `<video src="/data/demo.mp4">` 是 404，
「跑一遍测试素材」那条路直接断掉。

```bash
scp demo.mp4 server:.../BlinderDetector/data/
scp -r frames server:.../BlinderDetector/data/
```

`data/` 整个目录会被挂进容器（`./data:/srv/data`），拷进去就生效。

## 4. 起服务

```bash
docker compose up -d --build
docker compose logs -f app
```

## 5. 验一遍

```bash
curl -s https://your.domain/v1/health     # {"status": ...}，降级状态也在这
```

然后用**手机**打开 `https://your.domain/`：地址栏不该有「不安全」，
按住说话不该提示「语音输入需要 HTTPS 或 localhost」。这两条任一不过，就是第 1 步没做对。

## 上线前还该知道的

- **只能单进程**。紧急状态机与 `arbiter` 的已发 id 都在进程内存里，`--workers >1`
  会把状态劈成几份且不报错。扩容要先做状态外置。
- **没有任何鉴权**。所有路由匿名可调用，而每次感知/识别都在花你的 dashscope 额度。
  公网部署至少要加一层网关鉴权或限流。
- **`web/js/voice.js` 的 `DEMO_FALLBACK_DEST` 必须留空**（默认已是 `""`）。
  填上它等于替用户编一个目的地，那是拿假地名给盲人导航 —— 只该在演示时短暂打开。
