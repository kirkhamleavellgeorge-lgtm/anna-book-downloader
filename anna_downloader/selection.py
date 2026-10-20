from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from .core import CandidateGroup


class VolumeSelectionDialog:
    """Let the user choose volumes without blocking Tk's event loop."""

    def __init__(
        self,
        parent: tk.Misc,
        query: str,
        groups: list[CandidateGroup],
        on_complete: Callable[[list[str]], None],
        *,
        on_stop: Callable[[], None] | None = None,
    ):
        self.groups = list(groups)
        self.on_complete = on_complete
        self.on_stop = on_stop
        self.selected: set[str] = set()
        self._resolved = False
        self._previous_grab = parent.grab_current()
        self.window = tk.Toplevel(parent)
        self.window.title("选择要下载的分册")
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda event: self.close())
        self.window.bind("<Destroy>", self._on_destroy)

        width = min(1000, self.window.winfo_screenwidth() - 80)
        height = min(650, self.window.winfo_screenheight() - 100)
        self.window.geometry(f"{width}x{height}")
        self.window.minsize(min(720, width), min(460, height))

        content = ttk.Frame(self.window, padding=16)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text=f"“{query}”找到多个分册或书名", wraplength=900).pack(anchor="w")
        ttk.Label(
            content,
            text="勾选需要的书后继续下载。同一分册的不同格式会自动按偏好选择。点击行或按空格键勾选。",
            wraplength=900,
        ).pack(anchor="w", pady=(6, 12))

        table = ttk.Frame(content)
        table.pack(fill="both", expand=True)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("title", "author", "formats"), selectmode="browse")
        self.tree.heading("#0", text="勾选")
        self.tree.column("#0", width=54, minwidth=54, stretch=False, anchor="center")
        self.tree.heading("title", text="完整书名 / 分册")
        self.tree.column("title", width=470, minwidth=250)
        self.tree.heading("author", text="作者")
        self.tree.column("author", width=180, minwidth=100)
        self.tree.heading("formats", text="可用格式")
        self.tree.column("formats", width=130, minwidth=90)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        self._rows: dict[str, CandidateGroup] = {}
        for index, group in enumerate(self.groups):
            row_id = str(index)
            self._rows[row_id] = group
            authors = " / ".join(dict.fromkeys(c.author for c in group.candidates if c.author)) or "作者未标注"
            formats = " / ".join(dict.fromkeys(c.extension.upper() for c in group.candidates))
            self.tree.insert("", "end", iid=row_id, text="☐", values=(group.title, authors, formats))
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._toggle_focused)
        self.tree.bind("<<TreeviewSelect>>", self._show_details)

        ttk.Label(content, text="当前行的完整信息：").pack(anchor="w", pady=(12, 4))
        self.details = tk.Text(content, height=4, wrap="word", state="disabled", takefocus=True)
        self.details.pack(fill="x")

        controls = ttk.Frame(content)
        controls.pack(fill="x", pady=(12, 0))
        ttk.Button(controls, text="全选", command=self.select_all).pack(side="left")
        ttk.Button(controls, text="全不选", command=self.select_none).pack(side="left", padx=(8, 12))
        self.count_text = tk.StringVar(self.window)
        ttk.Label(controls, textvariable=self.count_text).pack(side="left")
        self.confirm_button = ttk.Button(controls, text="下载所选分册", command=self.confirm)
        self.confirm_button.pack(side="right")
        ttk.Button(controls, text="跳过这本书", command=self.close).pack(side="right", padx=(0, 8))
        self.stop_button = None
        if on_stop is not None:
            self.stop_button = ttk.Button(controls, text="停止队列", command=self._stop_queue)
            self.stop_button.pack(side="right", padx=(0, 8))
        self._refresh()
        if self._rows:
            first = next(iter(self._rows))
            self.tree.focus(first)
            self.tree.selection_set(first)
            self._show_details()
        self.window.update_idletasks()
        self.window.grab_set()
        self.tree.focus_set()

    def _refresh(self):
        for row_id, group in self._rows.items():
            self.tree.item(row_id, text="☑" if group.key in self.selected else "☐")
        self.count_text.set(f"已选 {len(self.selected)} / {len(self.groups)} 册")
        self.confirm_button.configure(state="normal" if self.selected else "disabled")

    def _toggle(self, row_id: str):
        if row_id not in self._rows or self._resolved:
            return
        key = self._rows[row_id].key
        if key in self.selected:
            self.selected.remove(key)
        else:
            self.selected.add(key)
        self._refresh()

    def _on_click(self, event):
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.focus(row_id)
            self.tree.selection_set(row_id)
            self.tree.focus_set()
            self._toggle(row_id)
            self._show_details()
            return "break"

    def _toggle_focused(self, event=None):
        self._toggle(self.tree.focus())
        return "break"

    def _show_details(self, event=None):
        group = self._rows.get(self.tree.focus())
        if not group:
            return
        lines = [group.title]
        for candidate in group.candidates:
            description = " · ".join(filter(None, (candidate.author, candidate.extension.upper(), candidate.metadata)))
            if description and description not in lines:
                lines.append(description)
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", "\n".join(lines))
        self.details.configure(state="disabled")

    def select_all(self):
        if not self._resolved:
            self.selected = {group.key for group in self.groups}
            self._refresh()

    def select_none(self):
        if not self._resolved:
            self.selected.clear()
            self._refresh()

    def confirm(self):
        if self.selected:
            self._finish([group.key for group in self.groups if group.key in self.selected])

    def close(self):
        self._finish([])

    def _stop_queue(self):
        if self._resolved:
            return
        try:
            if self.on_stop is not None:
                self.on_stop()
        finally:
            self.close()

    def _on_destroy(self, event):
        if event.widget is self.window:
            self._finish([])

    def _finish(self, keys: list[str]):
        if self._resolved:
            return
        self._resolved = True
        try:
            if self.window.winfo_exists():
                self.window.grab_release()
                self.window.destroy()
            if self._previous_grab is not None and self._previous_grab.winfo_exists():
                self._previous_grab.grab_set()
        except tk.TclError:
            pass
        self.on_complete(keys)
