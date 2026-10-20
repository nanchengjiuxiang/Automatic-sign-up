"""报名接龙工具 - Windows desktop prototype."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "报名接龙工具"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SignupChain"
DATA_FILE = DATA_DIR / "data.bin"
TEMP_DIR = DATA_DIR / "temp"


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buf = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    return DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def protect(data: bytes) -> bytes:
    """Encrypt local profile/task data using Windows DPAPI for the current user."""
    if sys.platform != "win32":
        raise RuntimeError("本版本的数据加密只支持 Windows")
    source, keepalive = _blob(data)
    target = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not crypt32.CryptProtectData(ctypes.byref(source), "SignupChain", None, None, None, 0, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("本版本的数据解密只支持 Windows")
    source, keepalive = _blob(data)
    target = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def load_data() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        return {"profile": [], "tasks": [], "logs": [], "binding": None, "early_retry_seconds": 30}
    raw = unprotect(DATA_FILE.read_bytes())
    result = json.loads(raw.decode("utf-8"))
    try:
        early_retry_seconds = int(result.get("early_retry_seconds", 30))
    except (TypeError, ValueError):
        early_retry_seconds = 30
    if not 1 <= early_retry_seconds <= 600:
        early_retry_seconds = 30
    return {"profile": result.get("profile", []), "tasks": result.get("tasks", []), "logs": result.get("logs", []), "binding": None, "early_retry_seconds": early_retry_seconds}


def save_data(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    temp_file = DATA_FILE.with_suffix(".tmp")
    temp_file.write_bytes(protect(payload))
    temp_file.replace(DATA_FILE)


class AutomationError(Exception):
    pass


def _descendants(control, depth=0, max_depth=15):
    if depth > max_depth:
        return
    try:
        children = control.GetChildren()
    except Exception:
        return
    for child in children:
        yield child
        yield from _descendants(child, depth + 1, max_depth)


def _control_type(control) -> str:
    return str(getattr(control, "ControlTypeName", ""))


def _name(control) -> str:
    return str(getattr(control, "Name", "") or "").strip()


def _click(control) -> None:
    control.SetFocus()
    control.Click()


def _set_edit_value(control, value: str) -> None:
    try:
        control.GetValuePattern().SetValue(value)
        return
    except Exception:
        pass
    try:
        import uiautomation as auto
        control.SetFocus()
        auto.SendKeys("{Ctrl}a" + value, waitTime=0.02)
    except Exception as exc:
        raise AutomationError(f"无法填写字段“{_name(control)}”：{exc}") from exc


def execute_signup(task: dict, profile: list[dict], binding=None, early_retry_seconds=30) -> str:
    """Run the screenshot-anchored flow for the QQ mini program."""
    try:
        from visual_automation import visual_signup
        return visual_signup(task, binding=binding, early_retry_seconds=early_retry_seconds)
    except ImportError as exc:
        raise AutomationError("缺少视觉识别依赖，请按 README 安装项目依赖。") from exc
    except Exception as exc:
        if isinstance(exc, AutomationError):
            raise
        raise AutomationError(str(exc)) from exc


class SignupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x780")
        self.minsize(980, 660)
        try:
            self.data = load_data()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"本地加密数据无法读取：{exc}\n数据不会被覆盖。")
            self.destroy()
            raise SystemExit(1)
        self.colors = {
            "bg": "#eaf2fc", "panel": "#ffffff", "line": "#e6edf6",
            "ink": "#17253c", "muted": "#74839a", "blue": "#3478e5",
            "blue_dark": "#2465cf", "pale": "#eaf2ff", "green": "#18a879",
            "sidebar": "#edf4fc", "soft": "#f5f8fc",
        }
        self.configure(bg=self.colors["bg"])
        self._style()
        self._build_shell()
        self._show_tasks()
        self.connection.configure(text="●  正在检查 QQ 页面…", fg="#a96812")
        self.after(700, self._auto_preflight_page)
        self.after(200, self._scheduler_tick)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=self.colors["bg"])
        style.configure("Panel.TFrame", background=self.colors["panel"])
        style.configure("TLabel", background=self.colors["bg"], foreground=self.colors["ink"], font=("Microsoft YaHei UI", 10))
        style.configure("Panel.TLabel", background=self.colors["panel"], foreground=self.colors["ink"], font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=self.colors["panel"], foreground=self.colors["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background=self.colors["bg"], foreground=self.colors["ink"], font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(13, 8), background="#ffffff", foreground=self.colors["ink"], bordercolor=self.colors["line"], lightcolor="#ffffff", darkcolor=self.colors["line"])
        style.map("TButton", background=[("active", self.colors["soft"])], foreground=[("disabled", "#aab4c2")])
        style.configure("Primary.TButton", background=self.colors["blue"], foreground="#ffffff", borderwidth=0, padding=(15, 9))
        style.map("Primary.TButton", background=[("active", self.colors["blue_dark"])], foreground=[("active", "#ffffff")])
        style.configure("Create.TButton", background="#20a464", foreground="#ffffff", borderwidth=0, padding=(15, 9))
        style.map("Create.TButton", background=[("active", "#188651")], foreground=[("active", "#ffffff")])
        style.configure("Treeview", rowheight=38, font=("Microsoft YaHei UI", 9), background="#ffffff", fieldbackground="#ffffff", foreground=self.colors["ink"], borderwidth=0, relief="flat")
        style.map("Treeview", background=[("selected", self.colors["pale"])], foreground=[("selected", self.colors["blue_dark"])])
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), background=self.colors["soft"], foreground=self.colors["muted"], relief="flat", padding=(10, 10))
        style.map("Treeview.Heading", background=[("active", self.colors["pale"])])
        style.configure("TEntry", padding=(8, 7), fieldbackground="#ffffff", foreground=self.colors["ink"], bordercolor=self.colors["line"], lightcolor=self.colors["line"], darkcolor=self.colors["line"])
        style.configure("TSpinbox", padding=(6, 5), fieldbackground="#ffffff", foreground=self.colors["ink"])

    def _build_shell(self):
        top = tk.Frame(self, bg="#ffffff", height=68, highlightbackground=self.colors["line"], highlightthickness=1)
        top.pack(fill="x")
        tk.Label(top, text="报", bg=self.colors["blue"], fg="white", width=2, height=1, font=("Microsoft YaHei UI", 15, "bold")).pack(side="left", padx=(24, 11), pady=15)
        brand = tk.Frame(top, bg="#ffffff"); brand.pack(side="left", pady=10)
        tk.Label(brand, text=APP_NAME, bg="#ffffff", fg=self.colors["ink"], font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        tk.Label(brand, text="SIGNUP DESK  ·  自动化工作台", bg="#ffffff", fg=self.colors["muted"], font=("Segoe UI", 8)).pack(anchor="w", pady=(2, 0))
        top_nav = tk.Frame(top, bg="#ffffff"); top_nav.pack(side="left", padx=(52, 0), fill="y")
        self.top_nav_buttons = {}
        for key, text, callback in [("tasks", "报名管理", self._show_tasks), ("logs", "运行记录", self._show_logs)]:
            item = tk.Frame(top_nav, bg="#ffffff"); item.pack(side="left", fill="y")
            button = tk.Button(item, text=text, command=callback, bg="#ffffff", fg=self.colors["muted"], activebackground=self.colors["soft"], activeforeground=self.colors["blue"], relief="flat", bd=0, font=("Microsoft YaHei UI", 10), padx=18, cursor="hand2")
            button.pack(fill="both", expand=True)
            indicator = tk.Frame(item, bg="#ffffff", height=2); indicator.pack(side="bottom", fill="x")
            self.top_nav_buttons[key] = (button, indicator)
        status_pill = tk.Frame(top, bg="#f3f7fc", highlightbackground=self.colors["line"], highlightthickness=1)
        status_pill.pack(side="right", padx=24, pady=16)
        self.connection = tk.Label(status_pill, text="●  QQ 页面待连接", bg="#f3f7fc", fg="#a96812", font=("Microsoft YaHei UI", 9), padx=11, pady=6)
        self.connection.pack()
        body = tk.Frame(self, bg=self.colors["bg"])
        body.pack(fill="both", expand=True)
        self.sidebar = tk.Frame(body, bg=self.colors["sidebar"], width=216)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        tk.Label(self.sidebar, text="工作空间", bg=self.colors["sidebar"], fg=self.colors["muted"], font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=23, pady=(28, 11))
        self.nav_buttons = {}
        for key, title, icon, callback in [
            ("tasks", "报名任务", "▦", self._show_tasks),
            ("profile", "报名资料", "◉", self._show_profile),
            ("logs", "执行记录", "≡", self._show_logs),
        ]:
            button = tk.Button(self.sidebar, text=f"{icon}   {title}", command=callback, anchor="w", bg=self.colors["sidebar"], fg=self.colors["muted"], activebackground="#ffffff", activeforeground=self.colors["blue"], relief="flat", bd=0, highlightthickness=0, font=("Microsoft YaHei UI", 10), padx=16, pady=11, cursor="hand2")
            button.pack(fill="x", padx=12, pady=3)
            self.nav_buttons[key] = button
        tk.Frame(self.sidebar, bg="#dbe6f3", height=1).pack(fill="x", padx=22, pady=(19, 15))
        tk.Label(self.sidebar, text="安全提示\n资料仅保存在本机并加密。", bg=self.colors["sidebar"], fg=self.colors["muted"], justify="left", font=("Microsoft YaHei UI", 9)).pack(side="bottom", anchor="w", padx=22, pady=22)
        self.content = tk.Frame(body, bg=self.colors["bg"])
        self.content.pack(side="left", fill="both", expand=True, padx=(26, 30), pady=25)

    def _set_active_nav(self, key):
        for item, button in self.nav_buttons.items():
            active = item == key
            button.configure(bg="#ffffff" if active else self.colors["sidebar"], fg=self.colors["blue"] if active else self.colors["muted"], font=("Microsoft YaHei UI", 10, "bold" if active else "normal"))
        top_key = "logs" if key == "logs" else "tasks"
        for item, (button, indicator) in self.top_nav_buttons.items():
            active = item == top_key
            button.configure(fg=self.colors["blue"] if active else self.colors["muted"], font=("Microsoft YaHei UI", 10, "bold" if active else "normal"))
            indicator.configure(bg=self.colors["blue"] if active else "#ffffff")

    def _clear(self):
        for widget in self.content.winfo_children():
            widget.destroy()

    def _header(self, title, subtitle):
        ttk.Label(self.content, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.content, text=subtitle, foreground=self.colors["muted"]).pack(anchor="w", pady=(5, 19))

    def _panel(self, parent=None):
        panel = tk.Frame(parent or self.content, bg="#ffffff", highlightbackground=self.colors["line"], highlightthickness=1, bd=0)
        return panel

    def _show_tasks(self):
        self._set_active_nav("tasks")
        self._clear()
        self._header("报名任务", "在报名开始前准备活动和资料，软件会在预约时间自动执行。")
        bar = tk.Frame(self.content, bg=self.colors["bg"])
        bar.pack(fill="x", pady=(0, 15))
        tk.Label(bar, text="任务总览  /  报名排期", bg=self.colors["bg"], fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left", pady=10)
        ttk.Button(bar, text="＋  新建报名任务", style="Create.TButton", command=self._task_editor).pack(side="right")
        page_actions = tk.Frame(bar, bg=self.colors["bg"])
        page_actions.pack(side="right", padx=(0, 10))
        ttk.Button(page_actions, text="只读检查控件树", command=self._inspect_qq_page, style="Primary.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(page_actions, text="连接并预检 QQ 页面", command=self._preflight_qq_page, style="Primary.TButton").pack(side="left")

        tasks = self.data["tasks"]
        pending_count = sum(task.get("status") == "待执行" for task in tasks)
        completed_count = sum(task.get("status") == "完成" for task in tasks)
        metric_row = tk.Frame(self.content, bg=self.colors["bg"])
        metric_row.pack(fill="x", pady=(0, 15))
        metric_values = [
            ("全部任务", str(len(tasks)), self.colors["blue"]),
            ("等待报名", str(pending_count), "#e4a12d"),
            ("已完成", str(completed_count), self.colors["green"]),
            ("QQ 页面", "已连接" if self.data.get("binding") else "待连接", self.colors["blue"] if self.data.get("binding") else "#a96812"),
        ]
        for index, (caption, value, accent) in enumerate(metric_values):
            metric = self._panel(metric_row)
            metric.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 10, 0))
            tk.Label(metric, text=caption, bg="#ffffff", fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16, pady=(12, 3))
            tk.Label(metric, text=value, bg="#ffffff", fg=accent, font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w", padx=16, pady=(0, 12))

        note = self._panel(); note.pack(fill="x", pady=(0, 12))
        note_body = tk.Frame(note, bg="#ffffff"); note_body.pack(fill="x", padx=16, pady=9)
        note_left = tk.Frame(note_body, bg="#ffffff"); note_left.pack(side="left", fill="x", expand=True)
        tk.Label(note_left, text="执行设置", bg="#ffffff", fg=self.colors["ink"], font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        binding = self.data.get("binding")
        binding_text = (f"已绑定：{binding.get('title', 'QQ')}；详情页识别分数 {binding.get('detail_score', 0):.2f}；检查于 {binding.get('checked_at', '')}。"
                        if binding else "尚未绑定 QQ 活动页。请打开目标活动详情页后连接并预检。")
        tk.Label(note_left, text=binding_text + " 正式执行前会再次核对页面；预检只读，不会填写或提交。", bg="#ffffff", fg=self.colors["muted"], wraplength=600, justify="left", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))
        retry_row = tk.Frame(note_body, bg="#f5f8fc", highlightbackground=self.colors["line"], highlightthickness=1)
        retry_row.pack(side="right", padx=(16, 0), ipadx=10, ipady=4)
        tk.Label(retry_row, text="未到开始提示后的重试时长", bg="#f5f8fc", fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=8, pady=(2, 0))
        self.retry_seconds_var = tk.StringVar(value=str(self.data.get("early_retry_seconds", 30)))
        setting_line = tk.Frame(retry_row, bg="#f5f8fc"); setting_line.pack(anchor="w", padx=8, pady=(0, 2))
        ttk.Spinbox(setting_line, from_=1, to=600, increment=1, width=6, textvariable=self.retry_seconds_var).pack(side="left")
        tk.Label(setting_line, text="秒", bg="#f5f8fc", fg=self.colors["muted"]).pack(side="left", padx=4)
        ttk.Button(setting_line, text="保存", command=self._save_retry_duration, style="Primary.TButton").pack(side="left", padx=(7, 0))

        panel = self._panel(); panel.pack(fill="both", expand=True)
        panel_head = tk.Frame(panel, bg="#ffffff"); panel_head.pack(fill="x", padx=17, pady=(13, 3))
        tk.Label(panel_head, text="报名排期", bg="#ffffff", fg=self.colors["ink"], font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        panel_actions = tk.Frame(panel_head, bg="#ffffff"); panel_actions.pack(side="right")
        ttk.Button(panel_actions, text="编辑选中任务", command=lambda: self._edit_selected(tree)).pack(side="right", padx=(8, 0))
        ttk.Button(panel_actions, text="删除选中", command=lambda: self._delete_selected(tree)).pack(side="right")
        columns = ("activity", "start", "status")
        table = tk.Frame(panel, bg="#ffffff"); table.pack(fill="both", expand=True, padx=15, pady=(5, 13))
        tree = ttk.Treeview(table, columns=columns, show="headings", height=7)
        tree.heading("activity", text="活动名称"); tree.heading("start", text="报名开始时间"); tree.heading("status", text="状态")
        tree.column("activity", width=390, anchor="w"); tree.column("start", width=230, anchor="center"); tree.column("status", width=130, anchor="center")
        tree.tag_configure("pending", foreground="#bf831a")
        tree.tag_configure("done", foreground=self.colors["green"])
        tree.tag_configure("failed", foreground="#cf5252")
        tree.pack(fill="both", expand=True)
        for task in sorted(tasks, key=lambda x: x.get("start_at", "")):
            status = task.get("status", "待执行")
            tag = "pending" if status == "待执行" else "done" if status == "完成" else "failed" if status in ("失败", "错过时段") else ""
            tree.insert("", "end", iid=task["id"], values=(task.get("activity", ""), task.get("start_at", ""), status), tags=(tag,))

    def _save_retry_duration(self):
        try:
            seconds = int(self.retry_seconds_var.get().strip())
        except (AttributeError, TypeError, ValueError):
            messagebox.showwarning(APP_NAME, "重试时长请输入 1 到 600 之间的整数秒。")
            return
        if not 1 <= seconds <= 600:
            messagebox.showwarning(APP_NAME, "重试时长范围为 1 到 600 秒。")
            return
        self.data["early_retry_seconds"] = seconds
        self._persist()
        messagebox.showinfo(APP_NAME, f"未到开始时间时的自动重试时长已保存为 {seconds} 秒。")

    def _auto_preflight_page(self):
        """Bind the current QQ page at startup, then return focus to this app."""
        if getattr(self, "_preflight_busy", False):
            return
        self._preflight_busy = True
        threading.Thread(target=self._do_preflight, args=(True, True), daemon=True).start()

    def _preflight_qq_page(self):
        if getattr(self, "_preflight_busy", False):
            return
        self._preflight_busy = True
        self.connection.configure(text="●  正在连接并检查页面…", fg="#a96812")
        threading.Thread(target=self._do_preflight, args=(True, False), daemon=True).start()

    def _do_preflight(self, activate, quiet):
        try:
            from visual_automation import preflight_page
            binding = preflight_page(activate=activate)
            error = None
        except Exception as exc:
            binding, error = None, str(exc)
        self.after(0, lambda: self._finish_preflight(binding, error, quiet))

    def _finish_preflight(self, binding, error, quiet):
        self._preflight_busy = False
        if binding:
            self.data["binding"] = binding
            self._persist()
            self.connection.configure(text=f"●  QQ 页面已连接（识别 {binding['detail_score']:.2f}）", fg=self.colors["green"])
            if self.content.winfo_exists():
                self._show_tasks()
            if not quiet:
                pending = [task for task in self.data["tasks"] if task.get("status") == "待执行"]
                configured = sum(
                    bool(str(task.get("activity", "")).strip())
                    and bool(task.get("fields"))
                    and len({str(field.get("label", "")).strip() for field in task.get("fields", [])}) == len(task.get("fields", []))
                    and all(str(field.get("label", "")).strip() and str(field.get("value", "")).strip() for field in task.get("fields", []))
                    for task in pending
                )
                messagebox.showinfo(APP_NAME,
                    "预检通过：\n"
                    f"窗口：{binding['title']}\n"
                    f"活动详情页识别分数：{binding['detail_score']:.2f}\n"
                    f"运行依赖与识别模板：正常\n"
                    f"待执行任务字段配置：{configured}/{len(pending)} 项齐全\n"
                    "页面信息未填写或提交。\n\n"
                    "请确认 QQ 当前显示的就是所选任务对应的活动。")
        else:
            self.data["binding"] = None
            self.connection.configure(text="●  页面未识别，请连接预检", fg="#a96812")
            if self.content.winfo_exists():
                self._show_tasks()
            if not quiet:
                messagebox.showwarning(APP_NAME, f"预检未通过：{error}\n\n请在 QQ 打开目标活动详情页后重试。")
        if quiet:
            self.after(80, self.lift)

    def _inspect_qq_page(self):
        """Display UI Automation names locally without clicking or storing page data."""
        try:
            import uiautomation as auto
        except ImportError:
            messagebox.showerror(APP_NAME, "缺少 uiautomation 依赖。请先安装 requirements.txt 中的依赖。")
            return
        result_queue = queue.Queue()
        progress = tk.Toplevel(self)
        progress.title("只读检查 QQ 页面")
        progress.geometry("390x125")
        progress.transient(self)
        progress.configure(bg=self.colors["bg"])
        tk.Label(progress, text="正在读取可访问控件…", bg=self.colors["bg"], fg=self.colors["ink"],
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=20, pady=(22, 8))
        tk.Label(progress, text="QQ 的控件响应较慢时仍可继续操作主窗口。", bg=self.colors["bg"],
                 fg=self.colors["muted"]).pack(anchor="w", padx=20)
        progress.protocol("WM_DELETE_WINDOW", progress.destroy)

        def inspect_worker():
            report = []
            try:
                auto.InitializeUIAutomationInCurrentThread()
                auto.SetGlobalSearchTimeout(1.0)
                started = time.monotonic()
                deadline = started + 12.0
                max_nodes = 450
                root = auto.GetRootControl()
                windows = [c for c in root.GetChildren() if _control_type(c) == "WindowControl"]
                candidates = [c for c in windows
                              if "QQ" in _name(c).upper() or "报名接龙工具" in _name(c)]
                count = 0
                for window in candidates:
                    report.append(f"窗口：{_name(window) or '[无标题]'}")
                    stack = [(child, 1) for child in reversed(window.GetChildren())]
                    while stack and count < max_nodes and time.monotonic() < deadline:
                        control, depth = stack.pop()
                        count += 1
                        kind = _control_type(control) or "Control"
                        name = _name(control)
                        # Never retrieve ValuePattern values; mask common phone-like strings in accessible names.
                        name = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", name)
                        automation_id = str(getattr(control, "AutomationId", "") or "")
                        if name or automation_id:
                            report.append(f"  {kind} | {name or '[无名称]'} | AutomationId={automation_id}")
                        if depth < 7 and count < max_nodes and time.monotonic() < deadline:
                            try:
                                children = control.GetChildren()
                                stack.extend((child, depth + 1) for child in reversed(children))
                            except Exception:
                                pass
                    report.append("")
                if not candidates:
                    report = ["未找到标题含 QQ 或“报名接龙工具”的可访问窗口。", "请先打开 QQ 和目标活动详情页，再重新检查。"]
                if count >= max_nodes or time.monotonic() >= deadline:
                    report.extend(["检查已到达安全上限，已显示部分控件。", f"最多读取 {max_nodes} 个控件或运行 12 秒。"])
            except Exception as exc:
                report = [f"读取 Windows UI Automation 树失败：{exc}"]
            finally:
                try:
                    auto.UninitializeUIAutomationInCurrentThread()
                except Exception:
                    pass
                result_queue.put(report)

        def poll_result():
            try:
                report = result_queue.get_nowait()
            except queue.Empty:
                if progress.winfo_exists():
                    self.after(100, poll_result)
                return
            if progress.winfo_exists():
                progress.destroy()
            win = tk.Toplevel(self)
            win.title("只读检查 QQ 页面")
            win.geometry("820x560")
            win.transient(self)
            win.configure(bg=self.colors["bg"])
            tk.Label(win, text="只读页面检查", bg=self.colors["bg"], fg=self.colors["ink"],
                     font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", padx=18, pady=(16, 4))
            tk.Label(win, text="仅显示可访问控件名称和 AutomationId；不会点击或填写，也不会保存检查结果。",
                     bg=self.colors["bg"], fg=self.colors["muted"]).pack(anchor="w", padx=18, pady=(0, 10))
            text = tk.Text(win, bg="#ffffff", fg=self.colors["ink"], relief="flat", font=("Consolas", 9), wrap="none")
            scroll_y = ttk.Scrollbar(win, orient="vertical", command=text.yview)
            scroll_x = ttk.Scrollbar(win, orient="horizontal", command=text.xview)
            text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
            text.pack(side="top", fill="both", expand=True, padx=(18, 0))
            scroll_y.place(relx=1.0, rely=0.12, relheight=0.78, anchor="ne")
            scroll_x.pack(fill="x", padx=18, pady=(0, 12))
            text.insert("1.0", "\n".join(report))
            text.configure(state="disabled")

        threading.Thread(target=inspect_worker, name="qq-uia-inspector", daemon=True).start()
        self.after(100, poll_result)

    def _edit_selected(self, tree):
        selection = tree.selection()
        if selection:
            task = next((t for t in self.data["tasks"] if t["id"] == selection[0]), None)
            if task: self._task_editor(task)
        else: messagebox.showinfo(APP_NAME, "请先选择一个任务。")

    def _delete_selected(self, tree):
        selection = tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "请先选择一个报名任务。")
            return
        if messagebox.askyesno(APP_NAME, "删除选中的报名任务？"):
            self.data["tasks"] = [t for t in self.data["tasks"] if t["id"] != selection[0]]
            self._persist(); self._show_tasks()

    def _task_editor(self, existing=None):
        task = existing or {
            "id": str(uuid.uuid4()),
            "activity": "",
            "miniapp": "报名接龙工具",
            "start_at": "",
            "join_button": "点我报名",
            "submit_button": "提交报名",
            "fields": [
                {"label": "姓名", "value": ""},
                {"label": "学号", "value": ""},
                {"label": "学院", "value": ""},
                {"label": "手机号", "value": ""},
            ],
            "status": "待执行",
        }
        win = tk.Toplevel(self); win.title("编辑报名任务" if existing else "新建报名任务"); win.geometry("760x700"); win.resizable(False, False); win.transient(self); win.grab_set()
        win.configure(bg=self.colors["bg"])
        frame = tk.Frame(win, bg=self.colors["bg"]); frame.pack(fill="both", expand=True, padx=25, pady=11)
        # Reserve the footer first so expanding content cannot push the action buttons offscreen.
        bottom = tk.Frame(frame, bg=self.colors["bg"]); bottom.pack(side="bottom", fill="x", pady=(10, 0))
        hero = tk.Frame(frame, bg="#eaf2ff", highlightbackground="#dce9fb", highlightthickness=1)
        hero.pack(fill="x", pady=(0, 13))
        tk.Label(hero, text="活动与自动填写", bg="#eaf2ff", fg=self.colors["blue_dark"], font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w", padx=16, pady=(8, 1))
        tk.Label(hero, text="提前录入报名资料，到预约时间由软件自动填写并提交。", bg="#eaf2ff", fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16, pady=(0, 8))
        form = self._panel(frame); form.pack(fill="x")
        values = {}
        for label, key, default in [("目标小程序", "miniapp", "报名接龙工具"), ("活动名称", "activity", ""), ("报名开始时间", "start_at", "")]:
            row = tk.Frame(form, bg="#ffffff"); row.pack(fill="x", padx=17, pady=5)
            tk.Label(row, text=label, width=18, anchor="w", bg="#ffffff", fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left")
            entry = ttk.Entry(row); entry.pack(side="left", fill="x", expand=True)
            entry.insert(0, str(task.get(key, default))); values[key] = entry
        tk.Label(form, text="时间格式：YYYY-MM-DD HH:MM:SS    例如：2026-10-24 19:00:00", bg="#ffffff", fg=self.colors["muted"], font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=17, pady=(0, 11))
        tk.Label(frame, text="报名字段", bg=self.colors["bg"], fg=self.colors["ink"], font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(9, 2))
        tk.Label(frame, text="页面字段名按小程序标签填写；运行时用文字识别匹配，顺序无关。未出现在页面的预设项会跳过。", bg=self.colors["bg"], fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 5))
        tablewrap = self._panel(frame); tablewrap.pack(fill="both", expand=True)
        fields_tree = ttk.Treeview(tablewrap, columns=("label", "value"), show="headings", height=4)
        fields_tree.heading("label", text="页面字段名称"); fields_tree.heading("value", text="预填内容")
        fields_tree.column("label", width=240); fields_tree.column("value", width=400)
        fields_tree.pack(fill="both", expand=True, padx=10, pady=10)
        for item in task.get("fields", []): fields_tree.insert("", "end", values=(item.get("label", ""), item.get("value", "")))
        fieldrow = tk.Frame(frame, bg=self.colors["bg"]); fieldrow.pack(fill="x", pady=6)
        label_in = ttk.Entry(fieldrow); label_in.insert(0, "字段名称"); label_in.pack(side="left", fill="x", expand=True)
        value_in = ttk.Entry(fieldrow); value_in.insert(0, "预填内容"); value_in.pack(side="left", fill="x", expand=True, padx=7)
        fields_tree.bind("<<TreeviewSelect>>", lambda _event: self._load_selected_field(fields_tree, label_in, value_in))
        ttk.Button(fieldrow, text="添加字段", command=lambda: self._add_field(fields_tree, label_in, value_in)).pack(side="left")
        ttk.Button(fieldrow, text="更新选中", command=lambda: self._update_field(fields_tree, label_in, value_in)).pack(side="left", padx=(7, 0))
        ttk.Button(fieldrow, text="删除选中", command=lambda: self._delete_fields(fields_tree)).pack(side="left", padx=(7, 0))
        if self.data["profile"]:
            ttk.Button(fieldrow, text="导入个人资料", command=lambda: self._import_profile(fields_tree)).pack(side="left", padx=(7, 0))
        tk.Label(frame, text="隐私选项默认保留小程序的“不公开信息”设置。", bg=self.colors["bg"], fg=self.colors["muted"], anchor="w", justify="left", wraplength=700, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 4))
        ttk.Button(bottom, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(bottom, text="保存任务", style="Primary.TButton", command=lambda: self._save_task(win, task, values, fields_tree)).pack(side="right", padx=8)

    @staticmethod
    def _add_field(tree, label_entry, value_entry):
        label, value = label_entry.get().strip(), value_entry.get().strip()
        if not label or label == "字段名称":
            messagebox.showwarning(APP_NAME, "请填写页面字段名称。"); return
        if value in ("预填内容", "内容"):
            value = ""
        tree.insert("", "end", values=(label, value))
        label_entry.delete(0, "end"); value_entry.delete(0, "end")

    @staticmethod
    def _delete_fields(tree):
        selection = tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "请先选择要删除的报名字段。")
            return
        for item in selection:
            tree.delete(item)

    @staticmethod
    def _load_selected_field(tree, label_entry, value_entry):
        selection = tree.selection()
        if len(selection) != 1:
            return
        label, value = tree.item(selection[0], "values")
        label_entry.delete(0, "end"); label_entry.insert(0, label)
        value_entry.delete(0, "end"); value_entry.insert(0, value)

    @staticmethod
    def _update_field(tree, label_entry, value_entry):
        selection = tree.selection()
        if len(selection) != 1:
            messagebox.showinfo(APP_NAME, "请先在字段列表中选择一项要修改的字段。")
            return
        label, value = label_entry.get().strip(), value_entry.get().strip()
        if not label or label == "字段名称":
            messagebox.showwarning(APP_NAME, "请填写页面字段名称。")
            return
        tree.item(selection[0], values=(label, value))

    def _import_profile(self, tree):
        existing = {str(tree.item(i, "values")[0]).strip() for i in tree.get_children()}
        imported = 0
        for item in self.data["profile"]:
            label = str(item.get("label", "")).strip()
            if label and label not in existing:
                tree.insert("", "end", values=(label, item.get("value", "")))
                existing.add(label); imported += 1
        if not imported:
            messagebox.showinfo(APP_NAME, "个人资料字段已全部导入，或尚未设置可导入的资料。")

    def _save_task(self, win, task, entries, tree):
        activity = entries["activity"].get().strip()
        start_text = entries["start_at"].get().strip()
        if not activity:
            messagebox.showwarning(APP_NAME, "请填写活动名称。"); return
        try:
            start = datetime.strptime(start_text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            messagebox.showwarning(APP_NAME, "报名时间格式应为 YYYY-MM-DD HH:MM:SS，例如 2026-10-24 19:00:00。")
            return
        fields = [{"label": tree.item(i, "values")[0], "value": tree.item(i, "values")[1]} for i in tree.get_children()]
        if not fields:
            messagebox.showwarning(APP_NAME, "至少添加一个预填字段；填写页未知时，请在报名开放前配置。")
            return
        normalized_labels = [re.sub(r"[\s:：*＊·•，。、《》()（）\[\]【】]", "", str(field["label"])).replace("必填", "").replace("必需", "").replace("选填", "").casefold() for field in fields]
        if any(not label for label in normalized_labels) or len(set(normalized_labels)) != len(normalized_labels):
            messagebox.showwarning(APP_NAME, "页面字段名不能为空或重复，请检查报名字段列表。")
            return
        if start <= datetime.now():
            messagebox.showwarning(APP_NAME, "报名开始时间必须晚于当前时间。")
            return
        task.update({k: e.get().strip() for k, e in entries.items()})
        task["fields"] = fields; task["status"] = "待执行"
        if not task.get("created"): task["created"] = datetime.now().isoformat(timespec="seconds")
        if not any(t["id"] == task["id"] for t in self.data["tasks"]): self.data["tasks"].append(task)
        self._persist(); win.destroy(); self._show_tasks()

    def _show_profile(self):
        self._set_active_nav("profile")
        self._clear(); self._header("报名资料", "管理常用报名信息；内容使用当前 Windows 账户加密保存在本机。")
        section = tk.Frame(self.content, bg=self.colors["bg"]); section.pack(fill="both", expand=True)
        panel = self._panel(section); panel.pack(fill="both", expand=True)
        title_row = tk.Frame(panel, bg="#ffffff"); title_row.pack(fill="x", padx=17, pady=(14, 4))
        tk.Label(title_row, text="个人资料库", bg="#ffffff", fg=self.colors["ink"], font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        ttk.Button(title_row, text="保存资料", style="Primary.TButton", command=lambda: self._save_profile(tree)).pack(side="right")
        tk.Label(panel, text="可在创建报名任务时一键导入。", bg="#ffffff", fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=17, pady=(0, 8))
        row = tk.Frame(panel, bg="#ffffff"); row.pack(fill="x", padx=17, pady=(0, 10))
        name = ttk.Entry(row); name.insert(0, "字段名称"); name.pack(side="left", fill="x", expand=True)
        value = ttk.Entry(row); value.insert(0, "内容"); value.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="更新选中", command=lambda: self._update_field(tree, name, value)).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="＋ 添加资料", command=lambda: self._add_field(tree, name, value)).pack(side="left")
        ttk.Button(row, text="删除选中", command=lambda: self._delete_profile(tree)).pack(side="left", padx=(8, 0))
        tree = ttk.Treeview(panel, columns=("label", "value"), show="headings", height=12)
        tree.heading("label", text="资料字段"); tree.heading("value", text="内容")
        tree.column("label", width=250, anchor="w"); tree.column("value", width=500, anchor="w")
        tree.pack(fill="both", expand=True, padx=17, pady=(0, 16))
        for field in self.data["profile"]: tree.insert("", "end", values=(field.get("label", ""), field.get("value", "")))
        tree.bind("<<TreeviewSelect>>", lambda _event: self._load_selected_field(tree, name, value))

    def _delete_profile(self, tree):
        selection = tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "请先选择要删除的资料。")
            return
        for item in selection: tree.delete(item)

    def _save_profile(self, tree):
        self.data["profile"] = [{"label": tree.item(i, "values")[0], "value": tree.item(i, "values")[1]} for i in tree.get_children()]
        self._persist(); messagebox.showinfo(APP_NAME, "报名资料已加密保存。")

    def _show_logs(self):
        self._set_active_nav("logs")
        self._clear()
        heading = tk.Frame(self.content, bg=self.colors["bg"])
        heading.pack(fill="x")
        ttk.Label(heading, text="执行记录", style="Title.TLabel").pack(side="left", anchor="w")
        ttk.Button(heading, text="清理临时文件", command=self._cleanup_temp_files).pack(side="right", anchor="e")
        ttk.Label(self.content, text="查看任务触发、页面识别和报名结果；不确定的提交不会自动重试。", foreground=self.colors["muted"]).pack(anchor="w", pady=(5, 19))
        panel = self._panel(); panel.pack(fill="both", expand=True)
        tk.Label(panel, text="最近运行记录", bg="#ffffff", fg=self.colors["ink"], font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=17, pady=(15, 6))
        box = tk.Text(panel, bg="#ffffff", fg=self.colors["ink"], relief="flat", font=("Microsoft YaHei UI", 10), wrap="word", padx=17, pady=9, spacing1=4, spacing3=9, borderwidth=0, highlightthickness=0)
        box.pack(fill="both", expand=True, padx=0, pady=(0, 14))
        for log in reversed(self.data["logs"]): box.insert("end", f"{log.get('time','')}  {log.get('activity','')}\n{log.get('message','')}\n\n")
        box.configure(state="disabled")

    def _cleanup_temp_files(self):
        """Remove only temporary files created in this app's private data directory."""
        candidates = []
        if DATA_DIR.is_dir():
            candidates.extend(
                path for path in DATA_DIR.glob("*.tmp")
                if path.is_file() and not path.is_symlink()
            )
        if TEMP_DIR.is_dir() and not TEMP_DIR.is_symlink():
            for path in TEMP_DIR.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    candidates.append(path)

        unique = list(dict.fromkeys(candidates))
        total_bytes = 0
        for path in unique:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
        if not unique:
            messagebox.showinfo(APP_NAME, "没有发现可清理的软件临时文件。报名资料、任务和运行记录均未更改。")
            return

        size_text = self._format_bytes(total_bytes)
        if not messagebox.askyesno(
            APP_NAME,
            f"发现 {len(unique)} 个软件临时文件，共 {size_text}。\n\n"
            "将仅清理软件数据目录中的 .tmp 文件和 temp 子目录内容；报名资料、任务及运行记录会保留。继续吗？",
        ):
            return

        removed = 0
        removed_bytes = 0
        failures = []
        for path in unique:
            try:
                size = path.stat().st_size
                path.unlink()
                removed += 1
                removed_bytes += size
            except OSError as exc:
                failures.append(f"{path.name}: {exc}")

        if TEMP_DIR.is_dir() and not TEMP_DIR.is_symlink():
            for folder in sorted((p for p in TEMP_DIR.rglob("*") if p.is_dir() and not p.is_symlink()),
                                 key=lambda p: len(p.parts), reverse=True):
                try:
                    folder.rmdir()
                except OSError:
                    pass
            try:
                TEMP_DIR.rmdir()
            except OSError:
                pass

        summary = f"已清理 {removed} 个文件，释放 {self._format_bytes(removed_bytes)}。"
        if failures:
            summary += f"\n有 {len(failures)} 个文件无法删除，可能正被占用。"
        summary += "\n报名资料、任务和运行记录未更改。"
        (messagebox.showwarning if failures else messagebox.showinfo)(APP_NAME, summary)

    @staticmethod
    def _format_bytes(size):
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def _scheduler_tick(self):
        now = datetime.now()
        for task in self.data["tasks"]:
            if task.get("status") != "待执行": continue
            try: start = datetime.strptime(task["start_at"], "%Y-%m-%d %H:%M:%S")
            except Exception: continue
            if now >= start:
                if (now - start).total_seconds() > 1:
                    task["status"] = "错过时段"
                    self._log(task, "软件未能在预约时刻后一秒内触发，已跳过，不会迟到补交。")
                    self._persist()
                    self._show_tasks()
                    continue
                task["status"] = "执行中"
                self._log(task, "预约时间到，开始查找报名页控件。")
                self._persist(); self._show_tasks()
                binding = dict(self.data["binding"]) if self.data.get("binding") else None
                retry_seconds = int(self.data.get("early_retry_seconds", 30))
                threading.Thread(target=self._run_task, args=(task["id"], binding, retry_seconds), daemon=True).start()
        self.after(200, self._scheduler_tick)

    def _run_task(self, task_id, binding=None, retry_seconds=30):
        task = next((t for t in self.data["tasks"] if t["id"] == task_id), None)
        if not task: return
        try:
            result = execute_signup(task, self.data["profile"], binding=binding, early_retry_seconds=retry_seconds)
            status = "完成" if result.startswith("报名成功") else "需核实"
        except Exception as exc:
            result = f"已停止：{exc}"
            status = "失败"
        self.after(0, lambda: self._finish_task(task_id, status, result))

    def _finish_task(self, task_id, status, message):
        task = next((t for t in self.data["tasks"] if t["id"] == task_id), None)
        if not task: return
        task["status"] = status; self._log(task, message); self._persist(); self._show_tasks()
        if status in ("失败", "需核实"):
            messagebox.showwarning(APP_NAME, message)
        else:
            messagebox.showinfo(APP_NAME, message)

    def _log(self, task, message):
        self.data["logs"].append({"time": datetime.now().isoformat(timespec="milliseconds"), "activity": task.get("activity", ""), "message": message})
        self.data["logs"] = self.data["logs"][-500:]

    def _persist(self):
        try: save_data(self.data)
        except Exception as exc: messagebox.showerror(APP_NAME, f"保存失败：{exc}")


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("报名接龙工具目前仅支持 Windows。")
    SignupApp().mainloop()
