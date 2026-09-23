/* 启动自检 —— **页面唯一一段 classic 脚本**，必须比 module 入口先跑。
 *
 * 起因（2026-09-23）：实测碰到一种最难查的失效 —— 入口（或它 import 的
 * 任何一个模块）只要有一处没跑起来，**整个模块图都不执行**，页面就停在
 * 初始文案上：「连接中…」「检查中」「准备中…」。
 * 它看起来和「网络慢」一模一样，实际上是一行 JS 都没跑，
 * **除了开发者控制台没有任何提示** —— 正是这个项目最不能接受的那种沉默。
 *
 * ★ 为什么不能用 module 写：module 图的失败恰恰会阻止 module 执行，
 *   自己报不了自己。所以必须是 classic，且必须排在入口之前
 *   （classic 脚本是解析阻塞的，module 是延迟的，顺序天然成立）。
 *
 * ★ 判据是「入口有没有**报到**」，不是「有没有报错」：
 *   入口跑完装配会派发一个 `lm:booted` 事件（见 js/main.js 末尾）。
 *   超过 BOOT_TIMEOUT_MS 还没收到，就按「没起来」处理。
 *
 *   ★ 为什么不能一有错误就喊：这一页会加载**第三方脚本**（百度 JSAPI GL
 *     从 CDN 拉），它自己天天在抛未捕获异常，而跨域脚本的报错会被浏览器
 *     抹成光秃秃的 `Script error.` —— 那和「页面坏了」毫无关系。
 *     误报比不报更糟：用户会学会无视这条横幅。所以：
 *       · 跨域的（`Script error.`）—— 忽略；
 *       · 自己脚本的错误 —— 记下来，等超时时一并显示。
 *
 * ★ 它的职责只有一条：**别让页面沉默**。
 *   不碰业务逻辑、不定义全局、不做任何降级 —— 只把第一个错误写到页面上。
 */
(function () {
  "use strict";

  //: 入口报到的时间上限。模块图是本地文件，正常在几十毫秒内就跑完装配；
  //: 3 秒还没报到，就是真没起来。
  var BOOT_TIMEOUT_MS = 3000;

  var booted = false;
  var shown = false;
  var detail = "";      // 自己脚本抛出的第一个错误（跨域的不算，见文件头）

  //: 这几类报错几乎只有一个原因：浏览器留着**旧版本的前端文件**。
  //: ★ 实测（2026-09-23）：`map.js` 由 classic 脚本改成 ES module 之后，
  //:   旧缓存里的那份没有 `export default`，于是入口直接抛
  //:   「does not provide an export named 'default'」——
  //:   光看这句话没人猜得到该按 Ctrl+Shift+R。
  var STALE_CACHE = /export named|Failed to fetch dynamically imported|Unexpected token|not a valid JavaScript MIME/i;

  //: 浏览器对**跨域**脚本的未捕获异常只给这一句，拿不到栈也拿不到出处。
  var REDACTED = "Script error.";

  function hint(msg) {
    return STALE_CACHE.test(msg)
      ? "\n（多半是浏览器缓存了旧版本的前端文件 —— 按 Ctrl+Shift+R / Cmd+Shift+R 强制刷新一次）"
      : "";
  }

  function show(msg) {
    if (shown) return;          // 只喊一次
    shown = true;

    var box = document.getElementById("bootErr");
    if (!box) {
      if (!document.body) return;
      box = document.createElement("div");
      box.id = "bootErr";
      box.setAttribute("role", "alert");
      document.body.appendChild(box);
    }
    box.textContent = "页面没能启动：" + msg + hint(msg);

    // 顺手把状态位也改成实话 —— 用户看的就是那里（「连接中…」是骗人的，
    // 因为连都没开始连）。
    var ws = document.getElementById("wsTxt");
    if (ws) ws.textContent = "启动失败";
    var pill = document.getElementById("wsPill");
    if (pill) pill.classList.add("bad");
    document.title = "启动失败 · " + document.title;
  }

  document.addEventListener("lm:booted", function () { booted = true; });

  // 捕获阶段监听：资源加载失败（module 取不到 / 解析失败）不会冒泡，
  // 但在 window 上用 capture 能收到派发在 <script> 上的那个 error 事件。
  window.addEventListener("error", function (e) {
    if (booted) return;

    var el = e.target;
    if (el && el !== window && (el.src || el.href)) {
      var url = el.src || el.href;
      // 只看自己域下的资源；CDN 上那个（百度）失败了页面照常工作
      if (url.indexOf(location.origin) === 0) {
        detail = detail || ("取不到 " + url);
        show(detail);
      }
      return;
    }
    if (!e.message || e.message === REDACTED) return;      // 跨域脚本，忽略
    if (e.filename && e.filename.indexOf(location.origin) !== 0) return;
    detail = detail || e.message;
    show(detail);
  }, true);

  window.addEventListener("unhandledrejection", function (e) {
    if (booted) return;
    var r = e.reason;
    detail = detail || ((r && (r.stack || r.message)) || String(r));
    show(detail);
  });

  // 兜底：没有报错、但入口就是没跑起来（比如模块被缓存成了旧版本、
  // 或者某个 import 静默失败）。这时候必须有话说。
  window.setTimeout(function () {
    if (booted || shown) return;
    show(detail || "入口没有跑起来（原因见控制台）");
  }, BOOT_TIMEOUT_MS);
})();
