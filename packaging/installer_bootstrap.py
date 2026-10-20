"""Self-contained per-user installer for the frozen Signup Desk application."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tkinter as tk
import winreg
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


APP_NAME = "报名接龙工具"
APP_VERSION = "1.0.0"
APP_EXE = "SignupDesk.exe"
INSTALL_DIR = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "SignupDesk"
START_MENU_DIR = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME
UNINSTALL_SCRIPT = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "报名接龙工具-卸载.ps1"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SignupDesk"


def _make_shortcut(shortcut: Path, target: Path, working_dir: Path) -> None:
    script = (
        "$shell = New-Object -ComObject WScript.Shell; "
        "$link = $shell.CreateShortcut($env:SIGNUP_LINK_PATH); "
        "$link.TargetPath = $env:SIGNUP_LINK_TARGET; "
        "$link.WorkingDirectory = $env:SIGNUP_LINK_WORKDIR; "
        "$link.Save()"
    )
    env = os.environ.copy()
    env["SIGNUP_LINK_PATH"] = str(shortcut)
    env["SIGNUP_LINK_TARGET"] = str(target)
    env["SIGNUP_LINK_WORKDIR"] = str(working_dir)
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _write_uninstaller(install_dir: Path) -> None:
    UNINSTALL_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    escaped_install_dir = str(install_dir).replace("'", "''")
    UNINSTALL_SCRIPT.write_text(
        r"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$installDir = '__INSTALL_DIR__'
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\报名接龙工具'
$desktopDir = [Environment]::GetFolderPath('Desktop')
$desktopLink = Join-Path $desktopDir '报名接龙工具.lnk'
if (Get-Process -Name 'SignupDesk' -ErrorAction SilentlyContinue) {
    [System.Windows.Forms.MessageBox]::Show('请先关闭报名接龙工具，再运行卸载。', '报名接龙工具') | Out-Null
    exit 1
}
Remove-Item -LiteralPath $desktopLink -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $startMenu -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $installDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SignupDesk' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
[System.Windows.Forms.MessageBox]::Show('程序已卸载。本机加密保存的资料与任务数据仍保留。', '报名接龙工具') | Out-Null
""".replace("__INSTALL_DIR__", escaped_install_dir),
        encoding="utf-8-sig",
    )


def install(root: tk.Tk, status: tk.StringVar, create_desktop: bool, install_location: str) -> None:
    source = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "payload"
    install_dir = Path(install_location).expanduser()
    try:
        if not install_dir.is_absolute() or install_dir == Path(install_dir.anchor):
            raise ValueError("请选择有效的安装文件夹。")
        if install_dir.exists() and any(install_dir.iterdir()) and not (install_dir / APP_EXE).exists():
            raise FileExistsError("所选文件夹非空。请选择空文件夹，或选择已安装本软件的目录进行更新。")
        app_path = install_dir / APP_EXE
        if not source.is_dir() or not (source / APP_EXE).is_file():
            raise FileNotFoundError("安装包中未找到应用文件，请重新下载完整安装包。")
        status.set("正在复制程序和 OCR 模型，请稍候…")
        root.update_idletasks()
        install_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, install_dir, dirs_exist_ok=True)

        status.set("正在创建快捷方式…")
        root.update_idletasks()
        START_MENU_DIR.mkdir(parents=True, exist_ok=True)
        _make_shortcut(START_MENU_DIR / f"{APP_NAME}.lnk", app_path, install_dir)
        if create_desktop:
            desktop = Path(subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "[Environment]::GetFolderPath('Desktop')"],
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).strip())
            _make_shortcut(desktop / f"{APP_NAME}.lnk", app_path, install_dir)

        _write_uninstaller(install_dir)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "Signup Desk")
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                              f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{UNINSTALL_SCRIPT}"')

        status.set("安装完成。")
        root.update_idletasks()
        if messagebox.askyesno(APP_NAME, "安装完成，现在启动软件吗？"):
            os.startfile(str(app_path))
        root.destroy()
    except Exception as exc:
        status.set("安装失败。")
        messagebox.showerror(APP_NAME, f"安装未完成：{exc}")


def main() -> None:
    root = tk.Tk()
    root.title(f"安装{APP_NAME}")
    root.geometry("620x300")
    root.resizable(False, False)
    root.configure(bg="#f3f6fb")
    root.eval("tk::PlaceWindow . center")

    body = ttk.Frame(root, padding=24)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=APP_NAME, font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
    ttk.Label(body, text=f"版本 {APP_VERSION} · 安装到当前 Windows 用户账户",
              font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(5, 18))
    status = tk.StringVar(value="程序和 OCR 模型将安装到本机，不需要单独安装 Python。")
    ttk.Label(body, textvariable=status, wraplength=405, justify="left").pack(anchor="w", pady=(0, 12))
    location = tk.StringVar(value=str(INSTALL_DIR))
    ttk.Label(body, text="安装位置").pack(anchor="w")
    location_row = ttk.Frame(body)
    location_row.pack(fill="x", pady=(4, 12))
    ttk.Entry(location_row, textvariable=location).pack(side="left", fill="x", expand=True)

    def choose_folder() -> None:
        selected = filedialog.askdirectory(
            parent=root,
            title="选择安装文件夹",
            initialdir=location.get() if Path(location.get()).exists() else str(Path(location.get()).parent),
            mustexist=False,
        )
        if selected:
            location.set(selected)

    ttk.Button(location_row, text="浏览…", command=choose_folder).pack(side="left", padx=(8, 0))
    desktop = tk.BooleanVar(value=True)
    ttk.Checkbutton(body, text="创建桌面快捷方式", variable=desktop).pack(anchor="w")
    buttons = ttk.Frame(body)
    buttons.pack(side="bottom", fill="x", pady=(16, 0))
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side="right")
    ttk.Button(buttons, text="安装", command=lambda: install(root, status, desktop.get(), location.get())).pack(side="right", padx=(0, 10))
    root.mainloop()


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("此安装包仅支持 Windows。")
    main()
