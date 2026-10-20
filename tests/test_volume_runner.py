"""Volume selection through the queue, using generated files and no real website."""

import asyncio
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from anna_downloader.browser import DownloadProblem, Options, Runner
from anna_downloader.core import BookRequest, Candidate, file_md5


SERIES = "科学帮帮忙"
TITLES = [f"{SERIES}  {name}" for name in ("第一册 磁铁", "第二册 化学", "第三册 天气")]
BASE = "http://books.invalid"


def make_book(title, variant=0):
    """Each result has its own locally generated payload, including alternate files."""
    payload = ("%PDF-1.4\n% Local test fixture: " + title + f" ({variant})\n%%EOF").encode("utf-8")
    digest = hashlib.md5(payload).hexdigest()
    candidate = Candidate(digest, title, "测试作者", "pdf", "zh [Chinese], pdf", f"{BASE}/md5/{digest}")
    return candidate, payload


class FakePlaywright:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *args):
        pass


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.closed = False

    def on(self, *args):
        pass

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.url = url

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.created = []
        self.closed = False
        self.page_listener = None

    @property
    def pages(self):
        return [page for page in self.created if not page.closed]

    def on(self, event, listener):
        if event == "page":
            self.page_listener = listener

    async def new_page(self):
        page = FakePage()
        self.created.append(page)
        if self.page_listener:
            self.page_listener(page)
        return page

    async def close(self):
        self.closed = True
        for page in self.created:
            await page.close()


class FakeDownload:
    def __init__(self, payload):
        self.payload = payload

    async def save_as(self, path):
        Path(path).write_bytes(self.payload)

    async def cancel(self):
        pass


class FixtureRunner(Runner):
    """Fake browser I/O; preserve production queue, grouping, save, and history."""

    def __init__(self, options, emit, stop, candidates, payloads, select_volumes=None, fail=()):
        super().__init__(options, emit, stop, select_volumes=select_volumes)
        self.fixture_candidates = candidates
        self.fixture_payloads = payloads
        self.fail = set(fail)
        self.searched = []
        self.attempted = []
        self.downloaded = []
        self.active_md5 = None
        self.fixture_context = FakeContext()

    async def _launch(self, playwright):
        return self.fixture_context

    async def _navigate(self, page, url, downloading=False):
        page.url = url
        parsed = urlsplit(url)
        if parsed.path == "/search":
            self.searched.append(parse_qs(parsed.query)["q"][0])
        elif parsed.path.startswith("/md5/"):
            digest = parsed.path.rsplit("/", 1)[-1]
            self.attempted.append(digest)
            if digest in self.fail:
                raise DownloadProblem("Generated test version is unavailable")
        elif parsed.path.startswith("/slow_download/"):
            self.active_md5 = parsed.path.rsplit("/", 1)[-1]

    async def _wait_content(self, page, mode):
        if mode == "search":
            query = parse_qs(urlsplit(page.url).query)["q"][0]
            return self.fixture_candidates[query]
        digest = urlsplit(page.url).path.rsplit("/", 1)[-1]
        return [f"{BASE}/slow_download/{digest}"]

    async def _wait_download(self):
        self.downloaded.append(self.active_md5)
        return FakeDownload(self.fixture_payloads[self.active_md5])


class VolumeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.books = [make_book(title) for title in TITLES]
        self.payloads = {book.md5: payload for book, payload in self.books}
        self.candidates = {SERIES: [book for book, _ in self.books]}

    def execute(self, select=None, requests=None, stop=None, fail=()):
        events = []
        runner = FixtureRunner(
            Options(output=self.root / "downloads", data=self.root / "data", base_url=BASE, preferred="pdf", language="zh"),
            lambda kind, data: events.append((kind, data.copy())),
            stop or threading.Event(), self.candidates, self.payloads, select, fail,
        )
        with patch("anna_downloader.browser.async_playwright", return_value=FakePlaywright()):
            totals = asyncio.run(runner.run(requests or [BookRequest(SERIES)]))
        errors = [data for kind, data in events if kind == "error"]
        self.assertEqual(errors, [])
        self.assertTrue(runner.fixture_context.closed)
        self.assertTrue(all(page.closed for page in runner.fixture_context.created))
        return runner, totals, events

    def assert_saved(self, runner, expected_books):
        self.assertEqual(runner.downloaded, [book.md5 for book in expected_books])
        paths = list((self.root / "downloads").glob("*.pdf"))
        self.assertEqual({file_md5(path) for path in paths}, {book.md5 for book in expected_books})
        self.assertFalse(list((self.root / "downloads").glob("*.part")))

    def test_select_nonfirst_volume_before_any_download(self):
        calls = []

        async def select(request, groups):
            self.assertEqual(list((self.root / "downloads").iterdir()), [])
            calls.append((request.query, [group.title for group in groups]))
            return [groups[1].key]

        runner, totals, events = self.execute(select)
        self.assertEqual(calls, [(SERIES, TITLES)])
        self.assertEqual(totals, {"done": 1, "skipped": 0, "failed": 0, "stopped": False})
        self.assert_saved(runner, [self.books[1][0]])
        self.assertEqual(runner.attempted, [self.books[1][0].md5])
        self.assertTrue(any(kind == "book" and data["status"] == "等待勾选" for kind, data in events))

    def test_multiple_checked_volumes_have_independent_files_and_rows(self):
        async def select(request, groups):
            return [groups[0].key, groups[2].key]

        runner, totals, events = self.execute(select)
        self.assertEqual(totals["done"], 2)
        self.assertEqual(totals["failed"], 0)
        self.assert_saved(runner, [self.books[0][0], self.books[2][0]])
        added = [data for kind, data in events if kind == "book_added"]
        self.assertEqual(added, [{"index": 1, "query": TITLES[2]}])
        completed = [data for kind, data in events if kind == "book" and data["status"] == "已完成"]
        self.assertEqual([row["index"] for row in completed], [0, 1])
        self.assertEqual([book["selected_title"] for book in runner.report], [TITLES[0], TITLES[2]])

    def test_failed_selected_volume_never_falls_back_to_an_unchecked_volume(self):
        alternatives = [make_book(TITLES[0], variant) for variant in range(4)]
        self.candidates[SERIES] = [book for book, _ in alternatives] + [self.books[1][0]]
        self.payloads.update({book.md5: payload for book, payload in alternatives})

        async def select(request, groups):
            return [groups[0].key]

        runner, totals, _ = self.execute(select, fail=[book.md5 for book, _ in alternatives])
        self.assertEqual(totals["failed"], 1)
        self.assertEqual(totals["done"], 0)
        self.assertEqual(runner.attempted, [book.md5 for book, _ in alternatives[:3]])
        self.assertEqual(runner.downloaded, [])
        self.assertEqual(list((self.root / "downloads").iterdir()), [])

    def test_stop_cancels_a_pending_selection_and_closes_pages(self):
        stop = threading.Event()
        cancelled = []

        async def select(request, groups):
            stop.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)

        started = time.monotonic()
        runner, totals, events = self.execute(select, stop=stop)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(totals["stopped"])
        self.assertEqual(cancelled, [True])
        self.assertEqual(runner.downloaded, [])
        self.assertTrue(any(kind == "book" and data["status"] == "已停止" for kind, data in events))

    def test_failed_selected_volume_does_not_prevent_another_selected_volume(self):
        async def select(request, groups):
            return [groups[0].key, groups[2].key]

        runner, totals, _ = self.execute(select, fail=[self.books[0][0].md5])
        self.assertEqual(totals["failed"], 1)
        self.assertEqual(totals["done"], 1)
        self.assert_saved(runner, [self.books[2][0]])
        self.assertEqual(runner.attempted, [self.books[0][0].md5, self.books[2][0].md5])
        self.assertEqual([book["status"] for book in runner.report], ["failed", "done"])

    def test_rerun_can_choose_extra_volume_and_skips_only_completed_groups(self):
        selected_positions = [[0], [1], [0, 1]]
        calls = []

        async def select(request, groups):
            calls.append(request.query)
            return [groups[index].key for index in selected_positions.pop(0)]

        first, totals, _ = self.execute(select)
        self.assertEqual(totals["done"], 1)
        second, totals, _ = self.execute(select)
        self.assertEqual(totals["done"], 1)
        self.assertEqual(second.downloaded, [self.books[1][0].md5])
        third, totals, _ = self.execute(select)
        self.assertEqual(totals["done"], 0)
        self.assertEqual(totals["skipped"], 2)
        self.assertEqual(third.downloaded, [])
        self.assertEqual(calls, [SERIES] * 3)
        self.assertEqual(first.searched, [SERIES])
        self.assertEqual(second.searched, [SERIES])
        self.assertEqual(third.searched, [SERIES])

    def test_legacy_query_history_does_not_hide_other_volumes(self):
        _, totals, _ = self.execute()
        self.assertEqual(totals["done"], 1)

        async def select(request, groups):
            return [groups[1].key]

        runner, totals, _ = self.execute(select)
        self.assertEqual(totals["done"], 1)
        self.assertEqual(totals["skipped"], 0)
        self.assertEqual(runner.downloaded, [self.books[1][0].md5])

    def test_empty_selection_skips_series_and_continues_next_query(self):
        other_title = "另一册测试图书"
        other_book, payload = make_book(other_title)
        self.candidates[other_title] = [other_book]
        self.payloads[other_book.md5] = payload
        calls = []

        async def select(request, groups):
            calls.append(request.query)
            return []

        runner, totals, _ = self.execute(select, requests=[BookRequest(SERIES), BookRequest(other_title)])
        self.assertEqual(totals["skipped"], 1)
        self.assertEqual(totals["done"], 1)
        self.assertEqual(calls, [SERIES])
        self.assert_saved(runner, [other_book])
        self.assertEqual(runner.report[0]["reason"], "no_selection")

    def test_without_selection_callback_keeps_single_book_history_behavior(self):
        runner, totals, _ = self.execute()
        self.assertEqual(totals["done"], 1)
        self.assert_saved(runner, [self.books[0][0]])
        second, totals, _ = self.execute()
        self.assertEqual(totals["skipped"], 1)
        self.assertEqual(second.searched, [])
        self.assertEqual(second.downloaded, [])


if __name__ == "__main__":
    unittest.main()
