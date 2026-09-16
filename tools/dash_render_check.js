/* 在 Node 里跑看板的真实渲染函数，确认短线精灵能出正确的行。
 *
 * 不引入 jsdom（装依赖没必要）：只搭一个够用的极小 DOM 替身，
 * 把 dashboard.html 里那段 <script> 原样 eval 出来，然后直接调
 * pushSpirit / spiritRow，检查生成的 HTML 字符串与去重行为。
 *
 * 跑法：node tools/dash_render_check.js
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(
  path.join(__dirname, "..", "src", "arad", "server", "dashboard.html"), "utf8");
const m = html.match(/<script[^>]*>([\s\S]*?)<\/script>/);
if (!m) { console.error("找不到 <script> 块"); process.exit(1); }

// ---- 极小 DOM 替身 --------------------------------------------------------
function mkEl(tag) {
  const el = {
    tagName: (tag || "div").toUpperCase(),
    children: [], childNodes: [], style: {}, dataset: {},
    _text: "", _html: "", className: "", title: "",
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
    },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this.children = []; },
    appendChild(c) { this.children.push(c); return c; },
    insertBefore(c) { this.children.unshift(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      return c;
    },
    remove() {},
    querySelector(sel) {
      /* 看板会 querySelector("thead")/("tbody")/(".ack") 之类；
         给一个同 tag 的替身就够，我们只关心精灵面板。 */
      const key = "_q_" + sel;
      if (!this[key]) this[key] = mkEl(sel.replace(/^[.#]/, ""));
      return this[key];
    },
    querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; },
    addEventListener() {},
    get firstChild() { return this.children[0] || null; },
    get lastChild() { return this.children[this.children.length - 1] || null; },
  };
  return el;
}

const REG = {};
const IDS = ["spiritList", "spiritMeta", "spiritBody", "spFilters", "alertList",
             "alertMeta", "alertBody", "filters", "quotesBody", "wlBody",
             "quotesTable", "wlTable", "statLine", "clock", "conn", "toasts"];
IDS.forEach((id) => { REG[id] = mkEl("div"); });

const doc = {
  createElement: mkEl,
  getElementById: (id) => REG[id] || (REG[id] = mkEl("div")),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  body: mkEl("body"),
  documentElement: mkEl("html"),
  readyState: "complete",
};

const sandbox = {
  document: doc, window: {}, console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  fetch: () => Promise.reject(new Error("no network in check")),
  EventSource: function () { this.addEventListener = () => {}; this.close = () => {}; },
  location: { href: "http://127.0.0.1:8899/", search: "", protocol: "http:" },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: { userAgent: "node" },
  requestAnimationFrame: (f) => setTimeout(f, 0),
  Notification: undefined,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

let failures = 0;
function check(label, cond, extra) {
  console.log(`  ${cond ? "✓" : "✗"} ${label}${extra ? "  " + extra : ""}`);
  if (!cond) failures++;
}

// ---- 执行看板脚本 ---------------------------------------------------------
// 看板脚本整体包在 IIFE 里（"use strict"; (function(){ ... })();），
// 函数不外泄到全局。测试时在 IIFE 收尾前插一段导出，只影响这份被测副本，
// 不改动 dashboard.html 本身。
const EXPORT_HOOK = `
;globalThis.__dash = {
  spiritRow: spiritRow, pushSpirit: pushSpirit, spiritMatches: spiritMatches,
  rerenderSpirit: rerenderSpirit, renderSpiritFilters: renderSpiritFilters,
  S: S, MAX_SPIRIT: MAX_SPIRIT
};
`;
const src = m[1].replace(/\}\)\(\);\s*$/, EXPORT_HOOK + "})();");
if (src === m[1]) {
  console.error("没能在 IIFE 收尾处插入导出钩子（dashboard.html 结构变了？）");
  process.exit(1);
}

try {
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: "dashboard.html:script" });
} catch (e) {
  console.error("执行看板脚本失败:", e.message);
  process.exit(1);
}
console.log("--- 脚本已执行（无异常）---");

const API = sandbox.__dash;
if (!API) { console.error("导出钩子没生效"); process.exit(1); }
const F = API.spiritRow, P = API.pushSpirit;
check("spiritRow 已定义", typeof F === "function");
check("pushSpirit 已定义", typeof P === "function");
if (typeof F !== "function") process.exit(1);

// ---- 用真实 /api/spirit 形状的数据渲染 ------------------------------------
const SAMPLES = [
  { key: "k1", ts: "2026-09-15T09:34:00", code: "600519", name: "贵州茅台",
    signal: "rocket", cn: "火箭发射", dir: "up", group: "price",
    price: 1800.5, pct: 3.21, severity: 2, hint: "快速上涨并创当日新高" },
  { key: "k2", ts: "2026-09-15T09:35:00", code: "300750", name: "宁德时代",
    signal: "open_limit_up", cn: "打开涨停", dir: "down", group: "limit",
    price: 58.2, pct: 16.4, severity: 3 },
  { key: "k3", ts: "2026-09-15T09:36:00", code: "sh000001", name: "上证指数",
    signal: "index_pull", cn: "拉升指数", dir: "up", group: "index",
    price: 3895.0, pct: 0.79, severity: 1 },
];

const rows = SAMPLES.map((a) => F(a));
check("行数 = 样本数", rows.length === 3);

const [up, down, idx] = rows.map((r) => r.innerHTML);
check("火箭发射行含中文信号名", up.includes("火箭发射"));
check("火箭发射方向为 up（红）", rows[0].className.includes("up"));
check("打开涨停方向为 down（绿）——最易搞反的一个", rows[1].className.includes("down"),
      rows[1].className);
check("打开涨停行含中文名", down.includes("打开涨停"));
check("指数行保留带前缀代码", idx.includes("sh000001"));
check("行内不含未转义脚本", !up.includes("<script"));

// severity -> class
check("severity 3 落到 sev3", rows[1].className.includes("sev3"), rows[1].className);
check("severity 1 落到 sev1", rows[2].className.includes("sev1"), rows[2].className);

// ---- 去重 ----------------------------------------------------------------
const before = REG.spiritList.children.length;
P(SAMPLES[0]);
const afterFirst = REG.spiritList.children.length;
P(SAMPLES[0]);   // 同 key 再推一次
const afterDup = REG.spiritList.children.length;
check("新 key 会插入一行", afterFirst === before + 1, `${before} -> ${afterFirst}`);
check("重复 key 被去重（SSE 重连会重放）", afterDup === afterFirst, `${afterFirst} -> ${afterDup}`);

// ---- 分组过滤 ------------------------------------------------------------
if (typeof API.spiritMatches === "function") {
  API.S.spGroup = "limit";
  check("筛选 limit 时只有打开涨停匹配", API.spiritMatches(SAMPLES[1]) &&
        !API.spiritMatches(SAMPLES[0]));
  API.S.spGroup = "all";
  check("筛选 all 时全部匹配", SAMPLES.every(API.spiritMatches));
} else {
  check("spiritMatches 已定义", false);
}

// ---- DOM 上限 ------------------------------------------------------------
if (typeof P === "function") {
  const N = API.MAX_SPIRIT + 25;
  API.S.spirit.length = 0;
  API.S.spSeen = {};
  REG.spiritList.children.length = 0;
  for (let i = 0; i < N; i++) {
    P(Object.assign({}, SAMPLES[0], { key: "bulk" + i }));
  }
  const domRows = REG.spiritList.children.length;
  check("长会话下 DOM 行数被 MAX_SPIRIT 截住", domRows <= API.MAX_SPIRIT,
        `${N} 次推送 -> ${domRows} 行（上限 ${API.MAX_SPIRIT}）`);
  check("内存列表同样被截住", API.S.spirit.length <= API.MAX_SPIRIT,
        `${API.S.spirit.length} 条`);
}

console.log("\n" + "=".repeat(50));
if (failures) { console.log(`✗ ${failures} 项未通过`); process.exit(1); }
console.log("✓ 短线精灵前端渲染正常");
/* 看板脚本里有 setInterval（轮询时钟/榜单），会拖住 Node 的事件循环，
   这里显式收尾，否则进程永不退出。 */
process.exit(0);
