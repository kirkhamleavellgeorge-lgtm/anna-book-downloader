import tkinter as tk
import unittest

from anna_downloader.core import Candidate, CandidateGroup
from anna_downloader.selection import VolumeSelectionDialog


def group(key, title, formats=("pdf",)):
    candidates = tuple(
        Candidate(str(index) * 32, title, "测试作者", extension, f"zh, {extension}, 2MB", "https://example.test/")
        for index, extension in enumerate(formats)
    )
    return CandidateGroup(key, title, candidates)


class SelectionDialogTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("240x80+20+20")
        self.results = []
        self.groups = [
            group("vol1", "科学帮帮忙 第 1 册：万能的磁铁", ("epub", "pdf")),
            group("vol2", "科学帮帮忙 第 2 册：变幻莫测的化学"),
            group("vol3", "科学帮帮忙 第 3 册：" + "很长的完整分册名称" * 15),
        ]
        self.dialog = VolumeSelectionDialog(self.root, "科学帮帮忙", self.groups, self.results.append)
        self.root.update()

    def tearDown(self):
        self.dialog.close()
        self.root.destroy()

    def test_initially_unchecked_and_confirm_disabled(self):
        self.assertEqual(self.dialog.selected, set())
        self.assertTrue(self.dialog.confirm_button.instate(["disabled"]))
        self.dialog.confirm()
        self.assertEqual(self.results, [])
        self.assertEqual(self.dialog.tree.item("0", "values")[2], "EPUB / PDF")

    def test_all_none_and_confirm_preserves_original_order(self):
        self.dialog.select_all()
        self.assertEqual(self.dialog.selected, {"vol1", "vol2", "vol3"})
        self.assertFalse(self.dialog.confirm_button.instate(["disabled"]))
        self.assertEqual(self.dialog.count_text.get(), "已选 3 / 3 册")
        self.dialog.select_none()
        self.assertTrue(self.dialog.confirm_button.instate(["disabled"]))
        self.dialog._toggle("2")
        self.dialog._toggle("0")
        self.dialog.confirm_button.invoke()
        self.assertEqual(self.results, [["vol1", "vol3"]])
        self.dialog.close()
        self.assertEqual(len(self.results), 1)

    def test_mouse_and_space_toggle_with_full_title_details(self):
        x, y, width, height = self.dialog.tree.bbox("1")
        self.dialog.tree.event_generate("<Button-1>", x=x + 10, y=y + height // 2)
        self.root.update()
        self.assertEqual(self.dialog.selected, {"vol2"})
        self.dialog.tree.focus_force()
        self.dialog.tree.event_generate("<space>")
        self.root.update()
        self.assertEqual(self.dialog.selected, set())
        self.dialog.tree.focus("2")
        self.dialog.tree.selection_set("2")
        self.root.update()
        self.assertIn(self.groups[2].title, self.dialog.details.get("1.0", "end"))

    def test_window_close_skips_exactly_once_and_releases_grab(self):
        self.dialog.select_all()
        close_command = self.dialog.window.protocol("WM_DELETE_WINDOW")
        self.dialog.window.tk.call(close_command)
        self.dialog.close()
        self.assertEqual(self.results, [[]])
        self.assertIsNone(self.root.grab_current())

    def test_external_window_destroy_resolves_once(self):
        self.dialog.window.destroy()
        self.dialog.close()
        self.assertEqual(self.results, [[]])

    def test_stop_queue_callback_runs_before_selection_resolves(self):
        self.dialog.close()
        self.results.clear()
        stops = []

        def on_stop():
            stops.append(list(self.results))
            self.dialog.close()  # App._stop closes its active dialog too.

        self.dialog = VolumeSelectionDialog(
            self.root, "科学帮帮忙", self.groups, self.results.append, on_stop=on_stop,
        )
        self.dialog.select_all()
        self.dialog.stop_button.invoke()
        self.dialog.close()
        self.assertEqual(stops, [[]])
        self.assertEqual(self.results, [[]])


if __name__ == "__main__":
    unittest.main()
