import asyncio
import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from anna_downloader.browser import Options, Runner
from anna_downloader.core import BookRequest, file_md5
from test_core import compact, epub_bytes


class FixtureSite:
    def __init__(self, mode="normal"):
        self.mode = mode
        self.extension = "pdf" if mode in ("inline_pdf", "chinese_series") else "epub"
        self.data = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF" if self.extension == "pdf" else epub_bytes()
        self.md5 = hashlib.md5(self.data).hexdigest()
        self.calls = []
        self.ready_at = 0
        self.early_download = False
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, status=200, attachment=False, content_type=None, set_cookie=None):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(status)
                book_type = "application/pdf" if site.extension == "pdf" else "application/epub+zip"
                self.send_header("Content-Type", content_type or (book_type if attachment else "text/html; charset=utf-8"))
                if attachment:
                    self.send_header("Content-Disposition", 'attachment; filename="unsafe-download-name.epub"')
                if set_cookie:
                    self.send_header("Set-Cookie", set_cookie)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                parsed = urlsplit(self.path)
                site.calls.append(self.path)
                if site.mode == "signed_in":
                    if parsed.path == "/login":
                        self.send("Signed into the local test library", set_cookie="anna-test-session=signed-in; HttpOnly; SameSite=Lax; Path=/")
                        return
                    if "anna-test-session=signed-in" not in self.headers.get("Cookie", ""):
                        self.send("Login required", status=401)
                        return
                if parsed.path == "/search":
                    if site.mode in ("verification", "stop"):
                        self.send('<title>DDoS-Guard</title><body>Checking your browser before accessing this site</body>')
                    elif site.mode == "automatic_then_continue":
                        self.send('<title>DDoS-Guard</title><body>Checking your browser before accessing this site</body><script>setTimeout(() => location.href="/verified-search", 500)</script>')
                    elif site.mode == "manual_verification":
                        self.send('<title>Security verification</title><body>Please verify that you are human<button onclick="fetch(\'/human-click\')">I am not a robot</button></body>')
                    elif site.mode == "chinese_series":
                        self.send('<title>Search</title>' + compact(site.md5, title="科学帮帮忙  变幻莫测的化学", author="测试作者", language="zh", extension="pdf"))
                    else:
                        query = parse_qs(parsed.query).get("q", [""])[0]
                        result_title = query.removesuffix(" Test Author")
                        self.send('<title>Search</title><div class="js-aarecord-list-outer">' + compact(site.md5, title=result_title, author="Test Author", extension=site.extension) + '</div>')
                elif parsed.path == "/verified-search":
                    self.send('<title>Search</title>' + compact(site.md5, title="Open Test Book", author="Test Author"))
                elif parsed.path.startswith("/md5/"):
                    self.send(f'<title>Book</title><div id="md5-panel-downloads"><a class="js-download-link" href="/fast_download/{site.md5}">Fast partner</a><a class="js-download-link" href="/slow_download/{site.md5}/0/0">Slow partner</a></div>')
                elif parsed.path.startswith("/slow_download/"):
                    if site.mode == "rate_limit":
                        self.send("Too many downloads", status=429)
                    elif site.mode == "rate_limit_body":
                        self.send("You have downloaded too many files recently. Please wait.")
                    elif site.mode == "download_automatic_check":
                        self.send('<title>DDoS-Guard</title><body>Checking your browser before accessing this site</body><script>setTimeout(() => location.href="/ready", 500)</script>')
                    else:
                        site.ready_at = time.monotonic() + 0.65
                        self.send('<title>Queue</title><span class="js-partner-countdown">1</span><script>setTimeout(() => location.href="/ready", 750)</script>')
                elif parsed.path == "/ready":
                    if site.mode == "copy_url":
                        self.send(f'<title>Download</title><p>To download, copy this URL:</p><span class="whitespace-nowrap"><button>Copy</button><span>{site.url}/file</span></span>')
                    elif site.mode == "popup":
                        self.send('<title>Download</title><script>setTimeout(() => window.open("/file"), 200)</script><body>Starting download</body>')
                    else:
                        self.send('<title>Download</title><p><a href="/file">Download now</a></p>')
                elif parsed.path == "/file":
                    if time.monotonic() < site.ready_at:
                        site.early_download = True
                    if site.mode == "inline_pdf":
                        self.send(site.data, content_type="application/pdf")
                    else:
                        self.send(b"<!doctype html><html>not a book</html>" if site.mode == "bad_file" else site.data, attachment=True)
                else:
                    self.send("Not found", status=404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class BrowserTests(unittest.TestCase):
    def execute(self, directory, site, requests=None, stop_on_verification=False, stop_on_completed=False):
        root = Path(directory)
        events = []
        stop = threading.Event()

        def emit(kind, data):
            events.append((kind, data))
            if stop_on_verification and kind == "book" and data["status"] in ("浏览器自动检查中", "等待手动验证码", "等待网站放行"):
                stop.set()
            if stop_on_completed and kind == "book" and data["status"] == "已完成":
                stop.set()

        options = Options(output=root / "downloads", data=root / "data", base_url=site.url, headless=True, browser_channel="msedge", queue_timeout=8, verification_timeout=1.5, download_timeout=10, page_timeout=5, language="zh" if site.mode == "chinese_series" else "")
        runner = Runner(options, emit, stop)
        totals = asyncio.run(runner.run(requests or [BookRequest("Open Test Book", "Test Author")]))
        errors = [data["message"] for kind, data in events if kind == "error"]
        self.assertFalse(errors, errors)
        return totals, events

    def test_full_queue_then_skip_without_network_search(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite() as site:
            totals, events = self.execute(directory, site)
            self.assertEqual(totals["done"], 1, events)
            self.assertFalse(site.early_download)
            self.assertFalse(any("/fast_download/" in call for call in site.calls))
            files = list((Path(directory) / "downloads").glob("*.epub"))
            self.assertEqual(len(files), 1)
            self.assertEqual(file_md5(files[0]), site.md5)
            call_count = len(site.calls)
            totals, _ = self.execute(directory, site)
            self.assertEqual(totals["skipped"], 1)
            self.assertFalse(any("/search?" in call for call in site.calls[call_count:]))

    def test_copy_url_download(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("copy_url") as site:
            totals, events = self.execute(directory, site)
            self.assertEqual(totals["done"], 1, events)

    def test_popup_download(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("popup") as site:
            totals, events = self.execute(directory, site)
            self.assertEqual(totals["done"], 1, events)

    def test_inline_pdf_download(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("inline_pdf") as site:
            totals, events = self.execute(directory, site)
            self.assertEqual(totals["done"], 1, events)
            self.assertEqual(len(list((Path(directory) / "downloads").glob("*.pdf"))), 1)

    def test_chinese_series_matches_volume_and_downloads_pdf_fallback(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("chinese_series") as site:
            totals, events = self.execute(directory, site, [BookRequest("科学帮帮忙")])
            self.assertEqual(totals["done"], 1, events)
            self.assertTrue(any(kind == "book" and data.get("matched") == "科学帮帮忙  变幻莫测的化学" for kind, data in events))
            files = list((Path(directory) / "downloads").glob("*.pdf"))
            self.assertEqual(len(files), 1)
            self.assertEqual(file_md5(files[0]), site.md5)

    def test_multiple_titles_in_one_queue(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite() as site:
            totals, events = self.execute(directory, site, [BookRequest("Open Test Book"), BookRequest("Second Test Book")])
            self.assertEqual(totals["done"], 2, events)

    def test_stop_after_completion_keeps_completed_status(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite() as site:
            totals, events = self.execute(directory, site, [BookRequest("Open Test Book"), BookRequest("Second Test Book")], stop_on_completed=True)
            self.assertEqual(totals["done"], 1, events)
            self.assertTrue(totals["stopped"])
            states = [data["status"] for kind, data in events if kind == "book"]
            self.assertEqual(states[-1], "已完成")

    def test_automatic_check_resolves_without_manual_prompt(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("automatic_then_continue") as site:
            totals, events = self.execute(directory, site)
            self.assertEqual(totals["done"], 1, events)
            self.assertTrue(any(kind == "book" and data["status"] == "浏览器自动检查中" for kind, data in events))
            self.assertFalse(any(kind == "book" and data["status"] == "等待手动验证码" for kind, data in events))

    def test_download_automatic_check_resolves_without_manual_prompt(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("download_automatic_check") as site:
            totals, events = self.execute(directory, site)
            self.assertEqual(totals["done"], 1, events)
            self.assertTrue(any(kind == "book" and data["status"] == "浏览器自动检查中" for kind, data in events))
            self.assertFalse(any(kind == "book" and data["status"] == "等待手动验证码" for kind, data in events))

    def test_explicit_captcha_requests_manual_action(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("manual_verification") as site:
            totals, events = self.execute(directory, site)
            self.assertEqual(totals["failed"], 1)
            self.assertTrue(any(kind == "book" and data["status"] == "等待手动验证码" for kind, data in events))
            self.assertFalse(any("/human-click" in call for call in site.calls))

    def test_verification_timeout_is_honest_failure(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("verification") as site:
            totals, events = self.execute(directory, site)
            self.assertEqual(totals["failed"], 1)
            self.assertEqual(totals["done"], 0)
            self.assertTrue(any(kind == "book" and "验证等待超时" in data.get("detail", "") for kind, data in events))

    def test_stop_during_verification(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("stop") as site:
            started = time.monotonic()
            totals, _ = self.execute(directory, site, stop_on_verification=True)
            self.assertTrue(totals["stopped"])
            self.assertLess(time.monotonic() - started, 12)

    def test_rate_limit_stops_remaining_titles(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("rate_limit") as site:
            totals, _ = self.execute(directory, site, [BookRequest("Open Test Book"), BookRequest("Second Book")])
            self.assertEqual(totals["failed"], 1)
            self.assertFalse(any("Second" in call for call in site.calls))

    def test_rate_limit_with_http_200_stops_queue(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("rate_limit_body") as site:
            totals, _ = self.execute(directory, site, [BookRequest("Open Test Book"), BookRequest("Second Book")])
            self.assertEqual(totals["failed"], 1)
            self.assertFalse(any("Second" in call for call in site.calls))

    def test_reject_html_and_remove_partial_file(self):
        with tempfile.TemporaryDirectory() as directory, FixtureSite("bad_file") as site:
            totals, _ = self.execute(directory, site)
            self.assertEqual(totals["failed"], 1)
            self.assertEqual(list((Path(directory) / "downloads").iterdir()), [])
            report = json.loads((Path(directory) / "data" / "last_run.json").read_text(encoding="utf-8"))
            self.assertIn("网页", report["books"][0]["error"])


if __name__ == "__main__":
    unittest.main()
