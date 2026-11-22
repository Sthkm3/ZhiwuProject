// 吱鼯計畫前端：純 JS，沒有框架
const LANGS = ["zh-TW", "zh-CN", "ja", "ko", "en"];
const HTML_LANG = { "zh-TW": "zh-Hant-TW", "zh-CN": "zh-Hans-CN", ja: "ja", ko: "ko", en: "en" };
const DEFAULT_AVATAR = "/assets/avatar.svg";

const $ = (id) => document.getElementById(id);
const HOME_TITLE = document.title; // 「吱鼯計畫 — 你今天吱吱了嗎」，伺服器會依 BETA 開關加上 (Beta)
const SITE_TITLE = HOME_TITLE.split(" — ")[0]; // 其他頁面的標題只接站名
// 伺服器填入的檔案版本，語言檔網址也帶上，改版後不會拿到快取的舊檔
const ASSET_V = document.querySelector('meta[name="asset-version"]')?.content || "";
const VIDEO_MAX_MS = 4000;   // 吱吱影片約 1.6 秒；ended 沒觸發（例如播放失敗）時的保險時間
const ZHI_GAP_MS = 1100;     // 伺服器一個帳號一秒只收一下；補送沒送成功的次數時，每下隔這麼久
const SUBMIT_MAX_BYTES = 5 * 1024 * 1024;

const state = {
  lang: "zh-TW",
  dict: {},
  me: null,
  today: null,
  email: "",
  cardBust: Date.now(),
  resendTimer: null,
  resetTimer: null,
  guest: null,        // 訪客紀錄 {code, clicks}，只存在這個瀏覽器（localStorage），伺服器不記
  guestOn: false,     // 現在是訪客吱吱的畫面（沒登入）
  installPrompt: null, // Android Chrome 的安裝事件
  zhiMode: null,      // null = 沒開 / "checkin" = 簽到那一下 / "guest" = 訪客那一下 / "more" = 繼續吱吱
  zhiBusy: false,     // 影片播放中：不接受點擊（簽到和繼續吱吱都一樣）
  zhiSkipped: false,  // 今天還沒簽到，但使用者按了「等等再吱」
  zhiPending: 0,      // 還沒送到伺服器的點擊次數
  zhiTimer: null,
  zhiAuto: false,     // 全自動吱吱開著：播完自動接下一次
  zhiAutoTimer: null,
  zhiLastAt: 0,       // 上一次繼續吱吱的時間
  zhiPlay: 0,         // 第幾次播放；離開再進來時，舊的那次播完不能動到新的
  zhiFlush: null,     // 正在送次數的 Promise（離開時等它送完）
};

// ---------- i18n ----------

function detectLang() {
  const saved = localStorage.getItem("lang");
  if (LANGS.includes(saved)) return saved;
  for (const raw of navigator.languages || [navigator.language || ""]) {
    const l = raw.toLowerCase();
    if (/^zh-(tw|hk|mo|hant)/.test(l)) return "zh-TW";
    if (l.startsWith("zh")) return "zh-CN";
    if (l.startsWith("ja")) return "ja";
    if (l.startsWith("ko")) return "ko";
    if (l.startsWith("en")) return "en";
  }
  return "en";
}

async function loadLang(lang) {
  const res = await fetch(`/assets/i18n/${lang}.json?v=${ASSET_V}`);
  state.dict = await res.json();
  state.lang = lang;
  localStorage.setItem("lang", lang);
  document.documentElement.lang = HTML_LANG[lang];
  $("lang").value = lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    el.setAttribute("aria-label", t(el.dataset.i18nAria));
  });
}

function t(key, vars = {}) {
  let s = state.dict[key] ?? key;
  for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v);
  return s;
}

// ---------- API ----------

class ApiError extends Error {
  constructor(code, status) { super(code); this.code = code; this.status = status; }
}

async function api(method, path, body) {
  const opt = { method, headers: { "X-ZHIWU": "1" }, credentials: "same-origin" };
  if (body instanceof FormData) {
    opt.body = body;
  } else if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opt);
  } catch {
    throw new ApiError("network", 0);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(typeof data.detail === "string" ? data.detail : "unknown", res.status);
  return data;
}

function errText(e) {
  const key = `err.${e?.code || "unknown"}`;
  return key in state.dict ? t(key) : t("err.unknown");
}

function showError(el, e) {
  if (e) { el.textContent = errText(e); el.hidden = false; } else { el.hidden = true; }
}

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 2500);
}

// ---------- 側邊欄：登入 / 個人資訊 ----------

// 底色上要用深色字還是白字（和 server/app/titles.py 的 text_on() 同一條公式）
function textOn(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return (r * 299 + g * 587 + b * 114) / 1000 >= 150 ? "#2a1400" : "#ffffff";
}

// UID 與稱號。管理員指定的顏色（title_color）優先，其次是預設的特殊序號（uid_tier，目前只有管理員）
function showUid(el, user) {
  const uid = document.createElement("span");
  uid.textContent = `UID ${user.uid_str}`;
  uid.className = "uid";
  const parts = [uid];
  let title = null;
  if (user.title) {
    title = document.createElement("span");
    title.textContent = user.title;
    title.className = "title-badge";
    parts.push(title);
  }
  if (user.title_color) {
    for (const p of parts) {
      p.classList.add("badge");
      p.style.background = user.title_color;
      p.style.color = textOn(user.title_color);
    }
  } else if (user.uid_tier) {
    for (const p of parts) p.classList.add("badge", `tier-${user.uid_tier}`);
  }
  el.className = "uid-wrap";
  el.replaceChildren(...parts);
}

function renderSidebar() {
  const me = state.me;
  $("auth-box").hidden = !!me;
  $("me-box").hidden = !me;
  if (!me) return;
  $("me-avatar").src = me.avatar_url || DEFAULT_AVATAR;
  $("me-name").textContent = me.display_name;
  showUid($("me-uid"), me);
  $("me-link").href = `/u/${me.uid_str}`;
  $("review-link").hidden = !me.is_admin;
  if (me.is_admin) $("review-link").textContent = t("nav.review", { n: me.pending_reviews ?? 0 });
}

function showCodeStep(show) {
  $("email-form").hidden = show;
  $("code-form").hidden = !show;
  showError($("auth-error"), null);
  if (show) {
    $("code-sent-msg").textContent = t("auth.sent", { email: state.email });
    $("code").value = "";
    $("code").focus();
  }
}

function startResendCountdown(seconds) {
  clearInterval(state.resendTimer);
  const btn = $("resend");
  let left = seconds;
  const tick = () => {
    btn.disabled = left > 0;
    btn.textContent = left > 0 ? t("auth.resend_wait", { s: left }) : t("auth.resend");
    if (left-- <= 0) clearInterval(state.resendTimer);
  };
  tick();
  state.resendTimer = setInterval(tick, 1000);
}

async function sendCode() {
  const r = await api("POST", "/api/auth/request-code", { email: state.email, lang: state.lang });
  showCodeStep(true);
  startResendCountdown(r.resend_in);
}

async function withBusy(button, fn) {
  button.disabled = true;
  try { await fn(); } finally { button.disabled = false; }
}

function bindAuth() {
  $("email-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    state.email = $("email").value.trim();
    withBusy(ev.submitter || $("email-form").querySelector("button"), async () => {
      try { await sendCode(); } catch (e) { showError($("auth-error"), e); }
    });
  });

  $("code-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const code = $("code").value.replace(/\D/g, "");
    withBusy(ev.submitter || $("code-form").querySelector("button[type=submit]"), async () => {
      try {
        const r = await api("POST", "/api/auth/verify", { email: state.email, code, lang: state.lang });
        state.me = r.user;
        state.guestOn = false;
        clearInterval(state.resendTimer);
        showCodeStep(false);
        $("email").value = "";
        if (r.user.lang !== state.lang) await loadLang(r.user.lang);
        renderSidebar();
        closeMenu();
        await route();
      } catch (e) {
        showError($("auth-error"), e);
        if (e.code === "expired" || e.code === "not_requested") $("code").value = "";
      }
    });
  });

  $("resend").addEventListener("click", async () => {
    try { await sendCode(); } catch (e) { showError($("auth-error"), e); }
  });

  $("change-email").addEventListener("click", () => {
    clearInterval(state.resendTimer);
    showCodeStep(false);
    $("email").focus();
  });

  $("logout").addEventListener("click", async () => {
    try { await api("POST", "/api/auth/logout"); } catch { /* 已經登出也沒關係 */ }
    state.me = null;
    state.today = null;
    state.zhiSkipped = false;
    renderSidebar();
    await route();
  });

  for (const id of ["guest-login", "done-login"]) {
    $(id).addEventListener("click", () => {
      openMenu();
      $("email").focus();
    });
  }
}

// ---------- 簽到 ----------

function showCheckinPart(part) {
  for (const id of ["checkin-guest", "checkin-ready", "checkin-done"]) $(id).hidden = id !== part;
}

function statsText(d) {
  return [
    t("checkin.total", { n: d.total_days }),
    t("checkin.streak", { n: d.streak }),
    t("checkin.clicks", { n: fmtNum(d.clicks ?? 0) }),
  ].join("　·　");
}

function fmtNum(n) { return Number(n).toLocaleString("en-US"); }

// 小卡上有吱吱次數，網址帶 c=<次數>，次數變了才會重新下載
function cardSrc(d) { return `${d.card_url}?v=${state.cardBust}&c=${d.clicks}`; }

function renderToday(justChecked) {
  const d = state.today;
  state.guestOn = false;
  $("share-btn").hidden = false;
  $("done-login").hidden = true;
  if (!d.checked_in) {
    showCheckinPart("checkin-ready");
    $("ready-stats").textContent = d.total_days || d.clicks ? statsText(d) : "";
    // 還沒簽到：直接蓋上全螢幕的吱吱畫面（除非使用者剛剛選了等等再吱）
    if (!state.zhiSkipped && state.zhiMode !== "checkin") openStage("checkin");
  } else {
    if (state.zhiMode === "checkin") closeStage(); // 例如在另一個分頁簽到了
    showCheckinPart("checkin-done");
    $("checkin-msg").textContent = justChecked ? t("checkin.success") : t("checkin.done_today");
    const src = cardSrc(d);
    if ($("card-img").getAttribute("src") !== src) $("card-img").src = src;
    $("download-btn").href = src;
    $("download-btn").setAttribute("download", `zhiwu-${d.day}.png`);
    $("done-stats").textContent = statsText(d);
    updateInstallHint();
  }
  startResetCountdown();
}

function fmtDuration(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

function startResetCountdown() {
  clearInterval(state.resetTimer);
  const el = $("next-reset");
  if (!state.today) { el.hidden = true; return; }
  const target = Date.parse(state.today.next_reset);
  // 伺服器以台灣時間 04:00 換日，這裡換算成裝置的當地時間顯示
  const when = new Intl.DateTimeFormat(HTML_LANG[state.lang], {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(target));
  el.hidden = false;
  const tick = async () => {
    const left = target - Date.now();
    el.textContent = t("checkin.next", { time: when, t: fmtDuration(left) });
    if (left <= 0) {
      clearInterval(state.resetTimer);
      if (state.zhiMode === "more") return; // 正在繼續吱吱就不打斷，回到小卡時會重新整理
      state.zhiSkipped = false;
      await refreshToday(); // 換日了
    }
  };
  tick();
  state.resetTimer = setInterval(tick, 1000);
}

async function refreshToday() {
  try {
    state.today = await api("GET", "/api/checkin/today");
    renderToday(false);
  } catch (e) {
    if (e.status === 401) { state.me = null; renderSidebar(); showGuest(); } else toast(errText(e));
  }
}

function showGuest() {
  clearInterval(state.resetTimer);
  $("next-reset").hidden = true;
  if (state.guestOn) { // 正在看訪客小卡（例如換了語言）
    if (state.zhiMode !== "more") renderGuestCard(false);
    return;
  }
  closeStage();
  showCheckinPart("checkin-guest");
  const g = loadGuest();
  $("guest-stats").hidden = !g;
  if (g) $("guest-stats").textContent = guestStatsText(g);
}

// ---------- 訪客吱吱 ----------
// 不用登入。代號（訪客 #XXXX）和吱吱次數只存在這個瀏覽器，伺服器什麼都不記；
// 小卡由伺服器照網址上的代號和次數即時畫出來，沒有 UID、沒有分享連結。

const GUEST_KEY = "zhiwu_guest";
const GUEST_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"; // 去掉容易看錯的 0 O 1 I

function loadGuest() {
  if (state.guest) return state.guest;
  try {
    const g = JSON.parse(localStorage.getItem(GUEST_KEY));
    if (g && /^[A-Z0-9]{4}$/.test(g.code) && Number.isInteger(g.clicks) && g.clicks >= 0) state.guest = g;
  } catch { /* 沒有紀錄或壞掉了：當作新訪客 */ }
  return state.guest;
}

function ensureGuest() {
  if (loadGuest()) return state.guest;
  const rnd = crypto.getRandomValues(new Uint8Array(4));
  state.guest = { code: [...rnd].map((b) => GUEST_CHARS[b % GUEST_CHARS.length]).join(""), clicks: 0 };
  saveGuest();
  return state.guest;
}

function saveGuest() {
  try { localStorage.setItem(GUEST_KEY, JSON.stringify(state.guest)); } catch { /* 無痕模式存不了就只留在這次 */ }
}

function addGuestClick() {
  state.guest.clicks = Math.min(state.guest.clicks + 1, 9999999);
  saveGuest();
}

function guestStatsText(g) {
  return `${t("guest.name", { code: g.code })}　·　${t("checkin.clicks", { n: fmtNum(g.clicks) })}`;
}

function guestCardSrc(g) {
  return `/card/guest.png?code=${g.code}&n=${g.clicks}&lang=${state.lang}`;
}

function renderGuestCard(just) {
  const g = state.guest;
  showCheckinPart("checkin-done");
  $("checkin-msg").textContent = t(just ? "guest.success" : "guest.card");
  const src = guestCardSrc(g);
  if ($("card-img").getAttribute("src") !== src) $("card-img").src = src;
  $("download-btn").href = src;
  $("download-btn").setAttribute("download", `zhiwu-guest-${g.code}.png`);
  $("share-btn").hidden = true;   // 伺服器沒有訪客紀錄，沒有可以分享的連結
  $("done-login").hidden = false;
  $("done-stats").textContent = guestStatsText(g);
  updateInstallHint();
}

// 訪客的那一下：播影片、次數 +1（存在本機），播完而且小卡下載好才亮出小卡
async function zhiGuest(x, y) {
  if (state.zhiBusy) return;
  state.zhiBusy = true;
  $("zhi-exit").disabled = true;
  playVideo();
  burst(x, y);
  const ended = waitVideoEnd();
  addGuestClick();
  state.zhiLastAt = Date.now();
  await Promise.all([ended, preloadImage(guestCardSrc(state.guest))]);
  if (state.zhiMode !== "guest") return;
  closeStage();
  renderGuestCard(true);
}

// ---------- 手機：提示安裝到桌面 ----------

const INSTALL_KEY = "zhiwu_install_dismissed";

function canHintInstall() {
  const mobile = window.matchMedia("(pointer: coarse)").matches && window.matchMedia("(max-width: 760px)").matches;
  const installed = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  let dismissed = false;
  try { dismissed = localStorage.getItem(INSTALL_KEY) === "1"; } catch { /* 讀不到就當作沒關過 */ }
  return mobile && !installed && !dismissed;
}

// 小卡下方的提示：Android Chrome 有安裝按鈕；iPhone 只能教他從分享選單加入主畫面
function updateInstallHint() {
  const el = $("install-hint");
  if (!canHintInstall()) { el.hidden = true; return; }
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  $("install-text").textContent = t(ios ? "install.ios" : state.installPrompt ? "install.android" : "install.generic");
  $("install-btn").hidden = !state.installPrompt;
  el.hidden = false;
}

function bindInstall() {
  window.addEventListener("beforeinstallprompt", (ev) => {
    ev.preventDefault(); // 不要讓瀏覽器自己跳橫幅，改由提示上的按鈕觸發
    state.installPrompt = ev;
    if (!$("checkin-done").hidden) updateInstallHint();
  });
  window.addEventListener("appinstalled", () => { state.installPrompt = null; $("install-hint").hidden = true; });
  $("install-btn").addEventListener("click", async () => {
    const ev = state.installPrompt;
    if (!ev) return;
    state.installPrompt = null;
    ev.prompt();
    await ev.userChoice.catch(() => {});
    $("install-hint").hidden = true;
  });
  $("install-close").addEventListener("click", () => {
    try { localStorage.setItem(INSTALL_KEY, "1"); } catch { /* 存不了就只關這次 */ }
    $("install-hint").hidden = true;
  });
}

async function showCheckin() {
  showView("view-checkin");
  if (!state.me) return showGuest();
  await refreshToday();
}

async function shareCard() {
  const d = state.today;
  const text = `${d.share_text} ${d.share_url}`;
  try {
    const blob = await fetch($("card-img").src).then((r) => r.blob());
    const file = new File([blob], `zhiwu-${d.day}.png`, { type: "image/png" });
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], text });
      return;
    }
    if (navigator.share) {
      await navigator.share({ text: d.share_text, url: d.share_url });
      return;
    }
  } catch (e) {
    if (e.name === "AbortError") return; // 使用者自己取消
  }
  try {
    await navigator.clipboard.writeText(text);
    toast(t("share.copied"));
  } catch {
    window.prompt("", text);
  }
}

// 從點擊的位置噴出粒子（橘色系，夾幾個「吱」字）
function burst(x, y, count = 28) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const spread = Math.min(window.innerWidth, window.innerHeight) * 0.28;
  const colors = ["#ff8a1f", "#ffd166", "#ffb347", "#fff6ea", "#ff6a00"];
  for (let i = 0; i < count; i++) {
    const p = document.createElement("span");
    const word = i % 5 === 0;
    p.className = word ? "particle digit" : "particle";
    if (word) p.textContent = "吱";
    const angle = (i / count) * Math.PI * 2 + Math.random() * 0.5;
    const dist = spread * 0.5 + Math.random() * spread;
    const size = word ? 22 + Math.random() * 18 : 6 + Math.random() * 9;
    p.style.cssText = [
      `--x:${x}px`, `--y:${y}px`,
      `--dx:${(Math.cos(angle) * dist).toFixed(1)}px`, `--dy:${(Math.sin(angle) * dist).toFixed(1)}px`,
      `--s:${size.toFixed(1)}px`, `--c:${colors[i % colors.length]}`,
      `--r:${Math.round(Math.random() * 360 - 180)}deg`, `--d:${(0.7 + Math.random() * 0.4).toFixed(2)}s`,
    ].join(";");
    p.addEventListener("animationend", () => p.remove());
    document.body.appendChild(p);
  }
}

// 先把小卡下載好，切換畫面時才不會看到空白
function preloadImage(src, maxMs = 6000) {
  return new Promise((resolve) => {
    const img = new Image();
    const timer = setTimeout(resolve, maxMs);
    img.onload = img.onerror = () => { clearTimeout(timer); resolve(); };
    img.src = src;
  });
}

// ---------- 全螢幕吱吱 ----------
// 還沒點擊時停在影片第一幀，中間寫「點擊吱吱」。
//   簽到模式（checkin）：點一下 → 有聲播到完 → 送出簽到 → 亮出小卡
//   繼續吱吱（more）：點一下播一次、吱吱次數 +1，播完才能再點（播放中點了沒反應），不會跳去小卡
//     中下方的「全自動吱吱」打開後，播完會自動接著吱下一次，直到關掉或回到小卡

// 影片回到第一幀並暫停，重新顯示「點擊吱吱」
function resetVideo() {
  const video = $("zhi-video");
  video.pause();
  try { video.currentTime = 0; } catch { /* 還沒載入 metadata */ }
  $("zhi-stage").classList.remove("playing");
}

// 從頭播放（一定要在點擊事件裡呼叫，瀏覽器才允許有聲播放）
function playVideo() {
  const video = $("zhi-video");
  $("zhi-stage").classList.add("playing");
  try { video.currentTime = 0; } catch { /* 還沒載入 metadata */ }
  video.muted = false;
  const p = video.play();
  if (p) p.catch(() => { /* 真的不能播就只剩粒子 */ });
}

// 等影片播完（或保險時間到）
function waitVideoEnd() {
  const video = $("zhi-video");
  return new Promise((resolve) => {
    const done = () => { clearTimeout(timer); video.removeEventListener("ended", done); resolve(); };
    const timer = setTimeout(done, VIDEO_MAX_MS);
    video.addEventListener("ended", done);
  });
}

function showZhiCount(bump) {
  const el = $("zhi-count");
  const n = state.guestOn ? state.guest.clicks : state.today?.clicks ?? 0;
  el.textContent = t("zhi.count", { n: fmtNum(n) });
  if (bump) { el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump"); }
}

function openStage(mode) {
  state.zhiMode = mode;
  state.zhiBusy = false;
  state.zhiPlay++;
  resetVideo();
  $("zhi-exit").textContent = t(mode === "more" ? "zhi.back" : "zhi.later");
  $("zhi-exit").disabled = false;
  $("zhi-count").hidden = mode !== "more";
  $("zhi-auto").hidden = mode !== "more";
  setZhiAuto(false);
  if (mode === "more") showZhiCount(false);
  $("zhi-stage").hidden = false;
  document.body.classList.add("zhi-on");
  closeMenu();
}

function closeStage() {
  if (!state.zhiMode) return;
  setZhiAuto(false);
  state.zhiMode = null;
  state.zhiBusy = false;
  resetVideo();
  $("zhi-stage").hidden = true;
  document.body.classList.remove("zhi-on");
}

// 簽到：播影片、噴粒子，同時送出簽到；影片播完而且小卡下載好才亮出小卡
async function zhiCheckin(x, y) {
  if (state.zhiBusy) return;
  state.zhiBusy = true;
  $("zhi-exit").disabled = true;
  playVideo();
  burst(x, y);
  const ended = waitVideoEnd();
  try {
    const today = await api("POST", "/api/checkin");
    await Promise.all([ended, today.card_url ? preloadImage(cardSrc(today)) : null]);
    state.today = today;
    state.zhiSkipped = false;
    closeStage();
    renderToday(!today.already);
    loadMe();
  } catch (e) {
    if (state.zhiMode === "checkin") {
      state.zhiBusy = false;
      $("zhi-exit").disabled = false;
      resetVideo();
    }
    toast(errText(e));
  }
}

// 繼續吱吱：播放中再點沒有反應；播完回到第一幀才能點下一次
function zhiMore(x, y) {
  if (state.zhiBusy) return;
  // 一個帳號一秒只能吱一次（伺服器也會擋）。影片比 1 秒長，只有離開再馬上進來時會遇到
  if (Date.now() - state.zhiLastAt < ZHI_GAP_MS) return;
  state.zhiLastAt = Date.now();
  state.zhiBusy = true;
  const play = ++state.zhiPlay;
  playVideo();
  burst(x, y, 12);
  if (state.guestOn) {
    addGuestClick(); // 訪客：只記在本機，不送伺服器
  } else {
    state.zhiPending++;
    state.today.clicks++;
    flushZhi();
  }
  showZhiCount(true);
  waitVideoEnd().then(() => {
    if (state.zhiMode !== "more" || state.zhiPlay !== play) return; // 播到一半按了回到小卡（或離開後又進來）
    resetVideo();
    state.zhiBusy = false;
    zhiAutoTick(); // 全自動開著就接著吱下一次
  });
}

// ---------- 全自動吱吱 ----------

function setZhiAuto(on) {
  state.zhiAuto = on;
  clearTimeout(state.zhiAutoTimer);
  state.zhiAutoTimer = null;
  const btn = $("zhi-auto");
  btn.textContent = t(on ? "zhi.auto_on" : "zhi.auto");
  btn.classList.toggle("on", on);
  btn.setAttribute("aria-pressed", String(on));
  if (on) zhiAutoTick();
}

// 可以吱就吱一下（從畫面中央）；正在播就等它播完再被叫一次；離上一下不到 1 秒就等滿再吱
function zhiAutoTick() {
  if (!state.zhiAuto || state.zhiMode !== "more" || state.zhiBusy) return;
  const wait = ZHI_GAP_MS - (Date.now() - state.zhiLastAt);
  if (wait > 0) {
    clearTimeout(state.zhiAutoTimer);
    state.zhiAutoTimer = setTimeout(zhiAutoTick, wait);
    return;
  }
  zhiMore(window.innerWidth / 2, window.innerHeight / 2);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// 把還沒送的次數一下一下送給伺服器（伺服器一個帳號一秒只收一下）
function flushZhi() {
  clearTimeout(state.zhiTimer);
  state.zhiTimer = null;
  if (!state.zhiFlush) state.zhiFlush = sendZhi().finally(() => { state.zhiFlush = null; });
  return state.zhiFlush;
}

async function sendZhi() {
  while (state.zhiPending > 0) {
    try {
      const r = await api("POST", "/api/zhi", { n: 1 });
      state.zhiPending--;
      // 以伺服器的數字為準，再加上還沒送出的
      if (state.today) state.today.clicks = r.clicks + state.zhiPending;
    } catch (e) {
      if (e.status === 401 || e.status === 400) { state.zhiPending = 0; return; } // 登出了：這幾下就不算
      state.zhiTimer = setTimeout(flushZhi, ZHI_GAP_MS * 3); // 網路問題：留著等一下再送
      return;
    }
    if (state.zhiPending > 0) await sleep(ZHI_GAP_MS);
  }
  if (state.zhiMode === "more") showZhiCount(false);
}

// 關掉分頁前把還沒送的那一下送出去（keepalive 讓請求在頁面關閉後繼續完成）
function flushZhiOnLeave() {
  if (state.zhiPending <= 0 || state.zhiFlush) return; // 正在送的那個請求會自己完成
  state.zhiPending--;
  fetch("/api/zhi", {
    method: "POST",
    headers: { "X-ZHIWU": "1", "Content-Type": "application/json" },
    credentials: "same-origin",
    keepalive: true,
    body: JSON.stringify({ n: 1 }),
  }).catch(() => {});
}

async function exitStage() {
  const mode = state.zhiMode;
  closeStage();
  if (mode === "guest") {
    state.guestOn = false; // 訪客按了等等再吱：回到首頁
    showGuest();
  } else if (mode === "more" && state.guestOn) {
    renderGuestCard(false); // 小卡換成新的吱吱次數
  } else if (mode === "more") {
    $("done-stats").textContent = statsText(state.today); // 數字先更新，小卡等伺服器收到次數後再換
    await flushZhi();
    await refreshToday(); // 小卡換成新的吱吱次數
    loadMe();
  } else {
    state.zhiSkipped = true; // 等等再吱：留在頁面上，可以用側欄
  }
}

function bindCheckin() {
  const stage = $("zhi-stage");

  stage.addEventListener("click", (ev) => {
    if (ev.target.closest("#zhi-exit, #zhi-auto")) return;
    // 鍵盤觸發的 click 沒有座標，改從畫面中央噴
    const x = ev.clientX || window.innerWidth / 2;
    const y = ev.clientY || window.innerHeight / 2;
    if (state.zhiMode === "checkin") zhiCheckin(x, y);
    else if (state.zhiMode === "guest") zhiGuest(x, y);
    else if (state.zhiMode === "more") zhiMore(x, y);
  });
  $("zhi-exit").addEventListener("click", exitStage);
  $("zhi-auto").addEventListener("click", () => setZhiAuto(!state.zhiAuto));
  $("zhi-open").addEventListener("click", () => { state.zhiSkipped = false; openStage("checkin"); });
  $("zhi-more").addEventListener("click", () => openStage("more"));
  $("guest-zhi").addEventListener("click", () => { ensureGuest(); state.guestOn = true; openStage("guest"); });
  $("share-btn").addEventListener("click", shareCard);

  window.addEventListener("pagehide", flushZhiOnLeave);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushZhiOnLeave();
  });
}

// ---------- 畫面切換 ----------

function showView(id) {
  for (const v of ["view-checkin", "view-user", "view-upload", "view-review"]) $(v).hidden = v !== id;
  if (id !== "view-checkin") { clearInterval(state.resetTimer); closeStage(); state.guestOn = false; }
}

// ---------- 投稿迷因圖 ----------

function renderUploads(r) {
  $("upload-quota").textContent = t("upload.remaining", { n: r.remaining, max: r.limit });
  $("upload-pick").classList.toggle("busy", r.remaining <= 0);
  const list = $("upload-list");
  list.replaceChildren();
  $("upload-empty").hidden = r.items.length > 0;
  for (const it of r.items) {
    const box = document.createElement("div");
    box.className = "sub-item";
    if (it.image_url) {
      const img = document.createElement("img");
      img.src = it.image_url;
      img.alt = "";
      img.loading = "lazy";
      box.append(img);
    }
    const badge = document.createElement("span");
    badge.className = `sub-status ${it.status}`;
    badge.textContent = t(`upload.status.${it.status}`);
    box.append(badge);
    list.append(box);
  }
}

async function refreshUploads() {
  try {
    renderUploads(await api("GET", "/api/submissions"));
  } catch (e) {
    showError($("upload-error"), e);
    $("upload-pick").classList.remove("busy");
  }
}

async function showUpload() {
  showView("view-upload");
  document.title = `${t("nav.upload")} · ${SITE_TITLE}`;
  showError($("upload-error"), null);
  if (!state.me) { location.replace("/"); return; }
  await refreshUploads();
}

function bindUpload() {
  $("upload-file").addEventListener("change", async (ev) => {
    const files = [...ev.target.files];
    ev.target.value = "";
    if (!files.length) return;
    $("upload-pick").classList.add("busy");
    showError($("upload-error"), null);
    let ok = 0;
    for (const file of files) { // 一張一張傳，額度用完就停
      if (file.size > SUBMIT_MAX_BYTES) { showError($("upload-error"), { code: "file_too_large" }); continue; }
      const fd = new FormData();
      fd.append("file", file);
      try {
        await api("POST", "/api/submissions", fd);
        ok++;
      } catch (e) {
        showError($("upload-error"), e);
        if (e.code === "quota_exceeded") break;
      }
    }
    if (ok) toast(t("upload.done", { n: ok }));
    await refreshUploads();
  });
}

// ---------- 管理員審核（一張一張） ----------

async function loadReview() {
  try {
    const r = await api("GET", "/api/admin/submissions");
    $("review-left").textContent = t("review.left", { n: r.pending });
    state.me.pending_reviews = r.pending;
    renderSidebar();
    const it = r.item;
    $("review-item").hidden = !it;
    $("review-empty").hidden = !!it;
    if (!it) return;
    $("review-item").dataset.id = it.id;
    $("review-img").src = it.image_url;
    $("review-name").textContent = it.uploader.display_name;
    $("review-name").href = `/u/${it.uploader.uid_str}`;
    showUid($("review-uid"), it.uploader);
    $("review-time").textContent = new Date(it.created_at).toLocaleString(HTML_LANG[state.lang]);
  } catch (e) {
    if (e.status === 401 || e.status === 403) { location.replace("/"); return; }
    toast(errText(e));
  }
}

async function showReview() {
  showView("view-review");
  document.title = `${t("review.heading")} · ${SITE_TITLE}`;
  if (!state.me?.is_admin) { location.replace("/"); return; }
  await loadReview();
}

function bindReview() {
  const buttons = [$("review-approve"), $("review-reject")];
  const decide = async (action) => {
    const sid = $("review-item").dataset.id;
    buttons.forEach((b) => { b.disabled = true; });
    try {
      await api("POST", `/api/admin/submissions/${sid}/${action}`);
      toast(t(`review.${action}d`));
    } catch (e) {
      // 已經審過（例如另一個分頁）就直接跳下一張
      if (e.code !== "already_reviewed" && e.code !== "not_found") toast(errText(e));
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
    await loadReview();
  };
  $("review-approve").addEventListener("click", () => decide("approve"));
  $("review-reject").addEventListener("click", () => decide("reject"));
}

// ---------- 個人頁 ----------

async function showUser(uid) {
  showView("view-user");
  try {
    const u = await api("GET", `/api/users/${uid}?lang=${state.lang}`);
    $("user-card").hidden = false;
    $("user-missing").hidden = true;
    $("user-avatar").src = u.avatar_url || DEFAULT_AVATAR;
    $("user-name").textContent = u.display_name;
    showUid($("user-uid"), u);
    // 管理員：下面多一個設定這個 UID 顏色與稱號的表單
    const form = $("title-form");
    form.hidden = !state.me?.is_admin;
    form.dataset.uid = u.uid;
    $("title-text").value = u.title || "";
    $("title-color").value = u.title_color || "#ff8a1f";
    showError($("title-error"), null);
    $("user-stats").textContent = statsText(u);
    const bio = $("user-bio");
    if (u.bio_md.trim()) {
      bio.innerHTML = u.bio_html; // 伺服器端已關閉原始 HTML
      bio.classList.remove("empty");
    } else {
      bio.textContent = t("user.no_bio");
      bio.classList.add("empty");
    }
    const joined = new Date(u.created_at).toLocaleDateString(HTML_LANG[state.lang]);
    $("user-joined").textContent = t("user.joined", { d: joined });
    document.title = `${u.display_name} · ${SITE_TITLE}`;
  } catch {
    $("user-card").hidden = true;
    $("user-missing").hidden = false;
  }
}

// ---------- 管理員：指定 UID 的顏色與稱號 ----------

function bindTitle() {
  const form = $("title-form");
  const save = async (body) => {
    try {
      const u = await api("PUT", `/api/admin/users/${form.dataset.uid}/title`, body);
      state.cardBust = Date.now();
      toast(t("title.saved"));
      if (state.me && u.uid === state.me.uid) await loadMe();
      await showUser(u.uid_str);
    } catch (e) {
      showError($("title-error"), e);
    }
  };
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    withBusy($("title-save"), () => save({ title: $("title-text").value, color: $("title-color").value }));
  });
  $("title-clear").addEventListener("click", () => withBusy($("title-clear"), () => save({ title: "", color: "" })));
}

// ---------- 編輯個人資料 ----------

function bindProfile() {
  const dialog = $("profile-dialog");

  $("edit-profile").addEventListener("click", () => {
    const me = state.me;
    $("edit-avatar").src = me.avatar_url || DEFAULT_AVATAR;
    $("edit-name").value = me.display_name;
    $("edit-bio").value = me.bio_md;
    setPreview(false);
    showError($("profile-error"), null);
    closeMenu();
    dialog.showModal();
  });

  $("profile-cancel").addEventListener("click", () => dialog.close());

  $("avatar-file").addEventListener("change", async (ev) => {
    const file = ev.target.files[0];
    ev.target.value = "";
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) return showError($("profile-error"), { code: "file_too_large" });
    const fd = new FormData();
    fd.append("file", file);
    try {
      state.me = await api("PUT", "/api/me/avatar", fd);
      state.cardBust = Date.now();
      $("edit-avatar").src = state.me.avatar_url;
      showError($("profile-error"), null);
      renderSidebar();
    } catch (e) {
      showError($("profile-error"), e);
    }
  });

  $("preview-toggle").addEventListener("click", async () => {
    if (!$("edit-preview").hidden) return setPreview(false);
    try {
      const r = await api("POST", "/api/markdown", { md: $("edit-bio").value });
      $("edit-preview").innerHTML = r.html;
      setPreview(true);
    } catch (e) {
      showError($("profile-error"), e);
    }
  });

  $("profile-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    withBusy($("profile-save"), async () => {
      try {
        state.me = await api("PATCH", "/api/me", {
          display_name: $("edit-name").value,
          bio_md: $("edit-bio").value,
        });
        state.cardBust = Date.now();
        dialog.close();
        toast(t("profile.saved"));
        renderSidebar();
        await route();
      } catch (e) {
        showError($("profile-error"), e);
      }
    });
  });
}

function setPreview(on) {
  $("edit-preview").hidden = !on;
  $("edit-bio").hidden = on;
  $("preview-toggle").textContent = on ? t("profile.edit_back") : t("profile.preview");
}

// ---------- 手機選單 ----------

function openMenu() { $("sidebar").classList.add("open"); }
function closeMenu() { $("sidebar").classList.remove("open"); }

// ---------- 啟動 ----------

async function loadMe() {
  try {
    state.me = await api("GET", "/api/me");
  } catch {
    state.me = null;
  }
  renderSidebar();
}

async function route() {
  document.title = HOME_TITLE;
  const m = location.pathname.match(/^\/u\/(\d+)$/);
  if (m) return showUser(m[1]);
  if (location.pathname === "/upload") return showUpload();
  if (location.pathname === "/review") return showReview();
  return showCheckin();
}

async function init() {
  await loadLang(detectLang());
  bindAuth();
  bindCheckin();
  bindProfile();
  bindTitle();
  bindInstall();
  bindUpload();
  bindReview();

  $("menu-toggle").addEventListener("click", () => $("sidebar").classList.toggle("open"));

  $("lang").addEventListener("change", async (ev) => {
    await loadLang(ev.target.value);
    if (state.me) {
      try {
        // 語言也會用在信件、小卡與分享文字，所以同步到伺服器
        state.me = await api("PATCH", "/api/me", { lang: state.lang });
        state.cardBust = Date.now();
      } catch { /* 介面已經切換，伺服器同步失敗就算了 */ }
    }
    renderSidebar();
    if (!$("code-form").hidden) $("code-sent-msg").textContent = t("auth.sent", { email: state.email });
    if (state.zhiMode) { // 全螢幕畫面上的按鈕文字不是 data-i18n，換語言時重填
      $("zhi-exit").textContent = t(state.zhiMode === "more" ? "zhi.back" : "zhi.later");
      $("zhi-auto").textContent = t(state.zhiAuto ? "zhi.auto_on" : "zhi.auto");
      showZhiCount(false);
    }
    await route();
  });

  await loadMe();
  await route();
}

init();
