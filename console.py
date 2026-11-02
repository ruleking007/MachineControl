"""脚本控制台：定时执行 + 步长编辑 + 合并拆分。

挂在主面板的新标签页，回放执行复用录制页的 Player（同一引擎，F7 同样能终止）。

三块功能：
1. 脚本编辑：加载任意录制文件，逐步查看/修改每步前置延迟，或按倍率整体缩放；
   保存回原文件或另存为新脚本（含 合并/拆分 产物）。
2. 合并：脚本列表多选 → 按列表顺序串接；衔接方式是把"脚本间等待"加到
   下一段首个事件的延迟上（不引入新事件类型，保持录制文件格式纯净）。
3. 拆分：选中某一步作为分界，该步起归入第二段，两段各存为新脚本。
4. 定时：任务表（间隔循环 / 每日 / 一次性），主线程 after 每秒轮询到期任务，
   到点调用录制页执行引擎跑脚本；任务持久化到 schedule_tasks.json。

所有操作都在主线程；无自建线程。
"""
from __future__ import annotations

import json
import os
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox, simpledialog, ttk

import common

common.enable_dpi_awareness()

import script_visual  # 键鼠图示（键盘 + 鼠标静态可视化）
import merge_split    # 合并/拆分弹窗

# 定时任务表存到用户数据目录：钉在 BASE_DIR 时，重新打包会随程序目录一起被替换，
# 用户辛苦设的每日任务就没了（录制文件踩过同样的坑）。
TASKS_FILE = common.data_path("schedule_tasks.json")
TICK_MS = 1000

MODE_INTERVAL = "间隔循环"
MODE_DAILY = "每日"
MODE_ONCE = "一次性"


# ===================== 小工具 =====================
def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def _write_events(path: str, events: list) -> None:
    payload = [list(e) for e in events]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


def _safe_name(name: str) -> str:
    keep = [c for c in name.strip() if c.isalnum() or c in "-_（）()"]
    return "".join(keep)[:60] or time.strftime("script_%H%M%S")


def _unique_path(base: str) -> str:
    """records/<base>.json，重名自动加序号。"""
    common.ensure_dirs()
    candidate = os.path.join(common.RECORDS_DIR, base + ".json")
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(common.RECORDS_DIR, f"{base}_{n}.json")
        n += 1
    return candidate


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    try:
        h, m = text.strip().split(":")[:2]
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def _parse_dt(text: str) -> float | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text.strip(), fmt)
            if dt.year == 1900:                       # 只写了月日时分 → 补当前年
                dt = dt.replace(year=datetime.now().year)
            return dt.timestamp()
        except ValueError:
            continue
    return None


class ScriptConsoleApp(ttk.Frame):
    """主控面板的「脚本控制台」页。recorder_app 为录制页实例（执行引擎宿主）。"""

    def __init__(self, master: tk.Misc, recorder_app) -> None:
        super().__init__(master)
        self.pack(fill=tk.BOTH, expand=True)
        self.engine = recorder_app            # RecorderApp：借用其 Player / file_var
        self._alive = True
        self._tick_job: str | None = None  # 待执行轮询任务，shutdown 时撤销

        # ---- 编辑区状态 ----
        self.edit_path: str | None = None     # 当前编辑中的脚本绝对路径
        self.edit_events: list = []
        self.dirty = False
        self.kb_view = None                   # 键鼠图示控件，_build_ui 里创建

        self.status_var = tk.StringVar(value="准备就绪")
        self.scale_var = tk.DoubleVar(value=1.0)
        self.gap_var = tk.DoubleVar(value=1.0)
        self.merge_wait_var = tk.DoubleVar(value=2.0)

        # ---- 定时任务 ----
        self.tasks: list[dict] = []
        self._load_tasks()

        self._build_ui()
        self.refresh_files()
        self._render_tasks()
        self._tick_job = self.after(TICK_MS, self._tick)

    # ================= UI =================
    def _build_ui(self) -> None:
        # 本页内容天然比窗口高（编辑区 420 + 右栏两块 460+430 + 可视化 340）。
        # 策略：定时区是高频功能，必须开窗即见；合并/拆分是低频的，
        # 做成紧凑条并排在定时区上方，超出的部分交给整页滚动。
        outer, body = common.make_scrollable(self)
        outer.pack(fill=tk.BOTH, expand=True)

        # 布局要点（Tk packer 的坑，都在这里踩过）：
        # 1. 带 expand=True 的控件会吃掉剩余空间，pack 顺序反了会有控件变 1x1。
        # 2. 左右两栏：先 pack 右栏（固定宽度），再让左栏 expand 吃剩余宽度；
        #    反过来右栏拿不到横向空间，里面控件全被压成 1x1。
        top = ttk.Frame(body)
        left = ttk.LabelFrame(top, text="脚本编辑（逐步延迟 / 倍率 / 保存）", padding=6)
        right = ttk.Frame(top)
        self.kb_view = script_visual.KeyboardMouseView(body)
        # 不限高：整页已在滚动容器里，可视化按宽度自然缩放即可。
        # 之前设限高 150 会把它压到 0.44 缩放（键宽只剩 25px、字糊成一团），
        # 用户看到的就是「键鼠动作又挤在一起」。
        self.kb_view.set_max_height(0)

        self.kb_view.pack(fill=tk.X, side=tk.BOTTOM, pady=(6, 0))
        # 在滚动容器里，top 按内容自然高度排布，不要 expand 吃视口高度；
        # 超出视口的部分由外层滚动条兜住。
        top.pack(fill=tk.X, side=tk.TOP)
        # 左右两栏用 grid 而不是 pack：pack 下右栏会按内容自然宽度膨胀
        # （实测吃掉 956px，把左栏的脚本编辑区挤到 174px）。
        # 比例 1:1 —— 右栏定时表要完整显示 5 列（含「10-30 08:30:00」这类长文本），
        # 之前的 5:4 让右栏只剩 470px，参数/下次触发会被硬切掉尾部的 "00"。
        top.grid_columnconfigure(0, weight=1, uniform="col")
        top.grid_columnconfigure(1, weight=1, uniform="col")
        left.grid(row=0, column=0, sticky="nsew")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        top.grid_rowconfigure(0, weight=1)

        row = ttk.Frame(left)
        row.pack(fill=tk.X, pady=(0, 4))
        self.edit_combo_var = tk.StringVar()
        self.edit_combo = ttk.Combobox(row, textvariable=self.edit_combo_var,
                                       state="readonly", width=20)
        # 按钮先 pack、下拉框后 pack 并 expand：反过来下拉框会把宽度吃光，
        # 两个按钮被压成 1px（文字完全看不见）。
        ttk.Button(row, text="载入", command=self.load_selected).pack(side=tk.RIGHT, padx=(2, 0))
        ttk.Button(row, text="🔄", width=3,
                   command=self.refresh_files).pack(side=tk.RIGHT, padx=2)
        self.edit_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)

        cols = ("idx", "kind", "delay", "args")
        # 表格放进独立容器 tree_box：容器占满剩余高度，表格与滚动条在容器内左右排。
        # 这样下面的工具条能拿到完整宽度（以前 tree 直接 side=LEFT 横向膨胀，
        # 把工具条挤成 65px，按钮文字被切）。
        # 重要：Treeview 必须**构造时父容器就是 tree_box** —— 若先以 left 为父
        # 再用 pack(in_=...) 二次挂载，控件尺寸看着正常但内容完全不绘制
        # （实测整块灰底、白色占比 0%，表现为「一片空白」）。
        tree_box = ttk.Frame(left)
        tree_box.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(tree_box, columns=cols, show="headings", height=8)
        # 四栏等分（用户要求「4栏平均宽」）。唯一例外是「参数」列设 stretch：
        # 它的内容最长，窗口变宽时吃额外空间，避免长参数被硬切又没有省略提示。
        for c, t, anchor in (("idx", "#", "e"), ("kind", "类型", "w"),
                             ("delay", "延迟(秒)", "e"), ("args", "参数", "w")):
            self.tree.heading(c, text=t, anchor=anchor)
            self.tree.column(c, width=112, minwidth=48, stretch=(c == "args"),
                             anchor=anchor)
        scroll = ttk.Scrollbar(tree_box, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda _e: self.edit_step_delay())
        # 点/切行即刷新键鼠图示：<<TreeviewSelect>> 覆盖鼠标点选与键盘上下键
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_visual())

        # 工具条分两行 + 缩短按钮文字：225% DPI 下「💾 保存(覆盖原文件)」单个就要 257px，
        # 原来横着一排放不下，Tk 会把按钮压扁到 93px 甚至 1px（文字被切一半）。
        tool = ttk.Frame(left, padding=(0, 4))
        tool.pack(fill=tk.X)
        r1 = ttk.Frame(tool)
        r1.pack(fill=tk.X)
        ttk.Button(r1, text="✏ 改延迟", command=self.edit_step_delay).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(r1, text="倍率").pack(side=tk.LEFT, padx=(4, 2))
        ttk.Spinbox(r1, from_=0.1, to=10.0, increment=0.1, width=4,
                    textvariable=self.scale_var).pack(side=tk.LEFT)
        ttk.Button(r1, text="应用", command=self.scale_all).pack(side=tk.LEFT, padx=4)

        r2 = ttk.Frame(tool)
        r2.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(r2, text="💾 保存", command=self.save_inplace).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(r2, text="另存为…", command=self.save_as).pack(side=tk.LEFT)

        # 右栏的容器在 _build_ui 开头已创建并 pack（见那里的 pack 顺序说明），
        # 这里不能再新建一个 —— 那会得到一个没被放进布局的空 Frame，
        # 里面的合并/定时控件全都看不见。
        merge = ttk.LabelFrame(right, text="脚本合并 / 拆分", padding=4)
        # 合并是低频功能，压成「列表 2 行 + 一行操作」的紧凑条，
        # 把纵向空间让给定时的四个操作按钮（它们贴着视口底边，差 1px 就点不到）。
        self.merge_list = tk.Listbox(merge, selectmode=tk.EXTENDED, exportselection=False,
                                     height=2, width=26, font=("Microsoft YaHei UI", 8))
        self.merge_list.pack(fill=tk.X, pady=(0, 2))
        row_m = ttk.Frame(merge)
        row_m.pack(fill=tk.X)
        ttk.Label(row_m, text="等待", font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        ttk.Spinbox(row_m, from_=0.0, to=600.0, increment=0.5, width=4,
                    textvariable=self.merge_wait_var).pack(side=tk.LEFT, padx=2)
        ttk.Button(row_m, text="➕ 合并", width=7,
                   command=self.merge_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(row_m, text="✂ 拆分", width=7,
                   command=self.split_script).pack(side=tk.LEFT)

        sched = ttk.LabelFrame(right, text="定时执行", padding=6)
        # 右栏两块面板都按「内容自然高度」排布，不要 expand ——
        # 有 expand 的那块会把右栏高度吃光，另一块被压成 1x1 完全看不见。
        # 总高超出视口由外层滚动容器兜住。
        merge.pack(fill=tk.X, side=tk.TOP)
        sched.pack(fill=tk.X, side=tk.TOP, pady=(4, 0))
        # 行数 3：行高按字体行距自动放大（高 DPI 下约 43px/行），
        # 4 行会把下面的表单和按钮顶出视口。
        self.task_tree = ttk.Treeview(sched, columns=("file", "plan", "detail", "next", "st"),
                                      show="headings", height=3)
        # 列宽：两栏各半后右栏约 550px，5 列按内容实际需要分配。
        # 「下次触发」要放「10-30 08:30:00」共 14 字符（约 145px @225%DPI），
        # 「参数」要放「每天 08:30」（约 125px），都留足余量，避免尾部 "00" 被切。
        # 「状态」只放「启用/停用/运行中」，固定窄宽、不参与拉伸。
        for col, title, width, stretch, anchor, minw in (
                ("file",   "脚本",      92, False, "w", 70),
                ("plan",   "策略",      72, False, "w", 64),
                ("detail", "参数",     128, True,  "w", 120),
                ("next",   "下次触发", 152, True,  "w", 145),
                ("st",     "状态",      64, False, "w", 64)):
            self.task_tree.heading(col, text=title, anchor=anchor)
            self.task_tree.column(col, width=width, stretch=stretch,
                                  minwidth=minw, anchor=anchor)
        self.task_tree.pack(fill=tk.BOTH, expand=True)
        self.task_tree.bind("<<TreeviewSelect>>", lambda _e: self._task_selected())

        form = ttk.Frame(sched, padding=(0, 4))
        form.pack(fill=tk.X)
        self.task_file_var = tk.StringVar()
        self.task_file_combo = ttk.Combobox(form, textvariable=self.task_file_var,
                     values=common.list_recordings(),
                     state="readonly", width=30)
        self.task_file_combo.grid(row=0, column=0, columnspan=2, sticky="w")
        self.task_mode_var = tk.StringVar(value=MODE_INTERVAL)
        # 注意必须用 variable= 而不是 textvariable=：
        # textvariable 会把按钮的显示文字绑到变量上，导致三个按钮全都显示
        # 当前选中的模式名（都变成「每日」），完全分不清哪个是哪个。
        # 正确做法是 variable 绑定值 + text 固定标签。
        self.mode_radios = []
        for col, (val, label) in enumerate((
                (MODE_INTERVAL, "间隔(分)"),
                (MODE_DAILY, "每日"),
                (MODE_ONCE, "一次性"))):
            rb = ttk.Radiobutton(form, variable=self.task_mode_var, value=val,
                                 text=label, command=self._on_mode_changed)
            rb.grid(row=1, column=col, sticky="w", padx=(0, 8))
            self.mode_radios.append(rb)

        # 参数区：三种模式各有一套控件，切换模式时只显示对应的那套。
        # 「每日」用两个 Spinbox 选时分（可点箭头也可直接键入数字），
        # 比让人手打 "08:30" 更省事、也不会格式写错。
        self.param_box = ttk.Frame(form)
        self.param_box.grid(row=2, column=0, columnspan=3, sticky="w", pady=2)
        self.task_param_var = tk.StringVar(value="60")     # 间隔模式
        self.interval_widgets = ttk.Frame(self.param_box)
        self.entry_interval = ttk.Entry(self.interval_widgets,
                                        textvariable=self.task_param_var, width=8)
        self.entry_interval.pack(side=tk.LEFT)
        ttk.Label(self.interval_widgets, text=" 分钟").pack(side=tk.LEFT)

        # 每日：时 / 分 两个 Spinbox + 冒号
        self.daily_h_var = tk.StringVar(value="08")
        self.daily_m_var = tk.StringVar(value="30")
        self.daily_widgets = ttk.Frame(self.param_box)
        ttk.Spinbox(self.daily_widgets, from_=0, to=23, wrap=True, width=3,
                    textvariable=self.daily_h_var, format="%02.0f",
                    font=("Consolas", 10)).pack(side=tk.LEFT)
        ttk.Label(self.daily_widgets, text=" 时 : ").pack(side=tk.LEFT)
        ttk.Spinbox(self.daily_widgets, from_=0, to=59, wrap=True, width=3,
                    textvariable=self.daily_m_var, format="%02.0f",
                    font=("Consolas", 10)).pack(side=tk.LEFT)
        ttk.Label(self.daily_widgets, text=" 分").pack(side=tk.LEFT)
        ttk.Button(self.daily_widgets, text="现在", width=5,
                   command=self._fill_daily_now).pack(side=tk.LEFT, padx=(8, 0))

        # 一次性：日期 + 时分
        self.once_date_var = tk.StringVar(value=time.strftime("%Y-%m-%d"))
        self.once_h_var = tk.StringVar(value=time.strftime("%H"))
        self.once_m_var = tk.StringVar(value=time.strftime("%M"))
        self.once_widgets = ttk.Frame(self.param_box)
        ttk.Entry(self.once_widgets, textvariable=self.once_date_var, width=11).pack(side=tk.LEFT)
        ttk.Spinbox(self.once_widgets, from_=0, to=23, wrap=True, width=3,
                    textvariable=self.once_h_var, format="%02.0f",
                    font=("Consolas", 10)).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(self.once_widgets, text=" : ").pack(side=tk.LEFT)
        ttk.Spinbox(self.once_widgets, from_=0, to=59, wrap=True, width=3,
                    textvariable=self.once_m_var, format="%02.0f",
                    font=("Consolas", 10)).pack(side=tk.LEFT)

        self._on_mode_changed()

        self.task_loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="循环执行", variable=self.task_loop_var).grid(row=3, column=0, columnspan=3, sticky="w")

        # 按钮分两行：225% DPI 下每个按钮约 186px，5 个横排要 930px，
        # 而右栏只有约 470px —— 硬排会把后面几个压成 1px（文字完全看不见）。
        btns = ttk.Frame(sched)
        btns.pack(fill=tk.X)
        b1 = ttk.Frame(btns)
        b1.pack(fill=tk.X)
        ttk.Button(b1, text="＋ 添加", command=self.add_task).pack(side=tk.LEFT, padx=(0, 3))
        ttk.Button(b1, text="📥 载入", command=self.load_task_to_form).pack(side=tk.LEFT, padx=3)
        ttk.Button(b1, text="⏯ 启停", command=self.toggle_task).pack(side=tk.LEFT, padx=3)

        b2 = ttk.Frame(btns)
        b2.pack(fill=tk.X, pady=(3, 0))
        ttk.Button(b2, text="▶ 立即", command=self.run_task_now).pack(side=tk.LEFT, padx=(0, 3))
        ttk.Button(b2, text="✖ 删除", command=self.delete_task).pack(side=tk.LEFT, padx=3)

        ttk.Label(self, textvariable=self.status_var, foreground="#0b5394",
                  padding=(10, 4)).pack(fill=tk.X, side=tk.BOTTOM)

    # ================= 文件列表 =================
    def _file_names(self) -> list[str]:
        return common.list_recordings()

    def refresh_files(self) -> None:
        names = self._file_names()
        self._all_files = names                     # 与 merge_list 行一一对应
        cur = self.edit_combo_var.get()
        self.edit_combo["values"] = names
        self.task_file_combo["values"] = names
        if cur in names:
            self.edit_combo_var.set(cur)
        sel = self.merge_list.curselection()
        self.merge_list.delete(0, tk.END)
        for n in names:
            self.merge_list.insert(tk.END, n)
        keep = tuple(i for i in sel if i < len(names))
        for i in keep:
            self.merge_list.selection_set(i)
        self._set_status(f"共 {len(names)} 个脚本")

    # ================= 编辑 =================
    def load_selected(self) -> None:
        name = self.edit_combo_var.get()
        if not name:
            self._set_status("⚠️ 请先选择脚本")
            return
        path = os.path.join(common.RECORDS_DIR, name)
        try:
            self.edit_events = common.load_events(path)
        except Exception as exc:
            self._set_status(f"❌ 读取失败：{exc}")
            return
        self.edit_path = path
        self.dirty = False
        self._render_events()
        self._set_status(f"已载入 {name}（{len(self.edit_events)} 步 / "
                         f"{common.count_duration(self.edit_events):.1f}s）")

    def _render_events(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, ev in enumerate(self.edit_events):
            kind, delay, *params = ev
            self.tree.insert("", tk.END, iid=str(i), values=(
                i + 1, kind, f"{delay:.3f}", " ".join(str(p) for p in params)))
        self._refresh_visual()

    def _refresh_visual(self) -> None:
        """把「表格里选中的那一步」映射到键鼠图示上。

        选中某行 → 图示只显示那一步的按键/鼠标操作；
        没选中 → 全灰（提示去点一行）。这是刻意的：图示回答的是
        「这一步行了什么」，而不是整段脚本的汇总。
        """
        if self.kb_view is None:
            return
        if not self.edit_events:
            self.kb_view.clear()
            return
        try:
            sel = self.tree.selection()
            if not sel:
                self.kb_view.show_step(None)
                return
            idx = int(sel[0])
            if not (0 <= idx < len(self.edit_events)):
                self.kb_view.show_step(None)
                return
            ev = self.edit_events[idx]
            summary = script_visual.summarize_one(ev)
            self.kb_view.show_step(summary, step_no=idx + 1,
                                   total_steps=len(self.edit_events), kind=ev[0])
        except Exception as exc:      # 图示出错不能影响编辑主流程
            self.kb_view.clear()
            self._set_status(f"⚠️ 键鼠图示渲染失败：{exc}")

    def _selected_index(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def edit_step_delay(self) -> None:
        if not self.edit_events:
            self._set_status("⚠️ 请先载入脚本")
            return
        idx = self._selected_index()
        if idx is None:
            self._set_status("⚠️ 请先点选一步")
            return
        ev = self.edit_events[idx]
        new = simpledialog.askfloat("修改延迟", f"第 {idx + 1} 步（{ev[0]}）前置延迟，秒：",
                                    initialvalue=ev[1], minvalue=0.0, maxvalue=86400.0,
                                    parent=self)
        if new is None:
            return
        self.edit_events[idx] = (ev[0], float(new), *ev[2:])
        self.dirty = True
        self._render_events()
        self.tree.selection_set(str(idx))
        self._set_status(f"第 {idx + 1} 步延迟 → {new:.3f}s（记得保存）")

    def scale_all(self) -> None:
        if not self.edit_events:
            self._set_status("⚠️ 请先载入脚本")
            return
        k = max(0.1, float(self.scale_var.get()))
        self.edit_events = [(e[0], round(e[1] * k, 4), *e[2:]) for e in self.edit_events]
        self.dirty = True
        self._render_events()
        self._set_status(f"全部延迟 ×{k:g}（{common.count_duration(self.edit_events):.1f}s，记得保存）")

    def save_inplace(self) -> None:
        if not self.edit_path or not self.edit_events:
            self._set_status("⚠️ 没有可保存的脚本")
            return
        if not messagebox.askyesno("确认覆盖", f"覆盖写回 {os.path.basename(self.edit_path)}？",
                                   parent=self):
            return
        try:
            _write_events(self.edit_path, self.edit_events)
        except Exception as exc:
            self._set_status(f"❌ 保存失败：{exc}")
            return
        self.dirty = False
        self._set_status(f"💾 已保存 {os.path.basename(self.edit_path)}")

    def save_as(self) -> None:
        if not self.edit_events:
            self._set_status("⚠️ 没有可保存的脚本")
            return
        base = os.path.splitext(os.path.basename(self.edit_path or "script"))[0]
        name = simpledialog.askstring("另存为", "新脚本名（不含扩展名）：",
                                      initialvalue=base, parent=self)
        if not name:
            return
        path = _unique_path(_safe_name(name))
        try:
            _write_events(path, self.edit_events)
        except Exception as exc:
            self._set_status(f"❌ 保存失败：{exc}")
            return
        self.edit_path = path
        self.dirty = False
        self.refresh_files()
        self.edit_combo_var.set(os.path.basename(path))
        self._set_status(f"💾 已另存 {os.path.basename(path)}")

    # ================= 合并 / 拆分 =================
    def merge_selected(self) -> None:
        """合并：选中 ≥2 个脚本 → 弹窗排顺序 + 设间隔 → 另存。"""
        sel = [self._all_files[i] for i in self.merge_list.curselection()]
        if len(sel) < 2:
            self._set_status("⚠️ 请按住 Ctrl 至少选两个脚本再点合并")
            return
        dlg = merge_split.MergeDialog(self, sel)
        self.wait_window(dlg)
        if not dlg.result:
            return
        order, gap, name = dlg.result
        merged: list = []
        try:
            for i, fname in enumerate(order):
                evs = common.load_events(os.path.join(common.RECORDS_DIR, fname))
                if not evs:
                    continue
                if merged and gap > 0:
                    # 脚本间等待并入下一段首步延迟，不引入新事件类型，保持文件格式纯净
                    kind, delay, *rest = evs[0]
                    evs = [(kind, delay + gap, *rest), *evs[1:]]
                merged.extend(evs)
        except Exception as exc:
            self._set_status(f"❌ 合并失败：{exc}")
            return
        if len(merged) < 2:
            self._set_status("⚠️ 所选脚本都没有有效事件")
            return
        path = _unique_path(_safe_name(name))
        try:
            _write_events(path, merged)
        except Exception as exc:
            self._set_status(f"❌ 保存失败：{exc}")
            return
        self.edit_events = merged
        self.edit_path = path
        self.dirty = False
        self._render_events()
        self.refresh_files()
        self.edit_combo_var.set(os.path.basename(path))
        self._set_status(f"➕ 已合并 {len(order)} 段（间隔 {gap:g}s）→ {os.path.basename(path)}"
                         f"（{len(merged)} 步 / {common.count_duration(merged):.1f}s）")

    def split_script(self) -> None:
        """拆分：弹窗选拆分时刻 → 另存为 A/B 两份。"""
        if not self.edit_events:
            self._set_status("⚠️ 请先载入要拆分的脚本")
            return
        base = os.path.splitext(os.path.basename(self.edit_path or "script"))[0]
        dlg = merge_split.SplitDialog(self, self.edit_events, base)
        self.wait_window(dlg)
        if not dlg.result:
            return
        cut_sec, name_a, name_b = dlg.result
        # 按累计时间切：第 1 段时间 <= cut_sec，其余归第二段
        part_a, part_b = [], []
        t = 0.0
        for ev in self.edit_events:
            t += float(ev[1])
            (part_a if t <= cut_sec else part_b).append(ev)
        if not part_a or not part_b:
            self._set_status("⚠️ 拆分时刻落在边界，没有拆出两段有效脚本")
            return
        # 用用户各自命名的两段名称（不再自动加 _A/_B 后缀）
        pa = _unique_path(_safe_name(name_a))
        pb = _unique_path(_safe_name(name_b))
        try:
            _write_events(pa, part_a)
            _write_events(pb, part_b)
        except Exception as exc:
            self._set_status(f"❌ 拆分失败：{exc}")
            return
        self.refresh_files()
        self._set_status(
            f"✂ 已在 {cut_sec:.2f}s 处拆分 → "
            f"前段 {os.path.basename(pa)}（{len(part_a)}步）/"
            f"后段 {os.path.basename(pb)}（{len(part_b)}步）")

    # ================= 定时任务 =================
    def _on_mode_changed(self) -> None:
        """切换「间隔 / 每日 / 一次性」时，只显示该模式对应的参数控件。

        三套控件各自是一个 Frame，切换时 pack_forget 全部再 pack 需要的那个，
        避免反复 pack 造成重复叠加。
        """
        for w in (self.interval_widgets, self.daily_widgets, self.once_widgets):
            w.pack_forget()
        mode = self.task_mode_var.get()
        if mode == MODE_DAILY:
            self.daily_widgets.pack(side=tk.LEFT)
        elif mode == MODE_ONCE:
            self.once_widgets.pack(side=tk.LEFT)
        else:
            self.interval_widgets.pack(side=tk.LEFT)

    def _fill_daily_now(self) -> None:
        """「现在」：把每日时间填成当前时刻，省得手动拨。"""
        self.daily_h_var.set(time.strftime("%H"))
        self.daily_m_var.set(time.strftime("%M"))

    def _load_tasks(self) -> None:
        try:
            with open(TASKS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self.tasks = [t for t in data if isinstance(t, dict) and t.get("file")]
        except (OSError, json.JSONDecodeError):
            self.tasks = []
        now = time.time()
        for t in self.tasks:
            t.setdefault("last_run", None)
            if t.get("mode") == MODE_ONCE:
                # 过期的一次性任务不补跑：重启后直接停用，避免误触发
                if t.get("once_ts", 0) <= now:
                    t["enabled"] = False
                else:
                    t["next_ts"] = t["once_ts"]
            elif t.get("mode") == MODE_INTERVAL:      # 间隔任务从"现在"起算，不补积压
                t["next_ts"] = now + max(1.0, t.get("param_min", 1) * 60)
            elif not t.get("next_ts"):
                self._recompute_next(t, now)

    def _save_tasks(self) -> None:
        try:
            with open(TASKS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.tasks, f, ensure_ascii=False, indent=1)
        except OSError as exc:
            self._set_status(f"❌ 任务保存失败：{exc}")

    def _recompute_next(self, task: dict, now: float) -> None:
        mode = task["mode"]
        if mode == MODE_INTERVAL:
            task["next_ts"] = now + max(1.0, task["param_min"] * 60)
        elif mode == MODE_DAILY:
            h, m = task["hm"]
            today = datetime.fromtimestamp(now)
            target = today.replace(hour=h, minute=m, second=0, microsecond=0)
            if target.timestamp() <= now:
                target += timedelta(days=1)
            task["next_ts"] = target.timestamp()
        else:  # MODE_ONCE
            task["next_ts"] = task["once_ts"]

    def _plan_text(self, task: dict) -> str:
        """「策略」列：只写模式本身，后面的具体数值交给「参数」列。"""
        mode = task["mode"]
        if mode == MODE_INTERVAL:
            return "循环" if task.get("loop") else "间隔"
        if mode == MODE_DAILY:
            return "每日"
        return "一次性"

    @staticmethod
    def _detail_text(task: dict) -> str:
        """「参数」列：把间隔分钟数 / 每日时刻 / 一次性时刻写清楚。

        这一列就是为了解决「切到间隔或一次性后，光看表格不知道设了多少」
        —— 状态右边直接给出具体数值。
        """
        mode = task["mode"]
        if mode == MODE_INTERVAL:
            mins = task.get("param_min", 0)
            # 小于 1 分钟时用秒更直观
            if mins < 1:
                return f"每 {mins * 60:.0f} 秒"
            return f"每 {mins:g} 分"
        if mode == MODE_DAILY:
            h, m = task.get("hm", (0, 0))
            return f"每天 {h:02d}:{m:02d}"
        ts = task.get("once_ts")
        return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"

    def _short_name(self, name: str) -> str:
        """按列宽像素截断脚本名，保证不溢出、也不浪费宽度。

        按字符数截断不准（中文比英文宽一倍），这里用字体实测宽度：
        从完整名字开始，放不下就逐步缩短直到能放进「脚本」列。
        """
        import tkinter.font as tkfont
        try:
            font = tkfont.nametofont("TkDefaultFont")
            # 列宽 ~96px，扣掉 Treeview 的行内边距与可能的展开箭头，留 12px 余量
            avail = max(40, int(self.task_tree.column("file", "width")) - 12)
        except Exception:
            base = os.path.splitext(name)[0]
            return base if len(base) <= 8 else base[:7] + "…"
        if font.measure(name) <= avail:
            return name
        base = os.path.splitext(name)[0]
        ell = "…"
        for cut in range(len(base), 0, -1):
            candidate = base[:cut] + ell
            if font.measure(candidate) <= avail:
                return candidate
        return ell

    def _render_tasks(self) -> None:
        """刷新任务表。**必须保留选中行**。

        这个函数每秒被 _tick 调一次。若直接 delete+insert，选中状态每次重建
        都会丢失 —— 表现为「启停/立即/删除点一次有效、之后就提示请先选中」，
        用户根本没法操作。所以先记下选中项，重建后再恢复。
        """
        sel = self.task_tree.selection()
        keep = sel[0] if sel else None
        # 滚动位置也记下来：每秒重建会把视图弹回顶部，用户滚到下面看任务时会被打断
        yview = self.task_tree.yview()[0]
        self.task_tree.delete(*self.task_tree.get_children())
        for i, t in enumerate(self.tasks):
            st = "运行中" if t.get("busy") else ("启用" if t.get("enabled") else "停用")
            self.task_tree.insert("", tk.END, iid=str(i), values=(
                self._short_name(t["file"]), self._plan_text(t),
                self._detail_text(t), _fmt_ts(t.get("next_ts")), st))
        if keep is not None and keep in self.task_tree.get_children():
            self.task_tree.selection_set(keep)
        if yview > 0.001:
            self.task_tree.yview_moveto(yview)

    def _task_selected(self) -> None:
        """选中任务行：**只高亮 + 提示**，不回填表单。

        以前点行会立刻把该任务的设置灌进表单，用户以为是「模式自己变了」，
        很困惑。现在改成显式操作：点「📥 载入到表单」才回填。
        """
        sel = self.task_tree.selection()
        if not sel:
            return
        t = self.tasks[int(sel[0])]
        self._set_status(f"已选中：{t['file']} · {self._plan_text(t)} "
                         f"{self._detail_text(t)}（点「📥 载入到表单」可改为编辑它）")

    def load_task_to_form(self) -> None:
        """把选中任务的设置载入到下方表单，用于修改。"""
        sel = self.task_tree.selection()
        if not sel:
            self._set_status("⚠️ 请先选中一个任务")
            return
        t = self.tasks[int(sel[0])]
        self.task_file_var.set(t["file"])
        self.task_mode_var.set(t["mode"])
        if t["mode"] == MODE_INTERVAL:
            self.task_param_var.set(f"{t['param_min']:g}")
        elif t["mode"] == MODE_DAILY:
            self.daily_h_var.set(f"{t['hm'][0]:02d}")
            self.daily_m_var.set(f"{t['hm'][1]:02d}")
        else:
            self.once_date_var.set(time.strftime("%Y-%m-%d", time.localtime(t["once_ts"])))
            self.once_h_var.set(time.strftime("%H", time.localtime(t["once_ts"])))
            self.once_m_var.set(time.strftime("%M", time.localtime(t["once_ts"])))
        self._on_mode_changed()      # 切到该任务所属模式对应的参数控件
        self.task_loop_var.set(bool(t.get("loop")))
        self._set_status(f"📥 已载入「{t['file']}」的设置到表单，改完点「＋ 添加任务」即为新增一份")

    @staticmethod
    def _spin_int(var: tk.StringVar, lo: int, hi: int) -> Optional[int]:
        """读取 Spinbox 的整数值；越界或非数字返回 None。

        Spinbox 允许直接键入，所以必须自己校验——写 '99' 时它不会拦。
        """
        try:
            v = int(float(str(var.get()).strip()))
        except (TypeError, ValueError):
            return None
        return v if lo <= v <= hi else None

    def add_task(self) -> None:
        name = self.task_file_var.get()
        if not name or not os.path.isfile(os.path.join(common.RECORDS_DIR, name)):
            self._set_status("⚠️ 请先在下拉框选择有效脚本")
            return
        mode = self.task_mode_var.get()
        task: dict = {"file": name, "mode": mode, "enabled": True,
                      "loop": bool(self.task_loop_var.get())}
        now = time.time()
        if mode == MODE_INTERVAL:
            try:
                minutes = float(self.task_param_var.get().strip())
            except ValueError:
                self._set_status("❌ 间隔请填写分钟数（如 60 或 0.5）")
                return
            if minutes <= 0:
                self._set_status("❌ 间隔必须大于 0")
                return
            task["param_min"] = minutes
        elif mode == MODE_DAILY:
            h = self._spin_int(self.daily_h_var, 0, 23)
            m = self._spin_int(self.daily_m_var, 0, 59)
            if h is None or m is None:
                self._set_status("❌ 每日时间无效：时填 0-23，分填 0-59")
                return
            task["hm"] = (h, m)
        else:
            date = self.once_date_var.get().strip()
            h = self._spin_int(self.once_h_var, 0, 23)
            m = self._spin_int(self.once_m_var, 0, 59)
            if h is None or m is None:
                self._set_status("❌ 时间无效：时填 0-23，分填 0-59")
                return
            ts = _parse_dt(f"{date} {h:02d}:{m:02d}")
            if ts is None:
                self._set_status("❌ 日期格式：2026-11-01")
                return
            if ts <= now:
                self._set_status("❌ 一次性任务的触发时间必须在将来")
                return
            task["once_ts"] = ts
        self._recompute_next(task, now)
        self.tasks.append(task)
        self._save_tasks()
        self._render_tasks()
        self._set_status(f"＋ 已添加任务：{name} · {self._plan_text(task)}")

    def delete_task(self) -> None:
        sel = self.task_tree.selection()
        if not sel:
            self._set_status("⚠️ 请先选中一个任务")
            return
        i = int(sel[0])
        t = self.tasks[i]
        if not messagebox.askyesno("确认", f"删除任务「{t['file']} · {self._plan_text(t)}」？",
                                   parent=self):
            return
        del self.tasks[i]
        self._save_tasks()
        self._render_tasks()
        self._set_status("✖ 已删除任务")

    def toggle_task(self) -> None:
        sel = self.task_tree.selection()
        if not sel:
            self._set_status("⚠️ 请先选中一个任务")
            return
        t = self.tasks[int(sel[0])]
        t["enabled"] = not t.get("enabled", True)
        if t["enabled"]:
            self._recompute_next(t, time.time())
        self._save_tasks()
        self._render_tasks()
        self._set_status(f"⏯ 任务已{'启用' if t['enabled'] else '停用'}：{t['file']}")

    def run_task_now(self) -> None:
        sel = self.task_tree.selection()
        if not sel:
            self._set_status("⚠️ 请先选中一个任务")
            return
        self._fire(self.tasks[int(sel[0])], manual=True)

    def _tick(self) -> None:
        self._tick_job = None      # 本次已触发，先清登记再排下一次
        if not self._alive:
            return
        now = time.time()
        if self.engine is not None and not self.engine.player.running:
            for t in self.tasks:            # 引擎单实例：空闲即清掉全部"运行中"标记
                if t.get("busy"):
                    t["busy"] = False
        for t in self.tasks:
            if t.get("enabled") and not t.get("busy") and t.get("next_ts") and now >= t["next_ts"]:
                self._fire(t, manual=False)
                if t["mode"] == MODE_ONCE:
                    t["enabled"] = False
                    self._save_tasks()
        self._render_tasks()          # 每秒重画：下次触发时间与运行状态实时走动
        try:
            self._tick_job = self.after(TICK_MS, self._tick)
        except tk.TclError:
            self._alive = False      # 窗口已销毁，静默收尾

    def _fire(self, task: dict, manual: bool) -> bool:
        """借用录制页执行引擎跑脚本。返回是否真正启动。"""
        if self.engine is None:
            self._set_status("❌ 执行引擎不可用（录制页加载失败）")
            return False
        path = os.path.join(common.RECORDS_DIR, task["file"])
        if not os.path.isfile(path):
            self._set_status(f"❌ 定时任务落空：{task['file']} 已不存在，自动停用")
            task["enabled"] = False
            self._save_tasks()
            self._render_tasks()
            return False
        if self.engine.player.running or self.engine.recorder.recording:
            self._set_status(f"⚠️ {'手动' if manual else '定时'}触发被跳过：{task['file']}"
                             f"（引擎正忙）")
            if not manual:
                self._recompute_next(task, time.time())
            return False
        task["last_run"] = time.time()
        task["busy"] = True              # 运行中：引擎空闲后由 _tick 复位
        self._save_tasks()
        self.engine.file_var.set(task["file"])
        self.engine.play(bool(task.get("loop")))
        if not manual:
            self._recompute_next(task, time.time())
        self._set_status(f"⏰ 已触发：{task['file']} · {self._plan_text(task)}（F7 可终止）")
        return True

    # ================= 生命周期 =================
    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def shutdown(self) -> None:
        self._alive = False
        # 撤销待执行的那一次轮询（_tick_job 恒指向唯一待执行任务）
        common.cancel_job(self, self._tick_job)
        self._tick_job = None


if __name__ == "__main__":
    root = tk.Tk()
    root.title("脚本控制台（独立调试）")
    root.geometry("960x680")
    try:
        import recorder as recorder_mod
        engine = recorder_mod.RecorderApp(root)
    except Exception as exc:
        engine = None
        print("引擎不可用:", exc)
    ScriptConsoleApp(root, engine)
    root.mainloop()
