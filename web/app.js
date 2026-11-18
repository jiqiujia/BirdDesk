"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtN = (n) => Number(n || 0).toLocaleString("zh-CN");
const fmtBytes = (b) => {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i >= 3 ? 2 : i ? 1 : 0)} ${u[i]}`;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  if (!r.ok) {
    let m = r.statusText;
    try { m = (await r.json()).detail || m; } catch (_) { /* ignore */ }
    throw new Error(m);
  }
  return r.json();
}

let ENV = null;
let LIB = null;
let PLAN = null;
let FILTER = "all";
let SORT = "time";
let SPECIES_SEL = "";        // 鸟种筛选（空 = 全部）
let SEL_MODE = false;
let SEL = new Set();         // 选中的照片 id（连拍按整组加入）
let SEL_LAST = null;         // shift 连选的锚点（卡片 DOM 索引）
let BURSTS = {};
let BUSY = false;          // 有阻塞任务（扫描/整理/撤销）时暂停轮询
const expanded = new Set();

const GRADE = {
  sharp: { label: "合焦", cls: "g-sharp", sub: "鸟体最锐区域细节锐利（黄圈为鸟体最锐区域，非眼睛；Z 优先查看稳定鸟眼）" },
  soft: { label: "偏软", cls: "g-soft", sub: "细节略软：轻微失焦、轻微抖动或大气扰动" },
  blur: { label: "失焦", cls: "g-blur", sub: "鸟体整体模糊，没有合焦" },
  missed: { label: "跑焦", cls: "g-missed", sub: "鸟比周围背景更模糊，焦点可能落在了前景或背景上" },
  motion: { label: "运动模糊", cls: "g-motion", sub: "细节沿一个方向被拖开：快门不够快，或鸟在快速运动" },
  nobird: { label: "无鸟", cls: "g-nobird", sub: "没有检测到鸟（太小、遮挡严重，或画面里没有鸟）" },
  error: { label: "读取失败", cls: "g-error", sub: "这张照片无法读取" },
};
const BAD = new Set(["blur", "missed", "motion"]);
const FILTERS = [
  ["all", "全部"], ["sharp", "合焦"], ["soft", "偏软"], ["bad", "失焦 / 跑焦 / 模糊"], ["nobird", "无鸟"],
  ["best", "每组最佳"], ["starred", "已打星"], ["rejected", "已淘汰"], ["pending", "未分析"],
];

// ------------------------------------------------------------------ toast / ask / progress
let toastTimer = null;
function toast(msg, isErr = false, ms = 3200) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (isErr ? " err" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), ms);
}
function ask(title, html, okText = "确定", danger = false) {
  return new Promise((resolve) => {
    $("#askTitle").textContent = title;
    $("#askBody").innerHTML = html;
    const ok = $("#askOk"), cancel = $("#askCancel"), m = $("#askModal");
    ok.textContent = okText; ok.className = "btn " + (danger ? "danger" : "primary");
    m.hidden = false;
    const done = (v) => { m.hidden = true; ok.onclick = cancel.onclick = null; resolve(v); };
    ok.onclick = () => done(true); cancel.onclick = () => done(false);
  });
}
async function waitJob(title) {
  const p = $("#progress");
  BUSY = true;
  $("#progTitle").textContent = title; $("#progMsg").textContent = "准备中…";
  $("#progBar").style.width = "0"; p.hidden = false;
  try {
    for (;;) {
      await sleep(250);
      const { job } = await api("/api/status");
      if (!job) continue;
      const bar = $("#progBar").parentElement;
      if (job.total > 0) { bar.classList.remove("indet"); $("#progBar").style.width = `${Math.min(100, (job.done / job.total) * 100)}%`; }
      else bar.classList.add("indet");
      $("#progMsg").textContent = job.message || "";
      if (!job.running) {
        if (job.error) throw new Error(job.error);
        return job;
      }
    }
  } finally { p.hidden = true; BUSY = false; }
}

// ------------------------------------------------------------------ init
async function init() {
  ENV = await api("/api/env");
  const verTag = $(".brand .tag");
  if (verTag && ENV.version) verTag.textContent = "v" + ENV.version;   // 版本号跟着后端走，避免写死过期
  if (!ENV.exiftool) toast("ExifTool 不可用，将无法读取拍摄时间：" + ENV.exiftool_version, true, 8000);
  LIB = await api("/api/library");
  applySettings(LIB.settings);
  bindUI();
  render();
  setInterval(poll, 1200);
}

function applySettings(s) {
  $("#gap").value = Math.min(3, s.gap); $("#gapNum").value = s.gap;
  $("#minBurst").value = s.min_burst; $("#splitCam").checked = s.split_by_camera;
  $("#recursive").checked = s.recursive;
  $("#strictness").value = s.strictness || "normal";
  SORT = s.sort || "time";
  $("#sort").value = SORT;
  $("#optRename").checked = s.rename_conflicts; $("#optCleanup").checked = s.cleanup_empty;
  $("#optReject").checked = !!s.reject_to_folder;
}

async function reloadLibrary(keepScroll = true) {
  LIB = await api("/api/library");
  render(keepScroll);
}

async function pickAndScan() {
  try {
    const { path } = await api("/api/pick_folder", { path: LIB?.root || null });
    if (path) await scanRoot(path);
  } catch (e) { toast(e.message, true); }
}
async function scanRoot(root) {
  try {
    await api("/api/scan", { root, recursive: $("#recursive").checked });
    await waitJob("正在扫描照片");
    LIB = await api("/api/library");
    expanded.clear();
    render();
    $("#content").scrollTop = 0;
    const s = LIB.stats;
    toast(`扫描完成：${fmtN(s.photos)} 张照片${s.analyzed < s.photos && ENV.model ? "，正在后台做合焦分析" : ""}`);
  } catch (e) { toast("扫描失败：" + e.message, true, 6000); }
}

let regroupTimer = null;
function regroupSoon(body) {
  clearTimeout(regroupTimer);
  regroupTimer = setTimeout(async () => {
    try {
      LIB = await api("/api/regroup", body);
      if (LIB.root) render(true);
    } catch (e) { toast(e.message, true); }
  }, 220);
}

function bindUI() {
  $("#btnPick").onclick = pickAndScan;
  $("#btnRescan").onclick = () => LIB.root && scanRoot(LIB.root);
  $("#btnOpenRoot").onclick = () => LIB.root && api("/api/reveal", { path: LIB.root });
  $("#btnPlan").onclick = openPlan;
  $("#btnHistory").onclick = openHistory;

  $("#gap").oninput = (e) => { $("#gapNum").value = e.target.value; regroupSoon({ gap: +e.target.value }); };
  $("#gapNum").onchange = (e) => { const v = Math.max(0.05, +e.target.value || 0.5); $("#gap").value = Math.min(3, v); regroupSoon({ gap: v }); };
  $("#minBurst").onchange = (e) => regroupSoon({ min_burst: Math.max(2, parseInt(e.target.value, 10) || 2) });
  $("#splitCam").onchange = (e) => regroupSoon({ split_by_camera: e.target.checked });
  $("#recursive").onchange = async () => {
    if (LIB.root && await ask("重新扫描？", "“包含子文件夹”的改动需要重新扫描当前目录才会生效。", "重新扫描")) scanRoot(LIB.root);
  };
  $("#sort").onchange = async (e) => {
    SORT = e.target.value;
    render(true);
    try { await api("/api/settings", { sort: SORT }); } catch (err) { toast("排序偏好保存失败：" + err.message, true); }
  };
  $("#strictness").onchange = async (e) => {
    try { LIB = await api("/api/settings", { strictness: e.target.value }); render(true); if (VIEW) showFrame(); }
    catch (err) { toast(err.message, true); }
  };

  $$("[data-close]").forEach((b) => (b.onclick = () => (b.closest(".modal").hidden = true)));
  $$('input[name="tmode"]').forEach((r) => (r.onchange = onTargetMode));
  $("#btnPickTarget").onclick = async () => {
    const { path } = await api("/api/pick_folder", { path: LIB.root });
    if (path) { const t = $("#targetPath"); t.textContent = t.title = t.dataset.v = path; genPlan(); }
  };
  $("#optRename").onchange = genPlan;
  $("#optReject").onchange = genPlan;
  $("#planAck").onchange = updateExecBtn;
  $("#btnExecute").onclick = runPlan;

  $("#speciesSel").onchange = (e) => {
    SPECIES_SEL = e.target.value;
    expanded.clear();
    render();
    $("#content").scrollTop = 0;
  };
  $("#vidFps").onchange = updateVideoHint;
  $("#vidFps").onchange = updateVideoHint;
  $("#vidGo").onclick = makeVideo;
  $("#btnBatch").onclick = openBatch;
  $("#btnSelect").onclick = () => setSelMode(!SEL_MODE);
  $("#btnAirDrop").onclick = () => airDrop(
    Object.keys(LIB.photos).filter((id) => (LIB.photos[id].r || 0) > 0), "已打星照片");
  $("#selAirDrop").onclick = () => airDrop([...SEL], "选中照片");
  $("#selExit").onclick = () => setSelMode(false);
  $("#selAll").onclick = () => { collectIds("filter").forEach((id) => SEL.add(id)); paintSelection(); };
  $("#selNone").onclick = () => { SEL.clear(); SEL_LAST = null; paintSelection(); };
  $("#selReject").onclick = () => applyRatingToSelection(-1);
  $("#selUnreject").onclick = () => applyRatingToSelection(0);
  $("#selDelete").onclick = () => deletePhotos([...SEL], "手动选择");
  $("#btnPurge").onclick = purgeRejected;
  $("#batchScope").onchange = refreshBatch;
  ["#batchDenoise", "#batchSubject", "#batchBg", "#batch16"].forEach((sel) => ($(sel).onchange = refreshBatch));
  $$("#batchPreset button").forEach((b) => (b.onclick = () => {
    BATCH.preset = b.dataset.v;
    $$("#batchPreset button").forEach((x) => x.classList.toggle("on", x === b));
    refreshBatch();
  }));
  $("#batchGo").onclick = runBatch;

  $("#tDev").onclick = () => VIEW && openDevelop(curId());
  $("#vDev").onclick = () => VIEW && openDevelop(curId());
  $("#devExport").onclick = exportDevelop;
  $$("#devPreset button").forEach((b) => (b.onclick = () => {
    DEV.preset = b.dataset.v;
    $$("#devPreset button").forEach((x) => x.classList.toggle("on", x === b));
    renderDevPreview();
  }));
  $$("#devView button").forEach((b) => (b.onclick = () => {
    DEV.view = b.dataset.v;
    $$("#devView button").forEach((x) => x.classList.toggle("on", x === b));
    renderDevPreview();
  }));
  ["#devDenoise", "#devSubject", "#devBg"].forEach((sel) => ($(sel).onchange = renderDevPreview));

  $("#vClose").onclick = closeViewer;
  $("#vPrev").onclick = () => stepViewer(-1);
  $("#vNext").onclick = () => stepViewer(1);
  $("#tBox").onclick = () => toggleOv("box");
  $("#tEye").onclick = () => toggleOv("eye");
  $("#tHeat").onclick = () => toggleOv("heat");
  $("#tZoom").onclick = () => toggleZoom();
  $("#viewerImg").onclick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    zoomAt((e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height);
  };
  $("#vZoom").onclick = () => exitZoom();
  window.addEventListener("resize", () => { if (VIEW) { fitStage(); if (VIEW.zoom) zoomAt(VIEW.zoom.x, VIEW.zoom.y); } });

  document.addEventListener("keydown", onKey);
  $("#content").addEventListener("click", onContentClick);
  $("#content").addEventListener("scroll", onScrollSpy, { passive: true });
}

function onKey(e) {
  if (e.target.matches("input, select, textarea")) return;
  if (!$("#viewer").hidden) {
    const k = e.key.toLowerCase();
    if (e.key === "ArrowLeft") { stepViewer(-1); e.preventDefault(); }
    else if (e.key === "ArrowRight") { stepViewer(1); e.preventDefault(); }
    else if (e.key === "Escape") { if (VIEW?.zoom) exitZoom(); else closeViewer(); }
    else if (k === "b") toggleOv("box");
    else if (k === "e") toggleOv("eye");
    else if (k === "h") toggleOv("heat");
    else if (k === "z") toggleZoom();
    else if (k === "p") openDevelop(curId());
    else if (k === "x") { const p = LIB.photos[curId()]; rate([curId()], p.r === -1 ? 0 : -1); }
    else if (/^[0-5]$/.test(e.key)) rate([curId()], +e.key);
  } else if (e.key === "Escape") {
    if ($$(".modal").some((m) => !m.hidden)) {
      $$(".modal").forEach((m) => { if (m.id !== "askModal") m.hidden = true; });
    } else if (SEL_MODE) setSelMode(false);
  } else if ((e.key === "Delete" || e.key === "Backspace") && SEL_MODE && SEL.size) {
    e.preventDefault();
    deletePhotos([...SEL], "Delete 键");
  } else if (SEL_MODE && (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "a") {
    e.preventDefault();
    collectIds("filter").forEach((id) => SEL.add(id));
    paintSelection();
  }
}

// ------------------------------------------------------------------ incremental updates
async function poll() {
  if (BUSY || !LIB || !LIB.root) { renderPill(null); return; }
  try {
    const u = await api(`/api/updates?since=${LIB.seq || 0}`);
    if (u.version !== LIB.version) { await reloadLibrary(true); renderPill(u.analysis); return; }
    applyUpdates(u);
    renderPill(u.analysis);
  } catch (_) { /* 服务器暂时不可用 */ }
}

let rerenderTimer = null;
function applyUpdates(u) {
  if (!u || !LIB.photos) return;
  const pids = Object.keys(u.photos || {});
  for (const pid of pids) {
    const p = LIB.photos[pid];
    if (!p) continue;
    p.f = u.photos[pid].f; p.r = u.photos[pid].r;
  }
  const bids = Object.keys(u.bursts || {});
  for (const bid of bids) {
    const b = BURSTS[bid];
    if (!b) continue;
    Object.assign(b, u.bursts[bid]);
    if (u.bursts[bid].best) b.cover = u.bursts[bid].best;
  }
  if (u.stats) { LIB.stats = u.stats; renderStats(); renderChips(); updatePurgeBtn(); }
  if (u.seq > (LIB.seq || 0)) LIB.seq = u.seq;
  if (!pids.length && !bids.length) return;
  if (FILTER !== "all") {
    // 筛选模式下，卡片的显隐会变化，节流整页重绘
    if (!rerenderTimer) rerenderTimer = setTimeout(() => { rerenderTimer = null; render(true); }, 1500);
  }
  for (const pid of pids) $$(`[data-pid="${CSS.escape(pid)}"]`).forEach(refreshEl);
  for (const bid of bids) {
    const card = $(`.card.burst[data-bid="${CSS.escape(bid)}"]`);
    if (card) refreshBurstCard(card);
    if (expanded.has(bid) && card) {
      const panel = card.nextElementSibling;
      if (panel?.classList.contains("burst-panel")) { panel.remove(); openBurst(card, false); }
    }
  }
  if (VIEW && pids.includes(curId())) showInfo(curId(), true);
}

function renderPill(a) {
  const el = $("#anaPill");
  if (!a || !LIB?.root || !ENV?.model || !LIB.stats?.photos) { el.hidden = true; return; }
  const s = LIB.stats;
  el.hidden = false;
  if (a.running) {
    const pct = a.total ? (a.done / a.total) * 100 : 0;
    const eta = a.eta ? (a.eta > 90 ? ` · 约 ${Math.round(a.eta / 60)} 分钟` : ` · 约 ${a.eta} 秒`) : "";
    el.innerHTML = a.loading_model
      ? `<span class="spin"></span>正在加载识别模型…`
      : `<span class="spin"></span>合焦分析 ${a.done}/${a.total}${eta}<span class="mini"><div style="width:${pct}%"></div></span><button class="btn" id="anaStop">暂停</button>`;
    const b = $("#anaStop"); if (b) b.onclick = () => api("/api/analysis/stop", {});
  } else if (s.analyzed < s.photos) {
    el.innerHTML = `已分析 ${fmtN(s.analyzed)}/${fmtN(s.photos)}${a.error ? ` · <span style="color:var(--danger)">${esc(a.error)}</span>` : ""}<button class="btn primary" id="anaGo">继续分析</button>`;
    $("#anaGo").onclick = async () => { try { await api("/api/analysis/start", {}); } catch (e) { toast(e.message, true); } };
  } else {
    el.innerHTML = `<span class="dot g-sharp"></span>合焦分析完成 · ${fmtN(s.photos)} 张${a.failed ? ` · ${a.failed} 张失败` : ""}&nbsp;&nbsp;`;
  }
}

// ------------------------------------------------------------------ badges
function fbadge(p) {
  const f = p.f;
  if (!f) return ENV?.model ? `<span class="fbadge pending"><i class="dot g-pending"></i>待分析</span>` : "";
  const g = GRADE[f.g] || GRADE.error;
  const af = f.af === false ? ` <span class="af-off" title="相机对焦点不在鸟身上">⊘</span>` : "";
  return `<span class="fbadge" title="${esc(g.sub)}"><i class="dot ${g.cls}"></i>${g.label}${f.s != null ? ` ${f.s}` : ""}${af}</span>`;
}
function rbadge(p) {
  if (p.r < 0) return `<span class="rbadge rej">淘汰</span>`;
  if (p.r > 0) return `<span class="rbadge">${"★".repeat(p.r)}</span>`;
  return "";
}
function refreshEl(el) {
  const p = LIB.photos[el.dataset.pid];
  if (!p) return;
  el.querySelector(".fbadge")?.remove();
  el.querySelector(".rbadge")?.remove();
  const host = el.classList.contains("frame") ? el : el.querySelector(".ph");
  host.insertAdjacentHTML("beforeend", fbadge(p) + rbadge(p));
  el.classList.toggle("rejected", p.r < 0);
}
function speciesOf(p) {
  // 已确认的优先，其次模型建议（只在"较可能/候选"时算）
  if (p.spc) return p.spc;
  const sp = p.f?.sp;
  if (sp?.zh && (sp.st === "ok" || sp.st === "low_confidence")) return sp.zh;
  return null;
}

function matchesSpecies(p) {
  if (!SPECIES_SEL) return true;
  if (SPECIES_SEL === "__none__") return !speciesOf(p);
  return speciesOf(p) === SPECIES_SEL;
}

function matches(p, F = FILTER) {
  if (!matchesSpecies(p)) return false;
  const g = p.f?.g;
  switch (F) {
    case "all": case "best": return true;
    case "sharp": return g === "sharp";
    case "soft": return g === "soft";
    case "bad": return BAD.has(g);
    case "nobird": return g === "nobird";
    case "starred": return p.r > 0;
    case "rejected": return p.r < 0;
    case "pending": return !p.f;
    default: return true;
  }
}
function filterCount(F) {
  const s = LIB.stats, gr = s.grades || {};
  switch (F) {
    case "all": return s.photos;
    case "sharp": return gr.sharp || 0;
    case "soft": return gr.soft || 0;
    case "bad": return (gr.blur || 0) + (gr.missed || 0) + (gr.motion || 0);
    case "nobird": return gr.nobird || 0;
    case "starred": return s.starred;
    case "rejected": return s.rejected;
    case "pending": return s.photos - s.analyzed;
    case "best": return s.bursts;
    default: return 0;
  }
}

// ------------------------------------------------------------------ render library
const thumb = (id, s = 360) => `/thumb/${id}?s=${s}`;

function renderStats() {
  const s = LIB.stats;
  if (!s) return;
  $("#stats").innerHTML = [
    `<b>${fmtN(s.photos)}</b> 张照片`,
    `<b>${s.days}</b> 天`,
    `<b>${fmtN(s.bursts)}</b> 组连拍（${fmtN(s.burst_photos)} 张）`,
    `<b>${fmtN(s.singles)}</b> 张单拍`,
    s.undated ? `<span class="warn">${s.undated} 张无拍摄时间</span>` : "",
    s.no_subsec ? `<span class="warn" title="这些照片 EXIF 里没有毫秒时间，连拍只能按 1 秒精度判断">${fmtN(s.no_subsec)} 张缺少毫秒时间</span>` : "",
  ].filter(Boolean).join(" · ");
}
function renderSpeciesOptions() {
  const sel = $("#speciesSel");
  const counts = new Map();
  let none = 0;
  for (const pid of Object.keys(LIB.photos)) {
    const zh = speciesOf(LIB.photos[pid]);
    if (zh) counts.set(zh, (counts.get(zh) || 0) + 1);
    else none++;
  }
  const opts = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "zh"));
  const html = [`<option value="">全部鸟种</option>`]
    .concat(opts.map(([zh, c]) => `<option value="${esc(zh)}">${esc(zh)}（${c}）</option>`))
    .concat([`<option value="__none__">未识别（${none}）</option>`]);
  sel.innerHTML = html.join("");
  if (SPECIES_SEL && ![...sel.options].some((o) => o.value === SPECIES_SEL)) SPECIES_SEL = "";
  sel.value = SPECIES_SEL;
}

function renderChips() {
  const host = $("#chips");
  host.innerHTML = FILTERS.filter(([k]) => ENV.model || !["sharp", "soft", "bad", "nobird", "best", "pending"].includes(k))
    .map(([k, label]) => {
      const n = filterCount(k);
      if (k === "pending" && !n && FILTER !== "pending") return "";
      return `<button class="chip ${FILTER === k ? "on" : ""}" data-f="${k}">${label}${k === "best" ? "" : ` <span class="n">${fmtN(n)}</span>`}</button>`;
    }).join("");
  $$(".chip", host).forEach((c) => (c.onclick = () => { FILTER = c.dataset.f; expanded.clear(); render(); $("#content").scrollTop = 0; }));
}

function render(keepScroll = false) {
  const has = !!LIB.root;
  $("#toolbar").hidden = !has; $("#sidebar").hidden = !has; $("#filterbar").hidden = !has;
  $("#btnRescan").disabled = !has; $("#btnOpenRoot").disabled = !has;
  $("#btnPlan").disabled = !has || !LIB.stats?.photos;
  $("#btnBatch").disabled = !has || !LIB.stats?.photos;
  $("#btnSelect").disabled = !has || !LIB.stats?.photos;
  updatePurgeBtn();
  $("#rootPath").textContent = LIB.root || "未选择目录";
  $("#rootPath").title = LIB.root || "";
  if (!has) return renderEmpty();

  renderStats(); renderSpeciesOptions(); renderChips();
  BURSTS = {};
  for (const d of LIB.days) for (const it of d.items) if (it.type === "burst") BURSTS[it.id] = it;

  const s = LIB.stats;
  const c = $("#content");
  const top = c.scrollTop;
  if (!s.photos) {
    $("#sidebar").innerHTML = "";
    c.innerHTML = `<div class="empty"><h2>这个目录里没有找到照片</h2><p>支持 ARW / CR3 / NEF / DNG 等 RAW 格式，以及 JPG / HEIF / TIFF。${s.other_files ? `（找到 ${s.other_files} 个其他文件，例如视频，不会被处理）` : ""}</p></div>`;
    return;
  }
  const html = [];
  const visibleDays = [];
  for (const d of LIB.days) { const h = renderDay(d); if (h) { html.push(h); visibleDays.push(d); } }
  if (LIB.undated.length) { const h = renderUndated(); if (h) html.push(h); }
  if (!html.length) html.push(`<div class="empty"><h2>没有符合筛选条件的照片</h2><p>换一个筛选条件试试。</p></div>`);
  c.innerHTML = html.join("");
  renderSidebar(visibleDays);
  for (const bid of [...expanded]) {
    const card = c.querySelector(`.card.burst[data-bid="${CSS.escape(bid)}"]`);
    if (card) openBurst(card, false); else expanded.delete(bid);
  }
  if (SEL_MODE) { c.classList.add("selmode"); $("#selbar").hidden = false; paintSelection(); }
  if (keepScroll) c.scrollTop = top;
}

function renderEmpty() {
  const last = LIB.settings?.last_root;
  $("#content").innerHTML = `
  <div class="empty">
    <svg viewBox="0 0 32 32" width="56" height="56" aria-hidden="true"><path d="M4 19c5 0 8-3 10-7 2-4 5-6 9-6 2 0 4 1 5 3l-3 1c0 6-4 12-12 13l-3 4-1-4c-2-1-4-2-5-4z" fill="#2f7d5b"/><circle cx="23" cy="9" r="1.2" fill="#fff"/></svg>
    <h2>选择存放鸟片的文件夹</h2>
    <p>BirdDesk 会读取拍摄时间，按日期分组、识别连拍，并在后台检测每张照片里的鸟是否合焦。扫描和分析只读取文件，不会改动任何照片；只有在你确认“整理预览”之后才会移动文件。</p>
    <button class="btn primary big" id="btnPick2">选择目录…</button>
    <div class="manual"><input id="manualPath" placeholder="或直接粘贴目录路径，例如 /Volumes/SD/DCIM" spellcheck="false"><button class="btn" id="btnManual">扫描</button></div>
    ${last ? `<div class="last">上次打开：<a id="lastRoot">${esc(last)}</a></div>` : ""}
    <div class="features">
      <div class="feature"><b>日期 + 连拍分组</b>同一天一个文件夹，连拍放进子文件夹</div>
      <div class="feature"><b>合焦检测</b>识别鸟体，判断合焦 / 跑焦 / 运动模糊，连拍自动选最佳</div>
      <div class="feature"><b>P2.1 鸟眼定位</b>仅定位主鸟双眼：绿色实框为双视角一致且通过瞳孔校验的定位，橙色虚框为候选。E 独立开关，Z 优先看稳定眼；黄圈为鸟体最锐区域，非眼睛。眼区锐度未校准，仅供参考，不参与合焦评级或自动择优。</div>
      <div class="feature"><b>鸟种识别</b>BioCLIP 2 + 中国鸟类名录，Top-5 候选，确认即写关键词</div>
      <div class="feature"><b>出片</b>AI 降噪 + 主体/背景分区增强，另存到 _出片/</div>
    </div>
  </div>`;
  $("#btnPick2").onclick = pickAndScan;
  const go = () => { const v = $("#manualPath").value.trim(); if (v) scanRoot(v); };
  $("#btnManual").onclick = go;
  $("#manualPath").onkeydown = (e) => e.key === "Enter" && go();
  if (last) $("#lastRoot").onclick = () => scanRoot(last);
}

function renderSidebar(days) {
  const out = [`<div class="side-title">拍摄日期</div>`];
  let year = null;
  for (const d of days) {
    const y = d.date.slice(0, 4);
    if (y !== year) { out.push(`<div class="side-year">${y}</div>`); year = y; }
    out.push(`<div class="side-item" data-day="${d.date}"><span><span class="d">${d.date.slice(5)}</span><span class="w">${d.weekday}</span></span>
      <span class="c">${d.photo_count}${d.burst_count ? ` · <i>${d.burst_count}组</i>` : ""}</span></div>`);
  }
  if (LIB.undated.length) out.push(`<div class="side-item" data-day="undated"><span>无拍摄时间</span><span class="c">${LIB.undated.length}</span></div>`);
  const sb = $("#sidebar");
  sb.innerHTML = out.join("");
  $$(".side-item", sb).forEach((el) => (el.onclick = () => {
    const sec = document.getElementById("day-" + el.dataset.day);
    if (sec) $("#content").scrollTo({ top: sec.offsetTop - 4, behavior: "smooth" });
  }));
}

function singleCard(p, extra = "") {
  return `<div class="card single ${p.r < 0 ? "rejected" : ""}" data-pid="${p.id}">
    <div class="ph"><img loading="lazy" decoding="async" src="${thumb(p.id)}" alt="">${fbadge(p)}${rbadge(p)}</div>
    <span class="badge r">${esc(p.exts.join("+"))}</span>${extra}
    <div class="cap"><span class="t">${esc(p.time || "--:--:--")}</span><span class="n">${p.spc ? esc(p.spc) : esc(p.stem)}</span></div>
  </div>`;
}

function burstCaption(b) {
  const first = LIB.photos[b.photo_ids[0]];
  const rate = b.fps ? ` · ${b.fps}张/秒` : "";
  const sharp = b.analyzed ? `<span class="sharpcnt" title="合焦张数">合焦 ${b.sharp}/${b.count}</span>` : `<span>${b.duration}秒${rate}</span>`;
  return `<span class="t">${esc((first.time || "").slice(0, 8))}</span>${sharp}`;
}
function burstCard(b) {
  const cover = LIB.photos[b.cover];
  const isBest = b.best && b.cover === b.best;
  return `<div class="card burst" data-bid="${esc(b.id)}" title="${esc(b.name)}，点击展开">
    <div class="ph"><img loading="lazy" decoding="async" src="${thumb(b.cover)}" alt="">${fbadge(cover)}${rbadge(cover)}</div>
    <span class="badge l burst">连拍 ${b.count}</span>
    ${isBest ? `<span class="crown">本组最佳</span>` : ""}
    <span class="badge r">${esc(cover.exts.join("+"))}</span>
    <div class="cap">${burstCaption(b)}</div>
  </div>`;
}
function refreshBurstCard(card) {
  const b = BURSTS[card.dataset.bid];
  const cover = LIB.photos[b.cover];
  const img = card.querySelector(".ph img");
  if (!img.src.endsWith(thumb(b.cover))) img.src = thumb(b.cover);
  card.querySelector(".fbadge")?.remove(); card.querySelector(".rbadge")?.remove();
  card.querySelector(".ph").insertAdjacentHTML("beforeend", fbadge(cover) + rbadge(cover));
  card.querySelector(".crown")?.remove();
  if (b.best && b.cover === b.best) card.querySelector(".badge.l").insertAdjacentHTML("afterend", `<span class="crown">本组最佳</span>`);
  card.querySelector(".cap").innerHTML = burstCaption(b);
}

// ------------------------------------------------------------------ 排序（只影响展示顺序）
const scoreOf = (p) => (Number.isFinite(p?.f?.s) ? p.f.s : null);

function itemMeta(it) {
  if (it.type === "single") {
    const p = LIB.photos[it.id];
    return { score: scoreOf(p), rating: p?.r || 0 };
  }
  const cover = LIB.photos[it.best || it.cover];
  const rating = Math.max(0, ...it.photo_ids.map((id) => LIB.photos[id]?.r || 0));
  return { score: scoreOf(cover), rating };
}

function sortItems(items) {
  if (SORT === "time") return items;
  const keyed = items.map((it) => ({ it, ...itemMeta(it) }));
  const noData = (v) => v === null || v === undefined;
  if (SORT === "score" || SORT === "score_asc") {
    const dir = SORT === "score" ? -1 : 1;
    keyed.sort((a, b) => {
      if (noData(a.score) && noData(b.score)) return 0;
      if (noData(a.score)) return 1;          // 未分析的一律排在最后
      if (noData(b.score)) return -1;
      return dir * (a.score - b.score);
    });
  } else if (SORT === "rating") {
    keyed.sort((a, b) => b.rating - a.rating || (noData(b.score) ? -1 : noData(a.score) ? 1 : b.score - a.score));
  }
  return keyed.map((k) => k.it);
}

function dayItemsHTML(items) {
  const out = [];
  for (const it of items) {
    if (it.type === "single") {
      const p = LIB.photos[it.id];
      if (matches(p)) out.push(singleCard(p));
    } else if (FILTER === "best") {
      const pid = it.best || it.cover;
      out.push(singleCard(LIB.photos[pid], `<span class="badge l burst" title="${esc(it.name)}">连拍最佳 · ${it.count}张</span>`));
    } else if (FILTER === "all" || it.photo_ids.some((id) => matches(LIB.photos[id]))) {
      out.push(burstCard(it));
    }
  }
  return out.join("");
}

function renderDay(d) {
  const items = dayItemsHTML(sortItems(d.items));
  if (!items) return "";
  const y = d.date.split("-");
  return `<section class="day" id="day-${d.date}" data-day="${d.date}">
    <div class="day-head"><h2>${y[0]}年${+y[1]}月${+y[2]}日<span>${d.weekday}</span></h2>
      <div class="meta">${d.photo_count} 张 · ${d.burst_count} 组连拍 · ${d.single_count} 张单拍</div>
      <div class="folder">→ ${d.date}/</div></div>
    <div class="grid">${items}</div></section>`;
}

function renderUndated() {
  const undated = LIB.undated.map((id) => LIB.photos[id]).filter((p) => matches(p));
  const sorted = SORT === "time" ? undated : sortItems(undated.map((p) => ({ type: "single", id: p.id }))).map((it) => LIB.photos[it.id]);
  const items = sorted.map((p) => singleCard(p)).join("");
  if (!items) return "";
  return `<section class="day" id="day-undated" data-day="undated">
    <div class="day-head"><h2>无拍摄时间</h2><div class="meta">${LIB.undated.length} 张 · EXIF 里没有拍摄时间</div><div class="folder">→ unknown-date/</div></div>
    <div class="grid">${items}</div></section>`;
}

function onContentClick(e) {
  // 多选模式（或按住 ⌘/Ctrl/Shift）下，卡片点击只做选择：不展开连拍、不打开大图
  const anyCard = e.target.closest(".card");
  const anyFrame = e.target.closest(".frame");
  if ((anyCard || anyFrame) && (SEL_MODE || e.metaKey || e.ctrlKey || e.shiftKey)) {
    e.preventDefault();
    if (anyCard && e.altKey) {          // ⌥ 点连拍卡片：只展开/收起，用来逐帧挑选
      if (anyCard.classList.contains("burst")) toggleBurst(anyCard);
      return;
    }
    if (!SEL_MODE) setSelMode(true);     // 用快捷键点选时，自动进入选择模式并显示选择栏
    toggleCardSel(anyFrame || anyCard, e.shiftKey);
    return;
  }
  const act = e.target.closest("[data-act]");
  if (act) { e.stopPropagation(); burstAction(act.dataset.act, act.dataset.bid); return; }
  const frame = e.target.closest(".frame");
  if (frame) {
    const b = BURSTS[frame.dataset.bid];
    openViewer(b.photo_ids, b.photo_ids.indexOf(frame.dataset.pid), b.name, b.id);
    return;
  }
  const bcard = e.target.closest(".card.burst");
  if (bcard) { toggleBurst(bcard); return; }
  const scard = e.target.closest(".card.single");
  if (scard) {
    const sec = scard.closest(".day");
    const ids = $$(".card.single", sec).map((x) => x.dataset.pid);
    const label = sec.dataset.day === "undated" ? "无拍摄时间" : `${sec.dataset.day}${FILTER === "best" ? " 精选" : " 单拍"}`;
    openViewer(ids, ids.indexOf(scard.dataset.pid), label, null);
  }
}

function collapseBurst(bid) {
  expanded.delete(bid);
  const card = $(`.card.burst[data-bid="${CSS.escape(bid)}"]`);
  if (!card) return;
  card.classList.remove("open");
  const panel = card.nextElementSibling;
  if (panel && panel.classList.contains("burst-panel")) panel.remove();
}

function toggleBurst(card) {
  const bid = card.dataset.bid;
  const content = $("#content");
  // 以被点卡片为锚点：展开/收起后把它按回原位，避免整页跟着跳
  const anchor = card.getBoundingClientRect().top;
  if (expanded.has(bid)) {
    collapseBurst(bid);
  } else {
    for (const other of [...expanded]) if (other !== bid) collapseBurst(other);  // 手风琴：只留一个展开
    openBurst(card, false);
  }
  const delta = card.getBoundingClientRect().top - anchor;
  if (Math.abs(delta) > 0.5) content.scrollTop += delta;
}

function openBurst(card, scroll) {
  const bid = card.dataset.bid;
  const b = BURSTS[bid];
  if (!b) return;
  expanded.add(bid);
  card.classList.add("open");
  const P = LIB.photos;
  let order = b.photo_ids.map((id, i) => i);
  if (SORT === "score" || SORT === "score_asc") {
    const dir = SORT === "score" ? -1 : 1;
    order = [...order].sort((x, y) => {
      const a = scoreOf(P[b.photo_ids[x]]), c = scoreOf(P[b.photo_ids[y]]);
      if (a === null && c === null) return x - y;
      if (a === null) return 1;
      if (c === null) return -1;
      return dir * (a - c);
    });
  }
  const frames = order.map((i) => {
    const id = b.photo_ids[i];
    const p = P[id];
    const gap = i === 0 ? "" : (p.gap != null ? `+${p.gap.toFixed(3)}s` : "");
    const best = id === b.best;
    const cls = ["frame", best ? "best" : "", p.r < 0 ? "rejected" : "", matches(p) ? "" : "dim"].join(" ");
    return `<div class="${cls}" data-bid="${esc(bid)}" data-pid="${id}"><img loading="lazy" decoding="async" src="${thumb(id)}" alt="">
      ${best ? `<span class="crown">最佳</span>` : ""}${fbadge(p)}${rbadge(p)}
      <div class="fi"><span>${i + 1}</span><span>${gap}</span></div></div>`;
  }).join("");
  const first = P[b.photo_ids[0]], last = P[b.photo_ids[b.photo_ids.length - 1]];
  const canCull = ENV.model && b.analyzed === b.count && b.best;
  const anyRated = b.photo_ids.some((id) => P[id].r !== 0);
  const allRejected = b.photo_ids.every((id) => (P[id].r || 0) < 0);
  const panel = document.createElement("div");
  panel.className = "burst-panel";
  panel.innerHTML = `<div class="bp-head"><b>${esc(b.name)}/</b>
    <span>${b.count} 张 · ${esc(first.time || "")} → ${esc((last.time || "").slice(0, 12))} · 共 ${b.duration} 秒${b.fps ? ` · 平均 ${b.fps} 张/秒` : ""}${b.analyzed ? ` · 合焦 ${b.sharp} 张` : ""}${SORT === "score" || SORT === "score_asc" ? ` · 帧按合焦分${SORT === "score" ? "降" : "升"}序` : ""}</span>
    ${b.species ? `<span class="sp-chip" title="组内按帧投票得出">鸟种：${esc(b.species.zh)}（${b.species.votes}/${b.species.of} 帧）</span>` : ""}
    <div class="bp-actions">
      ${canCull ? `<button class="btn small" data-act="cull" data-bid="${esc(bid)}" title="排名第一的保留，其余未打星的标记为淘汰（不会删除文件）">保留最佳，其余淘汰</button>` : (ENV.model && b.analyzed < b.count ? `<span style="color:var(--muted);font-size:12px">分析中 ${b.analyzed}/${b.count}…</span>` : "")}
      ${allRejected
        ? `<button class="btn small" data-act="unrejectAll" data-bid="${esc(bid)}" title="把整组从淘汰里撤回来">取消整组淘汰</button>`
        : `<button class="btn small" data-act="rejectAll" data-bid="${esc(bid)}" title="把整组全部标记为淘汰（只写 XMP 星级，不会删除文件）">整组淘汰</button>`}
      <button class="btn small" data-act="video" data-bid="${esc(bid)}" title="把这组连拍按帧率编成 MP4（升格）">升格视频</button>
      ${anyRated ? `<button class="btn small" data-act="clear" data-bid="${esc(bid)}">清除本组标记</button>` : ""}
      <button class="btn small" data-act="collapse" data-bid="${esc(bid)}">收起</button>
    </div></div>
    <div class="bp-strip">${frames}</div>`;
  card.after(panel);
  if (scroll) panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

async function burstAction(act, bid) {
  const card = $(`.card.burst[data-bid="${CSS.escape(bid)}"]`);
  if (act === "collapse") { if (card) toggleBurst(card); return; }
  if (act === "video") { openVideo(bid); return; }
  try {
    if (act === "cull") {
      const r = await api("/api/burst/cull", { burst_id: bid, keep: 1 });
      toast(r.rejected ? `已把 ${r.rejected} 张标记为淘汰（写入 XMP，未删除任何文件）` : "没有需要淘汰的照片（已打星的不会被改动）");
    } else if (act === "rejectAll") {
      const ids = BURSTS[bid].photo_ids.slice();
      await rate(ids, -1, true);
      toast(`已把整组 ${ids.length} 张标记为淘汰（写入 XMP，未删除文件）`);
    } else if (act === "unrejectAll") {
      const ids = BURSTS[bid].photo_ids.filter((id) => (LIB.photos[id].r || 0) < 0);
      await rate(ids, 0, true);
      toast(`已取消整组淘汰（${ids.length} 张）`);
    } else if (act === "clear") {
      const ids = BURSTS[bid].photo_ids.filter((id) => LIB.photos[id].r !== 0);
      await rate(ids, 0, true);
      toast(`已清除 ${ids.length} 张照片的标记`);
    }
    await poll();
  } catch (e) { toast(e.message, true, 6000); }
}

async function rate(ids, r, quiet = false) {
  if (!ids.length) return;
  try {
    const u = await api("/api/rate", { ids, rating: r });
    applyUpdates(u);
    if (VIEW) renderRate();
    if (!quiet && VIEW) toast(r < 0 ? "已标记淘汰" : r > 0 ? `${"★".repeat(r)}` : "已清除标记", false, 900);
  } catch (e) { toast(e.message, true, 6000); }
}

let spyTimer = null;
function onScrollSpy() {
  clearTimeout(spyTimer);
  spyTimer = setTimeout(() => {
    const c = $("#content");
    const secs = $$(".day", c);
    let cur = null;
    for (const s of secs) { if (s.offsetTop - c.scrollTop <= 60) cur = s.dataset.day; else break; }
    $$(".side-item").forEach((el) => el.classList.toggle("active", el.dataset.day === cur));
  }, 60);
}

// ------------------------------------------------------------------ viewer
let VIEW = null;
const OV = { box: true, eye: true, heat: false };
const curId = () => VIEW.ids[VIEW.idx];
const EYE_STATUS = {
  detected: "已识别", uncertain: "定位不确定", not_detected: "未检测到眼睛",
  too_small: "眼区太小", unavailable: "鸟眼模型不可用", error: "鸟眼分析失败", no_bird: "未检测到主鸟",
};
const normalized = (v) => Number.isFinite(v) && v >= 0 && v <= 1;

function eyeEntries(a) {
  const result = a?.eye;
  if (!Array.isArray(result?.eyes)) return [];
  return result.eyes.map((eye, index) => {
    const box = eye?.box;
    const valid = normalized(eye?.x) && normalized(eye?.y) && Array.isArray(box) && box.length === 4
      && box.every(normalized) && box[2] > box[0] && box[3] > box[1];
    return {
      eye, index, valid,
      name: eye?.side === "left" ? "左眼" : eye?.side === "right" ? "右眼" : `眼睛 ${index + 1}`,
      stable: result.status === "detected" && eye?.stable === true && valid,
    };
  });
}

function eyeOverlay(a) {
  if (!["detected", "uncertain"].includes(a?.eye?.status)) return "";
  const pct = (v) => `${(v * 100).toFixed(4)}%`;
  const entries = eyeEntries(a).filter((item) => item.valid);
  return entries.map(({ eye, name, stable }, index) => {
    const [x1, y1, x2, y2] = eye.box;
    const state = stable ? "stable" : "candidate";
    const label = `${name}${stable ? " · 已识别" : " · 候选"}`;
    const other = entries.length === 2 ? entries[1 - index].eye : null;
    const labelLeft = other && (eye.x < other.x || (eye.x === other.x && index === 0));
    // 坐标已旋正且相对整图归一化，不再按鸟体框偏移或旋转；双眼标签向外排开。
    return `<div class="eye-box ${state}${labelLeft ? " label-left" : ""}" title="${label}" style="left:${pct(x1)};top:${pct(y1)};width:${pct(x2 - x1)};height:${pct(y2 - y1)}"><span>${label}</span></div>
      <div class="eye-point ${state}" style="left:${pct(eye.x)};top:${pct(eye.y)}"></div>`;
  }).join("");
}

function zoomTarget(a) {
  const selected = a?.eye?.selected;
  const item = Number.isInteger(selected) ? eyeEntries(a)[selected] : null;
  if (item?.stable) return { x: item.eye.x, y: item.eye.y, message: `100% 查看主鸟${item.name}（双视角一致且通过瞳孔校验）` };
  const peak = a?.focus?.peak_xy;
  if (Array.isArray(peak) && peak.length === 2 && peak.every(normalized)) {
    return { x: peak[0], y: peak[1], message: "未提供选中的稳定鸟眼，已回退到鸟体最锐区域（黄圈，非眼睛）" };
  }
  return { x: 0.5, y: 0.5, message: "未提供选中的稳定鸟眼，也无鸟体最锐坐标，已回退到画面中心" };
}

function zoomEye(index, allowCandidate = false) {
  if (!VIEW) return;
  const item = Number.isInteger(index) ? eyeEntries(VIEW.detail?.analysis)[index] : null;
  if (!item?.valid || (!item.stable && !allowCandidate)) return;
  const label = item.stable ? `100% 查看主鸟${item.name}（双视角一致且通过瞳孔校验）` : `100% 查看${item.name}候选位置（未确认，不是已识别的眼睛）`;
  zoomAt(item.eye.x, item.eye.y, label);
}

function openViewer(ids, idx, label, bid) {
  VIEW = { ids, idx: Math.max(0, idx), label, bid, detail: null, zoom: null };
  $("#viewer").hidden = false;
  showFrame();
}
function closeViewer() {
  $("#devModal").hidden = true;
  $("#viewer").hidden = true; $("#viewerImg").src = ""; $("#vHeat").src = "";
  exitZoom(); VIEW = null;
}
// 网格里的"组"（连拍卡片 / 单拍卡片），顺序与屏幕一致（含当前筛选与排序）
function gridGroups() {
  return $$("#content .card").map((card) => ({
    ids: cardIds(card),
    bid: card.dataset.bid || null,
  })).filter((g) => g.ids.length);
}

function openGroup(group, atEnd) {
  if (!group) return;
  if (group.bid) {
    const n = group.ids.length;
    openViewer(group.ids.slice(), atEnd ? n - 1 : 0, BURSTS[group.bid]?.name || "", group.bid);
    return;
  }
  // 单拍：按点击卡片的老规矩，打开当天所有单拍，从这张开始
  const card = $(`.card.single[data-pid="${CSS.escape(group.ids[0])}"]`);
  const sec = card ? card.closest(".day") : null;
  const ids = sec ? $$(".card.single", sec).map((x) => x.dataset.pid) : group.ids.slice();
  const label = sec && sec.dataset.day === "undated" ? "无拍摄时间"
    : `${sec ? sec.dataset.day : ""} 单拍`;
  const i = Math.max(0, ids.indexOf(group.ids[0]));
  openViewer(ids, atEnd ? ids.length - 1 : i, label, null);
}

function stepViewer(d) {
  if (!VIEW) return;
  const n = VIEW.idx + d;
  if (n >= 0 && n < VIEW.ids.length) { VIEW.idx = n; showFrame(); return; }
  if (VIEW.zoom) return;                    // 放大看细节时不跨组，避免误跳
  // 走到本组尽头：继续按同一方向 → 跳相邻组的第一张 / 最后一张
  const groups = gridGroups();
  if (!groups.length) return;
  const anchor = d > 0 ? VIEW.ids[VIEW.ids.length - 1] : VIEW.ids[0];
  const gi = groups.findIndex((g) => g.ids.includes(anchor));
  if (gi < 0) return;
  openGroup(groups[gi + (d > 0 ? 1 : -1)], d < 0);
}
function fitStage() {
  const img = $("#viewerImg");
  if (!img.naturalWidth) return;
  const st = $("#vStage").getBoundingClientRect();
  const s = Math.min((st.width - 24) / img.naturalWidth, (st.height - 24) / img.naturalHeight);
  const w = $("#vWrap");
  w.style.width = `${Math.round(img.naturalWidth * s)}px`;
  w.style.height = `${Math.round(img.naturalHeight * s)}px`;
}
function toggleOv(k) {
  OV[k] = !OV[k];
  drawOverlay();
}
function drawOverlay() {
  for (const [key, id] of [["box", "tBox"], ["eye", "tEye"], ["heat", "tHeat"]]) {
    const button = $("#" + id);
    button.classList.toggle("on", OV[key]);
    button.setAttribute("aria-pressed", String(OV[key]));
  }
  const ov = $("#vOverlay"), heat = $("#vHeat");
  const a = VIEW?.detail?.analysis;
  ov.innerHTML = "";
  const hasFocus = !!a?.focus;
  $("#tHeat").disabled = !hasFocus;
  if (OV.heat && hasFocus) { const src = `/heat/${curId()}`; if (!heat.src.endsWith(src)) heat.src = src; heat.hidden = false; }
  else heat.hidden = true;
  $("#vHeatLegend").hidden = heat.hidden;
  if (!a) return;
  const pct = (v) => `${(v * 100).toFixed(2)}%`;
  if (OV.box) {
    (a.birds || []).forEach((b, i) => {
      const [x1, y1, x2, y2] = b.box;
      ov.insertAdjacentHTML("beforeend", `<div class="bbox ${i === a.main ? "main" : ""}" style="left:${pct(x1)};top:${pct(y1)};width:${pct(x2 - x1)};height:${pct(y2 - y1)}">${i === a.main ? `<span>鸟 ${Math.round(b.conf * 100)}%</span>` : ""}</div>`);
    });
    if (a.focus?.peak_xy) ov.insertAdjacentHTML("beforeend", `<div class="peak" title="黄圈：鸟体最锐区域，非眼睛" style="left:${pct(a.focus.peak_xy[0])};top:${pct(a.focus.peak_xy[1])}"></div>`);
    if (a.af) ov.insertAdjacentHTML("beforeend", `<div class="af" title="相机对焦点" style="left:${pct(a.af.x)};top:${pct(a.af.y)}"></div>`);
  }
  if (OV.eye) ov.insertAdjacentHTML("beforeend", eyeOverlay(a));
}

async function zoomAt(x, y, note) {
  if (!VIEW) return;
  const id = curId();
  const st = $("#vStage").getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(st.width * dpr), h = Math.round(st.height * dpr);
  const zoom = { x, y };
  VIEW.zoom = zoom;
  $("#tZoom").classList.add("on");
  $("#vZoom").hidden = false;
  $("#vZoomTip").textContent = (note ? note + " · " : "") + "正在加载 100% 原始像素…";
  try {
    const r = await fetch(`/crop/${id}?x=${x}&y=${y}&w=${w}&h=${h}`);
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const info = JSON.parse(r.headers.get("X-Crop") || "{}");
    const url = URL.createObjectURL(await r.blob());
    if (!VIEW || curId() !== id || VIEW.zoom !== zoom) { URL.revokeObjectURL(url); return; }
    const img = $("#vZoomImg");
    const old = img.src;
    img.onload = () => {
      const cw = img.naturalWidth / dpr, ch = img.naturalHeight / dpr;
      img.style.width = `${cw}px`; img.style.height = `${ch}px`;
      // 裁切贴边时裁切中心不等于请求点，因此按目标点在裁切内的实际位置对齐舞台中心
      const px = info.w ? ((x - info.x) / info.w) * cw : cw / 2;
      const py = info.h ? ((y - info.y) / info.h) * ch : ch / 2;
      const stage = $("#vStage").getBoundingClientRect();
      img.style.position = "absolute";
      img.style.left = `${Math.round(stage.width / 2 - px)}px`;
      img.style.top = `${Math.round(stage.height / 2 - py)}px`;
      if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    };
    img.src = url;
    $("#vZoomTip").textContent = `${note ? note + " · " : ""}100% 原始像素（${info.full_w}×${info.full_h}${info.src === "raw-preview" ? "，RAW 内嵌 JPEG" : ""}）· ←/→ 原地对比其他帧 · 点击或 Z 返回`;
  } catch (e) { if (VIEW && curId() === id && VIEW.zoom === zoom) $("#vZoomTip").textContent = "无法加载：" + e.message; }
}
function exitZoom() {
  if (VIEW) VIEW.zoom = null;
  $("#vZoom").hidden = true; $("#tZoom").classList.remove("on");
}
function toggleZoom() {
  if (!VIEW) return;
  if (VIEW.zoom) return exitZoom();
  const target = zoomTarget(VIEW.detail?.analysis);
  zoomAt(target.x, target.y, target.message);
}

function showFrame() {
  const id = curId();
  const img = $("#viewerImg");
  img.onload = () => { fitStage(); };
  img.src = thumb(id, 800);
  const big = new Image();
  big.onload = () => { if (VIEW && curId() === id) img.src = big.src; };
  big.src = thumb(id, 2048);
  if (VIEW.idx + 1 < VIEW.ids.length) new Image().src = thumb(VIEW.ids[VIEW.idx + 1], 800);
  $("#vCounter").textContent = `${VIEW.label} · ${VIEW.idx + 1} / ${VIEW.ids.length}`;
  $("#vPrev").hidden = VIEW.idx === 0; $("#vNext").hidden = VIEW.idx === VIEW.ids.length - 1;
  $("#vTitle").textContent = LIB.photos[id].stem;
  VIEW.detail = null;
  $("#vInfo").innerHTML = `<div class="focus-sub">正在加载照片详情与鸟眼定位…</div>`;
  drawOverlay();
  renderRate();
  showInfo(id, false);
}

function renderRate() {
  if (!VIEW) return;
  const p = LIB.photos[curId()];
  const stars = [1, 2, 3, 4, 5].map((n) => `<button class="star ${p.r >= n ? "on" : ""}" data-r="${n}" title="${n} 星（按 ${n}）">★</button>`).join("");
  $("#vRate").innerHTML = `${stars}<button class="btn small rej-btn ${p.r < 0 ? "on" : ""}" data-r="x" title="淘汰（按 X）">淘汰</button>`;
  $$("#vRate [data-r]").forEach((b) => (b.onclick = () => {
    const v = b.dataset.r === "x" ? (p.r === -1 ? 0 : -1) : (p.r === +b.dataset.r ? 0 : +b.dataset.r);
    rate([curId()], v);
  }));
}

function focusCard(d) {
  const a = d.analysis;
  if (!a) return `<div class="focus-card"><div class="focus-head"><i class="dot g-pending"></i>等待合焦分析</div><div class="focus-sub">后台分析中，完成后自动显示</div></div>`;
  if (a.error) return `<div class="focus-card"><div class="focus-head"><i class="dot g-error"></i>分析失败</div><div class="focus-sub">${esc(a.error)}</div></div>`;
  const f = d.f;
  const g = GRADE[f.g];
  if (!a.focus) return `<div class="focus-card"><div class="focus-head"><i class="dot ${g.cls}"></i>${g.label}</div><div class="focus-sub">${g.sub}</div></div>`;
  const F = a.focus;
  const main = a.birds[a.main];
  const color = { sharp: "var(--g-sharp)", soft: "var(--g-soft)" }[f.g] || "var(--g-bad)";
  const afTxt = !a.af ? "相机没有记录对焦点" : a.af.on_bird ? "在鸟身上" : "<b style='color:var(--g-bad)'>不在鸟身上</b>";
  const rank = d.burst_rank ? `<dt>本组排名</dt><dd>第 ${d.burst_rank} / ${d.burst_size}${d.burst_rank === 1 ? "（最佳）" : ""}</dd>` : "";
  return `<div class="focus-card">
    <div class="focus-head"><i class="dot ${g.cls}"></i>${g.label}<span class="score">${f.s} 分</span></div>
    <div class="meter"><div style="width:${f.s}%;background:${color}"></div></div>
    <div class="focus-sub">${g.sub}</div>
    ${main.edge ? `<div class="warnline">鸟被画面边缘截断</div>` : ""}
    <dl class="kv">
      ${rank}
      <dt>相机对焦点</dt><dd>${afTxt}</dd>
      <dt>鸟</dt><dd>${a.birds.length} 只 · 主体占画面 ${(main.area * 100).toFixed(1)}%</dd>
      <dt>最锐区域</dt><dd title="≥ 0.52 判为合焦（标准）">${F.sharp.toFixed(2)} <span style="color:var(--faint)">/ 合焦线 0.52</span></dd>
      <dt>身体中位</dt><dd>${F.body.toFixed(2)}</dd>
      <dt>周边背景</dt><dd>${F.bg ? F.bg.toFixed(2) : "—"}</dd>
      <dt>方向一致</dt><dd title="低于 0.6 且不合焦时判为运动模糊">${F.anis.toFixed(2)}</dd>
      <dt>噪声</dt><dd>${F.noise.toFixed(1)}</dd>
      <dt>分析来源</dt><dd>${a.src === "raw-preview" ? (Math.max(a.w, a.h) < 3000 ? "RAW 内嵌小预览（精度较低）" : "RAW 内嵌 JPEG") : "JPG"} ${a.w}×${a.h}</dd>
    </dl></div>`;
}

function eyeCard(d) {
  const a = d.analysis, result = a?.eye;
  const status = result ? (EYE_STATUS[result.status] || "鸟眼状态未知") : "暂无鸟眼结果";
  const reason = result?.reason || (result ? "未提供原因" : "当前照片尚未返回鸟眼分析；旧分析数据仍可正常查看。");
  const entries = eyeEntries(a);
  const items = entries.map(({ eye, index, name, valid, stable }) => {
    const score = Number.isFinite(eye?.confidence) && eye.confidence >= 0 && eye.confidence <= 1 ? eye.confidence.toFixed(3) : "—";
    const sharp = eye?.sharpness;
    const sharpValue = Number.isFinite(sharp?.value) ? sharp.value.toFixed(3) : "—";
    const agreement = Number.isFinite(eye?.agreement_px) ? `${eye.agreement_px.toFixed(2)} px` : "—";
    return `<div class="eye-entry">
      <div class="eye-entry-head"><b>${name}</b><span class="eye-state ${stable ? "stable" : "candidate"}">${stable ? "已识别 · 双视角+瞳孔校验" : "候选 · 未确认"}</span></div>
      <dl class="kv">
        <dt>定位模型分数</dt><dd>${score} <span class="eye-note">/ 1，非正确率</span></dd>
        <dt>双视角偏差</dt><dd>${agreement} ${eye?.refined ? '<span class="eye-note">已细化到瞳孔</span>' : ''}</dd>
        <dt>眼区锐度参考</dt><dd>${sharpValue} <span class="eye-note">${sharp?.usable === true ? "可供参考" : "不可用"}</span></dd>
      </dl>
      <p class="eye-note">${esc(sharp?.reason || "未提供眼区锐度说明")}</p>
      ${!valid ? `<p class="eye-note">眼区坐标无效，不绘制或定位。</p>` : ""}
      ${stable ? `<button class="btn small eye-zoom" data-eye-index="${index}">100% 查看${name}${index === result.selected ? "（Z 默认）" : ""}</button>` : (valid ? `<button class="btn small eye-zoom" data-eye-index="${index}" data-cand="1">100% 查看候选位置（未确认）</button>` : "")}
    </div>`;
  }).join("");
  return `<section class="eye-card" aria-label="主鸟眼睛定位">
    <div class="eye-head"><h4>主鸟眼睛</h4><span class="eye-state ${entries.some((item) => item.stable) ? "stable" : ""}">${status}</span></div>
    <p class="eye-reason">${esc(reason)}</p>
    <p class="eye-note">定位模型：${esc(result?.model || "未提供")} · 仅定位主鸟双眼，不定位其他鸟</p>
    ${items}
    <div class="eye-key"><span><i class="eye-swatch stable"></i>绿实框 / 中心点：双视角+瞳孔校验通过</span><span><i class="eye-swatch candidate"></i>橙虚框 / 中心点：候选</span><span><i class="peak-swatch"></i>黄圈：鸟体最锐区域，非眼睛</span></div>
    <p class="eye-note">眼区锐度未校准，不评级，不影响原身体合焦判断或连拍自动择优。未通过瞳孔校验的只作候选，不能据此判断鸟没有眼睛或已经失焦。Z 优先查看选中的稳定眼，否则回退鸟体最锐区域并提示；E 独立于 B。</p>
  </section>`;
}

const SP_STATUS = {
  ok: "较可能", low_confidence: "候选不明", uncertain: "无法判断",
  too_small: "鸟太小", unavailable: "模型不可用", error: "识别失败",
  no_bird: "无鸟", disabled: "已关闭",
};

function speciesCard(d) {
  const sp = d.analysis?.species;
  const conf = d.spc;
  if (!sp && !conf) return "";
  const st = SP_STATUS[sp?.status] || "未知";
  const top = sp?.top || [];
  const rows = top.map((t, i) => `
    <button class="sp-row ${i === 0 && sp.status === "ok" ? "first" : ""}" data-latin="${esc(t.latin)}" data-zh="${esc(t.zh)}" title="确认为 ${esc(t.zh)}">
      <span class="sp-name">${esc(t.zh)}</span>
      <span class="sp-bar"><i style="width:${Math.max(2, Math.round(t.p * 100))}%"></i></span>
      <span class="sp-p">${(t.p * 100).toFixed(0)}%</span>
      <span class="sp-latin">${esc(t.latin)}</span>
    </button>`).join("");
  return `<section class="sp-card" aria-label="鸟种识别">
    <div class="sp-head"><h4>鸟种</h4>
      ${conf ? `<span class="sp-confirmed">已确认：${esc(conf)}</span>` : `<span class="sp-state ${sp?.status || ""}">${st}</span>`}
    </div>
    ${top.length ? `<p class="sp-reason">${esc(sp.reason || "")} 点击名称即确认为该鸟种并写入关键词</p>
      <div class="sp-list">${rows}</div>` : `<p class="sp-reason">${esc(sp?.reason || "没有可用的鸟种结果")}</p>`}
    <p class="sp-note">BioCLIP 2 零样本 + 中国鸟类名录（1508 种）约束；33 张带标签公开图上 top-1 82%、top-5 100%，
      同属近缘种易混。确认后写入 XMP 关键词，Lightroom 可见，不改原图。</p>
  </section>`;
}

async function confirmSpecies(pid, zh, latin) {
  try {
    await api("/api/species/confirm", { ids: [pid], zh, latin });
    toast(`已确认：${zh}（已写入 XMP 关键词）`);
    await poll();
    if (VIEW) showInfo(pid, true);
  } catch (e) { toast("确认失败：" + e.message, true, 6000); }
}

async function showInfo(id, refresh) {
  try {
    const d = await api(`/api/photo/${id}`);
    if (!VIEW || curId() !== id) return;
    VIEW.detail = d;
    drawOverlay();
    if (VIEW.zoom && !refresh) zoomAt(VIEW.zoom.x, VIEW.zoom.y);
    const m = d.meta || {};
    const row = (k, v) => (v || v === 0 ? `<dt>${k}</dt><dd>${esc(v)}</dd>` : "");
    const fnum = m.FNumber ? `f/${m.FNumber}` : "";
    $("#vInfo").innerHTML = `
      ${ENV.model ? focusCard(d) : ""}
      ${speciesCard(d)}
      ${eyeCard(d)}
      <div class="vi-sec"><h4>拍摄信息</h4><dl class="kv">
        ${row("拍摄日期", d.date || "无")}
        ${row("拍摄时间", d.time ? d.time + (d.has_subsec ? "" : "（无毫秒）") : "无")}
        ${row("距上一张", d.gap != null ? `${d.gap.toFixed(3)} 秒` : "")}
        ${row("相机", [m.Make, m.Model].filter(Boolean).join(" "))}
        ${row("镜头", m.LensModel || m.LensID)}
        ${row("焦距", m.FocalLength)}
        ${row("快门", m.ExposureTime ? m.ExposureTime + " s" : "")}
        ${row("光圈", fnum)}
        ${row("ISO", m.ISO)}
        ${row("驱动模式", m.ReleaseMode || m.DriveMode)}
        ${row("对焦模式", [m.FocusMode, m.AFAreaMode].filter(Boolean).join(" / "))}
        ${row("所在文件夹", d.dir || "（根目录）")}
        ${row("将整理到", d.day ? d.day + "/" + (d.burst ? d.burst + "/" : "") : "unknown-date/")}
      </dl></div>
      <div class="vi-sec"><h4>文件</h4><ul class="files">${d.files.map((f) => `<li>${esc(f.name)} <span style="color:var(--faint)">${fmtBytes(f.size)}</span></li>`).join("")}</ul>
        <button class="btn small" id="vReveal" style="margin-top:8px">在 Finder 中显示</button></div>
      <div class="vi-sec"><h4>快捷键</h4><div style="font-size:12px;color:var(--muted);line-height:1.8">←/→ 翻页 · Z 100% 优先看稳定眼，否则回退鸟体最锐区域 · 点图片放大该处<br>E 鸟眼（独立开关） · B 鸟体/黄圈/对焦点 · H 热力图 · 1–5 星 · X 淘汰 · Esc 关闭</div></div>`;
    $$("#vInfo .sp-row").forEach((row) => {
      row.onclick = () => confirmSpecies(id, row.dataset.zh, row.dataset.latin);
    });
    $$("[data-eye-index]", $("#vInfo")).forEach((button) => {
      button.onclick = () => zoomEye(Number(button.dataset.eyeIndex), button.dataset.cand === "1");
    });
    $("#vReveal").onclick = () => api("/api/reveal", { id });
  } catch (e) { if (VIEW && curId() === id) $("#vInfo").innerHTML = `<div class="alert danger">${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------------ 多选 / 删除
function cardIds(el) {
  if (el.classList.contains("frame")) return el.dataset.pid ? [el.dataset.pid] : [];
  if (el.dataset.bid) return BURSTS[el.dataset.bid]?.photo_ids || [];
  return el.dataset.pid ? [el.dataset.pid] : [];
}

function setSelMode(on) {
  SEL_MODE = on;
  if (!on) { SEL.clear(); SEL_LAST = null; }
  $("#selbar").hidden = !on;
  $("#btnSelect").classList.toggle("primary", on);
  $("#content").classList.toggle("selmode", on);
  paintSelection();
}

function updatePurgeBtn() {
  const n = LIB.stats?.rejected || 0;
  $("#btnPurge").disabled = !(LIB.root && n);
  $("#btnPurge").textContent = n ? `清理淘汰 (${n})` : "清理淘汰";
  const starred = LIB.stats?.starred || 0;
  $("#btnAirDrop").disabled = !(LIB.root && starred);
  $("#btnAirDrop").textContent = starred ? `隔空传送 (${starred})` : "隔空传送";
}

async function airDrop(ids, what) {
  if (!ids.length) { toast("没有可传送的照片"); return; }
  try {
    const r = await api("/api/airdrop", { ids });
    if (!r.ok) { toast(r.message || "隔空传送失败", true, 9000); return; }
    toast(`已把 ${r.count} 张${what}交给隔空传送，请在系统窗口里选择设备`);
  } catch (e) { toast("隔空传送失败：" + e.message, true, 9000); }
}

function paintSelection() {
  $$("#content .card").forEach((card) => {
    const ids = cardIds(card);
    card.classList.toggle("selected", ids.length > 0 && ids.every((id) => SEL.has(id)));
  });
  $$("#content .frame").forEach((fr) => fr.classList.toggle("selected", SEL.has(fr.dataset.pid)));
  const n = SEL.size;
  $("#selCount").textContent = `已选 ${n} 张`;
  ["selReject", "selUnreject", "selDelete", "selNone", "selAirDrop"].forEach((id) => ($("#" + id).disabled = n === 0));
}

function toggleCardSel(card, shift) {
  const cards = $$("#content .card");
  const idx = cards.indexOf(card);
  if (shift && SEL_LAST !== null && Number.isInteger(SEL_LAST)) {
    const [a, b] = [Math.min(SEL_LAST, idx), Math.max(SEL_LAST, idx)];
    for (let i = a; i <= b; i++) cardIds(cards[i]).forEach((id) => SEL.add(id));
  } else {
    const ids = cardIds(card);
    const on = !ids.every((id) => SEL.has(id));
    ids.forEach((id) => (on ? SEL.add(id) : SEL.delete(id)));
  }
  SEL_LAST = idx;
  paintSelection();
}

async function applyRatingToSelection(rating) {
  const ids = [...SEL];
  if (!ids.length) return;
  await rate(ids, rating, true);
  toast(rating < 0 ? `已把 ${ids.length} 张标记为淘汰` : `已清除 ${ids.length} 张的标记`);
  updatePurgeBtn();
  paintSelection();
}

async function deletePhotos(ids, reason) {
  if (!ids.length) return false;
  const files = ids.reduce((n, id) => n + (LIB.photos[id]?.nf || 1), 0);
  const names = ids.slice(0, 5).map((id) => LIB.photos[id]?.primary?.name || LIB.photos[id]?.name || "").filter(Boolean);
  const more = ids.length > 5 ? ` 等 ${ids.length} 张` : "";
  const ok = await ask("移到废纸篓？",
    `<div class="alert danger"><b>⚠️ 此操作会把 ${ids.length} 张照片的原始文件（RAW / JPG / XMP 共 ${files} 个文件）移到系统废纸篓。</b><br>
     废纸篓可以恢复，也能在「整理记录」里一键恢复；清空废纸篓后就无法找回。</div>
     <p style="font-size:12.5px;color:var(--muted);margin:0 0 6px">即将删除：</p>
     <ul style="font-size:12px;margin:0;padding-left:18px">${names.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
     ${more ? `<p style="font-size:12px;color:var(--muted);margin:6px 0 0">…以及另外 ${ids.length - names.length} 张</p>` : ""}`,
    "移到废纸篓", true);
  if (!ok) return false;
  try {
    await api("/api/trash", { ids, note: reason || "" });
    const job = await waitJob("正在移入废纸篓");
    const r = job.result || {};
    LIB = await api("/api/library");
    SEL.clear();
    render();
    const bad = (r.errors || []).length;
    if (bad) toast(`已移入废纸篓 ${r.moved} 个文件，${bad} 个失败`, true, 8000);
    else toast(`已把 ${r.photos} 张照片移入废纸篓（可在整理记录里恢复）`);
    return true;
  } catch (e) { toast("删除失败：" + e.message, true, 8000); return false; }
}

async function purgeRejected() {
  const ids = Object.keys(LIB.photos).filter((id) => (LIB.photos[id].r || 0) < 0);
  if (!ids.length) { toast("没有已标记淘汰的照片"); return; }
  await deletePhotos(ids, "清理淘汰");
}

// ------------------------------------------------------------------ 升格视频
let VID = { bid: null, frames: 0, span: 0 };

function openVideo(bid) {
  const b = BURSTS[bid];
  if (!b) return;
  VID.bid = bid;
  VID.frames = b.count;
  VID.span = b.duration || 0;
  $("#vidName").textContent = b.name;
  $("#videoModal").hidden = false;
  $("#vidAll").checked = false;
  $("#vidFade").checked = false;
  updateVideoHint();
}

function videoSpec() {
  const n = VID.frames;
  const srcFps = VID.span > 0 ? (n - 1) / VID.span : 10;          // 这组连拍自己的帧率
  const chosen = +$("#vidFps").value;
  const fps = chosen > 0 ? chosen : Math.max(1, Math.round(srcFps));
  const sec = n / fps;                                            // 每帧只出现一次
  return { n, srcFps, fps, sec, tag: `${fps}fps` };
}

function updateVideoHint() {
  const v = videoSpec();
  const rate = v.srcFps >= 1 ? v.srcFps.toFixed(1) : v.srcFps.toFixed(2);
  $("#vidHint").textContent =
    `${v.n} 帧 → 成片 ${v.sec.toFixed(1)} 秒（${v.fps} 帧/秒；原始 ${rate} 帧/秒、实拍约 ${(v.srcFps > 0 ? (v.n - 1) / v.srcFps : 0).toFixed(2)} 秒）`;
  $("#vidOut").textContent =
    `${LIB.root}/_视频/${BURSTS[VID.bid].id.split("/")[0]}_${BURSTS[VID.bid].name}_${v.tag}.mp4`;
}

async function makeVideo() {
  if (!VID.bid) return;
  const spec = videoSpec();
  const body = {
    burst_id: VID.bid,
    fps: spec.fps,
    slow: 1,
    repeat: 1,
    tag: spec.tag,
    max_long: +$("#vidSize").value,
    include_rejected: $("#vidAll").checked,
    fade: $("#vidFade").checked ? 0.25 : 0,
  };
  $("#videoModal").hidden = true;
  try {
    await api("/api/burst/video", body);
    const job = await waitJob("正在生成升格视频");
    const r = job.result || {};
    const open = await ask("视频已生成",
      `<div class="alert ok">${r.source_frames} 张源帧 · 每帧重复 ${r.repeat} 次 → ${r.frames} 帧 @ ${r.fps}fps · ${r.seconds} 秒 · ${(r.bytes / 1048576).toFixed(1)} MB<br><code>${esc(r.path)}</code></div>`,
      "播放");
    if (open) api("/api/reveal", { path: r.path });
  } catch (e) { toast("生成失败：" + e.message, true, 8000); }
}

// ------------------------------------------------------------------ 批量出片
const BATCH = { preset: "standard", ids: [] };

function allItems() {
  const out = [];
  for (const d of LIB.days) for (const it of d.items) out.push(it);
  return out;
}

function collectIds(scope) {
  const P = LIB.photos;
  const rejected = (id) => (P[id]?.r ?? 0) < 0;
  if (scope === "starred") return Object.keys(P).filter((id) => (P[id].r || 0) > 0);
  if (scope === "species") {
    if (!SPECIES_SEL) return [];
    return Object.keys(P).filter((id) => matchesSpecies(P[id]) && (P[id].r || 0) >= 0);
  }
  if (scope === "all") return Object.keys(P).filter((id) => !rejected(id));
  if (scope === "best") {
    const ids = [];
    for (const it of allItems()) {
      if (it.type === "burst" && it.best) ids.push(it.best);
      else if (it.type === "single" && matches(P[it.id], "best")) ids.push(it.id);
    }
    return ids;
  }
  // filter：严格按当前筛选（含已淘汰等）
  const ids = [];
  for (const it of allItems()) {
    // 严格按"筛选命中的每一张"，不再折叠成每组一张（那是"每组最佳"的语义）
    if (it.type === "single") { if (matches(P[it.id])) ids.push(it.id); }
    else if (FILTER === "best") { if (it.best) ids.push(it.best); }
    else it.photo_ids.forEach((id) => { if (matches(P[id])) ids.push(id); });
  }
  return ids.filter(Boolean);
}

function batchSettings() {
  return {
    preset: BATCH.preset,
    denoise: $("#batchDenoise").value,
    subject: $("#batchSubject").checked,
    background: $("#batchBg").checked,
    bit_depth: $("#batch16").checked ? 16 : 8,
  };
}

function openBatch() {
  $("#batchModal").hidden = false;
  $("#batchOut").textContent = `${LIB.root}/_出片/（保持原目录结构）`;
  refreshBatch();
}

function refreshBatch() {
  const scope = $("#batchScope").value;
  BATCH.ids = collectIds(scope);
  const n = BATCH.ids.length;
  const label = { filter: "当前筛选", best: "每组最佳", starred: "已打星", species: "当前鸟种", all: "全部" }[scope];
  $("#batchCount").textContent = `${label}：${fmtN(n)} 张`;
  const tif = $("#batch16").checked;
  const perSec = { off: 1.6, raw: 2.8, ai: 3.4 }[$("#batchDenoise").value] || 2;
  const est = perSec * (tif ? 1.4 : 1);
  $("#batchGo").disabled = n === 0;
  if (scope === "species" && !SPECIES_SEL) $("#batchCount").textContent = "先在筛选栏选择鸟种";
  $("#batchGo").textContent = n ? `开始出片 ${fmtN(n)} 张${tif ? "（TIFF 16 位）" : ""}` : "没有可出的照片";
  const warn = $("#batchWarn");
  if (n > 200) {
    warn.hidden = false;
    warn.textContent = `数量较多：按每张约 ${est} 秒估算，可能要 ${Math.round((n * est) / 60)} 分钟。建议先用小范围试跑。`;
  } else warn.hidden = true;
}

async function runBatch() {
  if (!BATCH.ids.length) return;
  const ids = BATCH.ids.slice();
  const s = batchSettings();
  $("#batchModal").hidden = true;
  try {
    await api("/api/develop/export", { ids, ...s });
    const job = await waitJob(`正在批量出片（${ids.length} 张）`);
    const r = job.result || {};
    const bad = (r.items || []).filter((it) => it.error);
    if (bad.length) {
      await ask("部分照片出片失败", `<div class="alert danger">成功 ${r.ok} 张，失败 ${bad.length} 张。<br>第一个错误：${esc(bad[0].error)}</div>`, "知道了");
    } else {
      const again = await ask("出片完成", `<div class="alert ok">已导出 <b>${r.ok}</b> 张到：<br><code>${esc(r.out_dir)}</code></div>`, "在 Finder 中打开");
      if (again) api("/api/reveal", { path: r.out_dir });
    }
  } catch (e) { toast("批量出片失败：" + e.message, true, 8000); }
}

// ------------------------------------------------------------------ 出片
const DEV = { pid: null, preset: "standard", view: "after", meta: null, url: null, before: null };

function devSettings() {
  return {
    preset: DEV.preset,
    denoise: $("#devDenoise").value,
    subject: $("#devSubject").checked,
    background: $("#devBg").checked,
    bit_depth: $("#dev16").checked ? 16 : 8,
  };
}

function openDevelop(pid) {
  DEV.pid = pid;
  DEV.view = "after";
  $("#devName").textContent = LIB.photos[pid]?.stem || "";
  $("#devModal").hidden = false;
  $("#devImg").removeAttribute("src");
  $("#devLoading").hidden = false;
  $("#devNote").textContent = "";
  $$("#devView button").forEach((b) => b.classList.toggle("on", b.dataset.v === "after"));
  renderDevPreview();
}

let devTimer = null;
function renderDevPreview() {
  if (!DEV.pid) return;
  clearTimeout(devTimer);
  devTimer = setTimeout(async () => {
    const pid = DEV.pid;
    if (!DEV.before) DEV.before = `/thumb/${pid}?s=2048`;
    if (DEV.view === "before") {
      $("#devImg").src = DEV.before;
      $("#devLoading").hidden = true;
      return;
    }
    $("#devLoading").hidden = false;
    try {
      const r = await fetch("/api/develop/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pid, ...devSettings() }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const meta = JSON.parse(r.headers.get("X-Dev-Meta") || "{}");
      const url = URL.createObjectURL(await r.blob());
      // 期间可能已切到"原图"或换了照片：丢弃过期结果，避免覆盖
      if (DEV.pid !== pid || DEV.view !== "after") { URL.revokeObjectURL(url); return; }
      if (DEV.url) URL.revokeObjectURL(DEV.url);
      DEV.url = url; DEV.meta = meta;
      $("#devImg").src = url;
      $("#devLoading").hidden = true;
      const steps = (meta.steps || []).join(" · ") || "无处理步骤";
      $("#devMeta").textContent = meta.cached ? "缓存" : `${meta.ms} ms`;
      const dn = { off: "未降噪", ai: "AI 降噪(sRGB)", raw: "RAW 域降噪(线性)",
                   "ai-fallback": "无 RAW，已退回 AI" }[meta.denoise] || meta.denoise || "";
      $("#devNote").textContent = `${steps}${meta.mask && meta.mask !== "ok" ? ` · ${meta.mask}` : ""}` +
        (dn ? ` · ${dn}` : "");
    } catch (e) {
      $("#devLoading").hidden = true;
      $("#devNote").textContent = "预览失败：" + e.message;
    }
  }, 220);
}

async function exportDevelop() {
  if (!DEV.pid) return;
  const s = devSettings();
  $("#devModal").hidden = true;
  try {
    await api("/api/develop/export", { ids: [DEV.pid], ...s });
    const job = await waitJob("正在出片");
    const r = job.result || {};
    const bad = (r.items || []).filter((it) => it.error);
    const first = (r.items || []).find((it) => !it.error);
    if (bad.length) toast("出片失败：" + bad[0].error, true, 8000);
    else toast(`已导出：${first ? first.path.split("/").pop() : r.out_dir}`);
  } catch (e) { toast("出片失败：" + e.message, true, 8000); }
}

// ------------------------------------------------------------------ plan
async function onTargetMode() {
  const t = $("#targetPath");
  let other = $('input[name="tmode"]:checked').value === "other";
  if (other && !t.dataset.v) {
    const { path } = await api("/api/pick_folder", { path: LIB.root });
    if (path) t.dataset.v = path;
    else { $('input[name="tmode"][value="same"]').checked = true; other = false; }
  }
  $("#btnPickTarget").hidden = !other;
  t.textContent = t.title = other ? t.dataset.v : LIB.root;
  genPlan();
}
function targetRoot() {
  const other = $('input[name="tmode"]:checked').value === "other";
  const t = $("#targetPath").textContent.trim();
  return other && t ? t : null;
}
async function openPlan() {
  $("#planModal").hidden = false;
  $('input[name="tmode"][value="same"]').checked = true;
  $("#btnPickTarget").hidden = true;
  $("#targetPath").textContent = LIB.root;
  $("#optReject").closest(".opt-row").hidden = !LIB.stats.rejected;
  await genPlan();
}
async function genPlan() {
  $("#planAck").checked = false; PLAN = null; updateExecBtn();
  $("#planBody").innerHTML = `<div class="alert ok">正在生成整理预览…</div>`;
  try {
    PLAN = await api("/api/plan", { target_root: targetRoot(), rename_conflicts: $("#optRename").checked, reject_to_folder: $("#optReject").checked });
    renderPlan();
  } catch (e) { $("#planBody").innerHTML = `<div class="alert danger">${esc(e.message)}</div>`; }
  updateExecBtn();
}
function updateExecBtn() {
  const n = PLAN?.summary.files_to_move || 0;
  const b = $("#btnExecute");
  b.textContent = n ? `开始移动 ${fmtN(n)} 个文件` : "开始移动";
  b.disabled = !PLAN || !n || !$("#planAck").checked || !PLAN.summary.enough_space;
  $("#planAck").disabled = !n;
}
function renderPlan() {
  const s = PLAN.summary;
  const movedIds = new Set(PLAN.moves.map((m) => m.photo_id));
  const out = [];
  if (!s.files_to_move) {
    out.push(`<div class="alert ok"><b>所有照片都已经在正确的位置</b>，不需要移动。</div>`);
  } else {
    out.push(`<div class="alert danger"><b>⚠️ 此操作会移动你的原始照片文件。</b> 移动前会逐个检查，绝不覆盖已有文件；每一步都会记录日志，可以随时在“整理记录”里一键撤销。第一次使用建议先拿一个小文件夹试一下。</div>`);
  }
  if (s.rejected) out.push(`<div class="alert warn">${s.rejected} 张已淘汰的照片会放进当天的 <b>_淘汰/</b> 子文件夹（连拍保留原子文件夹名）。不会删除任何文件。</div>`);
  if (s.cross_device) {
    out.push(`<div class="alert ${s.enough_space ? "warn" : "danger"}">目标目录在另一块磁盘上：会先复制、校验大小，再删除原文件，速度会慢一些。需要 ${fmtBytes(s.bytes)}，目标磁盘剩余 ${fmtBytes(s.free_bytes)}${s.enough_space ? "" : "，<b>空间不足，无法执行</b>"}。</div>`);
  }
  out.push(`<div class="summary">
    <div class="sum hl"><div class="v">${fmtN(s.photos_to_move)}</div><div class="k">张照片要移动（${fmtN(s.files_to_move)} 个文件，${fmtBytes(s.bytes)}）</div></div>
    <div class="sum"><div class="v">${s.days}<span style="font-size:13px;font-weight:400"> 天</span> / ${fmtN(s.bursts)}<span style="font-size:13px;font-weight:400"> 组</span></div><div class="k">日期文件夹 / 连拍子文件夹</div></div>
    <div class="sum"><div class="v">${fmtN(s.in_place)}</div><div class="k">张已在正确位置，跳过</div></div>
    <div class="sum ${s.renamed || s.conflicts || s.undated ? "warn" : ""}"><div class="v">${s.renamed}${s.conflicts ? ` / ${s.conflicts}` : ""}</div><div class="k">${s.conflicts ? "重命名 / 冲突跳过" : "张因重名需改名"}${s.undated ? ` · ${s.undated} 张无时间` : ""}</div></div>
  </div>`);

  const tree = [];
  const rejByDay = {};
  for (const id of new Set(PLAN.moves.filter((m) => m.dst_rel.includes("/_淘汰/")).map((m) => m.photo_id))) {
    const day = LIB.photos[id]?.day; if (day) rejByDay[day] = (rejByDay[day] || 0) + 1;
  }
  for (const d of LIB.days) {
    let mv = 0;
    const bursts = [];
    let singles = 0, singlesMv = 0;
    for (const it of d.items) {
      if (it.type === "burst") {
        const m = it.photo_ids.filter((id) => movedIds.has(id)).length; mv += m;
        bursts.push(`<div class="sub"><span class="bn">${esc(it.name)}/</span> <span class="cnt">${it.count} 张</span>${m ? ` <span class="mv">+${m}</span>` : ""}</div>`);
      } else { singles++; if (movedIds.has(it.id)) { singlesMv++; mv++; } }
    }
    const single = singles ? `<div class="sub">单拍照片 <span class="cnt">${singles} 张</span>${singlesMv ? ` <span class="mv">+${singlesMv}</span>` : ""}</div>` : "";
    tree.push(`<details ${LIB.days.length <= 3 ? "open" : ""}><summary>${d.date}/ <span class="cnt">${d.photo_count} 张，${d.burst_count} 组连拍</span>${mv ? ` <span class="mv">移入 ${mv}</span>` : ""}</summary>${bursts.join("")}${single}${rejByDay[d.date] ? `<div class="sub"><span style="color:var(--danger)">_淘汰/</span> <span class="cnt">${rejByDay[d.date]} 张（从上面各组分出来）</span></div>` : ""}</details>`);
  }
  if (LIB.undated.length) tree.push(`<div>unknown-date/ <span class="cnt">${LIB.undated.length} 张</span></div>`);
  out.push(`<div class="sec-title">整理后的目录结构 <span>${esc(PLAN.target_root)}/</span></div><div class="tree">${tree.join("")}</div>`);

  if (PLAN.moves.length) {
    const LIM = 400;
    const rows = PLAN.moves.slice(0, LIM).map((m) => `<tr class="${m.renamed ? "rn" : ""}"><td title="${esc(m.src)}">${esc(m.src_rel)}</td><td class="arrow">→</td><td title="${esc(m.dst)}">${esc(m.dst_rel)}</td></tr>`).join("");
    out.push(`<div class="sec-title">文件明细 <span>${PLAN.moves.length > LIM ? `显示前 ${LIM} 条，共 ${fmtN(PLAN.moves.length)} 条` : `${PLAN.moves.length} 条`}</span></div>
      <div class="moves-wrap"><table class="moves">${rows}</table></div>`);
  }
  if (PLAN.conflicts.length) {
    out.push(`<div class="sec-title">冲突跳过 <span>目标位置已有同名文件，这些照片不会移动</span></div>
      <div class="moves-wrap"><table class="moves">${PLAN.conflicts.map((c) => `<tr><td>${esc(c.name)}</td><td class="arrow">✕</td><td>${esc(c.dst)}</td></tr>`).join("")}</table></div>`);
  }
  $("#planBody").innerHTML = out.join("");
}

async function runPlan() {
  if (!PLAN) return;
  const planId = PLAN.plan_id;
  $("#planModal").hidden = true;
  try {
    await api("/api/execute", { plan_id: planId, cleanup_empty: $("#optCleanup").checked });
    const job = await waitJob("正在整理照片");
    const r = job.result || {};
    LIB = await api("/api/library");
    expanded.clear(); render();
    if (r.error) {
      await ask("整理中途停止", `<div class="alert danger">已移动 ${r.moved}/${r.planned} 个文件后出错：<br>${esc(r.error)}</div><p>已移动的文件可以在“整理记录”里撤销。</p>`, "知道了");
    } else {
      const again = await ask("整理完成", `<div class="alert ok">已移动 <b>${fmtN(r.moved)}</b> 个文件${r.removed_dirs ? `，清理了 ${r.removed_dirs} 个空文件夹` : ""}。</div><p>如果结果不满意，可以立即撤销，文件会回到原来的位置。</p>`, "撤销本次整理", false);
      if (again) await doUndo(r.journal_id);
    }
  } catch (e) { toast("整理失败：" + e.message, true, 8000); }
}

// ------------------------------------------------------------------ history / undo
async function openHistory() {
  $("#historyModal").hidden = false;
  const body = $("#historyBody");
  body.innerHTML = "加载中…";
  try {
    const list = await api("/api/journals");
    if (!list.length) { body.innerHTML = `<p style="color:var(--muted)">还没有整理记录。</p>`; return; }
    body.innerHTML = list.map((j) => {
      const when = j.created ? new Date(j.created * 1000).toLocaleString("zh-CN", { hour12: false }) : j.id;
      const st = j.undone ? `<span class="pill">已撤销</span>` : j.status === "ok" ? `<span class="pill ok">完成</span>` : `<span class="pill err">${j.status === "error" ? "中途出错" : "未完成"}</span>`;
      if (j.kind === "trash") {
        return `<div class="hist"><div class="info"><div class="when">${esc(when)} <span class="pill err">删除</span>${j.undone ? `<span class="pill">已恢复</span>` : ""}</div>
          <div class="where">移到废纸篓 · ${fmtN(j.photos)} 张 · ${fmtN(j.moved)} 个文件${j.errors ? ` · ${j.errors} 个失败` : ""}${j.note ? ` · ${esc(j.note)}` : ""}</div>
          <div style="font-size:12px;color:var(--muted)">${j.undone && j.undo ? `已恢复 ${j.undo.restored} 个文件${j.undo.skipped?.length ? `，跳过 ${j.undo.skipped.length} 个` : ""}` : "可从废纸篓恢复回原位置"}</div></div>
          ${!j.undone && j.moved ? `<button class="btn small" data-restore="${esc(j.id)}">恢复</button>` : ""}</div>`;
      }
      const where = j.source_root === j.target_root ? j.source_root : `${j.source_root} → ${j.target_root}`;
      return `<div class="hist"><div class="info"><div class="when">${esc(when)} ${st}</div>
        <div class="where" title="${esc(where)}">${esc(where)}</div>
        <div style="font-size:12px;color:var(--muted)">移动了 ${fmtN(j.moved)} 个文件${j.error ? ` · ${esc(j.error)}` : ""}${j.undone && j.undo ? ` · 已还原 ${j.undo.restored} 个${j.undo.skipped.length ? `，跳过 ${j.undo.skipped.length} 个` : ""}` : ""}</div></div>
        ${!j.undone && j.moved ? `<button class="btn small" data-undo="${esc(j.id)}">撤销</button>` : ""}</div>`;
    }).join("");
    $$("[data-undo]", body).forEach((b) => (b.onclick = () => doUndo(b.dataset.undo)));
    $$("[data-restore]", body).forEach((b) => (b.onclick = () => doRestore(b.dataset.restore)));
  } catch (e) { body.innerHTML = `<div class="alert danger">${esc(e.message)}</div>`; }
}

async function doRestore(jid) {
  const ok = await ask("从废纸篓恢复？", "会把这些文件从废纸篓移回删除前的位置；如果原位置已有同名文件，会跳过不覆盖。", "恢复", false);
  if (!ok) return;
  $("#historyModal").hidden = true;
  try {
    await api("/api/trash/undo", { journal_id: jid });
    const job = await waitJob("正在恢复");
    const r = job.result || {};
    LIB = await api("/api/library");
    render();
    if (r.error) toast("恢复中途出错：" + r.error, true, 8000);
    else toast(`已恢复 ${fmtN(r.restored)} 个文件${r.skipped?.length ? `，跳过 ${r.skipped.length} 个` : ""}`);
  } catch (e) { toast("恢复失败：" + e.message, true, 8000); }
}

async function doUndo(jid) {
  const ok = await ask("撤销这次整理？", "所有被移动的文件会回到整理前的位置，整理时新建、撤销后变空的文件夹会被删除。", "撤销", true);
  if (!ok) return;
  $("#historyModal").hidden = true;
  try {
    await api("/api/undo", { journal_id: jid });
    const job = await waitJob("正在撤销");
    const r = job.result || {};
    LIB = await api("/api/library");
    expanded.clear(); render();
    if (r.error) toast("撤销中途出错：" + r.error, true, 8000);
    else toast(`已还原 ${fmtN(r.restored)} 个文件${r.skipped?.length ? `，跳过 ${r.skipped.length} 个` : ""}`);
  } catch (e) { toast("撤销失败：" + e.message, true, 8000); }
}

// 无浏览器自测：node /绝对路径/web/app.js --self-test；仅使用合成数据，不调用任何后端。
async function runEyeSelfTests() {
  const assert = require("node:assert/strict");
  const fs = require("node:fs"), path = require("node:path");
  let passed = 0;
  const test = async (name, run) => { await run(); passed++; console.log(`通过：${name}`); };
  const left = { side: "left", x: 0.24, y: 0.35, box: [0.22, 0.33, 0.26, 0.37], confidence: 0.876, stable: true, agreement_px: 1.25, sharpness: { value: 0.42, usable: true, reason: "未校准" } };
  const right = { ...left, side: "right", x: 0.64, box: [0.62, 0.33, 0.66, 0.37] };
  const analysis = {
    eye: { status: "detected", reason: "双视角通过", model: "eye-pose-v0", eyes: [left, right], selected: 1 },
    birds: [{ box: [0.1, 0.2, 0.8, 0.9], conf: 0.95, area: 0.49 }, { box: [0.8, 0.1, 0.9, 0.2], conf: 0.8 }], main: 0,
    focus: { peak_xy: [0.45, 0.6], sharp: 0.7, body: 0.6, bg: 0.2, anis: 0.8, noise: 1 }, af: { x: 0.3, y: 0.4, on_bird: true }, w: 3000, h: 2000,
  };
  const make = (patch) => ({ ...analysis, eye: { ...analysis.eye, ...patch } });
  await test("选中索引保留左右眼顺序，Z 只选 selected 的稳定眼", () => {
    assert.equal(zoomTarget(analysis).x, right.x);
    assert.equal(zoomTarget(make({ selected: 0 })).x, left.x);
    for (const selected of [null, undefined, -1, 2, 0.5, "0"]) assert.equal(zoomTarget(make({ selected })).x, 0.45);
    assert.equal(zoomTarget(make({ eyes: [left, { ...right, stable: false }] })).x, 0.45);
    assert.equal(zoomTarget(make({ status: "uncertain" })).x, 0.45);
  });
  await test("旧数据、空结果与缺失峰值正确回退，并明确黄圈非眼睛", () => {
    assert.match(eyeCard({}), /暂无鸟眼结果/);
    for (const eye of [undefined, null, { eyes: [], selected: null }]) {
      assert.equal(eyeOverlay({ eye }), "");
      assert.equal(zoomTarget({ ...analysis, eye }).x, 0.45);
      assert.match(zoomTarget({ ...analysis, eye }).message, /回退.*黄圈，非眼睛/);
    }
    assert.deepEqual(zoomTarget(null), { x: 0.5, y: 0.5, message: "未提供选中的稳定鸟眼，也无鸟体最锐坐标，已回退到画面中心" });
  });
  await test("七种状态、稳定与候选区分，非识别状态不画旧坐标", () => {
    for (const [status, label] of Object.entries(EYE_STATUS)) {
      const a = make({ status });
      assert.ok(eyeCard({ analysis: a }).includes(label));
      if (!["detected", "uncertain"].includes(status)) assert.equal(eyeOverlay(a), "");
    }
    const mixed = make({ eyes: [left, { ...right, stable: false }] });
    const overlay = eyeOverlay(mixed), card = eyeCard({ analysis: mixed });
    assert.match(overlay, /eye-box stable/); assert.match(overlay, /eye-box candidate/);
    assert.match(overlay, /右眼 · 候选/); assert.match(overlay, /eye-point candidate/);
    assert.match(card, /data-eye-index="0"/);
    // 候选只提供"未确认"的放大入口，不能伪装成已识别眼
    assert.match(card, /data-eye-index="1" data-cand="1"[^>]*>100% 查看候选位置（未确认）/);
    assert.doesNotMatch(card, /data-eye-index="1">100% 查看右眼/);
    assert.doesNotMatch(eyeOverlay(make({ status: "uncertain" })), /eye-box stable/);
    assert.match(eyeCard({ analysis: make({ status: "uncertain" }) }), /data-cand="1"/);
    assert.doesNotMatch(eyeCard({ analysis: make({ status: "uncertain" }) }), /data-eye-index="\d+">100% 查看(左眼|右眼)</);
  });
  await test("整图旋正坐标直用，仅绘制主鸟双眼，拒绝异常坐标", () => {
    const html = eyeOverlay(analysis);
    assert.equal((html.match(/class="eye-box /g) || []).length, 2);
    assert.match(html, /left:22\.0000%;top:33\.0000%;width:4\.0000%;height:4\.0000%/);
    assert.match(html, /left:64\.0000%;top:35\.0000%/);
    assert.equal((html.match(/label-left/g) || []).length, 1);
    for (const patch of [{ x: NaN }, { y: Infinity }, { x: -0.1 }, { x: "0.2" }, { y: 1.1 }, { box: [0.3, 0.3, 0.2, 0.4] }, { box: null }]) {
      const a = make({ eyes: [{ ...left, ...patch }], selected: 0 });
      assert.equal(eyeOverlay(a), ""); assert.equal(zoomTarget(a).x, 0.45);
      assert.doesNotMatch(eyeCard({ analysis: a }), /data-eye-index/);
    }
    const boundary = make({ eyes: [{ ...left, x: 0, y: 1 }], selected: 0 });
    assert.equal(zoomTarget(boundary).x, 0); assert.equal(zoomTarget(boundary).y, 1);
  });
  await test("模型分数非正确率、锐度未校准不评级，原因安全转义", () => {
    const card = eyeCard({ analysis: analysis });
    assert.match(card, /0\.876/); assert.match(card, /非正确率/); assert.match(card, /0\.420/);
    assert.match(card, /未校准，不评级，不影响原身体合焦判断或连拍自动择优/);
    const a = make({ reason: "<img onerror=1>", model: "<script>", eyes: [{ ...left, confidence: 0, sharpness: { value: null, usable: false, reason: "<svg>" } }] });
    const unsafe = eyeCard({ analysis: a });
    assert.doesNotMatch(unsafe, /<(img|script|svg)/); assert.match(unsafe, /&lt;img/);
    assert.match(unsafe, /0\.000/); assert.match(unsafe, /不可用/); assert.doesNotMatch(unsafe, /NaN/);
  });
  await test("鸟眼结果不改变身体合焦卡、缩略图评级和现有筛选", () => {
    const detail = { analysis, f: { g: "sharp", s: 85 } };
    const old = { ...detail, analysis: { ...analysis, eye: undefined } };
    assert.equal(focusCard(detail), focusCard(old));
    const photo = { f: { g: "sharp", s: 85 }, r: 0 };
    const withEye = { ...photo, f: { ...photo.f, eye_status: "detected", eye_count: 2 } };
    assert.equal(fbadge(photo), fbadge(withEye));
    for (const [filter] of FILTERS) assert.equal(matches(photo, filter), matches(withEye, filter));
    assert.ok(!FILTERS.some(([key]) => key === "eyefound" || key === "eyepending"));
  });

  // 最小 DOM 替身只记录节点状态；不启动浏览器、不访问图片或照片接口。
  const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  const nodes = new Map([...html.matchAll(/id="([^"]+)"/g)].map((m) => [`#${m[1]}`, {
    innerHTML: "", textContent: "", src: "", hidden: false, style: {}, attrs: {},
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute(k, v) { this.attrs[k] = v; },
    insertAdjacentHTML(_position, content) { this.innerHTML += content; },
    getBoundingClientRect() { return { width: 800, height: 600 }; },
  }]));
  globalThis.document = { querySelector(selector) { assert.ok(nodes.has(selector), `缺少节点 ${selector}`); return nodes.get(selector); } };
  globalThis.window = { devicePixelRatio: 2 };
  const realZoomAt = zoomAt, realToast = toast;
  const calls = [], messages = [];
  try {
    VIEW = { ids: ["synthetic"], idx: 0, detail: { analysis }, zoom: null };
    await test("E 默认开启，B/E/H 八种组合互不遮挡，旧数据可绘制", () => {
      assert.equal(OV.eye, true);
      for (const box of [false, true]) for (const eye of [false, true]) for (const heat of [false, true]) {
        Object.assign(OV, { box, eye, heat }); drawOverlay();
        const overlay = $("#vOverlay").innerHTML;
        assert.equal(overlay.includes("eye-box stable"), eye);
        assert.equal(overlay.includes("bbox main"), box);
        assert.equal(overlay.includes('class="peak"'), box);
        assert.equal(overlay.includes('class="af"'), box);
        assert.equal($("#vHeat").hidden, !heat);
        assert.equal($("#tEye").attrs["aria-pressed"], String(eye));
      }
      VIEW.detail = {}; drawOverlay(); assert.equal($("#vOverlay").innerHTML, "");
      assert.equal($("#tHeat").disabled, true);
      VIEW.detail = { analysis: { ...analysis, eye: undefined } }; drawOverlay();
      assert.match($("#vOverlay").innerHTML, /class="peak"/);
      VIEW.detail = { analysis };
    });
    await test("键盘 E/B/Z、稳定眼按钮与候选禁用，Z 再按退出", () => {
      zoomAt = (x, y, note) => { calls.push([x, y, note]); VIEW.zoom = { x, y }; };
      toast = (message) => messages.push(message);
      const key = (value) => onKey({ key: value, target: { matches: () => false }, preventDefault() {} });
      Object.assign(OV, { box: true, eye: true, heat: false });
      key("B"); assert.equal(OV.box, false); assert.match($("#vOverlay").innerHTML, /eye-box stable/);
      key("E"); assert.equal(OV.eye, false); assert.equal(OV.box, false);
      key("Z"); assert.deepEqual(calls.at(-1).slice(0, 2), [right.x, right.y]);
      key("z"); assert.equal(VIEW.zoom, null);
      zoomEye(0); assert.deepEqual(calls.at(-1).slice(0, 2), [left.x, left.y]);
      VIEW.detail = { analysis: make({ eyes: [left, { ...right, stable: false }] }) };
      const before = calls.length; zoomEye(1); assert.equal(calls.length, before);
      VIEW.zoom = null; key("z"); assert.deepEqual(calls.at(-1).slice(0, 2), analysis.focus.peak_xy);
      assert.match(calls.at(-1)[2], /已回退.*非眼睛/);
      key("e"); assert.equal(OV.eye, true);
      onKey({ key: "e", target: { matches: () => true } }); assert.equal(OV.eye, true);
      VIEW.detail = { analysis };
    });
    await test("100% 裁剪坐标和 DPR 保持兼容，快速切眼忽略过时响应", async () => {
      zoomAt = realZoomAt;
      const pending = [], revoked = [];
      globalThis.fetch = (url) => new Promise((resolve) => pending.push({ url, resolve }));
      URL.createObjectURL = (blob) => `blob:${blob}`;
      URL.revokeObjectURL = (url) => revoked.push(url);
      const response = (name) => ({ ok: true, headers: { get: () => JSON.stringify({ full_w: 3000, full_h: 2000 }) }, blob: async () => name });
      const first = zoomAt(left.x, left.y), second = zoomAt(right.x, right.y);
      assert.match(pending[1].url, /x=0\.64&y=0\.35&w=1600&h=1200/);
      pending[1].resolve(response("right")); await second;
      pending[0].resolve(response("left")); await first;
      assert.equal($("#vZoomImg").src, "blob:right"); assert.ok(revoked.includes("blob:left"));
      const closing = zoomAt(left.x, left.y); exitZoom();
      pending[2].resolve(response("closed")); await closing;
      assert.ok(revoked.includes("blob:closed")); assert.equal($("#vZoom").hidden, true);
    });
    await test("P3 标签、鸟眼独立按钮与实线/虚线样式存在", () => {
      const css = fs.readFileSync(path.join(__dirname, "style.css"), "utf8");
      assert.match(html, />P3<\/span>/);
      assert.match(css, /\.overlay \.eye-box \{[^}]*border: 2px solid #43e789/);
      assert.match(css, /\.overlay \.eye-box\.candidate \{[^}]*border-style: dashed/);
      assert.match(css, /\.overlay \.eye-point\.candidate \{[^}]*background: transparent/);
      assert.match(css, /\.overlay \{[^}]*isolation: isolate/);
      assert.match(css, /\.overlay \.eye-box\.label-left span \{[^}]*right: -2px/);
      assert.match(css, /--panel: #ffffff/);
    });
  } finally {
    zoomAt = realZoomAt; toast = realToast;
    VIEW = null; Object.assign(OV, { box: true, eye: true, heat: false });
  }
  console.log(`鸟眼 UI 自测通过：${passed} 组；未调用真实后端或照片操作。`);
}

if (typeof document !== "undefined") {
  init().catch((e) => toast("启动失败：" + e.message, true, 10000));
} else if (typeof module !== "undefined" && require.main === module && process.argv.includes("--self-test")) {
  runEyeSelfTests().catch((e) => { console.error(e); process.exitCode = 1; });
}
