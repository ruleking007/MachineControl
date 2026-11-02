"""屏幕文字侦查：框选区域 → 定时 OCR → 关键词规则触发。

改造要点（对应原诊断报告，报告已完成使命并删除）：
- A1  修复 json.dump(data_list, data_list) 必崩
- A2  日志经队列回到主线程写控件，后台线程不碰 tkinter
- A3  配置文件用脚本目录绝对路径
- A4  mss 实例在监控线程内用 with 管理，启停不泄漏；用 Event 控制生命周期
- A5  OCR 周期按“本轮起点 + interval”计算，停止可立即响应
- A6  触发判定前先记冷却时间戳，回调异常不会导致每轮刷屏
- A7  文本 hash 变化才判定触发（同一段文字不重复刷屏）
- A8  BGRA→GRAY；A9 去掉无效的 1x1 腐蚀核
- A12 拖拽前判空 rect_id
- A13 使用虚拟桌面（所有显示器），遮罩半透明可见
- A14 wait_window 代替嵌套 mainloop
- A15 withdraw + 延迟一帧再截图
- A16/A17 配置读写容错；logger 模块级定义
- A4 补充：停止不等待时，启动前先给旧线程一个退出窗口，避免两个线程同时截图；
  截图句柄随线程退出释放，停止后立即重启不会误报“已在运行”
- 合并入口修复：改用 winfo_toplevel 取真正的顶层窗口，否则页签内点“拾取新区域”会 AttributeError
- 框选背景改用 shot.rgb，避免预览图红蓝反相（与 A8 同类通道问题）
- 新增：触发规则可在界面增删改/启停，持久化到 trigger_rules.json，监控运行中即时生效
- 深色主题修复：二值化改为分块自动极性（矢量化），黑底白字区域不再输出乱码
- 新增图形监察（YOLO）：与 OCR 并存，同一区域内既可识别文字也可检测物体；
  YOLO 跑在 yolo_ocr conda 环境的子进程里（见 yolo_monitor.py），
  按独立节奏（默认 2s，CPU 推理约 0.7s/帧）执行，失败 3 次自动熔断
- 新增动作「执行脚本」：规则命中后回放指定录制文件（复用录制页 Player 引擎，F7 可终止）
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from typing import Callable, Dict, List, Optional, Tuple

import common

common.enable_dpi_awareness()  # 必须在创建窗口 / 导入 winctl 之前

import numpy as np
from PIL import Image

# 图形监察：本模块只做协议转发，真正的推理在 yolo_ocr 环境的子进程里
import yolo_monitor
from yolo_monitor import YoloError, YoloWorker

try:
    import mss
except ImportError:
    mss = None
try:
    import cv2
except ImportError:
    cv2 = None
try:
    import pytesseract
except ImportError:
    pytesseract = None

from tkinter import filedialog

logger = common.setup_logger("文字侦查")

CAPTURE_INTERVAL = 0.5
DEBUG_MODE = False
DEFAULT_LANG = "chi_sim+eng"

# 模型未加载时给类别列表的兜底：COCO 里常见的屏幕可检测对象
_COMMON_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "bus", "truck", "traffic light",
    "cat", "dog", "bird", "backpack", "umbrella", "handbag", "suitcase",
    "bottle", "cup", "bowl", "banana", "apple", "sandwich", "chair", "couch",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]


# ===================== Tesseract 探测 =====================
def locate_tesseract() -> Optional[str]:
    """按 环境变量 → 默认安装路径 → PATH 顺序查找，避免硬编码崩溃。

    不依赖 pytesseract 是否已安装：装了 pytesseract 时顺手把路径写进它的模块变量。
    """
    import shutil

    candidates = [os.environ.get("TESSERACT_CMD", ""),
                  r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]
    found = shutil.which("tesseract")
    if found:
        candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path):
            if pytesseract is not None:
                pytesseract.pytesseract.tesseract_cmd = path
            return path
    return None


# ===================== 规则 =====================
class TriggerRule:
    """监控线程侧的执行包装。

    配置字段（关键词/冷却/大小写/精确/动作/启用）动态读取 self.config，
    界面编辑的是同一个 RuleConfig 对象 → 改动即时生效，无需重启监控；
    冷却时间戳 last_triggered 存在实例上，编辑规则不会丢失冷却状态。

    source="文字" 的规则由 OCR 文本匹配（match），
    source="图形" 的规则由 YOLO 检测结果匹配（match_dets）。
    """

    def __init__(self, config: common.RuleConfig) -> None:
        self.config = config
        self.last_triggered = 0.0

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def cooldown(self) -> float:
        return self.config.cooldown

    @property
    def is_visual(self) -> bool:
        return self.config.source == "图形"

    def match(self, text: str) -> bool:
        """OCR 文本匹配（仅 source="文字" 的规则应调用）。"""
        cfg = self.config
        cmp_text = text if cfg.case_sensitive else text.lower()
        kws = cfg.keywords if cfg.case_sensitive else [k.lower() for k in cfg.keywords]
        if cfg.exact_match:
            return cmp_text in kws
        return any(k and k in cmp_text for k in kws)

    def match_dets(self, dets: List[tuple]) -> Optional[str]:
        """图形匹配：命中返回描述文本（供日志/状态栏用），否则 None。

        关键词即 YOLO 类名；要求同类的检出数达到 min_count 且
        至少一个的置信度 ≥ min_conf。
        """
        cfg = self.config
        if not dets:
            return None
        wanted = {k.strip().lower() for k in cfg.keywords if k.strip()}
        if not wanted:
            return None
        hits: List[tuple] = []
        for det in dets:
            name = str(det[0])
            if name.lower() not in wanted:
                continue
            try:
                score = float(det[1])
            except (TypeError, ValueError, IndexError):
                continue
            if score >= cfg.min_conf:
                hits.append((name, score))
        if len(hits) < max(1, cfg.min_count):
            return None
        # 同一类只报一次（取该类的最高置信度），便于阅读
        hits.sort(key=lambda h: h[1], reverse=True)
        best: Dict[str, float] = {}
        for name, score in hits:
            if name not in best or score > best[name]:
                best[name] = score
        return "、".join(f"{n} {s:.2f}" for n, s in best.items())

    def ready(self, now: float) -> bool:
        return now - self.last_triggered >= self.cooldown

    def describe_action(self) -> str:
        cfg = self.config
        if cfg.action == "执行脚本":
            return f"执行脚本 {cfg.script_file or '（未选择）'}"
        return cfg.action

    def execute(self, text: str) -> None:
        """命中动作。运行在监控线程里：只写日志/文件，绝不碰 tkinter。

        「执行脚本」不在这里真正回放：回放引擎属于录制页（tkinter 主线程），
        由 MonitorApp 通过 dispatcher 转交，见 MonitorApp._run_script_for_rule。
        """
        cfg = self.config
        tag = "图形" if self.is_visual else "文字"
        if cfg.action == "执行脚本":
            logger.warning("【命中|%s|%s】%s → %s", self.name, tag,
                           text.replace("\n", " ")[:160], self.describe_action())
            return
        if cfg.action == "日志+告警文件":
            logger.warning("【命中|%s|%s】%s", self.name, tag, text.replace("\n", " ")[:200])
            # 告警文件名只取 basename，防止配置里写 ../ 或绝对路径写到程序目录外
            fname = os.path.basename(cfg.alert_file or "error_log.txt")
            try:
                with open(os.path.join(common.BASE_DIR, fname), "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {self.name} | {text!r}\n")
            except OSError as exc:
                logger.error("写告警文件 %s 失败：%s", fname, exc)
        else:
            logger.info("【命中|%s|%s】%s", self.name, tag, text.replace("\n", " ")[:200])


class RuleSet:
    """界面线程增删、监控线程每轮快照遍历——一把锁保证两边不撕裂。"""

    def __init__(self, rules: Optional[List[TriggerRule]] = None) -> None:
        self._lock = threading.Lock()
        self._rules: List[TriggerRule] = list(rules or [])

    def snapshot(self) -> List[TriggerRule]:
        with self._lock:
            return list(self._rules)

    def add(self, rule: TriggerRule) -> None:
        with self._lock:
            self._rules.append(rule)

    def remove(self, name: str) -> None:
        with self._lock:
            self._rules = [r for r in self._rules if r.name != name]

    def replace_all(self, rules: List[TriggerRule]) -> None:
        with self._lock:
            self._rules = list(rules)


# ===================== OCR 引擎 =====================
class OcrEngine:
    def __init__(self, lang: str = DEFAULT_LANG, debug: bool = False) -> None:
        self.lang = lang
        self.debug = debug
        self.available = cv2 is not None and pytesseract is not None
        self.missing: List[str] = []
        if cv2 is None:
            self.missing.append("opencv-python")
        if pytesseract is None:
            self.missing.append("pytesseract")
        self.tesseract_path = locate_tesseract()
        if pytesseract is not None and self.tesseract_path is None:
            self.missing.append("Tesseract-OCR(tesseract.exe)")

    def preprocess(self, bgra: np.ndarray) -> np.ndarray:
        """mss 给的是 BGRA；转灰度后分块二值化。返回 PIL 可用的 RGB 图。

        固定 THRESH_BINARY 隐含“白底黑字”假设：监控深色界面（黑底白字）时
        会把文字变成黑底白字，tesseract 只能输出乱码。改为每块 Otsu 自适应
        阈值，并按“多数像素是背景”判断极性，统一翻转成白底黑字再识别。
        """
        gray = cv2.cvtColor(bgra, cv2.COLOR_BGRA2GRAY)
        # 大区域内文字偏小，放大 1.5 倍显著提升中文识别率（非整数倍会降速，这里用 INTER_LINEAR）
        if gray.shape[0] < 300:
            gray = cv2.resize(gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_LINEAR)
        binary = self._binarize(gray)
        return cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)

    @staticmethod
    def _binarize(gray: np.ndarray, block: int = 64, flat_eps: int = 48) -> np.ndarray:
        """分块自动极性二值化（矢量化）：输出统一为白底黑字。

        - 全局 Otsu 阈值分离“文字级”的明暗像素；
        - 块均值与阈值比较判断每块背景极性（深色主题块反相）；
        - 明暗极差 < flat_eps 的纯背景块直接填白，避免噪声被劈成椒盐点。
        """
        h, w = gray.shape
        thr_g, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        cols, rows = max(1, -(-w // block)), max(1, -(-h // block))
        bm = cv2.resize(gray, (cols, rows), interpolation=cv2.INTER_AREA)
        # 块级明暗极差：补齐后用 reshape 分块求 max/min（纯 numpy，矢量化）
        pad_h, pad_w = rows * block - h, cols * block - w
        gp = np.pad(gray, ((0, pad_h), (0, pad_w)), mode="edge")
        blocks = gp.reshape(rows, block, cols, block).transpose(0, 2, 1, 3)
        bmax = blocks.max(axis=(2, 3)).astype(np.int16)
        bmin = blocks.min(axis=(2, 3)).astype(np.int16)
        size = (cols, rows)
        light_bg = (bm > thr_g).astype(np.uint8) * 255
        flat = ((bmax - bmin) < flat_eps).astype(np.uint8) * 255
        light_up = cv2.resize(light_bg, (w, h), interpolation=cv2.INTER_NEAREST)
        flat_up = cv2.resize(flat, (w, h), interpolation=cv2.INTER_NEAREST)
        bright = np.where(gray > thr_g, 255, 0).astype(np.uint8)
        out = np.where(light_up == 255, bright, 255 - bright)  # 暗背景块反相
        out[flat_up == 255] = 255                              # 纯背景 → 白
        return out

    def recognize(self, bgra: np.ndarray, psm: int = 6, lang: Optional[str] = None) -> str:
        if not self.available:
            return ""
        rgb = self.preprocess(bgra)
        config = f"--oem 3 --psm {psm}"
        return pytesseract.image_to_string(
            Image.fromarray(rgb), lang=lang or self.lang, config=config).strip()


# ===================== 监控线程 =====================
class RegionMonitor:
    """单线程巡检：每个周期对区域做 OCR；若启用图形监察，再按更慢的节奏做 YOLO。

    YOLO 与 OCR 共用同一个区域截图，但推理节奏独立（CPU 约 0.7s/帧，远慢于 OCR），
    因此图形监察有独立的 interval，不会把 OCR 的 0.5s 周期拖慢。
    """

    def __init__(self, engine: OcrEngine, rules: "RuleSet",
                 on_hit: Callable[[TriggerRule, str], None]) -> None:
        self.engine = engine
        self.rules = rules
        self.on_hit = on_hit
        self.interval = CAPTURE_INTERVAL
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._regions: List[common.Region] = []
        self._last_hash: Dict[str, str] = {}
        # ---- 图形监察状态 ----
        self.yolo: Optional[YoloWorker] = None      # 由界面注入
        self.yolo_conf = 0.35
        self.yolo_interval = 2.0
        self.yolo_classes: List[str] = []
        self.visual_enabled = False
        self._last_visual = 0.0                     # 上次图形检测的时间戳
        self._visual_fail = 0                       # 连续失败次数（用于熔断）
        self._visual_warned = False

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, regions: List[common.Region]) -> bool:
        if self.running:
            # “停止后马上点启动”时旧线程可能还在收尾：先补发停止并短暂等待，
            # 给它一个退出窗口；仍不退就拒绝本次启动，避免两个线程同时截图。
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=1.5)
        if self.running:
            logger.warning("监控仍在收尾中，请稍后再点启动")
            return False
        if not regions:
            logger.error("没有可监控的区域")
            return False
        if mss is None:
            logger.error("缺少 mss 库：pip install mss")
            return False
        if not self.engine.available:
            logger.error("OCR 环境不完整，缺少：%s", ", ".join(self.engine.missing))
            return False
        self._regions = list(regions)
        self._last_hash.clear()
        self._last_visual = 0.0            # 启动后立刻做一次图形检测
        self._visual_fail = 0
        self._visual_warned = False
        # 每轮使用独立的停止事件：旧线程永远不会被新一轮的 clear 唤醒复活
        stop = threading.Event()
        self._stop = stop
        self._thread = threading.Thread(target=self._loop,
                                        args=(stop, list(regions)), daemon=True)
        self._thread.start()
        return True

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            if wait:
                thread.join(timeout=3.0)
            # 只有确认线程已退出才释放句柄，否则 running 会误报 False
            if not thread.is_alive():
                self._thread = None

    def _loop(self, stop: threading.Event, regions: List[common.Region]) -> None:
        logger.info("===== 监控已启动（%d 个区域，图形监察%s） =====",
                    len(regions), "开" if self.visual_enabled else "关")
        with mss.mss() as sct:  # 线程内创建、退出时自动 close，不泄漏句柄
            while not stop.is_set():
                started = time.monotonic()
                for region in regions:
                    if stop.is_set():
                        break
                    try:
                        self._scan(sct, region)
                    except Exception as exc:
                        logger.error("区域[%s]识别异常：%s", region.name, exc)
                elapsed = time.monotonic() - started
                stop.wait(max(0.0, self.interval - elapsed))
        logger.info("===== 监控已停止 =====")

    def _scan(self, sct, region: common.Region) -> None:
        shot = sct.grab(region.to_box())
        bgra = np.frombuffer(shot.bgra, np.uint8).reshape(shot.height, shot.width, 4)
        text = self.engine.recognize(bgra, psm=region.psm, lang=region.lang)
        # 图形监察：与 OCR 共用这一帧截图，但按自己的节奏触发
        self._maybe_visual(bgra, shot.width, shot.height, region)
        if not text:
            return
        digest = hashlib.md5("".join(text.split()).lower().encode("utf-8")).hexdigest()
        if self._last_hash.get(region.name) == digest:
            return  # 文字没变化，不做任何判定
        self._last_hash[region.name] = digest
        logger.info("[%s] 识别：%s", region.name, text.replace("\n", " ⏎ ")[:120])
        self._fire_text_rules(text)

    def _fire_text_rules(self, text: str) -> None:
        """OCR 命中判定：只跑 source="文字" 的规则。"""
        now = time.time()
        for rule in self.rules.snapshot():
            if rule.is_visual or not rule.enabled or not rule.match(text) or not rule.ready(now):
                continue
            rule.last_triggered = now  # 先记冷却，回调再慢也不会重复触发
            logger.info("【触发】%s ← %s", rule.name, text.replace("\n", " ")[:80])
            try:
                self.on_hit(rule, text)
            except Exception as exc:
                logger.error("回调 %s 执行失败：%s", rule.name, exc)

    # ---------- 图形监察 ----------
    def _maybe_visual(self, bgra, width: int, height: int, region: common.Region) -> None:
        """到点就跑一次 YOLO；失败不打断 OCR，连续失败后自动熔断。"""
        if not self.visual_enabled or self.yolo is None:
            return
        now = time.monotonic()
        if now - self._last_visual < max(0.2, self.yolo_interval):
            return
        self._last_visual = now
        try:
            png = yolo_monitor.bgra_to_png(bgra, width, height)
            dets = self.yolo.detect(png, conf=self.yolo_conf, classes=self.yolo_classes or None)
        except Exception as exc:
            self._visual_fail += 1
            logger.error("图形监察失败（第 %d 次）：%s", self._visual_fail, exc)
            if self._visual_fail >= 3:
                self.visual_enabled = False     # 熔断，避免每轮刷屏
                logger.error("图形监察已自动停止（连续 3 次失败），请检查配置后重新启动")
            return
        self._visual_fail = 0
        if not dets:
            return
        self._fire_visual_rules(dets, region)

    def _fire_visual_rules(self, dets: List[tuple], region: common.Region) -> None:
        now = time.time()
        for rule in self.rules.snapshot():
            if not rule.is_visual or not rule.enabled or not rule.ready(now):
                continue
            desc = rule.match_dets(dets)
            if desc is None:
                continue
            rule.last_triggered = now
            logger.info("【触发|图形】%s ← [%s] %s", rule.name, region.name, desc)
            try:
                self.on_hit(rule, desc)
            except Exception as exc:
                logger.error("回调 %s 执行失败：%s", rule.name, exc)


# ===================== 全屏框选 =====================
def pick_region(parent: tk.Misc) -> Optional[common.Region]:
    """在独立 Toplevel 上半透明框选，坐标直接取自物理像素，适配任意 DPI。"""
    if mss is None:
        messagebox.showerror("缺少依赖", "请先安装 mss：pip install mss")
        return None
    with mss.mss() as sct:
        vd = sct.monitors[0]  # [0] 是全虚拟桌面（含副屏负坐标）
        shot = sct.grab(vd)
    # shot.rgb 才是 RGB 字节序；用 shot.bgr 配 "RGB" 会把预览图红蓝通道颠倒
    img = Image.frombytes("RGB", shot.size, shot.rgb)

    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.geometry(f"{vd['width']}x{vd['height']}+{vd['left']}+{vd['top']}")
    win.attributes("-alpha", 0.75)

    photo = tk.PhotoImage(master=win)
    try:
        from PIL import ImageTk
        photo = ImageTk.PhotoImage(img, master=win)
        has_bg = True
    except Exception:
        has_bg = False

    canvas = tk.Canvas(win, width=vd["width"], height=vd["height"],
                       highlightthickness=0, bg="gray20", cursor="crosshair")
    canvas.pack(fill=tk.BOTH, expand=True)
    if has_bg:
        canvas.create_image(0, 0, anchor="nw", image=photo)

    state: Dict[str, object] = {"x": 0, "y": 0, "rect": None, "result": None}

    def on_down(event: tk.Event) -> None:
        state["x"], state["y"] = event.x, event.y
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#ff3b30", width=3)

    def on_move(event: tk.Event) -> None:
        rect = state["rect"]
        if rect is None:  # A12：没按下时移动画布，直接忽略
            return
        canvas.coords(rect, state["x"], state["y"], event.x, event.y)

    def on_up(event: tk.Event) -> None:
        rect = state["rect"]
        if rect is None:
            return
        x1, y1, x2, y2 = canvas.coords(rect)
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        win.destroy()
        if width < 20 or height < 20:
            messagebox.showwarning("无效区域", "框选范围太小，请重新拾取")
            return
        # 画布坐标是相对窗口左上角的；加上虚拟桌面原点即为物理屏幕坐标
        state["result"] = common.Region(
            name=f"区域_{time.strftime('%H%M%S')}",
            left=int(left + vd["left"]), top=int(top + vd["top"]),
            width=int(width), height=int(height))

    def on_cancel(event: tk.Event | None = None) -> None:
        win.destroy()

    canvas.bind("<Button-1>", on_down)
    canvas.bind("<B1-Motion>", on_move)
    canvas.bind("<ButtonRelease-1>", on_up)
    win.bind("<Escape>", on_cancel)
    win.bind("<Button-3>", on_cancel)
    win.focus_force()

    win.grab_set()       # A14：模态等待，不再嵌套 mainloop
    win.wait_window()
    if has_bg:
        del photo        # 防止 PhotoImage 被回收后画布空白闪烁
    return state["result"]  # type: ignore[return-value]


# ===================== 主界面 =====================
class MonitorApp(ttk.Frame):
    def __init__(self, master: tk.Tk, engine_host=None) -> None:
        """engine_host：录制页实例（RecorderApp），用于「执行脚本」动作回放。

        未提供时（例如单独运行本脚本）该动作会给出提示而不静默失败。
        """
        super().__init__(master)
        self.pack(fill=tk.BOTH, expand=True)
        # master 可能是 Notebook 里的页签（Frame），withdraw/deiconify 只对顶层窗口有效，
        # 因此统一取真正的 toplevel，否则合并入口下点“拾取新区域”会 AttributeError。
        self.root = self.winfo_toplevel()
        self.dispatcher = common.UiDispatcher(master)
        self.log_sink = common.UiLogSink()
        self.log_sink.consumer = self._append_log
        self.ui_logger = common.setup_logger("文字侦查", self.log_sink)

        self.engine = OcrEngine()
        self.engine_host = engine_host        # 录制页：执行脚本的引擎宿主
        self.rule_configs: List[common.RuleConfig] = common.load_rules()
        self.rules = RuleSet([TriggerRule(cfg) for cfg in self.rule_configs])
        self.monitor = RegionMonitor(self.engine, self.rules, on_hit=self._on_rule_hit)
        self.regions: List[common.Region] = common.load_regions()
        self._state_base = "未监控"       # 状态栏基本文案（命中闪烁结束后恢复它）
        self._hit_flash_job: Optional[str] = None
        self._alive = True                # shutdown 后置 False，止住 after 轮询
        self._drain_job: Optional[str] = None   # 待执行的日志排空任务，shutdown 时撤销

        # ---- 图形监察 ----
        self.yolo_config: common.YoloConfig = common.load_yolo_config()
        self.yolo_worker: Optional[YoloWorker] = None
        self._yolo_classes_all: List[str] = []      # worker 报告的完整类别表

        self._build_ui()
        self._refresh_combo()
        self._report_env()
        self._drain_job = self.after(100, self._drain_ui)

    # ---------- UI ----------
    def _make_scrollable(self, parent: tk.Misc) -> Tuple[ttk.Frame, ttk.Frame]:
        """把子面板放进一个可滚动区域（竖向为主，必要时横向也能滚）。

        返回 (外层容器, 内容容器)：子面板都 pack 到内容容器里。
        窗口够大时滚动条自动隐藏，够不着时才出现。

        横向兜底很重要：高 DPI 下同一套控件会宽出近一倍，
        固定不滚会让右侧按钮彻底点不到。
        """
        outer = ttk.Frame(parent)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        vbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        hbar = ttk.Scrollbar(outer, orient=tk.HORIZONTAL, command=canvas.xview)
        canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)

        body = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=body, anchor="nw")

        def _sync(_e=None) -> None:
            # 内容宽度取「自然宽度」与「画布宽度」的较大值：窗口够宽就铺满，
            # 不够宽就保留自然宽度并让横向滚动条出现
            need_w = max(body.winfo_reqwidth(), canvas.winfo_width())
            canvas.itemconfigure(window, width=need_w)
            canvas.configure(scrollregion=(0, 0, need_w, body.winfo_reqheight()))
            need_v = body.winfo_reqheight() > canvas.winfo_height()
            need_h = body.winfo_reqwidth() > canvas.winfo_width()
            if need_v and not vbar.winfo_ismapped():
                vbar.pack(side=tk.RIGHT, fill=tk.Y)
            elif not need_v and vbar.winfo_ismapped():
                vbar.pack_forget()
            if need_h and not hbar.winfo_ismapped():
                hbar.pack(side=tk.BOTTOM, fill=tk.X)
            elif not need_h and hbar.winfo_ismapped():
                hbar.pack_forget()

        def _on_wheel(e) -> None:
            if body.winfo_reqheight() > canvas.winfo_height():
                canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        def _on_wheel_x(e) -> None:
            if body.winfo_reqwidth() > canvas.winfo_width():
                canvas.xview_scroll(-1 if e.delta > 0 else 1, "units")

        body.bind("<Configure>", _sync)
        canvas.bind("<Configure>", lambda e: _sync())
        self._scroll_canvas = canvas
        self._wheel_handler = _on_wheel
        self._wheel_x_handler = _on_wheel_x
        return outer, body

    def _bind_wheel_deep(self, widget: tk.Misc) -> None:
        """递归给滚动区内所有子控件绑定滚轮（Listbox 自己处理滚动，跳过）。"""
        if not isinstance(widget, tk.Listbox):
            widget.bind("<MouseWheel>", self._wheel_handler)
            widget.bind("<Shift-MouseWheel>", self._wheel_x_handler)
        for child in widget.winfo_children():
            self._bind_wheel_deep(child)

    def _build_ui(self) -> None:
        # 顶部工具栏固定在窗口上沿，不随内容滚动：监控启停永远够得着。
        # 第一行是**总开关**：默认不勾 = 整个文字侦查静默（不扫描、不识别、
        # 不触发任何规则）；勾上后才允许「启动监控」真正跑起来。
        bar = ttk.Frame(self, padding=(10, 10, 10, 4))
        bar.pack(fill=tk.X, side=tk.TOP)
        self.power_var = tk.BooleanVar(value=False)
        self.power_check = ttk.Checkbutton(
            bar, text="启用文字侦查（总开关）", variable=self.power_var,
            command=self._on_power_toggle)
        self.power_check.grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(bar, text="监控区域：").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.region_var = tk.StringVar()
        self.combo = ttk.Combobox(bar, textvariable=self.region_var,
                                  state="readonly", width=28, height=20)
        self.combo.grid(row=1, column=1, padx=6, sticky="w", pady=(4, 0))
        self.btn_pick = ttk.Button(bar, text="✚ 拾取新区域", command=self.pick_new)
        self.btn_pick.grid(row=1, column=2, padx=3, pady=(4, 0))
        self.btn_del_region = ttk.Button(bar, text="🗑 删除区域", command=self.delete_region)
        self.btn_del_region.grid(row=1, column=3, padx=3, pady=(4, 0))
        self.btn_start = ttk.Button(bar, text="▶ 启动监控", command=self.start_monitoring)
        self.btn_start.grid(row=1, column=4, padx=3, pady=(4, 0))
        self.btn_stop = ttk.Button(bar, text="⏸ 停止监控", command=self.stop_monitoring)
        self.btn_stop.grid(row=1, column=5, padx=3, pady=(4, 0))

        self.state_var = tk.StringVar(value="未监控")
        self.state_label = ttk.Label(bar, textvariable=self.state_var,
                                     foreground="#0b5394")
        self.state_label.grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))
        # 总开关没勾时，这些按钮全部置灰（拾取区域例外 —— 那只是配置，不扫描）
        self._gated_buttons = [self.btn_del_region, self.btn_start, self.btn_stop]
        self._apply_power_state()

        # 其余面板放进可滚动容器：加了图形监察后内容高于 680px 窗口，
        # 否则运行日志会被挤到窗口外、永远不可见（Tk 不会映射它）。
        outer, self._scroll_body = self._make_scrollable(self)
        outer.pack(fill=tk.BOTH, expand=True, side=tk.TOP)

        # ----- 触发规则面板 -----
        rule_box = ttk.LabelFrame(self._scroll_body,
                                  text="触发规则（命中即执行动作，改动实时生效）", padding=8)
        rule_box.pack(fill=tk.X, padx=10, pady=(4, 6))

        left = ttk.Frame(rule_box)
        left.pack(side=tk.LEFT, fill=tk.Y)
        self.rule_list = tk.Listbox(left, width=26, height=7, activestyle="dotbox",
                                    font=("Microsoft YaHei UI", 9))
        self.rule_list.pack(side=tk.LEFT, fill=tk.Y)
        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.rule_list.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.rule_list.config(yscrollcommand=sb.set)
        self.rule_list.bind("<<ListboxSelect>>", lambda e: self._load_rule_form())

        btns = ttk.Frame(left)
        btns.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        ttk.Button(btns, text="➕ 新建", command=self.new_rule).pack(fill=tk.X, pady=2)
        ttk.Button(btns, text="💾 保存修改", command=self.save_rule).pack(fill=tk.X, pady=2)
        ttk.Button(btns, text="🗑 删除", command=self.delete_rule).pack(fill=tk.X, pady=2)
        ttk.Button(btns, text="♻ 恢复默认", command=self.reset_rules).pack(fill=tk.X, pady=2)

        form = ttk.Frame(rule_box)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))
        self.rf_name = tk.StringVar()
        self.rf_keys = tk.StringVar()
        self.rf_cooldown = tk.StringVar(value="5")
        self.rf_case = tk.BooleanVar(value=False)
        self.rf_exact = tk.BooleanVar(value=False)
        self.rf_enabled = tk.BooleanVar(value=True)
        self.rf_action = tk.StringVar(value="日志")
        self.rf_alert = tk.StringVar(value="error_log.txt")
        self.rf_source = tk.StringVar(value="文字")
        self.rf_conf = tk.StringVar(value="0.35")
        self.rf_count = tk.StringVar(value="1")
        self.rf_script = tk.StringVar()
        self.rf_loop = tk.BooleanVar(value=False)

        grid = ttk.Frame(form)
        grid.pack(fill=tk.X)
        # 高 DPI 下控件会宽出近一倍，这里刻意用较小的 width，
        # 保证整表单行宽度落在 960px 窗口内（横向滚动条只作兜底）。
        ttk.Label(grid, text="名称").grid(row=0, column=0, sticky="e")
        ttk.Entry(grid, textvariable=self.rf_name, width=12).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Label(grid, text="冷却(秒)").grid(row=0, column=2, sticky="e")
        ttk.Entry(grid, textvariable=self.rf_cooldown, width=5).grid(row=0, column=3, sticky="we", padx=4)
        ttk.Label(grid, text="来源").grid(row=0, column=4, sticky="e")
        self.rf_source_combo = ttk.Combobox(grid, textvariable=self.rf_source, state="readonly",
                                            values=list(common.RULE_SOURCES), width=5)
        self.rf_source_combo.grid(row=0, column=5, sticky="we", padx=4)
        self.rf_source_combo.bind("<<ComboboxSelected>>", lambda e: self._on_source_changed())

        ttk.Label(grid, text="动作").grid(row=1, column=0, sticky="e")
        act = ttk.Combobox(grid, textvariable=self.rf_action, state="readonly",
                           values=list(common.RULE_ACTIONS), width=10)
        act.grid(row=1, column=1, sticky="we", padx=4)
        act.bind("<<ComboboxSelected>>", lambda e: self._on_action_changed())
        self.rf_alert_label = ttk.Label(grid, text="告警文件")
        self.rf_alert_label.grid(row=1, column=2, sticky="e")
        self.rf_alert_entry = ttk.Entry(grid, textvariable=self.rf_alert, width=14)
        self.rf_alert_entry.grid(row=1, column=3, columnspan=2, sticky="we", padx=4)
        self.rf_script_btn = ttk.Button(grid, text="选择脚本…", command=self.pick_script)
        self.rf_script_btn.grid(row=1, column=5, sticky="we", padx=4)

        # 图形规则专用：最低置信度 / 最少个数，单独一行避免把表单撑宽
        self.visual_row = ttk.Frame(grid)
        ttk.Label(self.visual_row, text="最低置信度").pack(side=tk.LEFT)
        ttk.Entry(self.visual_row, textvariable=self.rf_conf, width=7).pack(side=tk.LEFT, padx=4)
        ttk.Label(self.visual_row, text="最少个数").pack(side=tk.LEFT)
        ttk.Entry(self.visual_row, textvariable=self.rf_count, width=5).pack(side=tk.LEFT, padx=4)

        self.rf_key_label = ttk.Label(grid, text="关键词\n(逗号分隔)")
        self.rf_key_label.grid(row=2, column=0, sticky="ne")
        # 宽度收窄：整页要在 960px 窗口内排得下，右侧还有「来源」列
        self.rf_key_text = ttk.Entry(grid, textvariable=self.rf_keys, width=34)
        self.rf_key_text.grid(row=2, column=1, columnspan=5, sticky="we", padx=4, pady=(2, 4))
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        opts = ttk.Frame(form)
        opts.pack(fill=tk.X, pady=(2, 0))
        ttk.Checkbutton(opts, text="区分大小写", variable=self.rf_case).pack(side=tk.LEFT)
        ttk.Checkbutton(opts, text="整段精确匹配", variable=self.rf_exact).pack(side=tk.LEFT, padx=8)
        ttk.Checkbutton(opts, text="启用", variable=self.rf_enabled).pack(side=tk.LEFT)
        self._on_action_changed()
        self._on_source_changed()

        # ----- 图形监察（YOLO）面板 -----
        vbox = ttk.LabelFrame(self._scroll_body,
                              text="图形监察（YOLO 物体检测，与文字识别并行）", padding=8)
        vbox.pack(fill=tk.X, padx=10, pady=(0, 6))

        vrow = ttk.Frame(vbox)
        vrow.pack(fill=tk.X)
        # 默认不勾：图形监察要显式开启才会拉模型（否则点启动监控会莫名加载几百 MB）
        self.visual_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(vrow, text="启用图形监察", variable=self.visual_var,
                        command=self._on_visual_toggle).pack(side=tk.LEFT)
        ttk.Label(vrow, text="间隔(秒)").pack(side=tk.LEFT, padx=(12, 2))
        self.vi_interval = tk.StringVar(value=f"{self.yolo_config.interval:g}")
        ttk.Entry(vrow, textvariable=self.vi_interval, width=6).pack(side=tk.LEFT)
        ttk.Label(vrow, text="置信度").pack(side=tk.LEFT, padx=(10, 2))
        self.vi_conf = tk.StringVar(value=f"{self.yolo_config.conf:g}")
        ttk.Entry(vrow, textvariable=self.vi_conf, width=6).pack(side=tk.LEFT)
        ttk.Button(vrow, text="🔄 加载模型", command=self.load_yolo_model).pack(side=tk.LEFT, padx=6)
        ttk.Button(vrow, text="选择权重…", command=self.pick_model).pack(side=tk.LEFT, padx=2)
        ttk.Button(vrow, text="💾 保存设置", command=self.save_visual_settings).pack(side=tk.LEFT, padx=2)

        prow = ttk.Frame(vbox)
        prow.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(prow, text="权重").pack(side=tk.LEFT)
        self.vi_model = tk.StringVar(value=self.yolo_config.model_path or "（未选择）")
        # 宽度受控：路径很长时只显示尾部，避免把整行撑出窗口
        ttk.Label(prow, textvariable=self.vi_model, foreground="#666",
                  width=34, anchor="w").pack(side=tk.LEFT, padx=4)
        ttk.Label(prow, text="解释器").pack(side=tk.LEFT, padx=(8, 0))
        self.vi_python = tk.StringVar(value=self.yolo_config.python_path or "（未找到）")
        ttk.Label(prow, textvariable=self.vi_python, foreground="#666",
                  width=30, anchor="w").pack(side=tk.LEFT, padx=4)

        # 类别关注列表：不选 = 全部；选中后只对选中类别报警，减少无用数据往返
        crow = ttk.Frame(vbox)
        crow.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(crow, text="关注类别（不选=全部）：").pack(side=tk.LEFT, anchor="n")
        # height 给足：COCO 有 80 类，只显示 4 行会让人以为「就这么几个」，
        # 必须滚动才发现后面还有一大截。这里放 8 行，配右侧滚动条。
        self.vi_classes = tk.Listbox(crow, selectmode=tk.EXTENDED, exportselection=False,
                                     height=8, font=("Microsoft YaHei UI", 9))
        self.vi_classes.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        csb = ttk.Scrollbar(crow, orient=tk.VERTICAL, command=self.vi_classes.yview)
        csb.pack(side=tk.LEFT, fill=tk.Y)
        self.vi_classes.config(yscrollcommand=csb.set)
        # Listbox 的选中态滚出可视区就看不见了，靠这行提示随时能确认选了哪些
        self.vi_classes.bind("<<ListboxSelect>>", lambda _e: self._update_class_hint())

        self.vi_class_hint = tk.StringVar(value="")
        ttk.Label(vbox, textvariable=self.vi_class_hint, foreground="#666",
                  anchor="w", justify="left", wraplength=1000).pack(fill=tk.X, pady=(2, 0))

        self._fill_class_list()

        box = ttk.LabelFrame(self._scroll_body, text="运行日志", padding=8)
        # 不再 expand：新增图形监察面板后 680px 窗口装不下，
        # 让日志区固定高度、由外层滚动容器兜住溢出（见 _make_scrollable）。
        box.pack(fill=tk.BOTH, expand=False, padx=10, pady=(0, 10))
        self.log_text = scrolledtext.ScrolledText(box, height=8, width=40, wrap=tk.WORD,
                                                  state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        # 命中行标绿加粗，方便事后回溯
        self.log_text.tag_config("hit", foreground="#1a7f37",
                                 font=("Consolas", 9, "bold"))

        self._refresh_rule_list()
        self._bind_wheel_deep(self._scroll_body)

    def _refresh_combo(self) -> None:
        names = [r.name for r in self.regions]
        self.combo["values"] = names
        if self.region_var.get() not in names:
            self.region_var.set(names[0] if names else "")

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        # 【触发】/【命中|…】开头的行标绿加粗
        tags = ("hit",) if "【触" in message or "【命中" in message else ()
        self.log_text.insert(tk.END, message + "\n", *tags)
        # 限制行数，避免长时间运行后卡顿
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _drain_ui(self) -> None:
        self._drain_job = None           # 本次已触发，先清登记再排下一次
        if not self._alive:
            return                       # 已 shutdown：不再排队，避免 destroy 后 Tcl 报错
        self.log_sink.drain()
        try:
            self._drain_job = self.after(100, self._drain_ui)
        except tk.TclError:
            self._alive = False          # 窗口已销毁，静默收尾

    # ---------- 环境 ----------
    def _report_env(self) -> None:
        if self.engine.missing:
            self.ui_logger.error("OCR 环境不完整，缺少：%s（可用 pip install opencv-python mss pytesseract 补齐）",
                                 ", ".join(self.engine.missing))
        else:
            self.ui_logger.info("OCR 就绪，Tesseract：%s", self.engine.tesseract_path)
        self.ui_logger.info("已加载 %d 个监控区域", len(self.regions))

    # ---------- 区域 ----------
    def pick_new(self) -> None:
        if self.monitor.running:
            messagebox.showinfo("提示", "请先停止监控再拾取新区域")
            return
        self.root.withdraw()
        self.root.after(200, self._do_pick)  # 等窗口真正隐藏再截屏

    def _do_pick(self) -> None:
        try:
            region = pick_region(self.root)
        finally:
            self.root.deiconify()
            self.root.focus_force()
        if region is None:
            self.ui_logger.info("本次未拾取有效区域")
            return
        self.regions.append(region)
        try:
            common.save_regions(self.regions)
        except Exception as exc:
            self.ui_logger.error("保存区域失败：%s", exc)
            return
        self.region_var.set(region.name)
        self._refresh_combo()
        self.region_var.set(region.name)
        self.ui_logger.info("✅ 新增区域 %s：(%d,%d) %dx%d", region.name,
                            region.left, region.top, region.width, region.height)

    def delete_region(self) -> None:
        name = self.region_var.get()
        if not name:
            messagebox.showwarning("提示", "未选中任何区域")
            return
        if not messagebox.askyesno("确认", f"删除区域 {name}？"):
            return
        self.regions = [r for r in self.regions if r.name != name]
        try:
            common.save_regions(self.regions)
        except Exception as exc:
            self.ui_logger.error("保存区域失败：%s", exc)
        self._refresh_combo()
        self.ui_logger.info("🗑 已删除区域 %s", name)

    # ---------- 总开关 ----------
    def _on_power_toggle(self) -> None:
        """总开关：关掉就立即停监控；打开只解锁按钮，不自动开始扫描。"""
        on = bool(self.power_var.get())
        self.yolo_config.enabled = on and bool(self.visual_var.get())
        if not on:
            self.stop_monitoring()          # 关总开关 = 立刻停掉一切扫描
        self._apply_power_state()
        self.ui_logger.info("文字侦查总开关已%s", "开启" if on else "关闭")

    def _apply_power_state(self) -> None:
        """按总开关状态启用/禁用相关按钮，并在状态栏说明当前是静默的。"""
        on = bool(self.power_var.get())
        state = tk.NORMAL if on else tk.DISABLED
        for btn in getattr(self, "_gated_buttons", []):
            try:
                btn.config(state=state)
            except Exception:
                pass
        if not on:
            self._state_base = "未监控（总开关未启用 —— 勾选上方复选框后才能启动）"
            self.state_var.set(self._state_base)
        else:
            # 勾上后把状态栏从「未启用」恢复成正常文案
            if not self.monitor.running and self._state_base.startswith("未监控（"):
                self._state_base = "未监控（已启用，请点「▶ 启动监控」）"
                self.state_var.set(self._state_base)

    # ---------- 监控 ----------
    def start_monitoring(self) -> None:
        if not self.power_var.get():
            self.ui_logger.warning("文字侦查未启用：请先勾选「启用文字侦查（总开关）」")
            messagebox.showinfo("文字侦查未启用",
                                "文字侦查当前处于关闭状态。\n"
                                "请先勾选顶部的「启用文字侦查（总开关）」，再点「启动监控」。")
            return
        name = self.region_var.get()
        target = next((r for r in self.regions if r.name == name), None)
        if target is None:
            messagebox.showerror("错误", "请先拾取并选择一个监控区域")
            return
        active_rules = [r for r in self.rules.snapshot() if r.enabled]
        if not active_rules:
            messagebox.showerror("错误", "没有任何启用中的触发规则\n"
                                 "（在规则面板勾选“启用”，或点“恢复默认”）")
            return
        # 图形监察：先把界面参数同步给监控线程，再按需拉起子进程
        want_visual = bool(self.visual_var.get())
        if want_visual:
            cfg = self._read_visual_form()
            if cfg is None:
                return
            try:
                common.save_yolo_config(cfg)
            except Exception as exc:
                self.ui_logger.error("保存图形监察设置失败：%s", exc)
            if self.yolo_worker is None or not self.yolo_worker.alive:
                self.ui_logger.info("正在加载 YOLO 模型（首次约需数秒）…")
                self.load_yolo_model()
            if self.yolo_worker is None:
                # 加载失败已弹过窗；不阻断 OCR，只是图形监察不参与
                self.visual_var.set(False)
                self.yolo_config.enabled = False
                want_visual = False
            else:
                self.monitor.yolo_conf = cfg.conf
                self.monitor.yolo_interval = cfg.interval
                self.monitor.yolo_classes = list(cfg.classes)
                self.monitor.yolo = self.yolo_worker
        self.monitor.visual_enabled = want_visual
        if not want_visual:
            visual_rules = [r for r in active_rules if r.is_visual]
            if visual_rules:
                self.ui_logger.warning(
                    "有 %d 条图形规则因未启用图形监察而不会生效", len(visual_rules))

        if self.monitor.start([target]):
            extra = ""
            if want_visual:
                extra = (f"，图形监察每 {self.monitor.yolo_interval:.1f}s"
                         f"（{len(self._yolo_classes_all) or len(_COMMON_CLASSES)} 类）")
            self._state_base = (f"监控中：{name}（每 {CAPTURE_INTERVAL:.1f}s 扫描，"
                                f"{len(active_rules)} 条规则生效{extra}）")
            self.state_var.set(self._state_base)

    def stop_monitoring(self) -> None:
        self.monitor.stop(wait=False)
        self._state_base = "未监控"
        self.state_var.set("未监控")
        self.ui_logger.info("已请求停止监控")

    # ---------- 规则与回调 ----------
    def _on_rule_hit(self, rule: TriggerRule, text: str) -> None:
        """监控线程里执行：动作只写日志/文件，绝不碰 tkinter 控件。

        「执行脚本」必须回主线程：回放引擎属于录制页，且要更新按钮状态。
        """
        rule.execute(text)
        if rule.config.action == "执行脚本":
            try:
                self.dispatcher.post(self._run_script_for_rule, rule.name,
                                     rule.config.script_file,
                                     bool(rule.config.script_loop))
            except Exception:
                pass
        # 状态栏“字幕”标绿属于 UI，必须回主线程做
        try:
            self.dispatcher.post(self._flash_hit, rule.name, text)
        except Exception:
            pass

    # ---------- 执行脚本（命中动作） ----------
    def _run_script_for_rule(self, rule_name: str, script_file: str, loop: bool) -> None:
        """主线程：把选中的录制脚本交给录制页引擎回放。"""
        if not script_file:
            self.ui_logger.error("规则「%s」要执行脚本，但没有配置脚本文件", rule_name)
            return
        host = self.engine_host
        if host is None:
            self.ui_logger.error("规则「%s」无法执行脚本：当前没有可用的回放引擎"
                                 "（请从主程序 main.py 启动，或先打开录制页）", rule_name)
            return
        path = os.path.join(common.RECORDS_DIR, script_file)
        if not os.path.isfile(path):
            self.ui_logger.error("规则「%s」的脚本已不存在：%s", rule_name, script_file)
            return
        if host.player.running or host.recorder.recording:
            self.ui_logger.warning("规则「%s」触发被跳过：引擎正忙（回放中或录制中）", rule_name)
            return
        try:
            events = common.load_events(path)
        except Exception as exc:
            self.ui_logger.error("规则「%s」读取脚本失败：%s", rule_name, exc)
            return
        if not events:
            self.ui_logger.error("规则「%s」的脚本没有有效事件：%s", rule_name, script_file)
            return
        speed = max(0.1, float(host.speed_var.get()))
        host.file_var.set(script_file)          # 让录制页显示正在跑哪个脚本
        if not host.player.start(events, loop, speed):
            self.ui_logger.warning("规则「%s」启动回放失败（引擎已占用）", rule_name)
            return
        host.btn_play.config(state=tk.DISABLED)
        host.btn_loop.config(state=tk.DISABLED)
        host.btn_halt.config(state=tk.NORMAL)
        host._set_status(f"▶️ 规则「{rule_name}」触发执行 {script_file}"
                         f"（{'循环' if loop else '单次'}，按 F7 终止）")
        self.ui_logger.info("▶️ 规则「%s」已开始执行脚本 %s（%d 步 / %s）",
                            rule_name, script_file, len(events),
                            "循环" if loop else "单次")

    def _flash_hit(self, name: str, text: str) -> None:
        """主线程：状态栏变绿显示命中信息，3 秒后恢复基本文案。"""
        summary = " ".join(text.split())[:40]
        self.state_var.set(f"✅ 命中「{name}」：{summary}")
        self.state_label.configure(foreground="#1a7f37")
        if self._hit_flash_job is not None:
            try:
                self.after_cancel(self._hit_flash_job)
            except Exception:
                pass
        self._hit_flash_job = self.after(3000, self._restore_state)

    def _restore_state(self) -> None:
        self._hit_flash_job = None
        self.state_var.set(self._state_base)
        self.state_label.configure(foreground="#0b5394")

    # ---------- 规则管理（界面） ----------
    def _refresh_rule_list(self, select: int = -1) -> None:
        self.rule_list.delete(0, tk.END)
        for cfg in self.rule_configs:
            mark = "" if cfg.enabled else "（已停用）"
            tag = "图" if cfg.source == "图形" else "文"
            self.rule_list.insert(tk.END, f"[{tag}] {cfg.name}{mark}")
        if 0 <= select < len(self.rule_configs):
            self.rule_list.selection_set(select)
            self.rule_list.see(select)

    def _selected_rule_index(self) -> Optional[int]:
        sel = self.rule_list.curselection()
        return int(sel[0]) if sel else None

    def _load_rule_form(self) -> None:
        idx = self._selected_rule_index()
        if idx is None:
            return
        cfg = self.rule_configs[idx]
        self.rf_name.set(cfg.name)
        self.rf_keys.set("，".join(cfg.keywords))
        self.rf_cooldown.set(f"{cfg.cooldown:g}")
        self.rf_case.set(cfg.case_sensitive)
        self.rf_exact.set(cfg.exact_match)
        self.rf_enabled.set(cfg.enabled)
        self.rf_action.set(cfg.action)
        self.rf_alert.set(cfg.alert_file or "error_log.txt")
        self.rf_source.set(cfg.source)
        self.rf_conf.set(f"{cfg.min_conf:g}")
        self.rf_count.set(str(cfg.min_count))
        self.rf_script.set(cfg.script_file or "")
        self.rf_loop.set(cfg.script_loop)
        self._on_action_changed()
        self._on_source_changed()

    def _on_action_changed(self) -> None:
        action = self.rf_action.get()
        need_file = action == "日志+告警文件"
        self.rf_alert_entry.config(state=tk.NORMAL if need_file else tk.DISABLED)
        need_script = action == "执行脚本"
        # 告警文件与选择脚本按钮互斥，同一行只让当前动作需要的那一个可用
        self.rf_script_btn.config(state=tk.NORMAL if need_script else tk.DISABLED)
        if need_script:
            self.rf_alert_label.config(text="脚本文件")
            self.rf_alert_entry.config(state=tk.NORMAL)
        else:
            self.rf_alert_label.config(text="告警文件")

    def _on_source_changed(self) -> None:
        """文字规则显示大小写/精确匹配；图形规则显示置信度/个数。"""
        visual = self.rf_source.get() == "图形"
        if visual:
            self.rf_key_label.config(text="类别名\n(逗号分隔)")
            self.rf_case.set(False)
            self.rf_exact.set(False)
            # 单独一行放右侧：grid 重排即可，不必 pack/pack_forget
            self.visual_row.grid(row=3, column=1, columnspan=4, sticky="w", padx=4, pady=(0, 4))
        else:
            self.rf_key_label.config(text="关键词\n(逗号分隔)")
            self.visual_row.grid_remove()

    def pick_script(self) -> None:
        """选择命中后要回放的录制脚本（records/ 下的 json/pkl）。"""
        files = common.list_recordings()
        if not files:
            messagebox.showwarning("没有脚本", "records 目录里还没有录制文件，请先在录制页录一段")
            return
        dlg = tk.Toplevel(self)
        dlg.title("选择要执行的脚本")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        ttk.Label(dlg, text="命中后回放哪个录制文件：", padding=8).pack(anchor="w")
        lb = tk.Listbox(dlg, width=48, height=12, font=("Consolas", 10))
        lb.pack(fill=tk.BOTH, expand=True, padx=8)
        for n in files:
            lb.insert(tk.END, n)
        cur = self.rf_script.get()
        if cur in files:
            lb.selection_set(files.index(cur))
        def _ok() -> None:
            sel = lb.curselection()
            if sel:
                self.rf_script.set(files[sel[0]])
            dlg.destroy()
        btns = ttk.Frame(dlg, padding=8)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="确定", command=_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side=tk.RIGHT)
        lb.bind("<Double-1>", lambda e: _ok())

    # ---------- 图形监察 ----------
    def _fill_class_list(self) -> None:
        """类别列表：优先用已加载模型的类别，否则用内置 COCO 常用类。"""
        self.vi_classes.delete(0, tk.END)
        names = self._yolo_classes_all or _COMMON_CLASSES
        for n in names:
            self.vi_classes.insert(tk.END, n)
        selected = []
        for want in self.yolo_config.classes:
            if want in names:
                self.vi_classes.selection_set(names.index(want))
                selected.append(want)
        src = "模型" if self._yolo_classes_all else "内置COCO"
        self._class_source = f"{len(names)}个·{src}"
        self._update_class_hint(selected)

    def _update_class_hint(self, selected: Optional[List[str]] = None) -> None:
        """列表下方提示行：总数 + 已选了哪些。

        Listbox 没有复选框，选中项滚出可视区就看不见了；这行让状态始终可见。
        """
        if selected is None:
            selected = [self.vi_classes.get(i) for i in self.vi_classes.curselection()]
        src = getattr(self, "_class_source", "")
        if not selected:
            self.vi_class_hint.set(
                f"[{src}] 当前全部参与判定 —— 想只看某几类，按住 Ctrl 点选；留空不影响功能")
        else:
            shown = "、".join(selected[:12])
            more = f" 等{len(selected)}个" if len(selected) > 12 else ""
            self.vi_class_hint.set(
                f"[{src}] 只关注：{shown}{more} —— 规则里的类名必须在此列，否则不会触发")

    def _on_visual_toggle(self) -> None:
        on = bool(self.visual_var.get())
        # 只有总开关也开着，图形监察才算真正启用；否则只记勾选状态不生效
        self.yolo_config.enabled = on and bool(self.power_var.get())
        if not on:
            # 关闭时顺手停掉子进程，释放几百 MB 内存
            self._shutdown_yolo()
            self.ui_logger.info("图形监察已关闭")
        elif not self.power_var.get():
            self.ui_logger.info("图形监察已勾选，但总开关未启用，暂不生效")
        else:
            self.ui_logger.info("图形监察已启用（启动监控后生效）")

    def load_yolo_model(self) -> None:
        """显式加载模型：把启动耗时与错误提前暴露，不等到点启动监控才报错。"""
        cfg = self._read_visual_form()
        if cfg is None:
            return
        model = cfg.model_path
        if not model:
            model = os.path.join(common.BASE_DIR, "yolov8n.pt")
            if not os.path.isfile(model):
                if not messagebox.askyesno(
                        "未选择权重",
                        "还没有指定 .pt 权重。\n是否用本目录的 yolov8n.pt？\n"
                        "（没有该文件时 ultralytics 会尝试联网下载）"):
                    return
            cfg.model_path = model
        self.ui_logger.info("正在加载 YOLO 模型：%s …", os.path.basename(cfg.model_path))
        self._shutdown_yolo()
        try:
            worker = YoloWorker(cfg.python_path, cfg.model_path)
            worker.start()
        except YoloError as exc:
            self.ui_logger.error("❌ YOLO 加载失败：%s", str(exc).replace("\n", " "))
            messagebox.showerror("加载失败", str(exc))
            return
        self.yolo_worker = worker
        self.monitor.yolo = worker
        self._yolo_classes_all = [worker.names[k] for k in sorted(worker.names)]
        self._fill_class_list()
        self.vi_model.set(cfg.model_path)
        self.ui_logger.info("✅ YOLO 就绪：%d 个类别可用（推理约 0.7s/帧）",
                            len(self._yolo_classes_all))

    def _shutdown_yolo(self) -> None:
        self.monitor.yolo = None
        self.monitor.visual_enabled = False
        worker, self.yolo_worker = self.yolo_worker, None
        if worker is not None:
            try:
                worker.close()
            except Exception:
                pass

    def pick_model(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 YOLO 权重",
            initialdir=common.BASE_DIR,
            filetypes=[("PyTorch 权重", "*.pt"), ("ONNX", "*.onnx"), ("所有文件", "*.*")])
        if not path:
            return
        self.yolo_config.model_path = path
        self.vi_model.set(path)
        try:
            common.save_yolo_config(self.yolo_config)
        except Exception as exc:
            self.ui_logger.error("保存图形监察设置失败：%s", exc)

    def _read_visual_form(self) -> Optional[common.YoloConfig]:
        cfg = self.yolo_config
        try:
            cfg.interval = min(600.0, max(0.2, float(self.vi_interval.get())))
        except ValueError:
            messagebox.showwarning("参数无效", "间隔必须是数字（秒），例如 2")
            return None
        try:
            cfg.conf = min(1.0, max(0.0, float(self.vi_conf.get())))
        except ValueError:
            messagebox.showwarning("参数无效", "置信度必须是 0~1 之间的数字，例如 0.35")
            return None
        cfg.python_path = self.vi_python.get() if os.path.isfile(self.vi_python.get()) else \
            common.detect_yolo_python()
        cfg.enabled = bool(self.visual_var.get())
        cfg.classes = [self.vi_classes.get(i) for i in self.vi_classes.curselection()]
        return cfg

    def save_visual_settings(self) -> None:
        cfg = self._read_visual_form()
        if cfg is None:
            return
        try:
            common.save_yolo_config(cfg)
        except Exception as exc:
            self.ui_logger.error("保存图形监察设置失败：%s", exc)
            return
        # 同步到监控线程（同一实例，改完立即生效）
        self.monitor.yolo_conf = cfg.conf
        self.monitor.yolo_interval = cfg.interval
        self.monitor.yolo_classes = list(cfg.classes)
        self.ui_logger.info("💾 图形监察设置已保存：间隔 %.1fs，置信度 %.2f，关注 %s",
                            cfg.interval, cfg.conf,
                            "、".join(cfg.classes) if cfg.classes else "全部类别")

    def _collect_form(self) -> Optional[common.RuleConfig]:
        """读取表单并校验；失败弹窗提示并返回 None。中英文逗号都当分隔符。"""
        name = self.rf_name.get().strip()
        if not name:
            messagebox.showwarning("规则无效", "规则名称不能为空")
            return None
        idx = self._selected_rule_index()
        dup = [i for i, c in enumerate(self.rule_configs)
               if c.name == name and i != idx]
        if dup:
            messagebox.showwarning("规则无效", f"已存在同名规则「{name}」，冷却与去重按名称管理，请换名")
            return None
        kws = [k.strip() for k in re.split(r"[，,;；]", self.rf_keys.get()) if k.strip()]
        if not kws:
            messagebox.showwarning("规则无效",
                                   "类别/关键词不能为空（多个用逗号分隔）")
            return None
        try:
            cooldown = max(0.0, float(self.rf_cooldown.get()))
        except ValueError:
            messagebox.showwarning("规则无效", "冷却时间必须是数字（秒）")
            return None
        alert = self.rf_alert.get().strip()
        action = self.rf_action.get()
        if action == "日志+告警文件" and not alert:
            alert = "error_log.txt"
        source = self.rf_source.get()
        # 图形规则的两个数值字段
        try:
            min_conf = min(1.0, max(0.0, float(self.rf_conf.get() or 0.35)))
        except ValueError:
            messagebox.showwarning("规则无效", "最低置信度必须是 0~1 的数字")
            return None
        try:
            min_count = max(1, int(float(self.rf_count.get() or 1)))
        except ValueError:
            messagebox.showwarning("规则无效", "最少个数必须是正整数")
            return None
        script = self.rf_script.get().strip()
        if action == "执行脚本":
            if not script:
                messagebox.showwarning("规则无效", "动作是「执行脚本」时必须先选择脚本")
                return None
            if not os.path.isfile(os.path.join(common.RECORDS_DIR, script)):
                messagebox.showwarning("规则无效", f"脚本文件不存在：{script}\n（可能已被删除，请重新选择）")
                return None
        return common.RuleConfig(
            name=name, keywords=kws, cooldown=cooldown,
            case_sensitive=self.rf_case.get(), exact_match=self.rf_exact.get(),
            action=action, alert_file=alert if action == "日志+告警文件" else "",
            enabled=self.rf_enabled.get(),
            source=source, min_conf=min_conf, min_count=min_count,
            script_file=script, script_loop=bool(self.rf_loop.get()))

    def _persist_rules(self) -> bool:
        try:
            common.save_rules(self.rule_configs)
            return True
        except Exception as exc:
            messagebox.showerror("保存失败", f"规则写入配置文件失败：{exc}")
            return False

    def new_rule(self) -> None:
        cfg = common.RuleConfig(name="新规则", keywords=["关键词"], cooldown=5.0)
        self.rule_configs.append(cfg)
        self.rules.add(TriggerRule(cfg))
        if not self._persist_rules():
            self.rule_configs.remove(cfg)
            self.rules.remove(cfg.name)
            return
        self._refresh_rule_list(select=len(self.rule_configs) - 1)
        self._load_rule_form()
        self.ui_logger.info("➕ 新建规则「%s」，请在右侧填写关键词后保存", cfg.name)

    def save_rule(self) -> None:
        idx = self._selected_rule_index()
        if idx is None:
            messagebox.showwarning("提示", "请先在左侧选择要修改的规则")
            return
        cfg = self._collect_form()
        if cfg is None:
            return
        # 原地更新字段：运行中的监控线程与 RuleSet 持同一引用，改动即时生效
        old = self.rule_configs[idx]
        old.name, old.keywords, old.cooldown = cfg.name, cfg.keywords, cfg.cooldown
        old.case_sensitive, old.exact_match = cfg.case_sensitive, cfg.exact_match
        old.action, old.alert_file, old.enabled = cfg.action, cfg.alert_file, cfg.enabled
        old.source, old.min_conf, old.min_count = cfg.source, cfg.min_conf, cfg.min_count
        old.script_file, old.script_loop = cfg.script_file, cfg.script_loop
        if not self._persist_rules():
            return
        self._refresh_rule_list(select=idx)
        self.ui_logger.info("💾 规则「%s」已更新：来源 %s，匹配 %s，冷却 %.1fs，动作 %s%s",
                            old.name, old.source, "、".join(old.keywords), old.cooldown,
                            old.describe_action() if hasattr(old, "describe_action")
                            else old.action,
                            "" if old.enabled else "（已停用）")

    def delete_rule(self) -> None:
        idx = self._selected_rule_index()
        if idx is None:
            messagebox.showwarning("提示", "请先选择要删除的规则")
            return
        name = self.rule_configs[idx].name
        if not messagebox.askyesno("确认", f"删除规则「{name}」？"):
            return
        backup = self.rule_configs[idx]
        del self.rule_configs[idx]
        self.rules.remove(name)
        if not self._persist_rules():
            self.rule_configs.insert(idx, backup)
            self.rules.add(TriggerRule(backup))
            self._refresh_rule_list(select=idx)
            return
        self._refresh_rule_list(select=min(idx, len(self.rule_configs) - 1))
        self.ui_logger.info("🗑 已删除规则「%s」", name)

    def reset_rules(self) -> None:
        if not messagebox.askyesno("确认", "恢复为内置默认 3 条规则？当前自定义规则会被覆盖。"):
            return
        self.rule_configs = common.load_default_rules()
        self.rules.replace_all([TriggerRule(c) for c in self.rule_configs])
        self._persist_rules()
        self._refresh_rule_list(select=0)
        self._load_rule_form()
        self.ui_logger.info("♻ 已恢复默认规则（%d 条）", len(self.rule_configs))

    # ---------- 关闭 ----------
    def shutdown(self) -> None:
        self._alive = False                # 先止住 after 轮询，再拆资源
        # 撤销待执行的那两次任务：destroy 后 Tk 仍会调用它们，控件已不存在，
        # 会抛 "invalid command name ..._drain_ui"。
        common.cancel_job(self, self._drain_job)
        common.cancel_job(self, self._hit_flash_job)
        self._drain_job = None
        self._hit_flash_job = None
        self.monitor.stop(wait=False)
        self._shutdown_yolo()          # 关掉子进程，不留孤儿 python
        self.dispatcher.stop()


class MainGUI(MonitorApp):
    """兼容旧名字：原来 `MainGUI(root)` 的用法继续可用。"""

    def __init__(self, root: tk.Tk) -> None:
        root.title("屏幕文字侦查 ｜ 区域 OCR 触发")
        root.geometry("900x620")
        super().__init__(root)


def main() -> None:
    root = tk.Tk()
    app = MainGUI(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.shutdown(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
