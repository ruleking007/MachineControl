"""键鼠录制回放工具。

改造要点（对应原诊断报告，报告已完成使命并删除）：
- B1  监听回调只往 deque/事件里写，不抢锁、不碰 UI
- B2  回放结束/中断/异常时释放所有已按下的键和鼠标键（finally）
- B3  回放线程异常也能复位 replaying，不会“永久回放中”
- B4  移动瞬时执行，1x 回放时长≈录制时长
- B5  Event.wait 代替 10ms 轮询 sleep
- B6  状态栏更新统一走主线程调度
- B7  键盘回调兼容 pynput>=1.8 的 injected 参数；J 键判定兼容大小写
- B9  停止时剔除末尾 N 秒可配置
- B10 同名文件自动加序号，不会覆盖
- B11 关闭窗口时停止监听器，避免 TclError
- B14 新增 JSON 存储；旧 .pkl 仍可读，读入时做类型校验
- B7 补充：鼠标回调同样兼容 pynput>=1.8 多传的 injected 参数（此前只改了键盘，
  漏改会让鼠标监听线程静默退出、鼠标动作全部录不到）
- B3 补充：回放被叫停后立即结束本轮遍历，不再空转剩余几千条事件
"""
from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk, messagebox

import common

# 操作层已重写为纯 ctypes（winctl.SendInput），不再依赖 pyautogui；
# winctl 使用物理像素坐标，必须在 DPI 感知设置之后导入。
common.enable_dpi_awareness()
import winctl

try:
    from pynput import mouse, keyboard
except ImportError:  # 缺库时给出可读提示而不是裸 Traceback
    mouse = keyboard = None

MOUSE_SAMPLE_INTERVAL = 0.03   # 鼠标移动最小采样间隔（秒）
RECORD_WAIT_SEC = 3            # 点击开始后的倒计时
CUT_LAST_SEC_DEFAULT = 0.0     # 停止时剔除末尾秒数（默认 0 = 不剔除，保留全部动作）
POLL_MS = 50                   # 主线程轮询周期


class Recorder:
    """录制状态机，所有状态读写都在 self.lock 内。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._events: deque = deque(maxlen=200_000)
        self._t0 = 0.0
        self._last = 0.0
        self._last_move = 0.0
        self.recording = False
        self.cancel_countdown = threading.Event()

    # ---------- 录制 ----------
    def begin(self) -> None:
        with self.lock:
            self._events.clear()
            now = time.time()
            self._t0 = self._last = self._last_move = now
            self.recording = True

    def end(self) -> list:
        with self.lock:
            self.recording = False
            return list(self._events)

    def snapshot_len(self) -> int:
        with self.lock:
            return len(self._events)

    def _push(self, kind: str, *params) -> None:
        now = time.time()
        # 冷路径：未录制时立即返回，不产生任何开销
        self._events.append((kind, now - self._last, *params))
        self._last = now

    def on_move(self, x: int, y: int) -> None:
        if not self.recording:
            return
        now = time.time()
        if now - self._last_move < MOUSE_SAMPLE_INTERVAL:
            return
        with self.lock:
            self._last_move = now
            self._push("move", int(x), int(y))

    def on_click(self, x: int, y: int, button, pressed: bool) -> None:
        if not self.recording:
            return
        with self.lock:
            if self.recording:
                self._push("click" if pressed else "release",
                           int(x), int(y), str(button))

    def on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        if not self.recording:
            return
        with self.lock:
            if self.recording:
                self._push("wheel", int(x), int(y), int(dx), int(dy))

    def on_key(self, key, pressed: bool) -> None:
        if not self.recording:
            return
        name = common.key_to_name(key)
        # 全局热键本身不能进录制内容，否则回放时会自己触发开始/停止/隐藏
        if name in common.HOTKEY_NAMES:
            return
        with self.lock:
            if self.recording:
                self._push("key_down" if pressed else "key_up", name)


class Player:
    """回放线程。停止通过 Event 通知，保证毫秒级响应且不残留按键状态。"""

    def __init__(self, on_state) -> None:
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.running = False
        self._lock = threading.Lock()
        self.on_state = on_state  # 接收状态文本的函数，内部会切回主线程

    def start(self, events: list, loop: bool, speed: float) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run, args=(events, loop, speed), daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stop_event.set()

    # ---------- 内部 ----------
    def _run(self, events: list, loop: bool, speed: float) -> None:
        pressed_buttons: list = []
        pressed_keys: list = []
        try:
            while not self.stop_event.is_set():
                for act in events:
                    if self.stop_event.is_set():
                        break
                    if not self._play_one(act, speed, pressed_buttons, pressed_keys):
                        break   # 等待期间被叫停，立即结束本轮，不再遍历剩余事件
                if not loop:
                    break
        except Exception as exc:  # 任意底层异常都要复位状态
            self.on_state(f"❌ 回放异常中止：{exc}")
        finally:
            self._release_all(pressed_buttons, pressed_keys)
            with self._lock:
                self.running = False
            self.on_state("⏹ 回放已停止")

    def _play_one(self, act, speed: float, buttons: list, keys: list) -> bool:
        """执行一个事件；返回 False 表示中止。"""
        kind, delay, *params = act
        if not self._wait(max(0.0, delay / speed)):
            return False

        if kind == "move":
            x, y = params
            winctl.move_to(x, y)
        elif kind in ("click", "release"):
            x, y, btn_str = params
            btn = btn_str.replace("Button.", "")
            if btn not in ("left", "right", "middle", "x1", "x2"):
                return True
            if kind == "click":
                winctl.mouse_down(x, y, button=btn)
                buttons.append(btn)
            else:
                winctl.mouse_up(x, y, button=btn)
                if btn in buttons:
                    buttons.remove(btn)
        elif kind == "wheel":
            x, y, dx, dy = params
            winctl.move_to(x, y)
            if int(dy):
                winctl.scroll(int(dy))
            if int(dx):        # 新版 pynput 会录到横向滚动，旧文件恒为 0
                winctl.scroll(int(dx), horizontal=True)
        elif kind in ("key_down", "key_up"):
            name = params[0]
            if kind == "key_down":
                if winctl.key_down(name):
                    keys.append(name)
            else:
                if winctl.key_up(name) and name in keys:
                    keys.remove(name)
        return True

    def _wait(self, seconds: float) -> bool:
        """可中断等待，返回 False 表示被要求停止。"""
        if seconds <= 0:
            return not self.stop_event.is_set()
        return not self.stop_event.wait(min(seconds, 86_400))

    def _release_all(self, buttons: list, keys: list) -> None:
        winctl.release_all_buttons(buttons)
        winctl.release_all_keys(keys)
        buttons.clear()
        keys.clear()


class RecorderApp(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.pack(fill=tk.BOTH, expand=True)
        self.dispatcher = common.UiDispatcher(master)
        self.status_var = tk.StringVar(value="准备就绪")
        self.speed_var = tk.DoubleVar(value=1.0)
        self.cut_var = tk.DoubleVar(value=CUT_LAST_SEC_DEFAULT)
        self.loop_var = tk.BooleanVar(value=False)   # F11 开始执行时使用的模式
        self.recorder = Recorder()
        self.player = Player(on_state=self._thread_safe_status)
        self._countdown_thread: threading.Thread | None = None
        self._closing = False              # shutdown 后置 True，止住 after 轮询
        self._poll_job: str | None = None  # 待执行轮询任务，shutdown 时撤销
        self._build_ui()
        self.refresh_files()
        self._start_listeners()
        self._poll_job = self.after(POLL_MS, self._poll)

    # ================= UI =================
    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)

        self.file_var = tk.StringVar()
        ttk.Label(top, text="录制文件：").grid(row=0, column=0, sticky="w")
        self.file_combo = ttk.Combobox(top, textvariable=self.file_var,
                                       state="readonly", width=34)
        self.file_combo.grid(row=0, column=1, padx=6, sticky="we")
        ttk.Button(top, text="🔄 刷新", command=self.refresh_files).grid(row=0, column=2, padx=2)
        ttk.Button(top, text="🗑 删除", command=self.delete_selected).grid(row=1, column=2, padx=2, pady=(2, 0))
        ttk.Button(top, text="📂 打开目录", command=self.open_records_dir).grid(row=1, column=3, padx=2, pady=(2, 0))

        opt = ttk.Frame(self, padding=(10, 0))
        opt.pack(fill=tk.X)
        ttk.Label(opt, text="回放速度").pack(side=tk.LEFT)
        ttk.Scale(opt, from_=0.1, to=5.0, variable=self.speed_var,
                  orient=tk.HORIZONTAL, length=260).pack(side=tk.LEFT, padx=6)
        self.speed_label = ttk.Label(opt, text="1.0x", width=6)
        self.speed_label.pack(side=tk.LEFT)
        ttk.Label(opt, text="  结束剔除(秒)").pack(side=tk.LEFT)
        ttk.Spinbox(opt, from_=0.0, to=10.0, increment=0.5, width=5,
                    textvariable=self.cut_var).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(opt, text="F11 循环执行", variable=self.loop_var).pack(side=tk.LEFT, padx=8)

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill=tk.X)
        style = {"width": 11}   # 按钮最小宽度（字符），22→11 收窄一半
        self.btn_start = ttk.Button(btns, text="🎥 开始录制", command=self.start_record, **style)
        self.btn_stop_rec = ttk.Button(btns, text="🛑 停止保存", command=self.stop_record,
                                       **style, state=tk.DISABLED)
        self.btn_play = ttk.Button(btns, text="▶️ 单次回放", command=lambda: self.play(False), **style)
        self.btn_loop = ttk.Button(btns, text="🔁 循环回放", command=lambda: self.play(True), **style)
        self.btn_halt = ttk.Button(btns, text="⏸ 停止 (F7)", command=self.stop_playback,
                                   **style, state=tk.DISABLED)
        for i, b in enumerate((self.btn_start, self.btn_stop_rec, self.btn_play,
                               self.btn_loop, self.btn_halt)):
            # 不写 sticky="we"：按钮按自身 width 收拢，不再撑满整列
            b.grid(row=i // 3, column=i % 3, padx=4, pady=4)

        ttk.Label(self, textvariable=self.status_var, foreground="#0b5394",
                  wraplength=700, padding=(10, 6)).pack(fill=tk.X)

        tips = (
            "操作提示：\n"
            "1. 点击开始录制后倒计时 3 秒正式开始\n"
            "2. 停止录制时会按“结束剔除”秒数删掉末尾动作（0 表示不剔除）\n"
            "3. 录制支持鼠标移动/左右中键/滚轮、键盘所有按键（含组合键）\n"
            "4. 按 F11 立即开始回放（模式见“F11 循环执行”勾选框），F7 终止（全局热键，面板隐藏时同样有效）\n"
            f"5. 鼠标移动采样间隔 {int(MOUSE_SAMPLE_INTERVAL * 1000)}ms，文件更小"
        )
        ttk.Label(self, text=tips, justify=tk.LEFT, padding=(10, 4)).pack(fill=tk.X)

    # ================= 文件 =================
    def refresh_files(self) -> None:
        files = common.list_recordings()
        self.file_combo["values"] = files
        current = self.file_var.get()
        if current not in files:
            self.file_var.set(files[0] if files else "")

    def delete_selected(self) -> None:
        name = self.file_var.get()
        if not name:
            messagebox.showwarning("提示", "请先选择要删除的录制文件")
            return
        if not messagebox.askyesno("确认删除", f"确定删除 {name}？删除后不可恢复！"):
            return
        try:
            os.remove(os.path.join(common.RECORDS_DIR, name))
            self._set_status(f"🗑 已删除：{name}")
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
        self.refresh_files()

    def open_records_dir(self) -> None:
        common.ensure_dirs()
        try:
            os.startfile(common.RECORDS_DIR)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    # ================= 录制 =================
    def start_record(self) -> None:
        if self.recorder.recording or self.player.running:
            self._set_status("⚠️ 录制或回放进行中")
            return
        self.recorder.cancel_countdown.clear()
        self.btn_start.config(state=tk.DISABLED)
        self._countdown_thread = threading.Thread(target=self._countdown, daemon=True)
        self._countdown_thread.start()

    def _countdown(self) -> None:
        for i in range(RECORD_WAIT_SEC, 0, -1):
            if self.recorder.cancel_countdown.is_set():
                return
            self._thread_safe_status(f"⏳ {i} 秒后开始录制，请勿操作键鼠…")
            if self.recorder.cancel_countdown.wait(1.0):
                return
        self.recorder.begin()
        self._thread_safe_status("🎥 录制中…")
        self.dispatcher.post(self._enter_recording_ui)

    def _enter_recording_ui(self) -> None:
        self.btn_stop_rec.config(state=tk.NORMAL)

    def stop_record(self) -> None:
        self.recorder.cancel_countdown.set()
        if not self.recorder.recording:
            self.btn_start.config(state=tk.NORMAL)
            self.btn_stop_rec.config(state=tk.DISABLED)
            self._set_status("⚠️ 当前未在录制")
            return
        events = self.recorder.end()
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop_rec.config(state=tk.DISABLED)
        if not events:
            self._set_status("⚠️ 无录制内容，未保存")
            return
        cut = max(0.0, float(self.cut_var.get() or 0.0))
        kept = common.trim_tail(events, common.count_duration(events) - cut) if cut else events
        if not kept:
            self._set_status(f"⚠️ 录制时长不足 {cut:g} 秒，剔除末尾后无有效数据，未保存")
            return
        try:
            path = common.save_events(kept)
        except Exception as exc:
            self._set_status(f"❌ 保存失败：{exc}")
            return
        self.refresh_files()
        self.file_var.set(os.path.basename(path))
        self._set_status(f"✅ 已保存 {os.path.basename(path)}，有效操作 {len(kept)} 条"
                         f"（时长 {common.count_duration(kept):.1f}s，剔除末尾 {cut:g}s）")

    # ================= 回放 =================
    def play_selected(self) -> None:
        """F11 开始执行：按界面上勾选的“F11 循环执行”决定单次/循环，
        立即执行无需打开面板（会先刷新列表，保证选中项有效）。"""
        self.refresh_files()
        self.play(bool(self.loop_var.get()))

    def play(self, loop: bool) -> None:
        name = self.file_var.get()
        if not name:
            self._set_status("⚠️ 请先选择录制文件")
            return
        path = os.path.join(common.RECORDS_DIR, name)
        if not os.path.isfile(path):
            self._set_status("❌ 文件不存在，已刷新列表")
            self.refresh_files()
            return
        if self.recorder.recording:
            self._set_status("⚠️ 录制中无法回放")
            return
        try:
            events = common.load_events(path)
        except Exception as exc:
            self._set_status(f"❌ 读取失败：{exc}")
            return
        if not events:
            self._set_status("⚠️ 文件里没有有效事件")
            return
        speed = max(0.1, float(self.speed_var.get()))
        if not self.player.start(events, loop, speed):
            self._set_status("⚠️ 已在回放中")
            return
        self.btn_play.config(state=tk.DISABLED)
        self.btn_loop.config(state=tk.DISABLED)
        self.btn_halt.config(state=tk.NORMAL)
        self._set_status(f"▶️ {'循环' if loop else '单次'}回放 {name}"
                         f"（{len(events)} 条 / {speed:.1f}x，按 F7 终止）")

    def stop_playback(self) -> None:
        if self.player.running:
            self.player.stop()
            self._set_status("⏹ 正在停止回放…")

    def _on_playback_finished(self) -> None:
        self.btn_play.config(state=tk.NORMAL)
        self.btn_loop.config(state=tk.NORMAL)
        self.btn_halt.config(state=tk.DISABLED)

    # ================= 监听与轮询 =================
    def _start_listeners(self) -> None:
        self.mouse_listener = None
        self.keyboard_listener = None
        if mouse is None or keyboard is None:
            self._set_status("❌ 缺少 pynput，无法录制：pip install pynput")
            return
        rec = self.recorder
        # pynput>=1.8 会给鼠标回调多传一个 injected 参数（on_move(x,y,injected) 等），
        # 写死参数个数会 TypeError 并让监听线程静默退出 → 鼠标动作全部录不到。
        # 用 *args 取值，兼容 1.7（不传）与 1.8+（传）。
        self.mouse_listener = mouse.Listener(
            on_move=lambda *a: rec.on_move(a[0], a[1]),
            on_click=lambda *a: rec.on_click(a[0], a[1], a[2], a[3]),
            on_scroll=lambda *a: rec.on_scroll(a[0], a[1], a[2], a[3]))
        self.keyboard_listener = keyboard.Listener(
            on_press=lambda *a: self._handle_key(a[0], True),
            on_release=lambda *a: self._handle_key(a[0], False))
        self.mouse_listener.start()
        self.keyboard_listener.start()

    def _handle_key(self, key, pressed: bool) -> None:
        """键盘回调：兼容 pynput>=1.8 多传的 injected 参数；只做最轻量的事。

        终止回放已由全局热键 F7 负责（main.py），此处不再做任何停止判定。
        """
        self.recorder.on_key(key, pressed)

    def _poll(self) -> None:
        # 先把“已触发”的这次登记清掉，再排下一次：这样 _poll_job 永远指向
        # 当前唯一待执行的那一个，shutdown 时撤销它就不会有残留。
        self._poll_job = None
        if self._closing:
            return
        try:
            self.speed_label.config(text=f"{self.speed_var.get():.1f}x")
            if self.player.thread is not None and not self.player.running:
                self.player.thread = None
                self._on_playback_finished()
            self._poll_job = self.after(POLL_MS, self._poll)
        except tk.TclError:
            self._closing = True       # 窗口已销毁，静默收尾

    # ================= 线程安全的状态更新 =================
    def _thread_safe_status(self, text: str) -> None:
        try:
            self.dispatcher.post(self._set_status, text)
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    # ================= 关闭 =================
    def shutdown(self) -> None:
        self._closing = True
        # 撤销待执行的那一次轮询（_poll_job 恒指向唯一待执行任务）
        common.cancel_job(self, self._poll_job)
        self._poll_job = None
        self.recorder.cancel_countdown.set()
        self.player.stop()
        for listener in (getattr(self, "mouse_listener", None),
                         getattr(self, "keyboard_listener", None)):
            try:
                if listener is not None:
                    listener.stop()
            except Exception:
                pass
        self.dispatcher.stop()


def main() -> None:
    root = tk.Tk()
    root.title("键鼠录制回放工具 ｜ F11 开始执行 · F7 停止回放")
    root.geometry("720x560")
    app = RecorderApp(root)

    # 单独运行本脚本时，F11/F7 在此注册（合并入口 main.py 里已另行注册，
    # 故不放进 RecorderApp.__init__，避免重复注册失败）
    hotkeys = winctl.HotKeyManager()
    hotkeys.register([
        (winctl.VK_F11, lambda _vk: app.dispatcher.post(app.play_selected)),
        (winctl.VK_F7, lambda _vk: app.dispatcher.post(app.stop_playback)),
    ])
    hotkeys.start()

    root.protocol("WM_DELETE_WINDOW",
                  lambda: (hotkeys.stop(), app.shutdown(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
