"use strict";

const state = document.getElementById("state");
const detail = document.getElementById("detail");
const indicator = document.getElementById("indicator");
const reconnect = document.getElementById("reconnect");
document.getElementById("welcome").hidden = !new URLSearchParams(location.search).has("welcome");

function display(status) {
  state.textContent = status.connected ? "已连接" : "等待连接";
  detail.textContent = status.detail || "请先打开本机图书助手。";
  indicator.classList.toggle("connected", Boolean(status.connected));
}

async function refresh() {
  try { display(await chrome.runtime.sendMessage({type: "status"})); }
  catch (_) { display({connected: false, detail: "扩展正在启动，请点击重新连接。"}); }
}

reconnect.addEventListener("click", async () => {
  reconnect.disabled = true;
  try { display(await chrome.runtime.sendMessage({type: "reconnect"})); }
  catch (_) { display({connected: false, detail: "连接未完成，请检查本机图书助手是否已打开。"}); }
  finally { reconnect.disabled = false; }
});

void refresh();
setInterval(() => { void refresh(); }, 1500);
