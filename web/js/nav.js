/* 导航 —— 目的地与起点。第三层**唯一**的一条提交路径。
 *
 * ★ 为什么单独一个模块：目的地有**两条**输入方式（按住说话 / 打字），而请求
 *   只能有一条。原先这条路挂在「开始导航」那个 button 上，语音那边靠
 *   `button[data-dest].click()` 去按它 —— 等于说「按钮」是这条路上不可删的
 *   一环。可那个按钮对用户已经完全多余了：语音是**松开就出发**，打字走回车。
 *   把路抽成一个函数，两条输入都调它，按钮就可以删掉了（少一个要用户找的东西）。
 *
 * ★ 起点 = 当前位置，自动取，不是按钮。
 *   这一页服务的是**正在走的人**，起点就是「他此刻在哪」，没有第二种可能。
 *   原来默认写死一组太原工业学院的坐标、旁边放一个「用当前位置」——
 *   把一个本来确定的事实变成了要用户去按一下的选项（还得先意识到要按）。
 *   现在：进页面就定位；拿不到就**如实说拿不到**并把默认值摆明（`.hint.warn`），
 *   而不是默默用一组假坐标规划出一条煞有介事的路线。
 */

import { $, readGeo } from "./dom.js";
import { log } from "./log.js";
import { postJson } from "./net.js";
import { confirmAction, localNote } from "./ui.js";

/** 把当前目的地提交到第三层 —— 打字（回车）和语音（松开）共用这一个出口。 */
export async function submitRoute() {
  const destination = $("dest").value.trim();
  const destGeo = readGeo("dest-geo");
  // ★ 目的地得有个着落：或者有名字（后端地理编码），或者有坐标。两个都没有
  //   就不要发请求 —— 让用户空跑一趟再听一句「没有拿到可播报的路线」更费解。
  if (!destination && !destGeo) {
    confirmAction("先说或输入一个目的地", "err");
    return false;
  }

  const extra = { destination };
  const origin = readGeo("geo");
  // 起点坐标由 initOrigin 填；读不到就不发（后端会用内置路网并如实播报）。
  if (origin) extra.geo = origin;
  if (destGeo) extra.destination_geo = destGeo;

  try {
    const g = await postJson("/v1/navigation/route", {
      frame_id: `ui_${Date.now()}`,
      ts: Date.now(),
      extra,
    });
    log(`POST /v1/navigation/route → 产出 ${g.produced} 条，放行 ${g.passed} 条`,
        g.produced ? "ok" : "dim");
    confirmAction(g.produced ? "路线已下发，播报马上到" : "没有拿到可播报的路线",
                  g.produced ? "ok" : "");
    return true;
  } catch (e) {
    log(`POST /v1/navigation/route 失败：${e.message}`, "err");
    confirmAction(`发送失败：${e.message}`, "err");
    return false;
  }
}

/** 起点状态写在这行字上（它同时是给读屏的状态区）。 */
function paintOrigin(accuracy, why) {
  const note = $("originNote");
  if (why) {
    // ★ 降级必须看得见、也读得出：这行字是「你听到的路线是从哪出发的」的唯一线索。
    note.textContent = `起点：默认坐标（${why}）—— 可在「起终点坐标」里手动改`;
    note.classList.add("warn");
    return;
  }
  note.textContent = `起点：当前位置（精度约 ${Math.round(accuracy)} 米）`;
  note.classList.remove("warn");
}

/** 取当前位置。`manual` = 用户按了「重新定位」，这时要出声确认。 */
function locate({ manual }) {
  const btn = $("geoBtn");
  const reset = () => { btn.disabled = false; btn.textContent = "重新定位"; };

  if (!navigator.geolocation) {
    paintOrigin(null, "这个浏览器不提供定位");
    if (manual) confirmAction("这个浏览器不提供定位", "err");
    return;
  }
  btn.disabled = true;
  if (manual) btn.textContent = "定位中…";

  navigator.geolocation.getCurrentPosition(
    (pos) => {
      // ★ 浏览器给的就是 WGS-84 —— 与服务端 AK 的 coord_type=wgs84 对齐。
      //   本项目**不做** WGS-84 → BD-09 的转换（见 app/core/routers/base.py）。
      $("geo-lat").value = pos.coords.latitude.toFixed(6);
      $("geo-lng").value = pos.coords.longitude.toFixed(6);
      paintOrigin(pos.coords.accuracy, null);
      if (manual) {
        confirmAction(`起点已更新（精度约 ${Math.round(pos.coords.accuracy)} 米）`, "ok");
      }
      reset();
    },
    (err) => {
      // 定位只在 https 或 localhost 下可用 —— 局域网 IP 直连会被浏览器拒绝。
      const why = err.code === 1 ? "没给定位权限" : (err.message || `错误 ${err.code}`);
      paintOrigin(null, why);
      if (manual) {
        confirmAction(`定位失败：${why}（需要 HTTPS 或 localhost）`, "err");
      } else {
        // ★ 自动定位失败**必须出声**（2026-09-23）。进页面那次拿不到位置时
        //   没人按过任何按钮，所以没有任何 confirmAction 会响 —— 不说的话
        //   用户听到的路线是从一组写死的坐标算出来的，而他毫不知情。
        //   「起点不对」和「路没有」一样属于不能沉默的降级。
        localNote(
          `起点定位失败：${why}。这次按默认坐标规划，可能不是你此刻的位置。`,
          { priority: 2, hapticKind: "double" },
        );
      }
      reset();
    },
    { enableHighAccuracy: true, timeout: 8000, maximumAge: 30_000 },
  );
}

export function initOrigin() {
  $("geoBtn").onclick = () => locate({ manual: true });

  // ★ 打字那条路：回车即出发（手机软键盘上的「前往」键就是回车）。
  //   原来靠「开始导航」那个按钮 —— 现在两条输入方式都直通 submitRoute()。
  $("dest").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    submitRoute();
  });

  // 进页面就定位，不等人按 —— 起点是「此刻在哪」，不是一道选择题。
  locate({ manual: false });
}
