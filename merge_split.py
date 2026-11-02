"""合并 / 拆分 的交互弹窗。

- MergeDialog：列出待合并脚本，**可拖动排序** + 设置脚本间间隔，确认后另存。
- SplitDialog：用滑块选拆分时刻（秒），实时显示切在哪个事件之后，确认后另存两份。

两者都用 ``result`` 属性回传结果，调用方 wait_window 后读取：
  MergeDialog.result -> (order: list[str], gap: float, name: str) | None
  SplitDialog.result -> (cut_sec: float, name: str) | None
"""
from __future__ import annotations

import os
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import List, Optional, Tuple

import common


def _script_duration(name: str) -> Tuple[int, float]:
    """返回 (事件数, 总时长秒)；读取失败返回 (0, 0.0)。"""
    try:
        evs = common.load_events(os.path.join(common.RECORDS_DIR, name))
    except Exception:
        return 0, 0.0
    return len(evs), common.count_duration(evs)


class _DialogBase(tk.Toplevel):
    """弹窗基类：居中于父窗口、模态、Esc 取消。"""

    def __init__(self, parent, title: str, size: str) -> None:
        super().__init__(parent)
        self.title(title)
        self.result = None
        self.transient(parent.winfo_toplevel())
        self.resizable(True, True)
        self.geometry(self._centered(parent, size))
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.grab_set()

    @staticmethod
    def _centered(parent, size: str) -> str:
        top = parent.winfo_toplevel()
        top.update_idletasks()
        w, h = (int(v) for v in size.split("x"))
        x = top.winfo_rootx() + (top.winfo_width() - w) // 2
        y = top.winfo_rooty() + (top.winfo_height() - h) // 2
        return f"{size}+{max(0, x)}+{max(0, y)}"

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _buttons(self, frame, on_ok, ok_text: str = "确定") -> None:
        row = ttk.Frame(frame)
        row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(row, text=ok_text, width=10, command=on_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(row, text="取消", width=8, command=self._cancel).pack(side=tk.RIGHT)


# ===================== 合并 =====================
class MergeDialog(_DialogBase):
    """合并脚本：可拖动排序 + 设置间隔 + 命名。"""

    def __init__(self, parent, files: List[str]) -> None:
        super().__init__(parent, "合并脚本 — 拖动调整顺序", "560x480")
        self.files = list(files)          # 按用户选择顺序作初始顺序
        self._drag_iid: Optional[str] = None

        body = ttk.Frame(self, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="合并顺序（上下拖动可调整，上面的先执行）：",
                  foreground="#0b5394").pack(anchor="w")

        tv_wrap = ttk.Frame(body)
        tv_wrap.pack(fill=tk.BOTH, expand=True, pady=(4, 6))
        self.tree = ttk.Treeview(tv_wrap, columns=("ord", "name", "steps", "dur"),
                                 show="headings", height=8)
        for col, title, w, anchor in (("ord", "顺序", 50, "center"),
                                      ("name", "脚本", 260, "w"),
                                      ("steps", "步数", 70, "e"),
                                      ("dur", "时长", 80, "e")):
            self.tree.heading(col, text=title, anchor=anchor)
            self.tree.column(col, width=w, anchor=anchor,
                             stretch=(col == "name"))
        sb = ttk.Scrollbar(tv_wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # 拖动排序：按下记行号，拖动中根据落点调整位置
        self.tree.bind("<ButtonPress-1>", self._drag_start)
        self.tree.bind("<B1-Motion>", self._drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._drag_end)

        ttk.Label(body, text="索引位置含义：上面的脚本先播放", foreground="#666").pack(anchor="w")

        opt = ttk.Frame(body)
        opt.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(opt, text="脚本间间隔（秒）").pack(side=tk.LEFT)
        self.gap_var = tk.StringVar(value="2.0")
        ttk.Spinbox(opt, from_=0.0, to=600.0, increment=0.5, width=7,
                    textvariable=self.gap_var).pack(side=tk.LEFT, padx=6)
        ttk.Label(opt, text="（加到下一段首步的延迟上）",
                  foreground="#666").pack(side=tk.LEFT)

        name_row = ttk.Frame(body)
        name_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(name_row, text="另存为").pack(side=tk.LEFT)
        self.name_var = tk.StringVar(value=time.strftime("merged_%H%M%S"))
        ttk.Entry(name_row, textvariable=self.name_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        self._buttons(body, self._ok, "合并并另存")
        self._refresh()

    # ---------- 列表 ----------
    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, name in enumerate(self.files):
            steps, dur = _script_duration(name)
            self.tree.insert("", tk.END, iid=str(i), values=(
                i + 1, name, steps, f"{dur:.1f}s"))

    def _drag_start(self, e) -> None:
        iid = self.tree.identify_row(e.y)
        self._drag_iid = iid if iid else None

    def _drag_motion(self, e) -> None:
        if not self._drag_iid:
            return
        target = self.tree.identify_row(e.y)
        if not target or target == self._drag_iid:
            return
        cur = int(self._drag_iid)
        dst = int(target)
        # 交换两项在 files 里的位置，再把 tree 行整体重排
        self.files.insert(dst, self.files.pop(cur))
        self._drag_iid = str(dst)
        self._refresh()
        self.tree.selection_set(str(dst))

    def _drag_end(self, _e) -> None:
        self._drag_iid = None

    # ---------- 确认 ----------
    def _ok(self) -> None:
        try:
            gap = max(0.0, float(self.gap_var.get()))
        except ValueError:
            messagebox.showwarning("参数无效", "间隔必须是数字（秒）", parent=self)
            return
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("缺少名称", "请填写新脚本名", parent=self)
            return
        self.result = (list(self.files), gap, name)
        self.destroy()


# ===================== 拆分 =====================
class SplitDialog(_DialogBase):
    """按时间点拆分：滑块选秒数，实时显示切在哪个事件之后。"""

    def __init__(self, parent, events: list, base_name: str) -> None:
        super().__init__(parent, "拆分脚本 — 选择拆分时刻", "560x400")
        self.events = list(events)
        # 每个事件的累计时间点，用来把秒数换算成事件下标
        self.cum: List[float] = []
        t = 0.0
        for ev in self.events:
            t += float(ev[1])
            self.cum.append(t)
        self.total = self.cum[-1] if self.cum else 0.0
        # 默认切点取「中间那个事件之后」，而不是总时长的一半 ——
        # 事件时间不均匀时（如 0.1/0.05），一半可能落在两事件之间，
        # 导致全部归到一段、默认就显示无效提示。默认应是个有效位置。
        default_cut = self._valid_default()

        body = ttk.Frame(self, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text=f"脚本共 {len(self.events)} 步，总时长 {self.total:.2f} 秒",
                  foreground="#0b5394").pack(anchor="w")
        ttk.Label(body, text="拖动滑块或输入秒数，选定拆分时刻：",
                  foreground="#666").pack(anchor="w", pady=(4, 2))

        self.cut_var = tk.DoubleVar(value=default_cut)
        self.slider = ttk.Scale(body, from_=0.01, to=max(0.02, self.total),
                                variable=self.cut_var, orient=tk.HORIZONTAL,
                                command=lambda _v: self._update_preview())
        self.slider.pack(fill=tk.X, pady=(0, 6))

        row = ttk.Frame(body)
        row.pack(fill=tk.X)
        ttk.Label(row, text="拆分时刻(秒)").pack(side=tk.LEFT)
        self.entry_var = tk.StringVar(value=f"{default_cut:.2f}")
        ent = ttk.Entry(row, textvariable=self.entry_var, width=10)
        ent.pack(side=tk.LEFT, padx=6)
        ent.bind("<Return>", lambda _e: self._from_entry())
        ttk.Button(row, text="应用", width=6,
                   command=self._from_entry).pack(side=tk.LEFT)

        self.preview_var = tk.StringVar()
        ttk.Label(body, textvariable=self.preview_var, justify=tk.LEFT,
                  foreground="#333", wraplength=520).pack(anchor="w", pady=(8, 0))

        # 两段各起一个名字，点确定才落盘生效
        name_a = ttk.Frame(body)
        name_a.pack(fill=tk.X, pady=(10, 2))
        ttk.Label(name_a, text="前段名称", width=8).pack(side=tk.LEFT)
        self.name_a_var = tk.StringVar(value=f"{base_name}_A")
        ttk.Entry(name_a, textvariable=self.name_a_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Label(name_a, text="（第 1 段）", foreground="#666").pack(side=tk.LEFT)

        name_b = ttk.Frame(body)
        name_b.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(name_b, text="后段名称", width=8).pack(side=tk.LEFT)
        self.name_b_var = tk.StringVar(value=f"{base_name}_B")
        ttk.Entry(name_b, textvariable=self.name_b_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Label(name_b, text="（第 2 段）", foreground="#666").pack(side=tk.LEFT)

        self._buttons(body, self._ok, "确定（拆分并另存）")
        self._update_preview()

    def _valid_default(self) -> float:
        """挑一个能真正把事件分成两段的默认切点。

        取中间事件的下标 m，切点取 cum[m-1] 与 cum[m] 之间（略偏 cum[m-1]），
        这样下标 _split_index 恰好返回 m。事件数 < 2 时退回总时长一半。
        """
        n = len(self.cum)
        if n < 2:
            return max(0.01, self.total / 2)
        m = max(1, n // 2)
        lo, hi = self.cum[m - 1], self.cum[m]
        # 落在两事件之间；若两者时间相同（首步延迟为 0），加个极小偏移
        cut = lo + (hi - lo) / 2 if hi > lo else lo + 1e-6
        return max(0.01, cut)

    def _from_entry(self) -> None:
        try:
            v = float(self.entry_var.get())
        except ValueError:
            return
        v = max(0.01, min(self.total, v))
        self.cut_var.set(v)
        self._update_preview()

    def _split_index(self, cut: float) -> int:
        """返回第二段的起始下标（第一个累计时间 > cut 的事件）。"""
        for i, t in enumerate(self.cum):
            if t > cut:
                return i
        return len(self.cum)

    def _update_preview(self) -> None:
        cut = float(self.cut_var.get())
        self.entry_var.set(f"{cut:.2f}")
        idx = self._split_index(cut)
        if idx <= 0 or idx >= len(self.events):
            self.preview_var.set("⚠️ 该时刻会把全部事件分到一边，请挪动滑块选中间位置")
            return
        a_ev = self.events[idx - 1]
        b_ev = self.events[idx]
        def desc(ev):
            kind = ev[0]
            if kind in ("click", "release"):
                return f"{kind}/{str(ev[4]).replace('Button.','')}"
            if kind == "wheel":
                return "wheel"
            if kind in ("key_down", "key_up"):
                return f"{kind} {ev[2]}"
            return kind
        self.preview_var.set(
            f"A 段：第 1 — {idx} 步（{len(self.events[:idx])} 步，"
            f"{self.cum[idx-1]:.2f}s 结束，最后一步 {desc(a_ev)}）\n"
            f"B 段：第 {idx+1} — {len(self.events)} 步（{len(self.events[idx:])} 步，"
            f"开头是 {desc(b_ev)}）")

    def _ok(self) -> None:
        cut = float(self.cut_var.get())
        idx = self._split_index(cut)
        if idx <= 0 or idx >= len(self.events):
            messagebox.showwarning("拆分时刻无效",
                                   "该时刻会把全部事件分到一边，请重新选择。",
                                   parent=self)
            return
        name_a = self.name_a_var.get().strip()
        name_b = self.name_b_var.get().strip()
        if not name_a or not name_b:
            messagebox.showwarning("缺少名称", "前段和后段的名称都不能为空", parent=self)
            return
        if name_a == name_b:
            messagebox.showwarning("名称重复", "前段与后段不能同名", parent=self)
            return
        self.result = (cut, name_a, name_b)
        self.destroy()
