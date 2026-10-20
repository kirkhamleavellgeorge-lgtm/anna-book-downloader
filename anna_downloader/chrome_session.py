from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from playwright.async_api import Error as BrowserError
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


class ChromeConnectionError(BrowserError):
    pass


class ChromeBridge:
    """Authenticated loopback transport to an extension in the user's Chrome profile."""

    def __init__(self, data: Path, extension_dir: Path, emit: Callable[[str, dict], None] | None = None):
        self.data, self.extension_dir = data, extension_dir
        self.emit = emit or (lambda *_: None)
        self.connected = threading.Event()
        self.ready = threading.Event()
        self.thread: threading.Thread | None = None
        self.loop = None
        self.socket = None
        self.pending = {}
        self.listeners = []
        self.listener_lock = threading.Lock()
        self.error = None
        self.url = ""
        self.token = ""
        self.extension_id = ""

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._thread_main, name="chrome-bridge", daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise ChromeConnectionError("Chrome 连接服务启动超时")
        if self.error:
            raise ChromeConnectionError(f"Chrome 连接服务未启动：{self.error}")

    def _thread_main(self):
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self.error = str(exc)
            logging.exception("Chrome bridge failed")
            self.emit("chrome", {"connected": False, "message": "Chrome 连接服务未启动"})
        finally:
            self.ready.set()

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.shutdown = asyncio.Event()
        self.token = secrets.token_urlsafe(32)
        async with serve(
            self._handle, "127.0.0.1", 0,
            origins=[re.compile(r"chrome-extension://[a-p]{32}")],
            max_size=16 * 1024 * 1024, close_timeout=2, ping_interval=20,
        ) as server:
            self.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            self.extension_dir.mkdir(parents=True, exist_ok=True)
            config = self.extension_dir / "local-config.json"
            temp = config.with_suffix(".tmp")
            temp.write_text(json.dumps({"url": self.url, "token": self.token}), encoding="utf-8")
            temp.replace(config)
            self.ready.set()
            await self.shutdown.wait()

    async def _handle(self, socket):
        authorized = False
        try:
            hello = json.loads(await asyncio.wait_for(socket.recv(), 5))
            origin = socket.request.headers.get("Origin", "")
            extension_id = hello.get("extensionId", "")
            if (hello.get("type") != "hello" or not isinstance(hello.get("token"), str)
                    or not secrets.compare_digest(hello["token"], self.token)
                    or origin != "chrome-extension://" + extension_id):
                await socket.close(4001, "Authentication required")
                return
            if self.connected.is_set():
                await socket.close(4002, "Another Chrome profile is connected")
                return
            self.socket = socket
            self.extension_id = extension_id
            authorized = True
            await socket.send(json.dumps({"type": "hello", "connected": True}))
            self.connected.set()
            logging.info("Chrome 扩展已连接，可以复用该配置的网站会话")
            self.emit("chrome", {"connected": True, "message": "已连接当前 Chrome，可使用该配置的网站登录状态"})
            async for raw in socket:
                message = json.loads(raw)
                if message.get("type") == "ping":
                    await socket.send(json.dumps({"type": "pong"}))
                elif message.get("type") == "result":
                    future = self.pending.pop(message.get("id"), None)
                    if future and not future.done():
                        if message.get("error"):
                            future.set_exception(ChromeConnectionError(str(message["error"])))
                        else:
                            future.set_result(message.get("result", {}))
                elif message.get("type") == "event":
                    with self.listener_lock:
                        listeners = list(self.listeners)
                    for listener in listeners:
                        listener(message)
        except (ConnectionClosed, asyncio.TimeoutError, ValueError, TypeError, KeyError):
            pass
        finally:
            if authorized and self.socket is socket:
                self.connected.clear()
                self.socket = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ChromeConnectionError("Chrome 扩展已断开，请重新连接后再运行"))
                self.pending.clear()
                logging.info("Chrome 扩展连接已断开")
                self.emit("chrome", {"connected": False, "message": "Chrome 扩展未连接"})

    def subscribe(self, listener):
        with self.listener_lock:
            self.listeners.append(listener)

        def unsubscribe():
            with self.listener_lock:
                if listener in self.listeners:
                    self.listeners.remove(listener)
        return unsubscribe

    async def call(self, method: str, params: dict | None = None, timeout: float = 35):
        if not self.connected.is_set() or not self.loop:
            raise ChromeConnectionError("请先在已登录的 Chrome 中加载扩展，并等待显示“已连接 Chrome”")
        future = asyncio.run_coroutine_threadsafe(self._request(method, params or {}, timeout), self.loop)
        return await asyncio.wrap_future(future)

    async def _request(self, method, params, timeout):
        if not self.socket or not self.connected.is_set():
            raise ChromeConnectionError("Chrome 扩展未连接")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.socket.send(json.dumps({"type": "command", "id": request_id, "method": method, "params": params}))
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise ChromeConnectionError(f"Chrome 操作超时：{method}") from exc
        except ConnectionClosed as exc:
            raise ChromeConnectionError("Chrome 扩展连接已断开") from exc
        finally:
            self.pending.pop(request_id, None)

    def stop(self):
        if self.loop and self.thread and self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.shutdown.set)
            self.thread.join(timeout=3)


class ChromeContext:
    """A small browser adapter. Its page list contains only this queue's own tabs."""

    def __init__(self, bridge: ChromeBridge):
        self.bridge = bridge
        self.loop = asyncio.get_running_loop()
        self._pages = {}
        self.downloads = {}
        self.handlers = {}
        self.closed = False

        def receive(event):
            if not self.closed and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self._event, event)
        self.unsubscribe = bridge.subscribe(receive)

    @property
    def pages(self):
        return [page for page in self._pages.values() if not page.is_closed()]

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    async def new_page(self, extension=""):
        result = await self.bridge.call("tab.create", {"url": "about:blank", "extension": extension})
        page = ChromePage(self, result["tabId"], extension)
        self._pages[page.tab_id] = page
        for handler in self.handlers.get("page", []):
            handler(page)
        return page

    def _event(self, event):
        if event.get("event") == "tab.closed":
            page = self._pages.get(event.get("tabId"))
            if page:
                page.closed = True
        elif event.get("event") == "download.created":
            page = self._pages.get(event.get("tabId"))
            if page:
                self.register_download(page, event["downloadId"], event.get("url", ""))

    def register_download(self, page, download_id, url):
        if download_id not in self.downloads:
            download = ChromeDownload(self.bridge, download_id, url)
            self.downloads[download_id] = download
            for handler in page.handlers.get("download", []):
                handler(download)
        return self.downloads[download_id]

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.unsubscribe()
        if self.bridge.connected.is_set():
            await self.bridge.call("context.close", {"tabIds": list(self._pages)}, timeout=8)
        for page in self._pages.values():
            page.closed = True


class ChromePage:
    def __init__(self, context: ChromeContext, tab_id: int, extension=""):
        self.context, self.tab_id = context, tab_id
        self.extension = extension
        self.url = "about:blank"
        self.closed = False
        self.handlers = {}
        self.snapshot = {}
        self.snapshot_at = 0.0

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def is_closed(self):
        return self.closed

    async def _snapshot(self, force=False):
        if force or time.monotonic() - self.snapshot_at > 0.25:
            self.snapshot = await self.context.bridge.call("tab.snapshot", {"tabId": self.tab_id})
            self.snapshot_at = time.monotonic()
            self.url = self.snapshot.get("url", self.url)
        return self.snapshot

    async def goto(self, url, **_kwargs):
        self.url = url
        result = await self.context.bridge.call("tab.navigate", {"tabId": self.tab_id, "url": url, "extension": self.extension})
        self.snapshot = result or {}
        self.snapshot_at = 0
        self.url = self.snapshot.get("url", url)
        return None

    async def start_download(self, url, extension):
        result = await self.context.bridge.call("download.start", {"tabId": self.tab_id, "url": url, "extension": extension})
        self.context.register_download(self, result["downloadId"], url)

    async def title(self):
        return (await self._snapshot(force=True)).get("title", "")

    async def content(self):
        return (await self._snapshot()).get("html", "")

    def locator(self, selector):
        return ChromeLocator(self, selector)

    async def bring_to_front(self):
        await self.context.bridge.call("tab.activate", {"tabId": self.tab_id})

    async def close(self):
        if not self.closed:
            await self.context.bridge.call("tab.close", {"tabId": self.tab_id})
            self.closed = True


class ChromeLocator:
    def __init__(self, page: ChromePage, selector: str):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    async def inner_text(self, **_kwargs):
        snapshot = await self.page._snapshot()
        return snapshot.get("body" if self.selector == "body" else "countdownText", "")

    async def count(self):
        return (await self.page._snapshot()).get("countdownCount", 0)


class ChromeDownload:
    def __init__(self, bridge: ChromeBridge, download_id: int, url: str):
        self.bridge, self.download_id, self.url = bridge, download_id, url

    async def save_as(self, target: str):
        while True:
            item = await self.bridge.call("download.status", {"downloadId": self.download_id})
            if item.get("state") == "complete":
                source = Path(item.get("filename", ""))
                if not source.is_absolute() or not source.is_file():
                    raise ChromeConnectionError("Chrome 返回的下载文件不存在")
                # Finish a local copy before cancellation removes the output .part file.
                copy = asyncio.create_task(asyncio.to_thread(shutil.copyfile, source, target))
                try:
                    await asyncio.shield(copy)
                except asyncio.CancelledError:
                    await copy
                    raise
                return
            if item.get("state") == "interrupted":
                raise ChromeConnectionError("Chrome 下载中断：" + str(item.get("error", "未知原因")))
            await asyncio.sleep(0.5)

    async def cancel(self):
        await self.bridge.call("download.cancel", {"downloadId": self.download_id}, timeout=5)

    async def release(self):
        await self.bridge.call("download.remove", {"downloadId": self.download_id}, timeout=5)
