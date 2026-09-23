/* 请求日志 —— 写开发者面板。
 *
 * ★ 单独一个文件，而不是放进 dev.js：**产品逻辑也要记日志**
 *   （WS 连上、上传被闸门丢弃……）。放进 dev.js 会让 ui.js / net.js
 *   反向依赖调试模块，那条界线就白划了。
 *   而它写的是开发者面板里的元素，所以本质上仍是调试设施 —— 面板不在
 *   （被裁掉）时安全退化为 no-op。
 */

import { $ } from "./dom.js";

let lines = 0;

export function log(line, kind = "") {
  const el = $("log");
  if (!el) return;

  const row = document.createElement("div");
  if (kind) row.className = `l-${kind}`;
  row.textContent = `${new Date().toLocaleTimeString()} ${line}`;
  el.prepend(row);

  while (el.children.length > 300) el.lastElementChild.remove();
  const tail = $("logTail");
  if (tail) tail.textContent = `${++lines} 条`;
}
