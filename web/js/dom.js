/* DOM 小工具与常量。
 *
 * 页面分两层，两层的元素**都在 DOM 里**（测试件是 `hidden` 而不是删掉），
 * 所以按 id 取用一律不判空 —— 见 index.html 头注释。
 */

export const $ = (id) => document.getElementById(id);

/** 需要用户**动作**的层 —— 只有它值得抢屏，见 ui.js 的 receive()。 */
export const SOURCE_EMERGENCY = "emergency";

/** 系统自己说的话（连接状态、降级告知）。★ 服务端也会发这个来源，
 *  前端本地产生的播报（见 ui.js::localNote）用同一个 —— 对用户来说
 *  「系统在说话」只有一件事，不该分成两个来源名。 */
export const SOURCE_SYSTEM = "system";

export const SOURCE_NAMES = {
  perception: "感知",
  safety: "安全",
  navigation: "导航",
  // ★ 不叫「紧急」：priority=3 那条优先级标签已经是「紧急」了，两枚并排
  //   会连成「紧急 紧急」——看起来像页面出错，读屏也会念两遍同一个词。
  //   这一栏是**来源**（第四层：紧急求助链路），用产品自己的词「求助」。
  emergency: "求助",
  system: "系统",
};

/** 播报流空态。与 index.html 里的那份必须一致 —— 清空后重新插的就是它。 */
export const EMPTY_HTML = '<div class="empty">' +
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6.5 9H3v6h3.5L11 19zM15.5 8.5a5 5 0 0 1 0 7" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>' +
  '<p>还没有播报</p><p class="dim">系统出声后，这里按时间倒序列出来</p></div>';

/** 从一组输入框里读坐标，格式约定 `{"lat","lng"}`，坐标系 WGS-84。
 *
 *  填不全就返回 null —— **绝不拿半个坐标去规划**。
 *
 *  ★ 放在这里而不是 dev.js：map.js 也要用它（画那条「输入框与路线起点
 *    有偏差」的提示虚线）。两边各写一份，等于把「字段 id 叫什么、怎么算
 *    合法」这个约定复制成两份 —— 改了一处另一处会静默读错。放在 dom.js
 *    这个共同依赖里，两边 import 同一个，且不会形成循环依赖。 */
export function readGeo(prefix) {
  const lat = parseFloat($(`${prefix}-lat`).value);
  const lng = parseFloat($(`${prefix}-lng`).value);
  return Number.isFinite(lat) && Number.isFinite(lng) ? { lat, lng } : null;
}
