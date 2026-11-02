"""开机自启动：读写当前用户的启动文件夹（Startup）。

为什么用启动文件夹而不是注册表 Run：
- 启动文件夹里的快捷方式在资源管理器里可见、可右键查看/删除，用户心里有数；
- 不需要管理员权限，也不容易被杀软当成可疑的 Run 项拦掉；
- 卸载时删一个 .lnk 就干净了。

目标永远是**当前正在运行的 exe**；源码运行时则指向 `pythonw.exe main.py`，
这样开发时勾选自启动也不会指向一个不存在的 exe。

建 .lnk 走 Windows Script Host 的 `WScript.Shell`（系统自带，零第三方依赖），
比自己手搓 COM vtable 稳得多。
"""
from __future__ import annotations

import os
import subprocess
import sys

SHORTCUT_NAME = "机器操控工具箱.lnk"


def _startup_dir() -> str:
    """当前用户的启动文件夹路径。"""
    appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def shortcut_path() -> str:
    return os.path.join(_startup_dir(), SHORTCUT_NAME)


def _target() -> tuple:
    """返回 (目标 exe 路径, 工作目录, 参数)。

    冻结（打包版）→ 直接用 exe 本身；
    源码运行     → 用 pythonw.exe（无控制台窗口）跑 main.py。
    """
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        return exe, os.path.dirname(exe), ""
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pyw):
        pyw = sys.executable        # 没有 pythonw 就退回 python（会带控制台）
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return pyw, os.path.dirname(script), f'"{script}"'


def is_enabled() -> bool:
    """启动文件夹里是否已有我们的快捷方式。"""
    return os.path.isfile(shortcut_path())


def _ps_quote(s: str) -> str:
    """PowerShell 单引号字符串转义（内部单引号写成两个）。"""
    return "'" + s.replace("'", "''") + "'"


def enable() -> str:
    """创建（或刷新）启动快捷方式。失败抛异常，由调用方提示。"""
    target, workdir, args = _target()
    if not os.path.isfile(target):
        raise FileNotFoundError(f"启动目标不存在：{target}")
    os.makedirs(_startup_dir(), exist_ok=True)
    link = shortcut_path()

    # 用 PowerShell 的 WScript.Shell 建 .lnk：系统自带、无第三方依赖，
    # 比手写 COM vtable 稳，也避免 ctypes 索引写错导致静默失败。
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut({_ps_quote(link)}); "
        f"$sc.TargetPath = {_ps_quote(target)}; "
        f"$sc.WorkingDirectory = {_ps_quote(workdir)}; "
        f"$sc.Arguments = {_ps_quote(args)}; "
        f"$sc.Description = '机器操控工具箱（开机自启动）'; "
        "$sc.Save(); "
        f"if (Test-Path {_ps_quote(link)}) {{ 'OK' }} else {{ 'FAIL' }}"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30, creationflags=flags)
    except Exception as exc:
        raise OSError(f"调用 PowerShell 失败：{exc}") from exc
    out = (r.stdout or "").strip()
    if "OK" not in out or not os.path.isfile(link):
        raise OSError(f"快捷方式写入失败：{out or r.stderr.strip()[:200]}")
    return f"已开启开机自启动 → {link}"


def disable() -> str:
    """删除启动快捷方式。"""
    link = shortcut_path()
    if os.path.isfile(link):
        try:
            os.remove(link)
        except OSError as exc:
            raise OSError(f"删除快捷方式失败：{exc}") from exc
        return "已关闭开机自启动"
    return "开机自启动本来就是关闭的"