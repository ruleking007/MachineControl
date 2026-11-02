"""公共基础设施：路径、DPI、日志、线程安全的 UI 队列、录制文件读写。

导入本模块不会 import 任何重型库（mss/cv2/pytesseract 仅在真正需要时导入），
因此三个入口脚本都可以放心 `from common import ...`。
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import queue
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

# ===================== 路径（全部基于脚本目录，与当前工作目录无关） =====================
# PyInstaller 冻结环境下 __file__ 指向临时解包目录，必须改用 exe 所在目录，
# 否则 records/配置会被写进系统临时目录、重启即丢。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_records_dir() -> str:
    """录制文件的存放位置。

    血泪教训：以前直接放 ``BASE_DIR/records``，而打包版 BASE_DIR 是 exe 所在目录
    （dist\\机器操控工具箱\\）。一旦重新打包就整目录替换，用户录好的脚本跟着没了。
    所以**数据与程序分离**：

    1. 环境变量 MACHINE_CTL_DATA 优先（便于把数据放到别处）
    2. 有历史数据（老版本留下的 BASE_DIR/records 且非空）→ 继续用它，
       避免升级后老用户"脚本凭空消失"
    3. 否则用用户数据目录 %LOCALAPPDATA%\\机器操控工具箱\\records
    """
    env = os.environ.get("MACHINE_CTL_DATA", "").strip()
    if env:
        return os.path.join(env, "records")
    legacy = os.path.join(BASE_DIR, "records")
    if os.path.isdir(legacy) and any(
            f.lower().endswith((".json", ".pkl")) for f in os.listdir(legacy)):
        return legacy          # 老位置已有数据，别动它
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "机器操控工具箱", "records")


RECORDS_DIR = _resolve_records_dir()
# 用户数据目录（与程序目录分离，重打包不会丢）。录制文件之外的
# 「用户产生的重要数据」都放这里，例如定时任务表。
def _resolve_data_dir() -> str:
    """用户数据的根目录，RECORDS_DIR 就在它下面。"""
    env = os.environ.get("MACHINE_CTL_DATA", "").strip()
    if env:
        return env
    # 沿用旧位置的情况：RECORDS_DIR 指向程序目录下的 records
    if os.path.dirname(os.path.abspath(RECORDS_DIR)) == os.path.abspath(BASE_DIR):
        return BASE_DIR
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "机器操控工具箱")


DATA_DIR = _resolve_data_dir()

# 配置类文件留在程序目录即可：它们体积小、且重打包时本来就该重置
CONFIG_FILE = os.path.join(BASE_DIR, "region_list.json")
RULES_FILE = os.path.join(BASE_DIR, "trigger_rules.json")
LOG_FILE = os.path.join(BASE_DIR, "app.log")

os.makedirs(RECORDS_DIR, exist_ok=True)


def data_path(filename: str) -> str:
    """用户数据文件的完整路径（与程序目录分离，重打包不丢）。"""
    return os.path.join(DATA_DIR, filename)


def ensure_dirs() -> None:
    os.makedirs(RECORDS_DIR, exist_ok=True)


# ===================== DPI 感知 =====================
def enable_dpi_awareness() -> None:
    """必须在创建任何窗口之前调用（winctl/pyautogui 均按物理像素解释坐标）。

    -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2。
    调用后 tkinter 事件坐标、mss 截图坐标、winctl SendInput 坐标统一为物理像素。
    """
    if os.name != "nt":
        return
    import ctypes

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # Windows 10 1703 之前没有该 API
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


# ===================== 日志 =====================
def setup_logger(name: str = "机器操控", ui_sink: Optional["UiLogSink"] = None) -> logging.Logger:
    """控制台/文件日志 + 可选 UI 日志。重复调用不会重复挂 handler。"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    _add_once(logger, file_handler, "file")

    # 打包成 windowed exe 后 sys.stdout/stderr 为 None，此时只保留文件日志，
    # 否则 StreamHandler 会在第一条日志处抛错。
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        # 控制台代码页可能是 GBK（中文 Windows），而日志里带 emoji（▶️/✅）。
        # 直接写会抛 UnicodeEncodeError 并把整条日志丢掉，这里降级为可替换字符，
        # 保证日志本身绝不因为一个符号而中断。
        _make_stream_safe(console)
        _add_once(logger, console, "console")

    if ui_sink is not None:
        handler = _UiLogHandler(ui_sink)
        handler.setFormatter(fmt)
        _add_once(logger, handler, "ui")
    return logger


def _make_stream_safe(handler: logging.StreamHandler) -> None:
    """把 StreamHandler 的写入包一层：编码不支持的字符替换掉而不是抛异常。"""
    stream = getattr(handler, "stream", None)
    encoding = getattr(stream, "encoding", None) or "utf-8"

    class _SafeStream:
        """代理原 stream，仅对 write 做编码兜底。"""

        def __init__(self, raw) -> None:
            self._raw = raw
            self.encoding = encoding

        def write(self, text: str) -> int:
            try:
                return self._raw.write(text)
            except UnicodeEncodeError:
                return self._raw.write(text.encode(encoding, "replace").decode(encoding, "replace"))

        def flush(self) -> None:
            try:
                self._raw.flush()
            except Exception:
                pass

        def __getattr__(self, item):
            return getattr(self._raw, item)

    try:
        handler.stream = _SafeStream(stream)  # type: ignore[assignment]
    except Exception:
        pass


def _add_once(logger: logging.Logger, handler: logging.Handler, tag: str) -> None:
    for h in logger.handlers:
        if getattr(h, "_tag", None) == tag:
            return
    handler._tag = tag  # type: ignore[attr-defined]
    logger.addHandler(handler)


class _UiLogHandler(logging.Handler):
    """只往线程安全队列里放消息，绝不在非主线程碰 tkinter 控件。"""

    def __init__(self, sink: "UiLogSink"):
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.queue.put(self.format(record))
        except Exception:
            pass


class UiLogSink:
    """主线程用 `drain()` 消费；后台线程只负责 `put`。"""

    def __init__(self) -> None:
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.consumer: Optional[Callable[[str], None]] = None

    def put(self, msg: str) -> None:
        self.queue.put(msg)

    def drain(self) -> int:
        n = 0
        while True:
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                return n
            if self.consumer is not None:
                self.consumer(msg)
            n += 1


def cancel_job(widget, job) -> None:
    """安全撤销一个 after 任务；job 为空或已失效都静默忽略。"""
    if not job:
        return
    try:
        widget.after_cancel(job)
    except Exception:
        pass


def make_scrollable(parent, hscroll: bool = False):
    """把内容套进一个竖向可滚动容器，返回 (外层容器, 内容容器)。

    内容容器里该怎么 pack 就怎么 pack；内容高于可视区时自动出现滚动条，
    够高时滚动条隐藏。用来解决「页面内容比窗口高、硬挤导致某块被压成 1x1」。

    参数 hscroll=True 时额外支持横向滚动（内容天然很宽的场景）。
    """
    import tkinter as tk
    from tkinter import ttk

    outer = ttk.Frame(parent)
    canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
    vbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    vbar.pack(side=tk.RIGHT, fill=tk.Y)

    hbar = None
    if hscroll:
        hbar = ttk.Scrollbar(outer, orient=tk.HORIZONTAL, command=canvas.xview)
        canvas.configure(xscrollcommand=hbar.set)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)

    body = ttk.Frame(canvas)
    window = canvas.create_window((0, 0), window=body, anchor="nw")

    def _sync(_e=None) -> None:
        # 无横向滚动时，内容宽度**跟随画布**，绝不用 body.winfo_reqwidth()。
        # 否则一旦有子控件（如键鼠画布）请求宽度更大，body 会被撑宽，
        # 反过来又让子控件拿到更大的可用宽度 —— 正反馈，实测撑到 1960px，
        # 而可视区只有 1138px，右侧内容溢出且缩放计算全错。
        if hscroll:
            need_w = max(body.winfo_reqwidth(), canvas.winfo_width())
        else:
            need_w = max(320, canvas.winfo_width())
        canvas.itemconfigure(window, width=need_w)
        canvas.configure(scrollregion=(0, 0, need_w, body.winfo_reqheight()))
        need_v = body.winfo_reqheight() > canvas.winfo_height()
        if need_v and not vbar.winfo_ismapped():
            vbar.pack(side=tk.RIGHT, fill=tk.Y)
        elif not need_v and vbar.winfo_ismapped():
            vbar.pack_forget()

    def _on_wheel(e) -> None:
        if body.winfo_reqheight() > canvas.winfo_height():
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def bind_wheel(widget) -> None:
        """递归给内容里所有控件绑滚轮（Listbox 自己管滚动，跳过）。"""
        if not isinstance(widget, tk.Listbox):
            widget.bind("<MouseWheel>", _on_wheel)
        for ch in widget.winfo_children():
            bind_wheel(ch)

    body.bind("<Configure>", _sync)
    canvas.bind("<Configure>", lambda e: _sync())
    for w in (canvas, body):
        w.bind("<MouseWheel>", _on_wheel)
    # 内容里的子控件也要能滚（Tk 把滚轮事件派给光标下最内层控件）
    body.after(300, lambda: bind_wheel(body))
    return outer, body


def fix_treeview_rowheight(root=None) -> int:
    """按当前字体行距设置 Treeview 行高，避免文字被上下裁掉。

    坑点：Treeview 的 rowheight 在 vista 主题下默认是**固定 20px**，不随 DPI
    或字号变化。高 DPI 屏（如 225%）里 TkDefaultFont 的 linespace 可到 35px，
    字比格子高 75%，于是每行只能露出中间一截 —— 表现就是「只能看到字的一半」。

    这里把行高设成 max(linespace + 内边距, 原默认值)。必须在创建 Treeview
    之前调用（样式是全局的）。
    """
    rh = 0
    try:
        import tkinter.font as tkfont
        from tkinter import ttk
        style = ttk.Style(root)
        try:
            font = tkfont.nametofont("TkDefaultFont", root=root)
        except Exception:
            font = tkfont.nametofont("TkDefaultFont")
        linespace = font.metrics("linespace")
        # 上下各留 4px 内边距，让字不贴边
        rh = max(20, int(linespace) + 8)
        style.configure("Treeview", rowheight=rh)
    except Exception:
        pass
    # 顺手修按钮宽度（与上面的行高同一个主题坑）
    fix_button_width(root)
    return rh


def fix_button_width(root=None) -> None:
    """去掉 ttk 按钮的「最小字符宽度」约束。

    vista 主题下 TButton 的默认 width=-11（负号表示按字符数算），在 225% DPI
    下等于强制每个按钮至少 186px —— 哪怕文字只要 54px。结果一排按钮放不下，
    Tk 会把它们压到 61px 甚至 1px，文字被切掉一半（「改进」「保存」只剩半个字）。

    这里把 width 设成 0（按内容自适应），并略收紧内边距。
    """
    try:
        from tkinter import ttk
        style = ttk.Style(root)
        style.configure("TButton", width=0, padding=(8, 4))
        # 数字微调按钮同理，否则会占掉表单大量宽度
        style.configure("TSpinbox", width=0)
    except Exception:
        pass


class UiDispatcher:
    """后台线程 → 主线程 的通用桥：`post(fn, *args)` 后由 `after` 轮询执行。"""

    def __init__(self, root) -> None:
        self.root = root
        self._q: "queue.Queue[Tuple[Callable, tuple]]" = queue.Queue()
        self._running = True
        self._job = self.root.after(50, self._poll)

    def post(self, fn: Callable, *args: Any) -> None:
        self._q.put((fn, args))

    def stop(self) -> None:
        self._running = False
        cancel_job(self.root, self._job)
        self._job = None

    def _poll(self) -> None:
        self._job = None          # 本次已触发，先清登记再排下一次
        if not self._running:
            return
        while True:
            try:
                fn, args = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:  # 单个回调出错不能拖垮 UI 轮询
                logging.getLogger("机器操控").exception("UI 回调异常")
        if not self._running:
            return          # 回调里可能调了 stop()，此时不该再排队
        try:
            self._job = self.root.after(50, self._poll)
        except Exception:   # 窗口已销毁（TclError），静默收尾
            self._running = False


# ===================== 事件模型 =====================
# 事件元组格式保持与旧版兼容：(类型, 延迟秒, *参数)
#   ("move", delay, x, y)
#   ("click", delay, x, y, "Button.left")
#   ("release", delay, x, y, "Button.left")
#   ("wheel", delay, x, y, dx, dy)        # 新版新增
#   ("key_down", delay, "a"/"Key.space")  # 新版对可打印字符用规范化名称
#   ("key_up", delay, "a"/"Key.space")
Event = Tuple[Any, ...]
VALID_EVENTS = {"move", "click", "release", "wheel", "key_down", "key_up"}

# 全局热键（pynput 键名口径）：F10 显隐面板 / F9 开始录制 / F8 停止录制 / F7 终止回放 / F11 开始执行
# 这些键既不做录制内容，也不做回放动作，只由主窗口绑定处理。
HOTKEY_SHOW = "Key.f10"
HOTKEY_PLAY = "Key.f11"
HOTKEY_START = "Key.f9"
HOTKEY_STOP = "Key.f8"
HOTKEY_HALT = "Key.f7"
HOTKEY_NAMES = {HOTKEY_SHOW, HOTKEY_PLAY, HOTKEY_START, HOTKEY_STOP, HOTKEY_HALT}

# pynput Key 名称 → pyautogui 按键名
# （历史遗留：操作层已改用 winctl 直接解析录制键名，本表仅作旧脚本兼容保留）
_PYNPUT_TO_PYAUTOGUI: Dict[str, str] = {
    "alt": "alt", "alt_l": "altleft", "alt_r": "altright", "alt_gr": "altright",
    "backspace": "backspace", "caps_lock": "capslock", "cmd": "win", "cmd_l": "winleft",
    "cmd_r": "winright", "ctrl": "ctrl", "ctrl_l": "ctrlleft", "ctrl_r": "ctrlright",
    "delete": "delete", "down": "down", "end": "end", "enter": "enter", "esc": "esc",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5", "f6": "f6", "f7": "f7",
    "f8": "f8", "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12", "f13": "f13",
    "f14": "f14", "f15": "f15", "f16": "f16", "f17": "f17", "f18": "f18", "f19": "f19",
    "f20": "f20", "home": "home", "insert": "insert", "left": "left", "num_lock": "numlock",
    "page_down": "pagedown", "page_up": "pageup", "pause": "pause", "print_screen": "printscreen",
    "right": "right", "scroll_lock": "scrolllock", "shift": "shift", "shift_l": "shiftleft",
    "shift_r": "shiftright", "space": "space", "tab": "tab", "up": "up", "menu": "apps",
}


def key_to_name(key: Any) -> str:
    """pynput 回调参数 → 稳定的字符串键名（不依赖键盘布局的物理意义）。

    - KeyCode: 用 `.char`（可打印字符，统一小写）或 `.vk`；无 char 且无 vk 时降级为 "<unknown>"
    - Key 枚举: "Key.space" 形式，回放时翻译成 pyautogui 名字
    """
    char = getattr(key, "char", None)
    if isinstance(char, str) and char:
        if char.isprintable() and char != " ":
            return char.lower()
        return "Key.space"
    name = getattr(key, "name", None)
    if isinstance(name, str) and name:
        return f"Key.{name}"
    vk = getattr(key, "vk", None)
    if vk is not None:
        return f"VK.{vk}"
    return "<unknown>"


def event_to_pyautogui_key(name: str) -> Optional[str]:
    """录制键名 → pyautogui.keyDown 可用的名字；无法映射返回 None。"""
    if name.startswith("Key."):
        return _PYNPUT_TO_PYAUTOGUI.get(name[4:])
    if name.startswith("VK."):
        return None
    return name


# ===================== 录制文件读写 =====================
def list_recordings() -> List[str]:
    ensure_dirs()
    files = [f for f in os.listdir(RECORDS_DIR)
             if f.lower().endswith((".pkl", ".json"))
             and os.path.isfile(os.path.join(RECORDS_DIR, f))]
    files.sort(reverse=True)
    return files


def sanitize_events(raw: Any) -> List[Event]:
    """校验并清洗事件序列：拒绝非元组元素、未知类型、非数值延迟。"""
    if not isinstance(raw, (list, tuple)):
        raise ValueError("录制文件内容不是事件列表")
    clean: List[Event] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        kind, delay = item[0], item[1]
        if not isinstance(kind, str) or kind not in VALID_EVENTS:
            continue
        if not isinstance(delay, (int, float)) or delay != delay or delay < 0:
            delay = 0.0
        clean.append((kind, float(delay), *item[2:]))
    return clean


def load_events(path: str) -> List[Event]:
    """优先按 JSON 读（新版格式），失败再按 pickle 读（兼容旧文件），均做安全校验。"""
    with open(path, "rb") as f:
        blob = f.read()
    try:
        return sanitize_events(json.loads(blob.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        pass
    return sanitize_events(pickle.loads(blob))


def unique_record_path(ext: str = ".json") -> str:
    ensure_dirs()
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    base = os.path.join(RECORDS_DIR, stamp)
    candidate = base + ext
    n = 1
    while os.path.exists(candidate):
        candidate = f"{base}_{n}{ext}"
        n += 1
    return candidate


def save_events(events: List[Event]) -> str:
    """写出 JSON 格式（可读、可手改、无反序列化风险）。"""
    path = unique_record_path(".json")
    payload = [list(e) for e in events]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return path


def trim_tail(events: List[Event], keep_until: float) -> List[Event]:
    """丢弃“相对开始时间 > keep_until 秒”的尾部事件（用于停止时剔除最后 N 秒）。"""
    out: List[Event] = []
    t = 0.0
    for e in events:
        t += e[1]
        if t > keep_until:
            break
        out.append(e)
    return out


def count_duration(events: List[Event]) -> float:
    return sum(e[1] for e in events if isinstance(e[1], (int, float)))


# ===================== 区域配置（文字侦查） =====================
@dataclass
class Region:
    name: str
    left: int
    top: int
    width: int
    height: int
    monitor: int = 1          # mss monitors 索引，0 = 全虚拟桌面
    psm: int = 6              # tesseract --psm
    lang: str = "chi_sim+eng"

    def to_box(self) -> Dict[str, int]:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}


def load_regions() -> List[Region]:
    if not os.path.exists(CONFIG_FILE):
        return []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [Region(**{k: v for k, v in d.items() if k in Region.__dataclass_fields__})
                for d in data if isinstance(d, dict) and "name" in d]
    except Exception:
        backup = CONFIG_FILE + ".bak"
        try:
            os.replace(CONFIG_FILE, backup)
        except Exception:
            pass
        return []


def save_regions(regions: List[Region]) -> None:
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in regions], f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ===================== 触发规则配置（文字侦查 / 图形监察） =====================
# 动作三种：写运行日志、同时追加到告警文件、回放一个录制脚本（键鼠回放）。
RULE_ACTIONS = ("日志", "日志+告警文件", "执行脚本")

# 规则来源：OCR 关键词命中，或 YOLO 检测到指定物体（两者共用同一套规则表）
RULE_SOURCES = ("文字", "图形")


@dataclass
class RuleConfig:
    name: str
    keywords: List[str] = field(default_factory=list)
    cooldown: float = 5.0
    case_sensitive: bool = False
    exact_match: bool = False
    action: str = "日志"
    alert_file: str = ""
    enabled: bool = True
    # ---- 图形监察（YOLO）专用 ----
    source: str = "文字"          # "文字" = OCR 关键词；"图形" = YOLO 物体类别
    min_conf: float = 0.35        # 图形规则：命中所需最低置信度
    min_count: int = 1            # 图形规则：至少出现几个该类物体才触发
    script_file: str = ""         # 动作=执行脚本 时回放的录制文件名
    script_loop: bool = False     # 该脚本是否循环回放


DEFAULT_RULES: List[RuleConfig] = [
    RuleConfig("问候检测", ["你好", "hello", "hi", "您好"], cooldown=3.0),
    RuleConfig("错误检测", ["错误", "error", "失败", "failed", "exception"],
               cooldown=5.0, action="日志+告警文件"),
    RuleConfig("成功检测", ["成功", "success", "完成", "ok", "done"], cooldown=2.0),
]


def _clean_rule(raw: Dict[str, Any]) -> Optional[RuleConfig]:
    """把配置文件里的一条记录清洗成 RuleConfig；关键字段非法则整条丢弃。"""
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    kws = raw.get("keywords")
    if not isinstance(kws, (list, tuple)):
        return None
    kws = [str(k) for k in kws if isinstance(k, (str, int, float)) and str(k).strip()]
    if not kws:
        return None
    try:
        cooldown = max(0.0, float(raw.get("cooldown", 5.0)))
    except (TypeError, ValueError):
        cooldown = 5.0
    action = raw.get("action")
    if action not in RULE_ACTIONS:
        action = "日志"
    alert = raw.get("alert_file")
    if not isinstance(alert, str) or not alert.strip():
        alert = "error_log.txt" if action == "日志+告警文件" else ""
    source = raw.get("source")
    if source not in RULE_SOURCES:
        source = "文字"
    try:
        min_conf = min(1.0, max(0.0, float(raw.get("min_conf", 0.35))))
    except (TypeError, ValueError):
        min_conf = 0.35
    try:
        min_count = max(1, int(raw.get("min_count", 1)))
    except (TypeError, ValueError):
        min_count = 1
    script = raw.get("script_file")
    if not isinstance(script, str):
        script = ""
    return RuleConfig(
        name=name.strip(), keywords=kws, cooldown=cooldown,
        case_sensitive=bool(raw.get("case_sensitive", False)),
        exact_match=bool(raw.get("exact_match", False)),
        action=action, alert_file=alert.strip(),
        enabled=bool(raw.get("enabled", True)),
        source=source, min_conf=min_conf, min_count=min_count,
        script_file=script.strip(), script_loop=bool(raw.get("script_loop", False)))


def load_rules() -> List[RuleConfig]:
    """没有配置文件时返回内置默认规则（首次运行体验不变）。"""
    if not os.path.exists(RULES_FILE):
        return [replace_defaults(r) for r in DEFAULT_RULES]
    try:
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("规则文件顶层不是列表")
        out = [cleaned for cleaned in (_clean_rule(d) for d in data if isinstance(d, dict))
               if cleaned is not None]
        if data and not out:
            # 文件里有内容但全部非法 → 视为损坏，回退默认；
            # 合法的空列表表示用户删光了所有规则，尊重之。
            raise ValueError("规则文件里没有合法条目")
        return out
    except Exception:
        backup = RULES_FILE + ".bak"
        try:
            os.replace(RULES_FILE, backup)
        except Exception:
            pass
        return [replace_defaults(r) for r in DEFAULT_RULES]


def replace_defaults(rule: RuleConfig) -> RuleConfig:
    """默认规则每次加载都要重新拷贝，避免界面/线程共享同一实例。"""
    return RuleConfig(rule.name, list(rule.keywords), rule.cooldown,
                      rule.case_sensitive, rule.exact_match, rule.action,
                      rule.alert_file, rule.enabled,
                      rule.source, rule.min_conf, rule.min_count,
                      rule.script_file, rule.script_loop)


def load_default_rules() -> List[RuleConfig]:
    """界面“恢复默认”按钮使用：每次返回独立拷贝。"""
    return [replace_defaults(r) for r in DEFAULT_RULES]


def save_rules(rules: List[RuleConfig]) -> None:
    tmp = RULES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rules], f, ensure_ascii=False, indent=2)
    os.replace(tmp, RULES_FILE)


# ===================== 图形监察配置（YOLO） =====================
# YOLO 跑在独立的 conda 环境里（ultralytics + torch 体积大，不进主程序依赖），
# 主进程只负责截图与协议转发。模型与解释器路径都可在界面上改。
YOLO_CONFIG_FILE = os.path.join(BASE_DIR, "yolo_config.json")

# 优先顺序：环境变量 → 常见 conda 位置。找不到就由用户在界面上指定。
_YOLO_PY_CANDIDATES = [
    r"C:\Users\21094\.conda\envs\yolo_ocr\python.exe",
    os.path.expanduser(r"~\.conda\envs\yolo_ocr\python.exe"),
    r"C:\ProgramData\miniconda3\envs\yolo_ocr\python.exe",
]


def detect_yolo_python() -> str:
    """找 yolo_ocr 环境的解释器；找不到返回空串（界面会提示手动指定）。"""
    env = os.environ.get("YOLO_PYTHON", "")
    if env and os.path.isfile(env):
        return env
    for path in _YOLO_PY_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return ""


@dataclass
class YoloConfig:
    """图形监察的运行参数。interval 默认 2.0s：CPU 推理约 0.7s/帧，留足余量。"""

    python_path: str = ""          # yolo_ocr 环境的 python.exe
    model_path: str = ""           # .pt 权重路径
    conf: float = 0.35             # 默认置信度阈值
    interval: float = 2.0          # 两次检测的最小间隔（秒）
    enabled: bool = False          # 是否随 OCR 一起启动图形监察
    classes: List[str] = field(default_factory=list)   # 关注类别；空 = 全部


def load_yolo_config() -> YoloConfig:
    cfg = YoloConfig()
    if not os.path.exists(YOLO_CONFIG_FILE):
        cfg.python_path = detect_yolo_python()
        return cfg
    try:
        with open(YOLO_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("图形监察配置顶层不是对象")
    except Exception:
        backup = YOLO_CONFIG_FILE + ".bak"
        try:
            os.replace(YOLO_CONFIG_FILE, backup)
        except Exception:
            pass
        cfg.python_path = detect_yolo_python()
        return cfg
    cfg.python_path = str(raw.get("python_path") or "") or detect_yolo_python()
    cfg.model_path = str(raw.get("model_path") or "")
    try:
        cfg.conf = min(1.0, max(0.0, float(raw.get("conf", 0.35))))
    except (TypeError, ValueError):
        cfg.conf = 0.35
    try:
        cfg.interval = min(600.0, max(0.2, float(raw.get("interval", 2.0))))
    except (TypeError, ValueError):
        cfg.interval = 2.0
    cfg.enabled = bool(raw.get("enabled", False))
    cls = raw.get("classes")
    cfg.classes = [str(c) for c in cls if str(c).strip()] if isinstance(cls, (list, tuple)) else []
    return cfg


def save_yolo_config(cfg: YoloConfig) -> None:
    tmp = YOLO_CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, ensure_ascii=False, indent=2)
    os.replace(tmp, YOLO_CONFIG_FILE)


# ===================== 虚拟桌面信息 =====================
def virtual_screen_rect() -> Tuple[int, int, int, int]:
    """返回 (left, top, width, height)，物理像素，覆盖所有显示器。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        # SM_XVIRTUALSCREEN .. SM_CYVIRTUALSCREEN
        left = user32.GetSystemMetrics(76)
        top = user32.GetSystemMetrics(77)
        width = user32.GetSystemMetrics(78)
        height = user32.GetSystemMetrics(79)
        if width > 0 and height > 0:
            return left, top, width, height
    try:
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        w, h = r.winfo_screenwidth(), r.winfo_screenheight()
        r.destroy()
        return 0, 0, w, h
    except Exception:
        return 0, 0, 1920, 1080
