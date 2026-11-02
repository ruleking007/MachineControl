"""统一入口：四个工具收进一个 Notebook（录制 / 脚本控制台 / 文字侦查 / 文件管理）。

- 常驻系统托盘：启动后面板不占桌面，双击托盘图标（或按 F10）唤出；
  点面板 × 只是收回托盘，托盘右键菜单"退出"才真正结束进程
- 全程只做一次 DPI 感知、只跑一个主循环、只有一份日志
- 全局热键 F10 显隐 / F9 开始录制 / F8 停止录制 / F7 终止回放
  （RegisterHotKey 在独立线程回调，经 UiDispatcher 转主线程，窗口未聚焦也生效）
- 缺 pystray 时自动退回"面板直接显示、无托盘"的旧行为

文字侦查页拿到 recorder_app 作为 engine_host：规则动作为「执行脚本」时，
命中后借用录制页的 Player 引擎回放脚本（与脚本控制台的定时执行同源，F7 可终止）。

需要单独运行某个工具时仍可直接 `python recorder.py` 或 `python ocr_monitor.py`
（后者无 engine_host，「执行脚本」动作会提示不可用）。
"""
from __future__ import annotations

import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import common

common.enable_dpi_awareness()
import winctl  # noqa: E402  操作层 + 全局热键

_logger = common.setup_logger("机器操控")

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None


def _make_tray_image():
    """画一个 32x32 托盘图标：深蓝圆底 + 白色鼠标轮廓。"""
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((1, 1, 30, 30), fill=(11, 83, 148, 255))          # 品牌蓝底
    d.rounded_rectangle((11, 7, 21, 25), radius=5,
                        outline=(255, 255, 255, 255), width=2)  # 鼠标外形
    d.line((16, 7, 16, 13), fill=(255, 255, 255, 255), width=2)  # 滚轮线
    return img


def _fit_geometry(root: tk.Tk, want_w: int = 1180, want_h: int = 800) -> tuple:
    """按屏幕可用空间与 DPI 缩放算出合适的窗口尺寸。

    坑点：tkinter 的 geometry() 吃的是「逻辑像素」，而 winctl.virtual_screen()
    给的是「物理像素」。在 225% 缩放的机器上，写死 960x680 看似没问题，
    实际窗口会被系统压到 439x337 左右，Notebook 的后两个页签直接被挤掉——
    表现就是「打开还是老样子，看不到新功能」。

    返回 (geometry 字符串, 期望的逻辑宽, 期望的逻辑高)。
    """
    try:
        scale = float(root.tk.call("tk", "scaling")) or 1.0
    except Exception:
        scale = 1.0
    # tk scaling 的 1.0 对应 72dpi，Windows 的 96dpi 基准即 1.3333。
    factor = max(0.5, min(4.0, scale / (96.0 / 72.0)))   # 1.0=100%, 1.5=150%, 2.25=225%
    vl, vt, vr, vb = winctl.virtual_screen()
    phys_w, phys_h = max(800, vr - vl), max(600, vb - vt)
    avail_w = int(phys_w / factor)
    avail_h = int(phys_h / factor)
    # 留边：宽度少 40，高度少 90（标题栏 + 任务栏）
    w = max(920, min(want_w, avail_w - 40))
    h = max(620, min(want_h, avail_h - 90))
    # 偏移也用逻辑像素：vl/vt 是物理值，必须同样除以 factor，否则多屏/缩放下跑偏
    off_x, off_y = int(vl / factor), int(vt / factor)
    return f"{w}x{h}+{off_x}+{off_y}", w, h


def _apply_geometry(root: tk.Tk) -> None:
    """设置窗口尺寸，并在窗口真正映射后按实测缩放比校正。

    冻结（PyInstaller）环境下，启动早期拿到的 tk scaling / 屏幕尺寸可能还是
    缩放前的旧值，单靠一次 geometry() 会被系统按缩放比缩水（1180x710 → 537x560）。
    这里在 map 之后重设几次，直到宽度确实达到期望值或次数用尽。
    """
    geometry, want_w, want_h = _fit_geometry(root)
    root.geometry(geometry)
    # minsize 用较小值：它只防止用户拖到不可用，不参与初始尺寸计算
    root.minsize(760, 520)

    state = {"tries": 0}

    def _real_size() -> tuple:
        """用 Win32 读窗口的真实物理尺寸与 DPI。

        Tk 的 winfo_width/height 在冻结环境下可能按 96dpi 报数，
        与 Windows 实际给的大小不一致；这里以 Win32 为准。
        """
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = int(root.frame(), 16) if str(root.frame()).startswith("0x") else int(root.frame())
            r = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            dpi = ctypes.windll.user32.GetDpiForWindow(hwnd) or 96
            return (r.right - r.left), (r.bottom - r.top), dpi
        except Exception:
            return 0, 0, 96

    def _correct() -> None:
        state["tries"] += 1
        try:
            pw, ph, dpi = _real_size()
            _logger.info("窗口校正#%d：物理 %dx%d（DPI %d），期望 %dx%d",
                         state["tries"], pw, ph, dpi, want_w, want_h)
            root.lift()
        except Exception as exc:
            _logger.warning("窗口校正失败：%s", exc)
        if state["tries"] < 3:
            root.after(220, _correct)
        else:
            pw, ph, dpi = _real_size()
            _logger.info("窗口校正结束：物理 %dx%d（DPI %d）", pw, ph, dpi)

    root.after(250, _correct)


def _single_instance_lock(pin: int = 0x4D43544B) -> bool:
    """单实例保护：同一用户下只允许运行一个工具箱。

    为什么必须有：开机自启动已经会在登录时拉起一个实例，用户再双击快捷方式
    就会起第二个。两个实例抢同一批全局热键（F7-F11），后起的那个注册失败，
    表现为「热键时灵时不灵」；两边还会同时轮询定时任务、同时抢回放引擎。

    成功拿到互斥体返回 True；已有实例在跑则返回 False。
    用 CreateMutexW 而非文件锁：进程崩溃时互斥体由系统自动释放，不会留下死锁。
    """
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE:
        return True             # 本进程已经拿过，直接复用，别重复建
    import ctypes
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(
            None, True, f"Local\\MachineCtlBox-{pin:08X}")
        if not handle:
            return True                     # 拿不到互斥体就别拦用户
        # ERROR_ALREADY_EXISTS = 183：说明别的实例已经建过这个互斥体
        err = ctypes.windll.kernel32.GetLastError()
        if err == 183:
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        # 全局保留句柄（不 close），进程退出时系统自动释放
        _MUTEX_HANDLE = handle
        return True
    except Exception:
        return True                         # 出错时放行，不要因为保护逻辑把人挡在外面


_MUTEX_HANDLE = None


def _reveal_existing_window() -> bool:
    """把「已经运行的那个实例」的面板窗口显示出来，成功返回 True。

    为什么需要（实测踩过的坑）：
      面板一旦隐藏，程序就完全依赖系统托盘图标作为入口。
      但托盘图标可能丢失——2026/11/01 就发生过：20:45 隐藏面板后，
      到 23:55 托盘窗口已消失（进程还活着，但既无面板也无托盘）。
      此时用户反复双击快捷方式，旧逻辑只是弹一句「已在运行」就退出，
      屏幕上什么都不会出现，用户只能认为「程序坏了」。

    所以第二个实例在退出前，先把已有实例的面板直接唤出来，做到自愈。
    """
    import ctypes
    from ctypes import wintypes as _wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    SW_RESTORE = 9
    SW_SHOWNORMAL = 1

    try:
        me = int(kernel32.GetCurrentProcessId())
    except Exception:
        return False

    found = []

    def _cb(hwnd, _lparam):
        pid = _wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value and pid.value != me:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value == "TkTopLevel":
                ln = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(ln + 1)
                user32.GetWindowTextW(hwnd, buf, ln + 1)
                if buf.value == "机器操控工具箱":
                    found.append(hwnd)
        return True

    user32.EnumWindows(
        ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)(_cb), 0)

    ok = False
    for hwnd in found:
        # 只做显示与前置，不用 MoveWindow 改尺寸：tkinter 被外部强制 resize
        # 后控件布局会错乱。
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.ShowWindow(hwnd, SW_SHOWNORMAL)
        user32.SetForegroundWindow(hwnd)
        ok = True
    return ok


def _tray_window_alive() -> bool:
    """判断本进程里的托盘隐藏消息窗口是否还在（即托盘图标是否还有效）。

    比 pystray 的 icon.visible 可靠：图标丢失时那个窗口会真的消失。
    """
    import ctypes
    from ctypes import wintypes as _wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    try:
        me = int(kernel32.GetCurrentProcessId())
    except Exception:
        return True

    # tkinter / 输入法自己会建的窗口，除此之外的顶层窗口就当是托盘窗口
    known = {"TkTopLevel", "TtkMonitorClass", "MSCTFIME UI", "IME"}
    state = {"alive": False}

    def _cb(hwnd, _lparam):
        pid = _wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value and cls.value not in known:
                state["alive"] = True
        return True

    try:
        user32.EnumWindows(
            ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)(_cb), 0)
    except Exception:
        return True          # 查不出来时按"还活着"处理，别乱弹面板
    return state["alive"]


def main() -> None:
    root = tk.Tk()
    root.title("机器操控工具箱")
    if not _single_instance_lock():
        # 已有实例在跑。退出前先把它的面板唤出来，否则用户双击后
        # 屏幕上什么都不会发生，只会以为程序坏了。
        revealed = _reveal_existing_window()
        _logger.info("已有实例在运行 —— %s，本次启动退出",
                     "已唤出它的面板" if revealed else "未能定位它的窗口")
        root.destroy()
        if not revealed:
            # 真的找不到窗口时才弹提示兜底
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "机器操控工具箱已在运行，但没能定位到它的面板窗口。\n\n"
                    "请到任务管理器结束「机器操控工具箱.exe」，再重新打开。",
                    "已在运行", 0x30)
            except Exception:
                pass
        sys.exit(0)
    # 定位到虚拟桌面左上角：默认居中会把右侧内容顶出窄屏，按钮被裁掉
    _apply_geometry(root)
    root.attributes("-topmost", True)  # 面板显示时始终最顶层

    # 必须在任何 Treeview 创建之前设好行高：vista 主题默认行高固定 20px，
    # 高 DPI 下字体行距可达 35px，字会被上下裁掉（「只能看到字的一半」）。
    rh = common.fix_treeview_rowheight(root)
    _logger.info("Treeview 行高设为 %dpx（按 %s 行距）", rh,
                 root.tk.call("tk", "scaling"))

    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True)

    pages = []
    recorder_app = None

    try:
        import recorder as recorder_mod
        recorder_app = recorder_mod.RecorderApp(notebook)
        notebook.add(recorder_app, text="🎥 键鼠录制回放")
        pages.append(recorder_app)
    except Exception as exc:  # 缺库/Missing DLL 时不影响其它页
        ttk.Label(notebook, text=f"录制模块加载失败：{exc}").pack(padx=20, pady=20)

    if recorder_app is not None:
        try:
            import console as console_mod
            console_frame = console_mod.ScriptConsoleApp(notebook, recorder_app)
            notebook.add(console_frame, text="⏱ 脚本控制台")
            pages.append(console_frame)
        except Exception as exc:
            ttk.Label(notebook, text=f"脚本控制台加载失败：{exc}").pack(padx=20, pady=20)

    try:
        import ocr_monitor as ocr_mod
        # 传入录制页实例：命中规则若动作为「执行脚本」，由它借用回放引擎
        ocr_frame = ocr_mod.MonitorApp(notebook, engine_host=recorder_app)
        notebook.add(ocr_frame, text="🔍 屏幕文字侦查")
        pages.append(ocr_frame)
    except Exception as exc:
        ttk.Label(notebook, text=f"文字侦查模块加载失败：{exc}").pack(padx=20, pady=20)

    try:
        import file_manager as fm_mod
        files_frame = fm_mod.FileManagerFrame(notebook)
        notebook.add(files_frame, text="📁 录制文件管理")
        pages.append(files_frame)
    except Exception as exc:
        ttk.Label(notebook, text=f"文件管理模块加载失败：{exc}").pack(padx=20, pady=20)

    # ---------- 显示 / 收起 ----------
    dispatcher = common.UiDispatcher(root)
    hidden = {"value": False}
    quitting = {"value": False}

    def show_panel() -> None:
        root.deiconify()
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        hidden["value"] = False
        _logger.info("面板已显示")

    def hide_panel() -> None:
        root.withdraw()
        hidden["value"] = True
        _logger.info("面板已隐藏（托盘/F10 可唤出）")

    def toggle_hide() -> None:
        show_panel() if hidden["value"] else hide_panel()

    def ensure_topmost() -> None:
        """每次热键命中都重新提升最顶层（对冲其它程序抢占 topmost）。"""
        try:
            root.attributes("-topmost", True)
            root.lift()
        except Exception:
            pass

    # ---------- 系统托盘 ----------
    icon = None
    if pystray is not None:
        def _tray_show(_icon=None, _item=None):
            dispatcher.post(show_panel)

        def _tray_quit(_icon=None, _item=None):
            dispatcher.post(quit_app)

        menu = pystray.Menu(
            pystray.MenuItem("显示面板", _tray_show, default=True),
            pystray.MenuItem("退出", _tray_quit),
        )
        icon = pystray.Icon("机器操控工具箱", _make_tray_image(),
                            "机器操控工具箱（F10 显示/隐藏面板）", menu)

        tray_ok = {"value": False}

        def _run_tray() -> None:
            try:
                icon.run(setup=lambda i: tray_ok.__setitem__("value", True))
            except Exception as exc:
                _logger.error("托盘图标创建失败：%s", exc)

        threading.Thread(target=_run_tray, daemon=True, name="tray-icon").start()

        # 等托盘真正就绪再收起面板（最多 3 秒）。
        # 以前是起线程后立刻 hide_panel()：万一托盘没建成，窗口已经藏了，
        # 程序就变成「在跑但完全没入口」——用户以为没启动。
        def _hide_when_tray_ready(tries: int = 0) -> None:
            if tray_ok["value"]:
                hide_panel()
                return
            if tries >= 30:
                # 3 秒还没建成 → 面板就留在屏幕上，至少用户看得见
                _logger.error("托盘图标 3 秒内未就绪，改为保持面板可见"
                              "（否则程序会既无面板也无托盘入口）")
                show_panel()
                return
            root.after(100, lambda: _hide_when_tray_ready(tries + 1))

        root.after(100, _hide_when_tray_ready)

        # ---------- 托盘看门狗 ----------
        # 托盘图标可能在运行期间丢失（explorer 重启、pystray 消息循环意外结束等）。
        # 实测 2026/11/01：20:45 隐藏面板后，到 23:55 托盘窗口已不存在，
        # 进程还活着，但既看不到面板、也没有托盘图标可点 —— 用户只能认为程序坏了。
        # 这里定期检查：只要面板是隐藏的、而托盘又没了，就自动把面板显示出来，
        # 保证任何时候都至少有一个能操作的入口。
        def _tray_watchdog() -> None:
            if quitting["value"]:
                return
            if hidden["value"] and not _tray_window_alive():
                _logger.warning("检测到托盘图标已丢失，自动把面板显示出来（避免无入口）")
                show_panel()
                return
            root.after(15000, _tray_watchdog)

        root.after(15000, _tray_watchdog)
    else:
        _logger.warning("未安装 pystray，托盘模式不可用（pip install pystray）")

    # ---------- 全局热键 ----------
    def _do_start() -> None:
        _logger.info("主线程执行：开始录制")
        if recorder_app is not None:
            recorder_app.start_record()

    def _do_stop() -> None:
        _logger.info("主线程执行：停止录制")
        if recorder_app is not None:
            recorder_app.stop_record()

    def _do_halt() -> None:
        _logger.info("热键触发：终止回放")
        if recorder_app is not None:
            recorder_app.stop_playback()

    def _do_play() -> None:
        _logger.info("主线程执行：F11 开始回放")
        if recorder_app is not None:
            recorder_app.play_selected()

    _VK_ACTION = {
        winctl.VK_F10: toggle_hide,
        winctl.VK_F11: _do_play,
        winctl.VK_F9: _do_start,
        winctl.VK_F8: _do_stop,
        winctl.VK_F7: _do_halt,
    }

    def on_hotkey(vk: int) -> None:
        """热键线程回调：只转发到主线程，绝不直接碰 tkinter。"""
        _logger.info("收到热键 vk=%02X loops=%d", vk, hotkeys.loops)
        if vk == winctl.VK_F10 and hidden["value"]:
            pass  # 面板收起时不需要置顶操作
        else:
            ensure_topmost()
        if vk in _VK_ACTION:
            dispatcher.post(_VK_ACTION[vk])

    hotkeys = winctl.HotKeyManager()
    hotkeys.register([(vk, on_hotkey) for vk in _VK_ACTION])
    if hotkeys.start():
        _logger.info("全局热键注册成功：F10 显隐 / F9 开始 / F8 停止 / F11 开始执行 / F7 终止回放")
    else:
        _logger.warning("部分热键注册失败（可能被其它程序占用）：%s",
                        ", ".join(f"F{k - 0x6F}" for k in hotkeys.failed))

    def _watchdog() -> None:
        """热键线程心跳：连续 3 个 5 秒周期计数不变 → 记一次停滞警告。"""
        last, stuck = hotkeys.loops, 0
        while True:
            threading.Event().wait(5.0)
            cur = hotkeys.loops
            stuck = stuck + 1 if cur == last else 0
            if stuck == 3:
                _logger.warning("热键线程疑似停滞 loops=%d last_error=%d",
                                cur, hotkeys.last_error)
            last = cur

    threading.Thread(target=_watchdog, daemon=True, name="hotkey-watchdog").start()

    # ---------- 开机自启动 ----------
    # 放在窗口底部一行：这是「整机级」设置，不属于任何单个页签。
    auto_row = ttk.Frame(root, padding=(10, 2))
    auto_row.pack(fill=tk.X, side=tk.BOTTOM)
    autostart_var = tk.BooleanVar(value=False)
    try:
        import autostart as autostart_mod
        autostart_var.set(autostart_mod.is_enabled())
    except Exception as exc:
        autostart_mod = None
        _logger.warning("开机自启动模块不可用：%s", exc)

    def _on_autostart_toggle() -> None:
        """勾选/取消开机自启动：真正写/删启动文件夹里的快捷方式。"""
        if autostart_mod is None:
            return
        want = bool(autostart_var.get())
        try:
            msg = autostart_mod.enable() if want else autostart_mod.disable()
            _logger.info(msg)
        except Exception as exc:
            _logger.error("设置开机自启动失败：%s", exc)
            # 失败要把勾选状态回滚，否则界面显示的与实际不符
            autostart_var.set(autostart_mod.is_enabled())
            messagebox.showerror("开机自启动设置失败", str(exc), parent=root)

    chk = ttk.Checkbutton(auto_row, text="开机自启动", variable=autostart_var,
                          command=_on_autostart_toggle)
    chk.pack(side=tk.LEFT)
    ttk.Label(auto_row, foreground="#666",
              text="（勾选后在「启动」文件夹创建快捷方式，登录 Windows 时自动运行）"
              ).pack(side=tk.LEFT, padx=(6, 0))

    ttk.Label(root, foreground="#0b5394", padding=(10, 4),
              text="全局热键：F10 隐藏/显示面板 ｜ F9 开始录制 ｜ F8 停止录制 ｜ F11 开始执行 ｜ F7 终止回放"
                   "（面板隐藏/收进托盘时依然生效）").pack(fill=tk.X, side=tk.BOTTOM)

    # ---------- 退出与关窗 ----------
    def quit_app() -> None:
        quitting["value"] = True
        for page in pages:
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    pass
        try:
            hotkeys.stop()
        except Exception:
            pass
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        dispatcher.stop()
        root.destroy()

    def on_close() -> None:
        """点 ×：有托盘时收进托盘（程序继续常驻）；无托盘时直接退出。"""
        if icon is not None:
            hide_panel()
        else:
            quit_app()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    sys.exit(0)


if __name__ == "__main__":
    main()
