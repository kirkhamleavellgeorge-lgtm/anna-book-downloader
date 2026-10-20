from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode
from zipfile import BadZipFile

from playwright.async_api import BrowserContext, Download, Error as BrowserError, Page, async_playwright

from .core import (
    BookRequest, Candidate, History, file_md5, free_download_links, parse_candidates,
    rank_candidates, ready_download_links, safe_filename, validate_download, verification_kind, verification_required,
)
from .chrome_session import ChromeBridge, ChromeContext, ChromeDownload, ChromePage


class DownloadProblem(RuntimeError):
    pass


class RateLimited(DownloadProblem):
    pass


def is_rate_limited(body: str) -> bool:
    return any(s in body.casefold() for s in (
        "daily download limit reached", "too many downloads", "download limit exceeded",
        "you have downloaded too many files", "too many requests", "已达到下载限额",
    ))


@dataclass
class Options:
    output: Path
    data: Path
    base_url: str = "https://annas-archive.pk"
    preferred: str = "epub"
    language: str = ""
    queue_timeout: float = 1800
    verification_timeout: float = 900
    download_timeout: float = 3600
    page_timeout: float = 45
    headless: bool = False
    browser_channel: str | None = "chrome"
    chrome_bridge: ChromeBridge | None = None


class Runner:
    """One browser session and a serial queue; human verification stays in the browser."""

    def __init__(self, options: Options, emit: Callable[[str, dict], None], stop: threading.Event):
        self.options, self.emit, self.stop = options, emit, stop
        self.context: BrowserContext | ChromeContext | None = None
        self.downloads: asyncio.Queue[tuple[int, Download]] = asyncio.Queue()
        self.generation = 0
        self.page_generations: dict[Page, int] = {}
        self.verifications: dict[Page, tuple[float, str]] = {}
        self.index = -1
        self.current_status = ""
        self.active_extension = "epub"
        self.report: list[dict] = []
        self.totals = {"done": 0, "skipped": 0, "failed": 0, "stopped": False}

    def log(self, message: str) -> None:
        logging.info(message)
        self.emit("log", {"message": message})

    def state(self, status: str, detail: str = "", **extra) -> None:
        self.current_status = status
        self.emit("book", {"index": self.index, "status": status, "detail": detail, **extra})

    async def run(self, requests: list[BookRequest]) -> dict:
        job = asyncio.create_task(self._run_browser(requests))

        async def watch_stop():
            while not self.stop.is_set():
                await asyncio.sleep(0.2)

        watcher = asyncio.create_task(watch_stop())
        try:
            done, _ = await asyncio.wait((job, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done and not job.done():
                self.totals["stopped"] = True
                job.cancel()
            await job
        except asyncio.CancelledError:
            self.totals["stopped"] = True
            if self.index >= 0 and self.current_status not in ("已完成", "已跳过", "失败", "受网站限额限制"):
                self.state("已停止", "重新开始会跳过已完成的文件")
                if self.report:
                    self.report[-1].setdefault("status", "stopped")
            self.log("队列已停止。")
        except Exception as exc:
            logging.exception("Queue failed")
            self.emit("error", {"message": str(exc)})
            if self.index >= 0:
                self.state("失败", str(exc))
        finally:
            if not job.done():
                job.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await job
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            self.options.data.mkdir(parents=True, exist_ok=True)
            report = {"totals": self.totals, "books": self.report}
            try:
                (self.options.data / "last_run.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                self.log(f"无法写入运行报告：{exc}")
            self.emit("finished", self.totals.copy())
        return self.totals

    async def _launch(self, playwright) -> BrowserContext | ChromeContext:
        if self.options.chrome_bridge is not None:
            if not self.options.chrome_bridge.connected.is_set():
                raise DownloadProblem("请先在已登录的 Chrome 中加载扩展，等待程序显示“已连接 Chrome”")
            await self.options.chrome_bridge.call("ping")
            self.log("已连接当前 Chrome，使用该 Chrome 配置的网站会话。")
            return ChromeContext(self.options.chrome_bridge)
        profile = self.options.data / "browser-profile"
        preferences = profile / "Default" / "Preferences"
        preferences.parent.mkdir(parents=True, exist_ok=True)
        try:
            prefs = json.loads(preferences.read_text(encoding="utf-8")) if preferences.exists() else {}
            prefs.setdefault("plugins", {})["always_open_pdf_externally"] = True
            preferences.write_text(json.dumps(prefs), encoding="utf-8")
        except (OSError, ValueError, TypeError):
            pass
        channels = [self.options.browser_channel] if self.options.browser_channel else ["chrome", "msedge", None]
        failures = []
        for channel in channels:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(profile), channel=channel, headless=self.options.headless,
                    accept_downloads=True, viewport={"width": 1180, "height": 820},
                    locale="en-US", timeout=30000,
                )
                self.log(f"已打开独立浏览器（{channel or 'Chromium'}），自动等待浏览器检查，并复用本机验证会话。")
                return context
            except BrowserError as exc:
                failures.append(str(exc))
        raise DownloadProblem("无法启动浏览器。请关闭本程序的其他实例后重试；或运行 .venv\\Scripts\\python -m playwright install chromium。\n" + failures[-1][:350])

    def _watch_page(self, page: Page) -> None:
        if page in self.page_generations:
            return
        generation = self.generation
        self.page_generations[page] = generation
        page.on("download", lambda download: self.downloads.put_nowait((generation, download)))
        page.on("dialog", lambda dialog: dialog.dismiss())

    async def _run_browser(self, requests: list[BookRequest]) -> None:
        self.options.output.mkdir(parents=True, exist_ok=True)
        history = History(self.options.data / "history.json")
        async with async_playwright() as playwright:
            self.context = await self._launch(playwright)
            self.context.on("page", self._watch_page)
            for page in self.context.pages:
                self._watch_page(page)
            try:
                for self.index, request in enumerate(requests):
                    self.current_status = "等待中"
                    result = {"query": request.query}
                    self.report.append(result)
                    key = history.key(request, self.options.preferred, self.options.language, self.options.output)
                    previous = await asyncio.to_thread(history.lookup, key)
                    if previous:
                        self.state("已跳过", str(previous), path=str(previous))
                        self.totals["skipped"] += 1
                        result.update(status="skipped", path=str(previous))
                        continue
                    try:
                        self.state("搜索中", request.query)
                        self.log(f"[{self.index + 1}/{len(requests)}] 搜索：{request.query}")
                        search = await self.context.new_page()
                        await self._navigate(search, self.options.base_url.rstrip("/") + "/search?" + urlencode({"q": request.query, "display": "list_compact"}))
                        matches = await self._wait_content(search, "search")
                        ranked = rank_candidates(matches, request, self.options.preferred, self.options.language)
                        self.log(f"搜索结果：识别到 {len(matches)} 条记录，其中 {len(ranked)} 条符合书名、作者和语言条件。")
                        if not ranked:
                            raise DownloadProblem("没有匹配的可下载版本。可补充作者、使用 ISBN，或将语言设为不限。")
                        last_error = "没有可用的免费通道"
                        saved = None
                        for candidate in ranked[:3]:
                            self.active_extension = candidate.extension
                            self.state("已匹配", f"{candidate.title} · {candidate.extension.upper()}", matched=candidate.title, url=candidate.url)
                            self.log(f"匹配：{candidate.title} / {candidate.extension.upper()} / {candidate.md5}")
                            expected = self.options.output / safe_filename(candidate.title, candidate.md5, candidate.extension)
                            if expected.is_file() and await asyncio.to_thread(file_md5, expected) == candidate.md5:
                                saved = expected
                            else:
                                await self._navigate(search, candidate.url)
                                links = await self._wait_content(search, "detail")
                                for link in links[:3]:
                                    self.generation += 1
                                    if isinstance(self.context, ChromeContext):
                                        attempt = await self.context.new_page(extension=candidate.extension)
                                    else:
                                        attempt = await self.context.new_page()
                                    self._watch_page(attempt)
                                    try:
                                        self.state("等待免费通道", "正在打开免费排队页")
                                        await self._navigate(attempt, link)
                                        download = await self._wait_download()
                                        saved = await self._save(download, candidate)
                                        break
                                    except RateLimited:
                                        raise
                                    except (DownloadProblem, BrowserError, ValueError, OSError, KeyError, BadZipFile) as exc:
                                        last_error = str(exc)
                                        self.log(f"此通道未完成：{last_error}")
                                    finally:
                                        await self._close_attempt()
                            if saved:
                                history.remember(key, candidate, saved)
                                self.state("已完成", str(saved), path=str(saved))
                                self.totals["done"] += 1
                                result.update(status="done", path=str(saved), md5=candidate.md5)
                                self.log(f"已保存：{saved.name}")
                                break
                        if not saved:
                            raise DownloadProblem(last_error)
                    except RateLimited as exc:
                        self.state("受网站限额限制", str(exc))
                        self.totals["failed"] += 1
                        result.update(status="rate_limited", error=str(exc))
                        self.log("网站提示下载限额，队列停止。请在限额恢复后再运行。")
                        break
                    except (DownloadProblem, BrowserError, ValueError, OSError) as exc:
                        self.state("失败", str(exc))
                        self.totals["failed"] += 1
                        result.update(status="failed", error=str(exc))
                        self.log(f"未完成《{request.title}》：{exc}")
                    finally:
                        for page in list(self.context.pages):
                            await self._dispose_page(page)
                        self.page_generations.clear()
                        self.verifications.clear()
                    # Keep the queue serial and leave a short gap between titles.
                    await asyncio.sleep(1)
            finally:
                with contextlib.suppress(BrowserError, asyncio.TimeoutError):
                    await asyncio.wait_for(self.context.close(), timeout=10)

    async def _dispose_page(self, page: Page) -> None:
        self.verifications.pop(page, None)
        if page.is_closed():
            return
        if isinstance(page, ChromePage):
            with contextlib.suppress(BrowserError, asyncio.TimeoutError):
                await asyncio.wait_for(page.close(), timeout=5)
            return
        # A navigation that became a download can leave Edge's target half-open.
        # Commit a blank document before closing, and bound cleanup independently.
        with contextlib.suppress(BrowserError, asyncio.TimeoutError):
            await asyncio.wait_for(page.goto("about:blank", wait_until="commit", timeout=1500), timeout=2)
        with contextlib.suppress(BrowserError, asyncio.TimeoutError):
            await asyncio.wait_for(page.close(), timeout=2)

    async def _close_attempt(self) -> None:
        for page, generation in list(self.page_generations.items()):
            if generation == self.generation:
                await self._dispose_page(page)
                self.page_generations.pop(page, None)

    async def _navigate(self, page: Page, url: str, downloading: bool = False) -> None:
        if isinstance(page, ChromePage) and downloading:
            await page.start_download(url, self.active_extension)
            return
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except BrowserError as exc:
            if downloading and ("ERR_ABORTED" in str(exc) or "Download is starting" in str(exc)):
                return
            if "Timeout" in type(exc).__name__ and not page.is_closed():
                return  # A verification page may remain usable after navigation timeout.
            raise
        if response and response.status == 429:
            raise RateLimited("网站返回 HTTP 429，请稍后再试")
        if response and response.status >= 400:
            title, body = await page.title(), await page.locator("body").inner_text(timeout=5000)
            if is_rate_limited(body):
                raise RateLimited("网站提示下载限额，请稍后再试")
            if not verification_required(title, body):
                raise DownloadProblem(f"网页返回 HTTP {response.status}：{url}")

    async def _observe_verification(self, page: Page, title: str, body: str) -> bool:
        kind = verification_kind(title, body)
        previous = self.verifications.get(page)
        if kind is None:
            if previous is not None:
                self.verifications.pop(page, None)
                self.log("网站验证已通过，自动继续。")
            return False
        now = time.monotonic()
        started = previous[0] if previous is not None else now
        phase = "automatic_pending" if kind == "automatic" and now - started >= 30 else kind
        if previous is None or previous[1] != phase:
            self.verifications[page] = (started, phase)
            if kind == "interactive":
                self.state("等待手动验证码", "请在浏览器中完成验证码；通过后自动继续")
                self.log("网站明确要求人工验证，请在打开的浏览器中完成验证码。")
                await page.bring_to_front()
            elif phase == "automatic_pending":
                self.state("等待网站放行", "网站自动检查仍未通过；请查看浏览器提示，通过后会自动继续")
                self.log("网站的浏览器自动检查尚未放行，继续等待。")
            else:
                self.state("浏览器自动检查中", "等待网站自行完成检查，无需操作；通过后自动继续")
                self.log("检测到网站的浏览器自动检查，正在等待检查结果。")
        if now - started > self.options.verification_timeout:
            reason = "网站自动检查尚未通过" if kind == "automatic" else "仍需在浏览器中完成验证码"
            raise DownloadProblem(f"浏览器验证等待超时：{reason}")
        return True

    async def _wait_content(self, page: Page, mode: str):
        deadline = time.monotonic() + self.options.page_timeout
        started = time.monotonic()
        while True:
            if page.is_closed():
                raise DownloadProblem("浏览器页面已关闭")
            try:
                title = await page.title()
                body = await page.locator("body").inner_text(timeout=3000)
                html = await page.content()
            except BrowserError as exc:
                check = self.verifications.get(page)
                read_deadline = check[0] + self.options.verification_timeout if check else deadline
                if page.is_closed() or time.monotonic() > read_deadline:
                    raise DownloadProblem("网页无法读取或已关闭，请检查浏览器和网络") from exc
                await asyncio.sleep(0.5)
                continue
            if is_rate_limited(body):
                raise RateLimited("网站提示下载限额，请稍后再试")
            was_verifying = page in self.verifications
            if not await self._observe_verification(page, title, body):
                if was_verifying:
                    self.state("搜索中" if mode == "search" else "读取下载通道")
                    started = time.monotonic()
                    deadline = started + self.options.page_timeout
                if mode == "search":
                    value = parse_candidates(html, page.url)
                    if value:
                        return value
                    if time.monotonic() - started > 7 and ("js-aarecord-list-outer" in html or any(s in body.casefold() for s in ("no files found", "no results found", "没有找到", "未找到"))):
                        return []
                else:
                    value = free_download_links(html, page.url)
                    if value:
                        return value
                    if "md5-panel-downloads" in html and time.monotonic() - started > 5:
                        return []
                if time.monotonic() > deadline:
                    raise DownloadProblem("网页未出现可识别的书籍/免费通道，可能是网络问题或网站结构已改变")
            await asyncio.sleep(0.7)

    async def _wait_download(self) -> Download:
        deadline = time.monotonic() + self.options.queue_timeout
        opened: set[str] = set()
        last_countdown = None
        while time.monotonic() < deadline:
            while not self.downloads.empty():
                generation, download = self.downloads.get_nowait()
                if generation == self.generation:
                    return download
            pages = [p for p, generation in self.page_generations.items() if generation == self.generation and not p.is_closed()]
            if not pages:
                raise DownloadProblem("下载页面已关闭")
            for page in pages:
                try:
                    title = await page.title()
                    body = await page.locator("body").inner_text(timeout=3000)
                    html = await page.content()
                except BrowserError:
                    continue
                was_verifying = page in self.verifications
                if await self._observe_verification(page, title, body):
                    continue
                if was_verifying:
                    self.state("等待免费通道", "验证已通过，自动继续")
                if is_rate_limited(body):
                    raise RateLimited("免费通道提示下载限额")
                countdown = page.locator(".js-partner-countdown")
                if await countdown.count():
                    remaining = (await countdown.first.inner_text()).strip()
                    if remaining != last_countdown:
                        self.state("排队中", f"网站倒计时：{remaining} 秒")
                        last_countdown = remaining
                    continue  # Let the website's own countdown/reload finish.
                for url in ready_download_links(html, page.url):
                    if url not in opened:
                        opened.add(url)
                        self.state("准备下载", "已取得页面公开的下载链接")
                        await self._navigate(page, url, downloading=True)
                        break
            await asyncio.sleep(0.5)
        raise DownloadProblem("免费通道等待超过设定时间，可稍后重试")

    async def _save(self, download: Download, candidate: Candidate) -> Path:
        self.state("下载中", f"{candidate.title} · {candidate.extension.upper()}")
        temp = self.options.output / f".{candidate.md5}-{uuid.uuid4().hex[:8]}.part"
        try:
            await asyncio.wait_for(download.save_as(str(temp)), timeout=self.options.download_timeout)
            self.state("校验中", "检查文件格式和 MD5")
            await asyncio.to_thread(validate_download, temp, candidate)
            target = self.options.output / safe_filename(candidate.title, candidate.md5, candidate.extension)
            suffix = 2
            while target.exists():
                if await asyncio.to_thread(file_md5, target) == candidate.md5:
                    return target
                target = self.options.output / f"{Path(safe_filename(candidate.title, candidate.md5, candidate.extension)).stem} ({suffix}).{candidate.extension}"
                suffix += 1
            temp.replace(target)
            return target
        except asyncio.TimeoutError as exc:
            await download.cancel()
            raise DownloadProblem("文件传输超时，请稍后重试") from exc
        except asyncio.CancelledError:
            with contextlib.suppress(BrowserError):
                await download.cancel()
            raise
        finally:
            if isinstance(download, ChromeDownload):
                with contextlib.suppress(BrowserError):
                    await download.release()
            temp.unlink(missing_ok=True)
