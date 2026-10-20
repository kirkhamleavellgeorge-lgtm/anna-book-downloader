import asyncio
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from playwright.async_api import async_playwright
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from anna_downloader.browser import DownloadProblem, Options, Runner
from anna_downloader.chrome_session import ChromeBridge, ChromeConnectionError
from anna_downloader.core import BookRequest, file_md5
from test_browser import FixtureSite


ROOT = Path(__file__).resolve().parent.parent
EXTENSION_ID = "a" * 32


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bridge = ChromeBridge(root / "data", root / "extension")
        self.bridge.start()

    def tearDown(self):
        self.bridge.stop()
        self.temp.cleanup()

    async def authenticate(self, socket):
        await socket.send(json.dumps({"type": "hello", "extensionId": EXTENSION_ID, "token": self.bridge.token}))
        self.assertTrue(json.loads(await socket.recv())["connected"])
        for _ in range(30):
            if self.bridge.connected.is_set():
                return
            await asyncio.sleep(0.01)
        self.fail("Bridge did not mark the authenticated connection ready")

    async def test_rejects_wrong_token(self):
        async with connect(self.bridge.url, origin="chrome-extension://" + EXTENSION_ID, proxy=None) as socket:
            await socket.send(json.dumps({"type": "hello", "extensionId": EXTENSION_ID, "token": "wrong-token"}))
            with self.assertRaises(ConnectionClosed):
                await socket.recv()
        self.assertFalse(self.bridge.connected.is_set())

    async def test_command_roundtrip_and_disconnect_cleanup(self):
        async with connect(self.bridge.url, origin="chrome-extension://" + EXTENSION_ID, proxy=None) as socket:
            await self.authenticate(socket)
            call = asyncio.create_task(self.bridge.call("ping", {"sequence": 7}))
            command = json.loads(await socket.recv())
            self.assertEqual(command["method"], "ping")
            await socket.send(json.dumps({"type": "result", "id": command["id"], "result": {"sequence": 7}}))
            self.assertEqual(await call, {"sequence": 7})
            pending = asyncio.create_task(self.bridge.call("tab.snapshot", {"tabId": 5}))
            await socket.recv()
            await socket.close()
            with self.assertRaises(ChromeConnectionError):
                await pending

    async def test_missing_extension_does_not_launch_anonymous_browser(self):
        root = Path(self.temp.name)
        runner = Runner(Options(root / "books", root / "data", chrome_bridge=self.bridge), lambda *_: None, threading.Event())
        with self.assertRaisesRegex(DownloadProblem, "已登录的 Chrome"):
            await runner._launch(None)


class ChromeExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_signed_in_session_and_keeps_user_tab(self):
        """Run the actual extension in a separate test profile, never the user's profile."""
        with tempfile.TemporaryDirectory() as folder, FixtureSite("signed_in") as site:
            root = Path(folder)
            extension = root / "extension"
            shutil.copytree(ROOT / "chrome_extension", extension, ignore=shutil.ignore_patterns("local-config.json"))
            bridge = ChromeBridge(root / "data", extension)
            bridge.start()
            try:
                async with async_playwright() as playwright:
                    executable = Path(playwright.chromium.executable_path)
                    if not executable.exists():
                        cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
                        executables = list(cache.glob("chromium-*/chrome-win64/chrome.exe"))
                        if not executables:
                            self.skipTest("Install Playwright Chromium for the unpacked extension test")
                        executable = sorted(executables)[-1]
                    browser = await playwright.chromium.launch_persistent_context(
                        str(root / "browser"), executable_path=str(executable), channel="chromium",
                        headless=True, accept_downloads=True, downloads_path=str(root / "staging"),
                        args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
                    )
                    try:
                        # This ordinary navigation creates the test site's HttpOnly login cookie.
                        user_tab = await browser.new_page()
                        await user_tab.goto(site.url + "/login")
                        for _ in range(100):
                            if bridge.connected.is_set():
                                break
                            await asyncio.sleep(0.1)
                        self.assertTrue(bridge.connected.is_set(), "Chrome extension never connected")
                        events = []
                        options = Options(root / "books", root / "data", base_url=site.url, chrome_bridge=bridge, queue_timeout=15, page_timeout=10, download_timeout=15)
                        totals = await Runner(options, lambda kind, data: events.append((kind, data)), threading.Event()).run([BookRequest("Open Test Book", "Test Author")])
                        self.assertEqual(totals["done"], 1, events)
                        saved = list((root / "books").glob("*.epub"))
                        self.assertEqual(len(saved), 1)
                        self.assertEqual(file_md5(saved[0]), site.md5)
                        self.assertFalse(user_tab.is_closed(), "The user's existing tab was closed")
                        self.assertEqual(user_tab.url, site.url + "/login")
                        self.assertEqual(await user_tab.locator("body").inner_text(), "Signed into the local test library")
                        # Chrome may also retain the extension's one-time welcome tab.
                        self.assertFalse(any(page.url.startswith(site.url + path) for page in browser.pages for path in ("/search", "/md5/", "/slow_download/", "/ready")))
                    finally:
                        await asyncio.wait_for(browser.close(), timeout=10)
            finally:
                bridge.stop()


if __name__ == "__main__":
    unittest.main()
