import asyncio
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from anna_downloader.app import App
from anna_downloader.core import BookRequest, Candidate, group_candidates


class AppSelectionTests(unittest.TestCase):
    """Exercise worker-to-Tk selection without starting the real Chrome bridge."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.patch_data = patch("anna_downloader.app.DATA", Path(self.folder.name))
        self.patch_bridge = patch("anna_downloader.app.ChromeBridge")
        self.patch_data.start()
        self.bridge = self.patch_bridge.start().return_value
        self.app = App(self.root)
        self.result = None
        self.error = None

    def tearDown(self):
        self.app.stop.set()
        self.app._close_volume_dialog()
        if getattr(self, "worker", None):
            self.worker.join(timeout=2)
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.patch_bridge.stop()
        self.patch_data.stop()
        self.folder.cleanup()

    def pump(self, condition):
        end = time.monotonic() + 5
        while not condition() and time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(condition(), self.error)

    def start_selection(self):
        books = [Candidate(str(i) * 32, f"测试丛书 第{i}册", "测试作者", "pdf", "zh, pdf", f"https://example.test/md5/{i}") for i in (1, 2)]
        groups = group_candidates(books)

        def work():
            try:
                self.result = asyncio.run(self.app._select_volumes(BookRequest("测试丛书"), groups))
            except Exception as exc:
                self.error = exc

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self.pump(lambda: self.app.volume_dialog is not None)
        return groups

    def test_dialog_result_returns_to_worker_and_closes(self):
        groups = self.start_selection()
        # Use the actual popup callback, on Tk's owning thread.
        dialog = self.app.volume_dialog
        dialog._toggle("1")
        dialog.confirm_button.invoke()
        self.pump(lambda: not self.worker.is_alive())
        self.pump(lambda: self.app.volume_dialog is None)
        self.assertEqual(self.result, [groups[1].key])
        self.assertIsNone(self.error)
        self.assertIsNone(self.app.selection_token)

    def test_stop_closes_dialog_and_releases_waiting_worker(self):
        self.start_selection()
        self.app.volume_dialog.stop_button.invoke()
        self.pump(lambda: not self.worker.is_alive())
        self.assertEqual(self.result, [])
        self.assertIsNone(self.app.volume_dialog)
        self.assertTrue(self.app.stop.is_set())

    def test_extra_volume_gets_its_own_exportable_progress_row(self):
        self.app.events.put(("book_added", {"index": 0, "query": "测试丛书 第2册"}))
        self.app.events.put(("book", {"index": 0, "status": "已完成", "matched": "测试丛书 第2册", "path": "/test/book.pdf", "detail": "/test/book.pdf"}))
        self.pump(lambda: len(self.app.row_data) == 1 and self.app.row_data[0]["status"] == "已完成")
        self.assertEqual(self.app.row_data[0]["query"], "测试丛书 第2册")
        self.assertEqual(self.app.row_data[0]["path"], "/test/book.pdf")


if __name__ == "__main__":
    unittest.main()
