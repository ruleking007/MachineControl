"""脚本可视化：把录制脚本里的键鼠动作映射成「键盘 + 鼠标」图示。

设计要点：
- **只读静态展示**：加载脚本时一次性统计，不跟回放走、不碰回放线程。
- **不含鼠标移动**：move 事件完全跳过（脚本里移动事件占了绝大多数，
  画出来只会糊成一片，也没有信息量）。
- 鼠标只表达三件事：左键、右键、滚轮方向 —— 这正是「点了什么、滚了哪边」。
- 计数用**按下次数**（click / wheel），不用 release：一次点击只算一次，
  长按也如实显示为「按住几次」。

坐标说明：录制文件的 click/release/wheel 都带 (x, y)，但本模块刻意不画轨迹，
只在需要时给出「涉及 N 个不同位置」这类概要。
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 键名 → 键盘布局上的显示文字。布局按 ANSI 104 键（Windows 常见）。
# 键名来自 common.key_to_name()：可打印字符是小写单字符，特殊键是 "Key.xxx"。
_KEY_LABELS: Dict[str, str] = {
    "Key.esc": "Esc", "Key.f1": "F1", "Key.f2": "F2", "Key.f3": "F3",
    "Key.f4": "F4", "Key.f5": "F5", "Key.f6": "F6", "Key.f7": "F7",
    "Key.f8": "F8", "Key.f9": "F9", "Key.f10": "F10", "Key.f11": "F11",
    "Key.f12": "F12",
    "`": "~", "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
    "6": "6", "7": "7", "8": "8", "9": "9", "0": "0", "-": "-", "=": "=",
    "Key.backspace": "Back",
    "Key.tab": "Tab",
    "q": "Q", "w": "W", "e": "E", "r": "R", "t": "T", "y": "Y", "u": "U",
    "i": "I", "o": "O", "p": "P", "[": "[", "]": "]", "\\": "\\",
    "a": "A", "s": "S", "d": "D", "f": "F", "g": "G", "h": "H", "j": "J",
    "k": "K", "l": "L", ";": ";", "'": "'",
    "Key.enter": "Enter",
    "z": "Z", "x": "X", "c": "C", "v": "V", "b": "B", "n": "N", "m": "M",
    ",": ",", ".": ".", "/": "/",
    "Key.shift_l": "Shift", "Key.shift_r": "Shift",
    "Key.ctrl_l": "Ctrl", "Key.ctrl_r": "Ctrl",
    "Key.alt_l": "Alt", "Key.alt_r": "Alt", "Key.alt_gr": "AltGr",
    "Key.cmd": "Win", "Key.cmd_l": "Win", "Key.cmd_r": "Win",
    "Key.space": "Space",
    "Key.caps_lock": "Caps",
    "Key.up": "↑", "Key.down": "↓", "Key.left": "←", "Key.right": "→",
    "Key.delete": "Del", "Key.insert": "Ins", "Key.home": "Home",
    "Key.end": "End", "Key.page_up": "PgUp", "Key.page_down": "PgDn",
    "Key.num_lock": "Num", "Key.print_screen": "PrtSc",
    "Key.scroll_lock": "ScrLk", "Key.pause": "Pause", "Key.menu": "Menu",
}

# 每个键在布局网格里的 (行, 列, 跨列数)。列宽单位固定。
# 主键区 15 列：Esc 区 / 功能键区 / 编辑键区各自占位，视觉上分三段。
_LAYOUT: List[Tuple[str, int, int, int]] = [
    # (键名, 行, 列, 跨列)
    ("Key.esc", 0, 0, 1),
    ("Key.f1", 0, 2, 1), ("Key.f2", 0, 3, 1), ("Key.f3", 0, 4, 1), ("Key.f4", 0, 5, 1),
    ("Key.f5", 0, 6, 1), ("Key.f6", 0, 7, 1), ("Key.f7", 0, 8, 1), ("Key.f8", 0, 9, 1),
    ("Key.f9", 0, 10, 1), ("Key.f10", 0, 11, 1), ("Key.f11", 0, 12, 1), ("Key.f12", 0, 13, 1),

    ("`", 1, 0, 1), ("1", 1, 1, 1), ("2", 1, 2, 1), ("3", 1, 3, 1), ("4", 1, 4, 1),
    ("5", 1, 5, 1), ("6", 1, 6, 1), ("7", 1, 7, 1), ("8", 1, 8, 1), ("9", 1, 9, 1),
    ("0", 1, 10, 1), ("-", 1, 11, 1), ("=", 1, 12, 1), ("Key.backspace", 1, 13, 2),

    ("Key.tab", 2, 0, 2), ("q", 2, 2, 1), ("w", 2, 3, 1), ("e", 2, 4, 1), ("r", 2, 5, 1),
    ("t", 2, 6, 1), ("y", 2, 7, 1), ("u", 2, 8, 1), ("i", 2, 9, 1), ("o", 2, 10, 1),
    ("p", 2, 11, 1), ("[", 2, 12, 1), ("]", 2, 13, 1), ("\\", 2, 14, 1),

    ("Key.caps_lock", 3, 0, 2), ("a", 3, 2, 1), ("s", 3, 3, 1), ("d", 3, 4, 1),
    ("f", 3, 5, 1), ("g", 3, 6, 1), ("h", 3, 7, 1), ("j", 3, 8, 1), ("k", 3, 9, 1),
    ("l", 3, 10, 1), (";", 3, 11, 1), ("'", 3, 12, 1), ("Key.enter", 3, 13, 2),

    ("Key.shift_l", 4, 0, 3), ("z", 4, 3, 1), ("x", 4, 4, 1), ("c", 4, 5, 1),
    ("v", 4, 6, 1), ("b", 4, 7, 1), ("n", 4, 8, 1), ("m", 4, 9, 1), (",", 4, 10, 1),
    (".", 4, 11, 1), ("/", 4, 12, 1), ("Key.shift_r", 4, 13, 2),

    ("Key.ctrl_l", 5, 0, 2), ("Key.cmd_l", 5, 2, 1), ("Key.alt_l", 5, 3, 1),
    ("Key.space", 5, 4, 6), ("Key.alt_r", 5, 10, 1), ("Key.cmd_r", 5, 11, 1),
    ("Key.menu", 5, 12, 1), ("Key.ctrl_r", 5, 13, 3),
]

# 方向键/编辑键单独一组，画在键盘右侧
_NAV_LAYOUT: List[Tuple[str, int, int, int]] = [
    ("Key.print_screen", 0, 0, 1), ("Key.scroll_lock", 0, 1, 1), ("Key.pause", 0, 2, 1),
    ("Key.insert", 1, 0, 1), ("Key.home", 1, 1, 1), ("Key.page_up", 1, 2, 1),
    ("Key.delete", 2, 0, 1), ("Key.end", 2, 1, 1), ("Key.page_down", 2, 2, 1),
    ("Key.left", 3, 0, 1), ("Key.up", 3, 1, 1), ("Key.right", 3, 2, 1),
    ("Key.down", 4, 1, 1),
]

_MOUSE_BUTTON_LABELS = {"left": "左键", "right": "右键", "middle": "中键",
                        "x1": "侧键1", "x2": "侧键2"}


# 版面尺寸（像素）。键盘主体 16 列（Space 加宽到 6 格，
# 让右侧 Alt/Win/Menu/Ctrl 那一段不再挤在一起），右边再放导航键区与鼠标。
#
# 这套尺寸是「自然尺寸」：窗口不够宽时会整体等比缩小，下限 0.72。
# 因此自然尺寸不宜过大 —— 否则 1180 宽的窗口里一下缩到下限，键又变挤了。
# 当前值在 1180 窗口下约缩到 0.73，键宽仍有 ~45px，字清晰可分。
_CELL_W = 46
_CELL_H = 40
_GAP = 5
_KB_COLS = 16
_NAV_COLS = 3
_MOUSE_W = 150
_MARGIN = 10
# 键盘与导航键区、导航键区与鼠标之间的空隙
_GAP_KBN_NAV = 26
_GAP_NAV_MOUSE = 34

# 字号缩放系数：0.5 = 所有字号减半（用户要求把键盘/鼠标上的字缩小一半）。
# 注意 Tk Canvas 的 scale() 只缩放坐标、**不缩放字体**，所以字号必须单独算。
# winfo / Tk 的字号单位是 point，最小可用 1pt 以上；这里统一向下取整并设下限 1。
_FONT_SCALE = 0.5


def _fs(size: int, bold: bool = False) -> tuple:
    """按 _FONT_SCALE 折算字号，返回 Tk 字体元组。"""
    px = max(1, int(round(size * _FONT_SCALE)))
    return ("Microsoft YaHei UI", px, "bold") if bold else ("Microsoft YaHei UI", px)

# 各部分占宽
_KB_W = _KB_COLS * _CELL_W + (_KB_COLS - 1) * _GAP
_NAV_W = _NAV_COLS * _CELL_W + (_NAV_COLS - 1) * _GAP
_MOUSE_X = _MARGIN + _KB_W + _GAP_KBN_NAV + _NAV_W + _GAP_NAV_MOUSE
_CANVAS_W = _MOUSE_X + _MOUSE_W + _MARGIN
# 高度取键盘 6 行与「鼠标本体 + 滚轮指示盘」的较大者
_KB_H = 6 * _CELL_H + 5 * _GAP
_MOUSE_H = 132 + 40 + 24 + 24      # 本体 + 指示盘中心偏移 + 方块半高 + 余量
_CANVAS_H = _MARGIN + max(_KB_H, _MOUSE_H) + _MARGIN


@dataclass
class VisualSummary:
    """一个脚本的键鼠动作统计（静态、不含移动）。"""

    key_presses: Dict[str, int] = field(default_factory=dict)   # 键名 → 按下次数
    buttons: Dict[str, int] = field(default_factory=dict)       # 鼠标键 → 按下次数
    wheel_up: int = 0
    wheel_down: int = 0
    wheel_left: int = 0
    wheel_right: int = 0
    positions: int = 0          # 涉及的不同坐标数（仅作概要，不画轨迹）
    total: int = 0              # 计入图示的事件总数
    skipped_move: int = 0       # 跳过的移动事件数


def summarize_events(events: List[tuple]) -> VisualSummary:
    """把事件序列压成统计结果。只统计 key_down / click / wheel。"""
    s = VisualSummary()
    pos = set()
    for ev in events:
        if ev[0] == "move":
            s.skipped_move += 1
            continue
        _accumulate(s, pos, ev)
    s.positions = len(pos)
    return s


def summarize_one(event: tuple) -> Optional[VisualSummary]:
    """只统计单个事件，供「点表格某一行 → 图示只显示该步」使用。

    返回 None 表示这一步与图示无关（移动 / 抬键），调用方据此给出文字说明。
    """
    if not isinstance(event, (list, tuple)) or len(event) < 2:
        return None
    if event[0] in ("move", "release"):
        return None          # 移动不画；release 只是抬键，不额外表达
    s = VisualSummary()
    _accumulate(s, set(), event)
    return s if s.total else None


def _accumulate(s: VisualSummary, pos: set, ev: tuple) -> None:
    """把单个事件并入统计（summarize_events 与 summarize_one 共用，避免两份逻辑）。"""
    kind = ev[0]
    if kind in ("click", "release"):
        # 坐标在索引 2、3；button 在索引 4
        if len(ev) >= 5 and isinstance(ev[2], int) and isinstance(ev[3], int):
            pos.add((ev[2], ev[3]))
        if kind != "click":
            return
        btn = str(ev[4]).replace("Button.", "")
        s.buttons[btn] = s.buttons.get(btn, 0) + 1
        s.total += 1
    elif kind == "wheel":
        if len(ev) >= 6 and isinstance(ev[2], int) and isinstance(ev[3], int):
            pos.add((ev[2], ev[3]))
        try:
            dx, dy = int(ev[4]), int(ev[5])
        except (TypeError, ValueError, IndexError):
            return
        # 向上滚动 = dy > 0（与 winctl.scroll 的语义一致）
        if dy > 0:
            s.wheel_up += 1
        elif dy < 0:
            s.wheel_down += 1
        if dx > 0:
            s.wheel_right += 1
        elif dx < 0:
            s.wheel_left += 1
        if dx or dy:
            s.total += 1
    elif kind == "key_down":
        if len(ev) >= 3:
            name = str(ev[2])
            s.key_presses[name] = s.key_presses.get(name, 0) + 1
            s.total += 1


class KeyboardMouseView(tk.Frame):
    """键盘 + 鼠标的静态可视化控件。

    键盘按键被按下过 → 该键填色（越频繁越深）+ 右上角显示次数；
    鼠标对应键位填色；滚轮用上下左右四个箭头表达方向。
    """

    # 配色
    KEY_BG = "#e8eaed"          # 普通键
    KEY_FG = "#333333"
    KEY_USED_BG = "#1a7f37"     # 用到的键（绿）
    KEY_USED_FG = "#ffffff"
    PANEL_BG = "#fbfbfc"
    BORDER = "#c9cdd3"

    def __init__(self, master: tk.Misc, **kw) -> None:
        super().__init__(master, bg=self.PANEL_BG,
                         highlightthickness=1, highlightbackground=self.BORDER, **kw)
        self._summary: Optional[VisualSummary] = None
        self._key_ids: Dict[str, int] = {}
        self._label_ids: Dict[str, int] = {}
        self._scale = 1.0
        self._scaled_for = None           # 记录上次缩放依据，避免重复缩放
        self._max_h = 300                 # 高度上限，可由外部调整
        self._build()

    # ---------- 构建 ----------
    def _build(self) -> None:
        head = tk.Frame(self, bg=self.PANEL_BG)
        head.pack(fill=tk.X, padx=8, pady=(6, 0))
        self.title_var = tk.StringVar(value="键鼠动作（不含鼠标移动）")
        tk.Label(head, textvariable=self.title_var, bg=self.PANEL_BG,
                 fg="#0b5394", anchor="w",
                 font=_fs(9, bold=True)).pack(side=tk.LEFT)

        # 画布：内部按 _CANVAS_W 的自然尺寸绘制，再整体等比缩放到可用宽度。
        # 缩放下限 0.72（见 _on_canvas_resize），保证键位不会挤到看不清；
        # 真装不下时用横向滚动条兜住，不再继续压。
        wrap = tk.Frame(self, bg=self.PANEL_BG)
        wrap.pack(fill=tk.X, padx=8, pady=(2, 0))
        canvas = tk.Canvas(wrap, bg=self.PANEL_BG, highlightthickness=0,
                           width=_CANVAS_W, height=_CANVAS_H,
                           scrollregion=(0, 0, _CANVAS_W, _CANVAS_H))
        hbar = tk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=canvas.xview)
        canvas.configure(xscrollcommand=hbar.set)
        # 不用 fill=X：那会让画布撑满父容器（父容器又被内容撑宽），
        # 实测会变成 1942px 宽、内容只占一半，缩放比算错。
        # 画布宽度由 _on_canvas_resize 按实际可用宽度设置。
        canvas.pack(side=tk.TOP, expand=False)
        self.canvas = canvas
        self._hbar = hbar                # 默认隐藏，_sync_hbar 按需显示

        # 键盘主体
        self._draw_grid(canvas, _LAYOUT, x0=_MARGIN, y0=_MARGIN)
        # 右侧导航键区（与主键区第 2 行对齐，视觉上更像真实键盘）
        self._draw_grid(canvas, _NAV_LAYOUT,
                        x0=_MARGIN + _KB_W + _GAP_KBN_NAV,
                        y0=_MARGIN + _CELL_H + _GAP)
        # 鼠标
        self._draw_mouse(canvas, x0=_MOUSE_X, y0=_MARGIN)

        # 宽度跟随外层容器（window 实际可用宽度），而不是画布自己的请求宽度
        canvas.bind("<Configure>", self._on_canvas_resize)
        wrap.bind("<Configure>", self._on_wrap_resize)

        self.legend = tk.StringVar(value="载入脚本后显示用到的按键与鼠标操作")
        tk.Label(self, textvariable=self.legend, bg=self.PANEL_BG, fg="#666",
                 anchor="w", justify="left", wraplength=_CANVAS_W,
                 font=_fs(8)).pack(fill=tk.X, padx=8, pady=(4, 6))

    def _draw_grid(self, canvas: tk.Canvas, layout, x0: int, y0: int) -> None:
        cell_w, cell_h, gap = _CELL_W, _CELL_H, _GAP
        for name, row, col, span in layout:
            x1 = x0 + col * (cell_w + gap)
            y1 = y0 + row * (cell_h + gap)
            x2 = x1 + span * cell_w + (span - 1) * gap
            y2 = y1 + cell_h
            rect = canvas.create_rectangle(
                x1, y1, x2, y2, fill=self.KEY_BG,
                outline=self.BORDER, width=1)
            label = _KEY_LABELS.get(name, name.replace("Key.", "").upper())
            # 宽键（Shift/Ctrl/Tab 等）文字靠左，其余居中，视觉更接近真键盘
            leftish = span >= 2 or name in ("Key.enter", "Key.backspace")
            tx = x1 + 8 if leftish else (x1 + x2) / 2
            anchor = "w" if leftish else "center"
            # 字号随键宽走；再由 _fs 统一按 _FONT_SCALE 折扣
            font_size = 8 if span >= 3 else (9 if span == 2 else 10)
            txt = canvas.create_text(
                tx, (y1 + y2) / 2, text=label, fill=self.KEY_FG,
                font=_fs(font_size), anchor=anchor)
            self._key_ids[name] = rect
            self._label_ids[name] = txt

    def _draw_mouse(self, canvas: tk.Canvas, x0: int, y0: int) -> None:
        """画鼠标：上方左右键，中间滚轮，下方一个醒目的四向滚动指示盘。

        滚轮以前只是一小块淡灰箭头，最难看懂。现在改成：
        - 滚轮本体放大成胶囊形，滚过就整块变绿并写方向汇总
        - 下方四个方向各占一个方块，带大箭头；命中时方块+箭头一起变绿
        - 每个方向块右上角标注次数，一眼看出「滚了几次」
        """
        w = _MOUSE_W
        body_h = 132                       # 鼠标本体高度
        cx = x0 + w / 2
        pad = 20                           # 本体左右内缩，别贴边
        # 鼠标外形：上圆下方
        canvas.create_oval(x0 + pad, y0, x0 + w - pad, y0 + 36,
                           outline=self.BORDER, width=1, fill="#ffffff")
        canvas.create_rectangle(x0 + pad, y0 + 18, x0 + w - pad, y0 + body_h,
                                outline=self.BORDER, width=1, fill="#ffffff")
        canvas.create_line(cx, y0 + 2, cx, y0 + 58, fill=self.BORDER)

        self._mouse_ids = {}
        self._mouse_labels = {}
        for key, (x1, y1, x2, y2, label) in {
            "left": (x0 + pad + 3, y0 + 20, cx - 4, y0 + 58, "左键"),
            "right": (cx + 4, y0 + 20, x0 + w - pad - 3, y0 + 58, "右键"),
        }.items():
            rid = canvas.create_rectangle(x1, y1, x2, y2, fill=self.KEY_BG,
                                          outline=self.BORDER, width=1)
            tid = canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=label,
                                     fill=self.KEY_FG, font=_fs(9))
            self._mouse_ids[key] = rid
            self._mouse_labels[key] = tid

        # ---- 滚轮本体：加大的胶囊形，比左右键明显 ----
        wy0, wy1 = y0 + 28, y0 + 82
        self._wheel_id = canvas.create_rectangle(
            cx - 17, wy0, cx + 17, wy1,
            fill=self.KEY_BG, outline=self.BORDER, width=2)
        self._wheel_text = canvas.create_text(
            cx, (wy0 + wy1) / 2, text="滚轮", fill=self.KEY_FG,
            font=_fs(10, bold=True))

        # ---- 四向指示盘：每向一个方块 + 大箭头 + 角标次数 ----
        ay = y0 + body_h + 46
        half_w, half_h = 40, 30
        self._arrow_ids = {}
        self._arrow_bg = {}
        self._arrow_count = {}
        dirs = {
            "up":    (cx,      ay - 44, "↑"),
            "down":  (cx,      ay + 44, "↓"),
            "left":  (cx - 62, ay,      "←"),
            "right": (cx + 62, ay,      "→"),
        }
        for name, (px, py, glyph) in dirs.items():
            box = canvas.create_rectangle(px - half_w, py - half_h,
                                          px + half_w, py + half_h,
                                          fill="#f2f3f5", outline=self.BORDER)
            arr = canvas.create_text(px, py, text=glyph, fill="#c2c7cd",
                                     font=_fs(22, bold=True))
            cnt = canvas.create_text(px + half_w - 7, py - half_h + 5, text="",
                                     fill="#ffffff",
                                     font=_fs(10, bold=True),
                                     anchor="ne")
            self._arrow_bg[name] = box
            self._arrow_ids[name] = arr
            self._arrow_count[name] = cnt
        # 中心标：说明这四个方块是滚轮方向
        canvas.create_text(cx, ay, text="滚\n轮", fill="#9aa0a6",
                           font=_fs(9), justify="center")

    # ---------- 缩放适配 ----------
    def _on_wrap_resize(self, event) -> None:
        """外层容器变宽/变窄时，同步画布宽度，再触发一次缩放计算。"""
        try:
            self.canvas.configure(width=max(320, int(event.width)))
        except Exception:
            pass

    def _on_canvas_resize(self, event) -> None:
        """窗口尺寸变化时，把画布内容等比缩放，既铺满宽度又不过高。

        只缩不放：窗口很宽时保持自然尺寸，避免键位被拉得过大失真。
        另外按可用高度再压一次上限 —— 控制台页上方还有编辑区和定时区，
        可视化占太高会把它们挤出窗口（用户截图里表格只剩半截行）。
        """
        avail_w = max(320, int(event.width) - 4)
        scale = min(1.0, avail_w / float(_CANVAS_W))
        # 高度上限：高度不够时可以适度缩小，但**不能小到看不清**。
        # 之前按 max_h/_CANVAS_H 硬压（0.44），键宽只剩 25px、字糊成一团。
        # 这里设下限 0.72（键宽仍有 ~42px），再不够就靠横向滚动条兜住。
        max_h = self._max_h or 10 ** 6
        if _CANVAS_H * scale > max_h:
            scale = max(0.72, max_h / float(_CANVAS_H))
        if abs(scale - self._scale) < 0.01 and self._scaled_for == (avail_w, max_h):
            return
        # 先还原到自然尺寸，再按新比例缩放（scale 是相对当前值累加的）
        self.canvas.scale("all", 0, 0, 1.0 / self._scale, 1.0 / self._scale)
        self._scale = scale
        if abs(scale - 1.0) > 0.001:
            self.canvas.scale("all", 0, 0, scale, scale)
        self._scaled_for = (avail_w, max_h)
        # 内容比视口宽时（缩到下限还不够），放开横向滚动条而不是继续缩；
        # 内容比视口窄时，画布自身宽度收窄到内容宽，避免右侧留一大片空白。
        content_w = int(_CANVAS_W * scale)
        content_h = int(_CANVAS_H * scale)
        self.canvas.configure(
            height=max(150, content_h),
            scrollregion=(0, 0, max(avail_w, content_w), content_h))
        self._sync_hbar(avail_w, content_w)

    def _sync_hbar(self, avail_w: int, content_w: int) -> None:
        """按需显示/隐藏横向滚动条。"""
        hbar = getattr(self, "_hbar", None)
        if hbar is None:
            return
        need = content_w > avail_w + 2
        if need and not hbar.winfo_ismapped():
            hbar.pack(side=tk.BOTTOM, fill=tk.X)
        elif not need and hbar.winfo_ismapped():
            hbar.pack_forget()

    def set_max_height(self, pixels: int) -> None:
        """由外部（控制台页）限制可视化最大高度，避免挤掉上方区域。

        传 0 表示不限制（恢复自然高度）。
        """
        self._max_h = 0 if not pixels else max(120, int(pixels))
        self._scaled_for = None           # 强制下次 Configure 重算

    # ---------- 刷新 ----------
    def show_step(self, summary: Optional[VisualSummary], step_no: int = 0,
                  total_steps: int = 0, kind: str = "") -> None:
        """显示表格里选中的那一步；没选中时传 summary=None（全灰）。

        与 show_summary 的区别只在文案：这里强调「第 N / 共 M 步」，
        让人清楚图示此刻对应表格里的哪一行。
        """
        detail = ""
        if summary is not None:
            bits = []
            if summary.key_presses:
                for k, n in summary.key_presses.items():
                    label = _KEY_LABELS.get(k, k.replace("Key.", "").upper())
                    bits.append(f"按下「{label}」" + (f" ×{n}" if n > 1 else ""))
            for k, n in summary.buttons.items():
                label = _MOUSE_BUTTON_LABELS.get(k, k)
                bits.append(f"点{label}" + (f" ×{n}" if n > 1 else ""))
            if summary.wheel_up:
                bits.append(f"滚轮 ↑{summary.wheel_up}")
            if summary.wheel_down:
                bits.append(f"滚轮 ↓{summary.wheel_down}")
            if summary.wheel_left:
                bits.append(f"滚轮 ←{summary.wheel_left}")
            if summary.wheel_right:
                bits.append(f"滚轮 →{summary.wheel_right}")
            detail = "、".join(bits)
        self.show_summary(summary, f"第 {step_no} 步 / 共 {total_steps} 步", detail)

    def show_summary(self, summary: Optional[VisualSummary],
                     source_name: str = "", detail: str = "") -> None:
        """把统计结果画到图示上；传 None 则清空（全灰）。"""
        # 先全部复位
        for name, rid in self._key_ids.items():
            self.canvas.itemconfigure(rid, fill=self.KEY_BG)
            self.canvas.itemconfigure(self._label_ids[name], fill=self.KEY_FG)
            self.canvas.itemconfigure(self._label_ids[name], text=_KEY_LABELS.get(
                name, name.replace("Key.", "").upper()))
        for key, rid in self._mouse_ids.items():
            self.canvas.itemconfigure(rid, fill=self.KEY_BG)
            self.canvas.itemconfigure(self._mouse_labels[key], fill=self.KEY_FG)
            self.canvas.itemconfigure(
                self._mouse_labels[key],
                text={"left": "左键", "right": "右键"}.get(key, key))
        self.canvas.itemconfigure(self._wheel_id, fill=self.KEY_BG)
        self.canvas.itemconfigure(self._wheel_text, fill=self.KEY_FG)
        for name, aid in self._arrow_ids.items():
            self.canvas.itemconfigure(aid, fill="#c2c7cd")
            self.canvas.itemconfigure(self._arrow_bg[name], fill="#f2f3f5")
            self.canvas.itemconfigure(self._arrow_count[name], text="")
        self.canvas.itemconfigure(self._wheel_text, text="滚轮")
        self.canvas.itemconfigure(self._wheel_text, font=_fs(10, bold=True))

        if summary is None or not summary.total:
            self.title_var.set("键鼠动作（不含鼠标移动）")
            self.legend.set("点上方表格里任意一步，这里就显示那一步的按键与鼠标操作"
                            if summary is None
                            else (detail or "这一步是鼠标移动或抬键，没有可显示的键鼠动作"))
            return

        # 键盘：用到的键点亮，并在键位上叠加次数
        for name, count in summary.key_presses.items():
            rid = self._key_ids.get(name)
            if rid is None:
                continue                     # 布局表里没有的键（如 VK.xxx）不画
            self.canvas.itemconfigure(rid, fill=self.KEY_USED_BG)
            self.canvas.itemconfigure(self._label_ids[name], fill=self.KEY_USED_FG)
            base = _KEY_LABELS.get(name, name.replace("Key.", "").upper())
            if count > 1:
                self.canvas.itemconfigure(self._label_ids[name], text=f"{base}×{count}")

        # 鼠标按键
        for key, count in summary.buttons.items():
            rid = self._mouse_ids.get(key)
            if rid is None:
                continue                     # middle/x1/x2 图示里没有
            self.canvas.itemconfigure(rid, fill=self.KEY_USED_BG)
            self.canvas.itemconfigure(self._mouse_labels[key], fill=self.KEY_USED_FG)
            label = {"left": "左键", "right": "右键"}.get(key, key)
            self.canvas.itemconfigure(self._mouse_labels[key],
                                      text=f"{label}×{count}" if count > 1 else label)

        # 滚轮方向
        arrows = {"up": summary.wheel_up, "down": summary.wheel_down,
                  "left": summary.wheel_left, "right": summary.wheel_right}
        total_wheel = sum(arrows.values())
        if total_wheel:
            self.canvas.itemconfigure(self._wheel_id, fill=self.KEY_USED_BG)
            self.canvas.itemconfigure(self._wheel_text, fill=self.KEY_USED_FG)
            for direction, n in arrows.items():
                if not n:
                    continue
                # 方向块、箭头、角标次数一起点亮，让「往哪滚、滚几次」一目了然
                self.canvas.itemconfigure(self._arrow_bg[direction], fill="#1a7f37")
                self.canvas.itemconfigure(self._arrow_ids[direction], fill="#ffffff")
                self.canvas.itemconfigure(self._arrow_count[direction],
                                          text=("×%d" % n) if n > 1 else "✓")
            # 滚轮本体上直接写方向汇总，例如 "↓2"
            parts = []
            if summary.wheel_up:
                parts.append(f"↑{summary.wheel_up}")
            if summary.wheel_down:
                parts.append(f"↓{summary.wheel_down}")
            if summary.wheel_left:
                parts.append(f"←{summary.wheel_left}")
            if summary.wheel_right:
                parts.append(f"→{summary.wheel_right}")
            self.canvas.itemconfigure(self._wheel_text, text="/".join(parts))
            self.canvas.itemconfigure(self._wheel_text, font=_fs(9, bold=True))

        # 标题与图例
        self.title_var.set(f"键鼠动作（{source_name or '当前脚本'}）")
        if detail:
            # 单步模式：直接说明这一步干了什么
            self.legend.set(f"本步操作：{detail}")
            return
        bits = []
        if summary.key_presses:
            bits.append(f"按键 {len(summary.key_presses)} 种"
                        f"（共 {sum(summary.key_presses.values())} 次）")
        if summary.buttons:
            btn_txt = "、".join(f"{_MOUSE_BUTTON_LABELS.get(k, k)}{v}次"
                               for k, v in summary.buttons.items())
            bits.append(f"鼠标 {btn_txt}")
        if total_wheel:
            bits.append(f"滚轮 {total_wheel} 次")
        if summary.positions:
            bits.append(f"涉及 {summary.positions} 个坐标点")
        bits.append(f"已忽略 {summary.skipped_move} 个移动事件")
        self.legend.set(" ｜ ".join(bits))

    def clear(self) -> None:
        self.show_summary(None)