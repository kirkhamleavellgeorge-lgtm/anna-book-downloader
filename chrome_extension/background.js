"use strict";

// This connector never requests cookies, browser history, or access to other tabs.
// WebSocket activity every 20 seconds keeps MV3 workers alive on Chrome 116+:
// https://developer.chrome.com/docs/extensions/how-to/web-platform/websockets
const PROTOCOL_VERSION = "1.0";
const RECONNECT_ALARM = "anna-book-helper-reconnect";
const STAGING_DIRECTORY = "AnnaBookHelper";
const BOOK_EXTENSIONS = new Set([
  "epub", "pdf", "mobi", "azw3", "djvu", "txt", "fb2", "cbz", "cbr", "docx", "rtf",
]);
const ANNA_HOSTS = new Set(["annas-archive.pk", "annas-archive.gl", "annas-archive.gd"]);
const ownedTabs = new Map();
const ownedDownloads = new Map();
const pendingApiDownloads = new Map();
let pendingEvents = [];
let socket = null;
let authenticated = false;
let heartbeatTimer = null;
let handshakeTimer = null;
let connectFlight = null;
let persistFlight = Promise.resolve();
let connectionStatus = {connected: false, detail: "等待本机程序连接", updatedAt: Date.now()};

const ready = (async () => {
  const saved = await chrome.storage.session.get(["ownedTabs", "ownedDownloads", "pendingEvents"]);
  for (const [id, info] of Object.entries(saved.ownedTabs || {})) {
    if (Number.isSafeInteger(Number(id)) && Number(id) >= 0) ownedTabs.set(Number(id), info);
  }
  for (const [id, info] of Object.entries(saved.ownedDownloads || {})) {
    if (Number.isSafeInteger(Number(id)) && Number(id) >= 0 && info.api === true) ownedDownloads.set(Number(id), info);
  }
  pendingEvents = Array.isArray(saved.pendingEvents) ? saved.pendingEvents.filter((packet) =>
    !String(packet.event || "").startsWith("download.") || ownedDownloads.has(packet.downloadId)).slice(-200) : [];
})();

function persist() {
  const value = {
    ownedTabs: Object.fromEntries(ownedTabs),
    ownedDownloads: Object.fromEntries(ownedDownloads),
    pendingEvents: pendingEvents.slice(-200),
  };
  persistFlight = persistFlight.catch(() => {}).then(() => chrome.storage.session.set(value));
  return persistFlight;
}

function messageError(error) {
  return error instanceof Error ? error.message : String(error);
}

function send(packet) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  try {
    socket.send(JSON.stringify(packet));
    return true;
  } catch (_) {
    return false;
  }
}

function emitEvent(event, fields) {
  const packet = {type: "event", event, ...fields};
  if (!authenticated || !send(packet)) {
    pendingEvents.push(packet);
    pendingEvents = pendingEvents.slice(-200);
  }
  void persist();
}

function flushEvents() {
  while (authenticated && pendingEvents.length && send(pendingEvents[0])) pendingEvents.shift();
  void persist();
}

function setStatus(connected, detail) {
  connectionStatus = {connected, detail, updatedAt: Date.now()};
  void chrome.action.setBadgeText({text: connected ? "ON" : ""});
  void chrome.action.setBadgeBackgroundColor({color: connected ? "#236b46" : "#67768d"});
  void chrome.action.setTitle({title: `Anna 图书助手：${detail}`});
}

function clearConnectionTimers() {
  clearInterval(heartbeatTimer);
  clearTimeout(handshakeTimer);
  heartbeatTimer = null;
  handshakeTimer = null;
}

function validatedConfig(raw) {
  const target = new URL(raw.url);
  if (target.protocol !== "ws:" || target.hostname !== "127.0.0.1" || !target.port ||
      target.username || target.password || target.pathname !== "/" || target.search || target.hash) {
    throw new Error("本机连接配置无效，请从图书助手重新生成连接配置");
  }
  if (typeof raw.token !== "string" || raw.token.length < 16 || raw.token.length > 512) {
    throw new Error("本机连接令牌无效，请重新启动图书助手");
  }
  return {url: target.href, token: raw.token};
}

async function connect(force = false) {
  await ready;
  if (connectFlight) return connectFlight;
  if (!force && socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  connectFlight = (async () => {
    if (socket) {
      const previous = socket;
      socket = null;
      previous.close();
    }
    authenticated = false;
    clearConnectionTimers();
    setStatus(false, "正在连接本机程序");
    try {
      // The Python app rewrites this file. Do not retain an old port/token.
      const response = await fetch(chrome.runtime.getURL("local-config.json"), {cache: "no-store"});
      if (!response.ok) throw new Error("请先打开本机图书助手，再点击重新连接");
      const config = validatedConfig(await response.json());
      const candidate = new WebSocket(config.url);
      socket = candidate;
      candidate.onopen = () => {
        if (socket !== candidate) return;
        send({type: "hello", token: config.token, extensionId: chrome.runtime.id, version: PROTOCOL_VERSION});
        handshakeTimer = setTimeout(() => {
          if (socket === candidate && !authenticated) candidate.close();
        }, 8000);
        heartbeatTimer = setInterval(() => {
          if (socket === candidate) send({type: "ping"});
        }, 20000);
      };
      candidate.onmessage = (event) => {
        if (socket !== candidate) return;
        let packet;
        try { packet = JSON.parse(event.data); } catch (_) { return; }
        if (packet.type === "hello" && packet.connected === true) {
          authenticated = true;
          clearTimeout(handshakeTimer);
          setStatus(true, "已连接本机程序，使用当前 Chrome 会话");
          flushEvents();
        } else if (authenticated && packet.type === "command") {
          void handleCommand(packet, candidate);
        }
      };
      candidate.onclose = () => {
        if (socket !== candidate) return;
        socket = null;
        authenticated = false;
        clearConnectionTimers();
        setStatus(false, "本机程序未连接；稍后自动重连，也可点击重新连接");
      };
      candidate.onerror = () => {
        if (socket === candidate) setStatus(false, "未能连接本机程序，请确认图书助手已打开");
      };
    } catch (error) {
      setStatus(false, messageError(error));
    }
  })();
  try { await connectFlight; } finally { connectFlight = null; }
}

function requireId(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`${label}无效`);
  return value;
}

function bookExtension(value) {
  const extension = String(value || "").toLowerCase();
  if (!BOOK_EXTENSIONS.has(extension)) throw new Error("不支持的图书文件格式");
  return extension;
}

function navigationUrl(value, allowBlank = false) {
  if (allowBlank && value === "about:blank") return value;
  const url = new URL(value);
  if (url.username || url.password ||
      !(url.protocol === "https:" && ANNA_HOSTS.has(url.hostname) ||
        url.protocol === "http:" && url.hostname === "127.0.0.1")) {
    throw new Error("仅允许读取 Anna 网站和本机测试页面");
  }
  return url.href;
}

function downloadUrl(value) {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
    throw new Error("下载地址必须为公开 HTTP(S) 地址");
  }
  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  if (host === "127.0.0.1") return url.href; // Local HTTP fixture, never other loopback hosts.
  if (!host.includes(".") || host.includes(":") || /(?:^|\.)(?:localhost|local|internal|lan|home)$/.test(host)) {
    throw new Error("不允许从本地或专用网络地址下载");
  }
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
    const [a, b] = host.split(".").map(Number);
    if (a === 0 || a === 10 || a === 127 || a >= 224 ||
        a === 169 && b === 254 || a === 172 && b >= 16 && b <= 31 ||
        a === 192 && (b === 0 || b === 168) || a === 100 && b >= 64 && b <= 127 ||
        a === 198 && (b === 18 || b === 19)) {
      throw new Error("不允许从本地或专用网络地址下载");
    }
  }
  return url.href;
}

async function ownedTab(tabId) {
  requireId(tabId, "标签页 ID");
  if (!ownedTabs.has(tabId)) throw new Error("此标签页并非由图书助手创建");
  const tab = await chrome.tabs.get(tabId);
  if (tab.url) ownedTabs.get(tabId).url = tab.url;
  return tab;
}

function ownedDownload(downloadId) {
  requireId(downloadId, "下载 ID");
  const record = ownedDownloads.get(downloadId);
  if (!record) throw new Error("此下载并非由图书助手创建");
  return record;
}

async function snapshot(tabId) {
  const tab = await ownedTab(tabId);
  const currentUrl = tab.url || ownedTabs.get(tabId).url;
  if (currentUrl === "about:blank") {
    return {url: currentUrl, title: "", body: "", html: "", countdownCount: 0, countdownText: ""};
  }
  navigationUrl(currentUrl);
  const frames = await chrome.scripting.executeScript({
    target: {tabId},
    injectImmediately: true,
    func: () => {
      const countdown = document.querySelectorAll(".js-partner-countdown");
      return {
        url: location.href,
        title: document.title,
        body: document.body ? document.body.innerText : "",
        html: document.documentElement ? document.documentElement.outerHTML : "",
        countdownCount: countdown.length,
        countdownText: countdown.length ? (countdown[0].innerText || "").trim() : "",
      };
    },
  });
  if (!frames.length || !frames[0].result) throw new Error("网页尚未可读取，请稍后重试");
  navigationUrl(frames[0].result.url);
  ownedTabs.get(tabId).url = frames[0].result.url;
  await persist();
  return frames[0].result;
}

async function navigate(params) {
  const tabId = params.tabId;
  await ownedTab(tabId);
  const url = navigationUrl(params.url, true);
  if (params.extension) ownedTabs.get(tabId).extension = bookExtension(params.extension);
  let listener;
  let timer;
  let finish;
  const loaded = new Promise((resolve) => {
    finish = resolve;
    listener = (changedId, change) => {
      if (changedId === tabId && change.status === "complete") resolve();
    };
    chrome.tabs.onUpdated.addListener(listener);
    timer = setTimeout(resolve, 25000);
  });
  try {
    const updated = await chrome.tabs.update(tabId, {url});
    if (updated.status === "complete" && updated.url === url) finish();
    await loaded;
    return await snapshot(tabId);
  } finally {
    clearTimeout(timer);
    chrome.tabs.onUpdated.removeListener(listener);
  }
}

function newStagingName(extension) {
  return `${STAGING_DIRECTORY}/${crypto.randomUUID()}.${bookExtension(extension)}`;
}

async function trackCreated(item, explicit = null) {
  await ready;
  let record = ownedDownloads.get(item.id);
  if (!record && explicit) {
    record = {...explicit, createdEmitted: false};
    ownedDownloads.set(item.id, record);
  }
  if (!record && item.byExtensionId === chrome.runtime.id) {
    const pending = [...pendingApiDownloads.values()].find((info) => info.url === item.url);
    if (pending) {
      record = {...pending, createdEmitted: false};
      ownedDownloads.set(item.id, record);
    }
  }
  // Native browser downloads are deliberately excluded, even when their referrer
  // matches an owned tab. Another user tab may be displaying that identical URL.
  if (!record) return null;
  if (!record.createdEmitted) {
    record.createdEmitted = true;
    emitEvent("download.created", {tabId: record.tabId, downloadId: item.id,
      url: item.url || record.url, suggestedFilename: String(item.filename || "").split(/[\\/]/).pop() || record.stagingName.split("/").pop()});
  }
  await persist();
  return record;
}

async function startDownload(params) {
  await ownedTab(params.tabId);
  const url = downloadUrl(params.url);
  const extension = bookExtension(params.extension);
  const stagingName = newStagingName(extension);
  const record = {tabId: params.tabId, url, extension, stagingName, api: true};
  pendingApiDownloads.set(stagingName, record);
  try {
    const downloadId = await chrome.downloads.download({url, filename: stagingName, saveAs: false, conflictAction: "uniquify"});
    // onCreated can arrive before the API promise returns. Both paths deduplicate.
    const items = await chrome.downloads.search({id: downloadId});
    await trackCreated(items[0] || {id: downloadId, url, filename: stagingName}, record);
    return {downloadId};
  } finally {
    pendingApiDownloads.delete(stagingName);
  }
}

async function downloadStatus(downloadId) {
  ownedDownload(downloadId);
  const items = await chrome.downloads.search({id: downloadId});
  if (!items.length) throw new Error("该下载已从 Chrome 下载记录中移除");
  const item = items[0];
  return {state: item.state, filename: item.filename, bytesReceived: item.bytesReceived,
    totalBytes: item.totalBytes, error: item.error || "", url: item.finalUrl || item.url,
    danger: item.danger || "safe"};
}

async function cancelDownload(downloadId) {
  const before = await downloadStatus(downloadId);
  // Chrome rejects cancel() after completion. Stopping a queue is idempotent.
  if (before.state !== "in_progress") return {cancelled: false, state: before.state};
  try {
    await chrome.downloads.cancel(downloadId);
  } catch (error) {
    const current = await downloadStatus(downloadId);
    if (current.state === "in_progress") throw error;
    return {cancelled: current.state === "interrupted", state: current.state};
  }
  const current = await downloadStatus(downloadId);
  return {cancelled: current.state === "interrupted", state: current.state};
}

async function closeTab(tabId) {
  requireId(tabId, "标签页 ID");
  if (!ownedTabs.has(tabId)) throw new Error("此标签页并非由图书助手创建");
  try { await chrome.tabs.remove(tabId); } catch (_) { /* The user may have already closed it. */ }
  if (ownedTabs.delete(tabId)) emitEvent("tab.closed", {tabId});
  await persist();
  return {closed: true};
}

async function dispatch(method, params = {}) {
  await ready;
  switch (method) {
    case "ping": return {ok: true, version: PROTOCOL_VERSION};
    case "tab.create": {
      const url = navigationUrl(params.url || "about:blank", true);
      const extension = params.extension ? bookExtension(params.extension) : null;
      // Always create blank first, so navigation/download events cannot precede ownership.
      const tab = await chrome.tabs.create({url: "about:blank", active: true});
      ownedTabs.set(tab.id, {url: "about:blank", extension});
      await persist();
      if (url !== "about:blank") await navigate({tabId: tab.id, url, extension});
      return {tabId: tab.id};
    }
    case "tab.navigate": return navigate(params);
    case "tab.snapshot": return snapshot(params.tabId);
    case "tab.activate": {
      const tab = await ownedTab(params.tabId);
      await chrome.tabs.update(params.tabId, {active: true});
      await chrome.windows.update(tab.windowId, {focused: true});
      return {active: true};
    }
    case "tab.close": return closeTab(params.tabId);
    case "download.start": return startDownload(params);
    case "download.status": return downloadStatus(params.downloadId);
    case "download.cancel": return cancelDownload(params.downloadId);
    case "download.remove": {
      const record = ownedDownload(params.downloadId);
      const items = await chrome.downloads.search({id: params.downloadId});
      if (items.length) {
        const item = items[0];
        if (item.state === "in_progress") throw new Error("请先取消正在进行的下载");
        const filename = String(item.filename || "").replace(/\\/g, "/");
        const expected = `/${record.stagingName}`;
        // Only staging files named by this extension may be deleted.
        if (filename && !filename.endsWith(expected)) throw new Error("下载文件不在助手的临时目录中，未删除文件");
        if (item.exists !== false && item.state === "complete") {
          try { await chrome.downloads.removeFile(params.downloadId); } catch (_) { /* Python may have moved it already. */ }
        }
        await chrome.downloads.erase({id: params.downloadId});
      }
      ownedDownloads.delete(params.downloadId);
      await persist();
      return {removed: true};
    }
    case "context.close": {
      const ids = Array.isArray(params.tabIds) ? params.tabIds : [];
      for (const tabId of ids) requireId(tabId, "标签页 ID");
      const closingTabs = new Set(ids);
      // A page can close while its transfer continues. Only this context's
      // registered in-progress downloads may be cancelled; completed source
      // files remain available for the Python app's copy and validation.
      for (const [downloadId, record] of [...ownedDownloads]) {
        if (!closingTabs.has(record.tabId)) continue;
        try {
          const status = await downloadStatus(downloadId);
          if (status.state !== "in_progress") continue;
          const result = await cancelDownload(downloadId);
          if (result.state === "interrupted") {
            await chrome.downloads.erase({id: downloadId});
            ownedDownloads.delete(downloadId);
          }
        } catch (_) { /* A missing download must not prevent owned tab cleanup. */ }
      }
      for (const tabId of ids) {
        if (ownedTabs.has(tabId)) await closeTab(tabId);
      }
      await persist();
      return {closed: true};
    }
    default: throw new Error(`不支持的连接器操作：${method}`);
  }
}

async function handleCommand(packet, sourceSocket) {
  if ((typeof packet.id !== "string" && typeof packet.id !== "number") || typeof packet.method !== "string") return;
  let reply;
  try {
    reply = {type: "result", id: packet.id, result: await dispatch(packet.method, packet.params || {})};
  } catch (error) {
    reply = {type: "result", id: packet.id, error: messageError(error)};
  }
  if (socket === sourceSocket && authenticated) send(reply);
}

chrome.tabs.onUpdated.addListener((tabId, change) => {
  void ready.then(() => {
    if (ownedTabs.has(tabId) && change.url) {
      ownedTabs.get(tabId).url = change.url;
      return persist();
    }
  });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void ready.then(() => {
    if (ownedTabs.delete(tabId)) emitEvent("tab.closed", {tabId});
  });
});

chrome.downloads.onCreated.addListener((item) => { void trackCreated(item); });

chrome.downloads.onChanged.addListener((change) => {
  void ready.then(() => {
    const record = ownedDownloads.get(change.id);
    if (record && (change.state || change.error || change.danger)) {
      emitEvent("download.state", {tabId: record.tabId, downloadId: change.id,
        state: change.state ? change.state.current : undefined,
        error: change.error ? change.error.current : undefined,
        danger: change.danger ? change.danger.current : undefined});
    }
  });
});

chrome.runtime.onMessage.addListener((request, sender, respond) => {
  if (sender.id !== chrome.runtime.id) return false;
  if (request.type === "status") {
    respond({...connectionStatus, extensionId: chrome.runtime.id,
      ownedTabCount: ownedTabs.size, ownedDownloadCount: ownedDownloads.size});
    return false;
  }
  if (request.type === "reconnect") {
    void connect(true).then(() => respond(connectionStatus)).catch((error) => respond({connected: false, detail: messageError(error)}));
    return true;
  }
  return false;
});

async function ensureReconnectAlarm() {
  const current = await chrome.alarms.get(RECONNECT_ALARM);
  if (!current) {
    // Half-minute alarms require Chrome 120; WebSocket support itself needs 116.
    const match = navigator.userAgent.match(/Chrome\/(\d+)/);
    const periodInMinutes = match && Number(match[1]) >= 120 ? 0.5 : 1;
    await chrome.alarms.create(RECONNECT_ALARM, {periodInMinutes});
  }
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) void connect();
});
chrome.runtime.onStartup.addListener(() => { void ensureReconnectAlarm(); void connect(); });
chrome.runtime.onInstalled.addListener((details) => {
  void ensureReconnectAlarm();
  void connect();
  if (details.reason === "install") void chrome.tabs.create({url: chrome.runtime.getURL("popup.html?welcome=1")});
});

void ensureReconnectAlarm();
void connect();
