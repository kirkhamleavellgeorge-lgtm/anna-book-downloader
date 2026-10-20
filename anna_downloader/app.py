from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .browser import Options, Runner
from .chrome_session import ChromeBridge
from .core import parse_requests


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MIRRORS = ("https://annas-archive.pk", "https://annas-archive.gl", "https://annas-archive.gd")
LANGUAGES = {"不限语言": "", "中文": "zh", "英文": "en"}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.worker: threading.Thread | None = None
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.chrome_bridge = ChromeBridge(DATA, ROOT / "chrome_extension", lambda kind, data: self.events.put((kind, data)))
        self.chrome_guide = None
        self.closing = False
        self.rows = []
        self.row_data = []
        self.controls = []
        self.settings = self._load_settings()
        self.root.title("Anna 图书下载助手")
        self.root.geometry("1120x820")
        self.root.minsize(900, 680)
        self.root.configure(bg="#f4f6fa")
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6fa")
        style.configure("TLabel", background="#f4f6fa", foreground="#243247")
        style.configure("TButton", padding=(12, 7))
        style.configure("Treeview", rowheight=32, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Accent.TButton", background="#285dd0", foreground="white")
        style.map("Accent.TButton", background=[("active", "#1948ad"), ("disabled", "#9aaacb")])

        main = ttk.Frame(root, padding=22)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="Anna 图书下载助手", font=("Microsoft YaHei UI", 21, "bold")).pack(anchor="w")
        ttk.Label(main, text="一次输入书单，自动搜索、匹配版本并依次下载。", foreground="#65728a").pack(anchor="w", pady=(5, 16))

        entry_header = ttk.Frame(main)
        entry_header.pack(fill="x")
        ttk.Label(entry_header, text="书单", font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self._button(entry_header, "导入 TXT", self._import, side="right")
        ttk.Label(entry_header, text="每行一本；可输入：书名 | 作者，或 ISBN", foreground="#65728a").pack(side="left", padx=14)
        self.input = tk.Text(main, height=5, wrap="word", relief="solid", borderwidth=1, padx=10, pady=8, undo=True)
        self.input.pack(fill="x", pady=(8, 12))
        self.input.insert("1.0", self.settings.get("books", ""))
        self.controls.append(self.input)

        settings = ttk.Frame(main)
        settings.pack(fill="x")
        self.output = tk.StringVar(value=self.settings.get("output", str(ROOT / "downloads")))
        self.mirror = tk.StringVar(value=self.settings.get("mirror", MIRRORS[0]))
        if self.mirror.get() not in MIRRORS:
            self.mirror.set(MIRRORS[0])
        self.language = tk.StringVar(value=self.settings.get("language", "不限语言"))
        if self.language.get() not in LANGUAGES:
            self.language.set("不限语言")
        self.preferred = tk.StringVar(value=self.settings.get("preferred", "EPUB"))
        if self.preferred.get() not in ("EPUB", "PDF", "MOBI", "AZW3", "TXT"):
            self.preferred.set("EPUB")
        ttk.Label(settings, text="保存目录").grid(row=0, column=0, sticky="w", pady=5)
        output_entry = ttk.Entry(settings, textvariable=self.output)
        output_entry.grid(row=0, column=1, columnspan=5, sticky="ew", padx=9)
        self.controls.append(output_entry)
        browse = ttk.Button(settings, text="选择目录", command=self._choose_output)
        browse.grid(row=0, column=6)
        self.controls.append(browse)
        ttk.Label(settings, text="网站地址").grid(row=1, column=0, sticky="w", pady=8)
        self._combo(settings, self.mirror, MIRRORS, 1, 1, 30)
        ttk.Label(settings, text="语言").grid(row=1, column=2, padx=(18, 0))
        self._combo(settings, self.language, tuple(LANGUAGES), 1, 3, 11)
        ttk.Label(settings, text="优先格式").grid(row=1, column=4, padx=(18, 0))
        self._combo(settings, self.preferred, ("EPUB", "PDF", "MOBI", "AZW3", "TXT"), 1, 5, 9)
        settings.columnconfigure(1, weight=1)

        chrome = ttk.Frame(main)
        chrome.pack(fill="x", pady=(10, 0))
        self.chrome_status = tk.StringVar(value="Chrome 未连接：首次需加载扩展，即可复用当前网站登录状态")
        ttk.Label(chrome, textvariable=self.chrome_status, foreground="#65728a").pack(side="left")
        ttk.Button(chrome, text="连接已登录 Chrome", command=self._chrome_setup).pack(side="right")

        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=(12, 13))
        self.start_button = self._button(actions, "开始下载", self._start, side="left", style="Accent.TButton")
        self.stop_button = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="打开下载目录", command=self._open_output).pack(side="left")
        ttk.Button(actions, text="导出结果 CSV", command=self._export).pack(side="right")

        table_frame = ttk.Frame(main)
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_frame, columns=("query", "matched", "status", "detail"), show="headings", height=6)
        for key, label, width in (("query", "输入书名", 220), ("matched", "匹配版本", 220), ("status", "状态", 125), ("detail", "进度 / 保存位置", 430)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=80, stretch=key != "status")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<Double-1>", self._open_row)

        self.status = tk.StringVar(value="准备就绪。自动等待浏览器检查；如需手动完成验证码，程序会提示，通过后自动继续。")
        ttk.Label(main, textvariable=self.status, wraplength=1000).pack(anchor="w", pady=(12, 6))
        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 10))
        self.log_text = tk.Text(main, height=5, wrap="word", bg="#eaf0f8", fg="#42516a", relief="flat", padx=10, pady=7, state="disabled", font=("Microsoft YaHei UI", 9))
        self.log_text.pack(fill="x")
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Control-Return>", lambda _: self._start())
        self.root.after(50, self._start_chrome_bridge)
        self.root.after(100, self._poll)

    def _start_chrome_bridge(self):
        try:
            self.chrome_bridge.start()
        except Exception as exc:
            self.chrome_status.set("Chrome 连接服务启动失败")
            self._log(str(exc))

    @staticmethod
    def _chrome_path():
        paths = [Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe", Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe", Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"]
        return next((path for path in paths if path.is_file()), None)

    def _chrome_setup(self):
        if self.chrome_guide is not None and self.chrome_guide.winfo_exists():
            self.chrome_guide.lift()
            return
        guide = self.chrome_guide = tk.Toplevel(self.root)
        guide.title("连接已登录的 Chrome")
        guide.geometry("720x335")
        guide.transient(self.root)
        frame = ttk.Frame(guide, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="首次加载一次扩展，即可使用当前 Chrome 登录状态", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(0, 12))
        steps = "1. 在已登录网站的 Chrome 中打开 chrome://extensions/。\n2. 开启右上角“开发者模式”，点击“加载已解压的扩展程序”。\n3. 选择下面的文件夹，等待本程序显示“已连接 Chrome”。"
        ttk.Label(frame, text=steps, justify="left", wraplength=660).pack(anchor="w")
        directory = str((ROOT / "chrome_extension").resolve())
        entry = ttk.Entry(frame)
        entry.insert(0, directory)
        entry.configure(state="readonly")
        entry.pack(fill="x", pady=14)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")

        def open_chrome():
            chrome = self._chrome_path()
            if chrome:
                subprocess.Popen([str(chrome), "chrome://extensions/"], close_fds=True)
            else:
                messagebox.showerror("未找到 Chrome", "请在已登录的 Chrome 中手动打开 chrome://extensions/。", parent=guide)

        def copy_path():
            self.root.clipboard_clear()
            self.root.clipboard_append(directory)

        ttk.Button(buttons, text="打开 Chrome 扩展页", command=open_chrome).pack(side="left")
        ttk.Button(buttons, text="复制扩展目录", command=copy_path).pack(side="left", padx=8)
        ttk.Button(buttons, text="打开扩展文件夹", command=lambda: os.startfile(directory)).pack(side="left")
        ttk.Label(frame, textvariable=self.chrome_status, wraplength=660).pack(anchor="w", pady=(16, 0))

    def _button(self, parent, text, command, **pack):
        style = pack.pop("style", "TButton")
        button = ttk.Button(parent, text=text, command=command, style=style)
        button.pack(**pack)
        self.controls.append(button)
        return button

    def _combo(self, parent, variable, values, row, column, width):
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=width)
        combo.grid(row=row, column=column, sticky="ew", padx=9)
        self.controls.append(combo)

    @staticmethod
    def _load_settings():
        try:
            value = json.loads((DATA / "settings.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        DATA.mkdir(parents=True, exist_ok=True)
        settings = {"books": self.input.get("1.0", "end-1c"), "output": self.output.get(), "mirror": self.mirror.get(), "language": self.language.get(), "preferred": self.preferred.get()}
        try:
            (DATA / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self._log(f"设置未能保存：{exc}")

    def _import(self):
        path = filedialog.askopenfilename(filetypes=[("书单文本", "*.txt"), ("所有文件", "*.*")])
        if path:
            try:
                raw = Path(path).read_bytes()
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("gb18030")
                self.input.delete("1.0", "end")
                self.input.insert("1.0", text)
            except (OSError, UnicodeError) as exc:
                messagebox.showerror("无法导入", str(exc))

    def _choose_output(self):
        path = filedialog.askdirectory(initialdir=self.output.get())
        if path:
            self.output.set(path)

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        books = parse_requests(self.input.get("1.0", "end"))
        if not books:
            messagebox.showinfo("请输入书名", "每行输入一本书，也可以导入 TXT 书单。")
            return
        if not self.output.get().strip():
            messagebox.showinfo("请选择目录", "请选择图书保存目录。")
            return
        if not self.chrome_bridge.connected.is_set():
            self.status.set("请先连接已登录的 Chrome，再开始下载。")
            self._chrome_setup()
            return
        output = Path(self.output.get()).expanduser().resolve()
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("目录不可用", str(exc))
            return
        self._save_settings()
        self.table.delete(*self.table.get_children())
        self.rows, self.row_data = [], []
        for book in books:
            self.rows.append(self.table.insert("", "end", values=(book.query, "", "等待中", "")))
            self.row_data.append({"query": book.query, "matched": "", "status": "等待中", "detail": ""})
        self.stop.clear()
        self._busy(True)
        options = Options(output=output, data=DATA, base_url=self.mirror.get(), preferred=self.preferred.get().lower(), language=LANGUAGES[self.language.get()], chrome_bridge=self.chrome_bridge)

        def work():
            try:
                runner = Runner(options, lambda kind, data: self.events.put((kind, data)), self.stop)
                asyncio.run(runner.run(books))
            except Exception as exc:
                logging.exception("Worker failed")
                self.events.put(("error", {"message": str(exc)}))
                self.events.put(("finished", {"done": 0, "skipped": 0, "failed": 0, "stopped": True}))

        self.worker = threading.Thread(target=work, name="anna-downloads", daemon=True)
        self.worker.start()
        self.status.set(f"已加入 {len(books)} 本书；正在使用已连接的 Chrome。")

    def _busy(self, busy):
        for control in self.controls:
            if isinstance(control, ttk.Combobox):
                control.configure(state="disabled" if busy else "readonly")
            else:
                control.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _stop(self):
        self.stop.set()
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止任务，并关闭本程序创建的 Chrome 标签页……")

    def _log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 600:
            self.log_text.delete("1.0", "100.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self._log(data["message"])
                elif kind == "chrome":
                    self.chrome_status.set("已连接 Chrome：使用当前配置的网站会话" if data.get("connected") else data["message"])
                    if data.get("connected"):
                        self._log(data["message"])
                elif kind == "book" and 0 <= data["index"] < len(self.rows):
                    index = data["index"]
                    self.row_data[index].update(data)
                    row = self.row_data[index]
                    self.table.item(self.rows[index], values=tuple(row.get(k, "") for k in ("query", "matched", "status", "detail")))
                    self.table.see(self.rows[index])
                    self.status.set(f"{row['query']}：{row['status']}  {row['detail']}")
                elif kind == "error":
                    self._log(data["message"])
                    if not self.closing:
                        messagebox.showerror("运行未完成", data["message"])
                elif kind == "finished":
                    self._busy(False)
                    for index, row in enumerate(self.row_data):
                        if row["status"] == "等待中":
                            row["status"] = "未运行"
                            self.table.set(self.rows[index], "status", "未运行")
                    self.status.set(f"{'已停止' if data.get('stopped') else '队列结束'}：完成 {data['done']}，跳过 {data['skipped']}，失败 {data['failed']}。双击已完成的记录可打开文件。")
        except queue.Empty:
            pass
        if self.closing and (not self.worker or not self.worker.is_alive()):
            self.chrome_bridge.stop()
            self.root.destroy()
            return
        self.root.after(100, self._poll)

    def _open_output(self):
        path = Path(self.output.get()).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc))

    def _open_row(self, _event):
        selection = self.table.selection()
        if selection:
            index = self.rows.index(selection[0])
            path = self.row_data[index].get("path")
            if path and Path(path).is_file():
                try:
                    os.startfile(path)
                except OSError as exc:
                    messagebox.showerror("无法打开文件", str(exc))

    def _export(self):
        if not self.row_data:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="图书下载结果.csv", filetypes=[("CSV 表格", "*.csv")])
        if path:
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(("输入书名", "匹配版本", "状态", "详情", "文件位置", "来源页面"))
                    for row in self.row_data:
                        # Prevent spreadsheet programs from treating website titles as formulas.
                        values = [str(row.get(k, "")) for k in ("query", "matched", "status", "detail", "path", "url")]
                        writer.writerow(["'" + value if value.startswith(("=", "+", "-", "@")) else value for value in values])
            except OSError as exc:
                messagebox.showerror("无法导出", str(exc))

    def _close(self):
        self._save_settings()
        self.closing = True
        if self.worker and self.worker.is_alive():
            self._stop()
        else:
            self.chrome_bridge.stop()
            self.root.destroy()


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[RotatingFileHandler(DATA / "application.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")])
    root = tk.Tk()
    App(root)
    root.mainloop()
