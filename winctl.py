"""纯 ctypes 操作层：用 Win32 SendInput 实现键鼠回放动作，取代 pyautogui。

- 零第三方依赖，只调用 user32/kernel32，回放精度不受 pyscreeze 缓存等影响
- 坐标一律物理像素，要求进程已开启 DPI 感知（common.enable_dpi_awareness）
- 键名直接吃录制端格式："a" / "Key.space" / "VK.102"，不再需要
  pynput→pyautogui 的中间翻译层；无法解析的键名返回 False 由调用方跳过
- HotKeyManager：RegisterHotKey + 线程消息循环实现全局热键
  （pynput 钩子线程在 PyInstaller windowed 打包下实测只回调一次就静默停止）
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes

if os.name != "nt":
    raise RuntimeError("winctl 仅支持 Windows（依赖 user32 SendInput）")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ===================== INPUT 结构 =====================
# union 以最大的 MOUSEINPUT 为模板，天然容纳 KEYBDINPUT，无需 HARDWAREINPUT
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

XBUTTON1 = 0x0001  # 后退
XBUTTON2 = 0x0002  # 前进
WHEEL_DELTA = 120  # 一格滚轮的 delta 绝对值


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ssize_t),   # ULONG_PTR
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ssize_t),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [("type", wintypes.DWORD), ("_u", _INPUT_UNION)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int
user32.VkKeyScanW.argtypes = (wintypes.WCHAR,)
user32.VkKeyScanW.restype = ctypes.c_short


# ===================== 虚拟桌面 / 紧急停止 =====================
def virtual_screen() -> tuple:
    """(left, top, right, bottom)，物理像素，覆盖所有显示器。"""
    left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
    right = left + user32.GetSystemMetrics(78)
    bottom = top + user32.GetSystemMetrics(79)
    if right <= left or bottom <= top:    # 取不到时退化为单屏
        left = top = 0
        right = user32.GetSystemMetrics(0) or 1920
        bottom = user32.GetSystemMetrics(1) or 1080
    return left, top, right, bottom


def _cursor_pos() -> tuple | None:
    pt = POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return pt.x, pt.y


def _send(*inputs: INPUT) -> None:
    n = user32.SendInput(len(inputs), ctypes.byref(inputs[0]), ctypes.sizeof(INPUT))
    if n != len(inputs):
        raise OSError(f"SendInput 只接受了 {n}/{len(inputs)} 个事件"
                      "（常见原因：切到安全桌面或被更高完整性窗口拦截）")


# ===================== 鼠标 =====================
def _to_abs(x: int, y: int) -> tuple:
    """物理像素 → MOUSEEVENTF_ABSOLUTE|VIRTUALDESK 的 0..65535 归一化坐标。"""
    left, top, right, bottom = virtual_screen()
    nx = round((x - left) * 65535 / max(1, right - left - 1))
    ny = round((y - top) * 65535 / max(1, bottom - top - 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def _mouse(x, y, flags: int, data: int = 0) -> None:
    dx = dy = 0
    if x is not None and y is not None:
        dx, dy = _to_abs(int(x), int(y))
        flags |= MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF,
                        dwFlags=flags, time=0, dwExtraInfo=0)
    _send(inp)


_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    "x1": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    "x2": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}


def move_to(x: int, y: int) -> None:
    """瞬时移动到绝对坐标（录制端已按采样间隔抽稀，无需补间）。"""
    _mouse(x, y, MOUSEEVENTF_MOVE)


def mouse_down(x: int, y: int, button: str = "left") -> None:
    """按下前先绝对定位到 (x, y)，与旧 pyautogui.mouseDown(x, y) 行为一致。"""
    down, _up, data = _BUTTON_FLAGS[button]
    _mouse(x, y, down, data)


def mouse_up(x, y, button: str = "left") -> None:
    """x/y 传 None 表示在光标当前位置释放（用于回放中断后的清理）。"""
    _down, up, data = _BUTTON_FLAGS[button]
    _mouse(x, y, up, data)


def scroll(notches: int, horizontal: bool = False) -> None:
    """按格滚动：正数 = 向上/向左；SendInput 滚轮不带坐标，须先 move_to。"""
    flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _mouse(None, None, flags, data=notches * WHEEL_DELTA)


def release_all_buttons(buttons) -> None:
    """中断清理：在光标当前位置松开所有已按下但未释放的鼠标键。"""
    for btn in reversed(list(buttons)):
        if btn in _BUTTON_FLAGS:
            try:
                mouse_up(None, None, btn)
            except Exception:
                pass


# ===================== 键盘 =====================
# 键名（pynput 侧名称 / pyautogui 习惯名都收录）→ VK 码
_KEY_VK = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D,
    "shift": 0x10, "shift_l": 0x10, "shift_r": 0xA1,
    "ctrl": 0x11, "control": 0x11, "ctrl_l": 0xA0, "ctrl_r": 0xA3,
    "alt": 0x12, "menu": 0x12, "alt_l": 0xA2, "alt_r": 0xA5, "alt_gr": 0xA5,
    "pause": 0x13, "caps_lock": 0x14, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "page_up": 0x21, "pageup": 0x21,
    "page_down": 0x22, "pagedown": 0x22,
    "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "print_screen": 0x2C, "printscreen": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "help": 0x2F,
    "cmd": 0x5B, "cmd_l": 0x5B, "cmd_r": 0x5C,
    "win": 0x5B, "winleft": 0x5B, "winright": 0x5C,
    "apps": 0x5D, "sleep": 0x5F,
    "num_lock": 0x90, "numlock": 0x90,
    "scroll_lock": 0x91, "scrolllock": 0x91,
    "browser_back": 0xA6, "browser_forward": 0xA7, "browser_refresh": 0xA8,
    "browser_stop": 0xA9, "browser_search": 0xAA, "browser_favorites": 0xAB,
    "browser_home": 0xAC, "volume_mute": 0xAD, "volume_down": 0xAE,
    "volume_up": 0xAF, "media_next_track": 0xB0, "media_prev_track": 0xB1,
    "media_stop": 0xB2, "media_play_pause": 0xB3,
    "launch_mail": 0xB8, "launch_media_select": 0xB9,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
for _i in range(1, 25):
    _KEY_VK[f"f{_i}"] = 0x6F + _i          # F1=0x70 … F24=0x87

# 右侧修饰键与导航/编辑键必须带 KEYEVENTF_EXTENDEDKEY，否则部分应用识别不到
_EXTENDED_VK = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                          0x2C, 0x2D, 0x2E, 0x2F, 0x5C, 0x5D, 0x90, 0x91,
                          0xA1, 0xA3, 0xA5})


def _vk_for(name: str):
    """录制键名 → VK 码；解析失败返回 None。"""
    if name.startswith("Key."):
        name = name[4:]
    vk = _KEY_VK.get(name.lower())
    if vk is not None:
        return vk
    if len(name) == 1:
        if "A" <= name.upper() <= "Z" or "0" <= name <= "9":
            return ord(name.upper())       # A-Z / 0-9 的 VK 与 ASCII 主区一致
        scan = user32.VkKeyScanW(name)     # argtypes=WCHAR，直接传字符
        if scan != -1 and scan < 0xFF:     # 其余可打印字符按当前键盘布局取 VK
            return scan & 0xFF
    return None


def _key_event(vk: int, flags: int) -> None:
    if vk in _EXTENDED_VK:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    _send(inp)


def _key(name: str, up: bool) -> bool:
    """按下/松开一个键；返回 False 表示键名无法解析（调用方跳过即可）。

    录制端已把可打印字符统一小写存储，且 Shift 等修饰键本身就是独立事件，
    因此这里不需要再合成 Shift。
    """
    if name.startswith("VK."):
        try:
            vk = int(name[3:])
        except ValueError:
            return False
        _key_event(vk, KEYEVENTF_KEYUP if up else 0)
        return True
    vk = _vk_for(name)
    if vk is None:
        return False
    _key_event(vk, KEYEVENTF_KEYUP if up else 0)
    return True


def key_down(name: str) -> bool:
    return _key(name, up=False)


def key_up(name: str) -> bool:
    return _key(name, up=True)


def release_all_keys(names) -> None:
    """中断清理：松开所有已按下未释放的键。"""
    for n in reversed(list(names)):
        try:
            key_up(n)
        except Exception:
            pass


# ===================== 全局热键 =====================
# RegisterHotKey + 消息循环：由系统保证投递，窗口未聚焦/已隐藏同样生效，
# 且不依赖第三方库。线程级热键要求注册与消息循环在同一线程内完成。
WM_HOTKEY = 0x0312
PM_NOREMOVE = 0x0000
PM_REMOVE = 0x0001
MOD_NONE = 0x0000

VK_F7, VK_F8, VK_F9, VK_F10, VK_F11 = 0x76, 0x77, 0x78, 0x79, 0x7A


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = (ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT,
                                wintypes.UINT, wintypes.UINT)
user32.PeekMessageW.restype = ctypes.c_int     # 出错时返回 -1，按有符号判断


class HotKeyManager:
    """独立线程内注册线程级热键（hwnd=None）并轮询其消息队列。

    register() 必须在 start() 之前调用；实际注册在消息线程里完成。
    命中回调运行在后台线程：绝不可直接操作 tkinter，调用方需自行转回主线程。
    用 PeekMessage 轮询（而非 GetMessage 阻塞）保证 stop() 毫秒级生效。
    """

    def __init__(self) -> None:
        self._spec: list[tuple[int, object]] = []    # (vk, callback)
        self._map: dict[int, object] = {}            # hotkey_id → callback
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.failed: list[int] = []                  # 注册失败的 vk
        self.loops = 0                               # 心跳计数：诊断线程是否存活
        self.last_error = 0

    def register(self, vk_callbacks: list[tuple[int, object]]) -> None:
        if self._thread is not None:
            raise RuntimeError("热键只能在 start 之前设置")
        self._spec = list(vk_callbacks)

    def start(self, wait: bool = True) -> bool:
        """启动消息线程并等待注册结果；返回 False 表示有热键注册失败。"""
        self._stop.clear()
        self._ready.clear()
        self.failed = []
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="hotkey-loop")
        self._thread.start()
        if wait:
            self._ready.wait(2.0)
        return not self.failed

    def _loop(self) -> None:
        msg = MSG()
        # 先取一次消息强制创建本线程消息队列，再注册线程级热键
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        ids: list[int] = []
        for i, (vk, cb) in enumerate(self._spec, start=1):
            if user32.RegisterHotKey(None, i, MOD_NONE, vk):
                ids.append(i)
                self._map[i] = cb
            else:
                self.failed.append(vk)
        self._ready.set()
        while not self._stop.is_set():
            self.loops += 1
            got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE)
            if got > 0:
                if msg.message == WM_HOTKEY:
                    cb = self._map.get(int(msg.wParam))
                    if cb is not None:
                        try:
                            cb((int(msg.lParam) >> 16) & 0xFFFF)  # 高字=VK，低字=修饰键
                        except Exception:
                            pass
            elif got < 0:
                self.last_error = kernel32.GetLastError()
                self._stop.wait(0.2)             # 不退出：静默退出会让全部热键失效且无人知晓
            else:
                self._stop.wait(0.03)
        for hid in ids:
            user32.UnregisterHotKey(None, hid)
        self._map.clear()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


# ===================== 自检 =====================
def probe() -> bool:
    """在光标当前位置发一次无害的绝对移动，验证 SendInput 通路。"""
    pos = _cursor_pos()
    if pos is None:
        return False
    _mouse(pos[0], pos[1], MOUSEEVENTF_MOVE)
    return True


if __name__ == "__main__":
    print("虚拟桌面:", virtual_screen())
    print("SendInput 自检:", "通过" if probe() else "失败")
    hk = HotKeyManager()
    got: list[int] = []
    hk.register([(VK_F10, got.append)])
    print("F10 注册:", "成功" if hk.start() else f"失败 {hk.failed}")
    hk.stop()
