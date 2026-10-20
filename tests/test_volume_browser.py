"""Real browser selection flow against locally generated multi-volume books."""

import asyncio
import hashlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from anna_downloader.browser import Options, Runner
from anna_downloader.core import BookRequest, file_md5
from test_core import compact


class VolumeBrowserTests(unittest.TestCase):
    def test_checked_volumes_download_without_accessing_unchecked_volume(self):
        titles = [f"科学帮帮忙第{number}册" for number in (1, 2, 3)]
        payloads = [(f"%PDF-1.4\n% Generated volume {number}\n%%EOF").encode() for number in (1, 2, 3)]
        digests = [hashlib.md5(data).hexdigest() for data in payloads]
        files = dict(zip(digests, payloads))
        calls, events, selections = [], [], []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                calls.append(path)
                is_file = path.startswith("/file/")
                if path == "/search":
                    body = "<title>Local search</title>" + "".join(
                        compact(digest, title=title, author="测试作者", language="zh", extension="pdf")
                        for digest, title in zip(digests, titles)
                    )
                elif path.startswith("/md5/"):
                    body = f'<title>Local book</title><a href="/slow_download/{path.rsplit("/", 1)[-1]}">Free download</a>'
                elif path.startswith("/slow_download/"):
                    body = f'<title>Local ready page</title><a href="/file/{path.rsplit("/", 1)[-1]}">Download now</a>'
                elif is_file:
                    body = files[path.rsplit("/", 1)[-1]]
                else:
                    self.send_error(404)
                    return
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf" if is_file else "text/html; charset=utf-8")
                if is_file:
                    self.send_header("Content-Disposition", 'attachment; filename="fixture.pdf"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        async def select(request, groups):
            self.assertFalse(any(path.startswith("/md5/") for path in calls))
            selections.append((request.query, [group.title for group in groups]))
            return [groups[0].key, groups[2].key]

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                options = Options(output=root / "downloads", data=root / "data",
                                  base_url=f"http://127.0.0.1:{server.server_port}", preferred="pdf", language="zh",
                                  headless=True, browser_channel="msedge", page_timeout=5, queue_timeout=8, download_timeout=10)
                runner = Runner(options, lambda kind, data: events.append((kind, data.copy())), threading.Event(), select)
                totals = asyncio.run(runner.run([BookRequest("科学帮帮忙")]))
                self.assertEqual([data for kind, data in events if kind == "error"], [])
                self.assertEqual(totals, {"done": 2, "skipped": 0, "failed": 0, "stopped": False}, events)
                self.assertEqual(selections, [("科学帮帮忙", titles)])
                saved = list(options.output.glob("*.pdf"))
                self.assertEqual({file_md5(path) for path in saved}, {digests[0], digests[2]})
                self.assertTrue(all(path.read_bytes().startswith(b"%PDF-") for path in saved))
                self.assertNotIn(f"/md5/{digests[1]}", calls)
                self.assertNotIn(f"/slow_download/{digests[1]}", calls)
                self.assertIn(f"/slow_download/{digests[0]}", calls)
                self.assertIn(f"/slow_download/{digests[2]}", calls)
                self.assertEqual([data for kind, data in events if kind == "book_added"], [{"index": 1, "query": titles[2]}])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
