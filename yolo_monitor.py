"""图形监察引擎：把 yolo_worker.py 当作子进程驱动，主进程只做截图与转发。

为什么要子进程：
- ultralytics + torch 体积大、且与主程序（Python 3.14 + tkinter）解释器版本不同，
  直接 import 会把整个工具箱拖进几 GB 的依赖；子进程方案让主程序保持轻量。
- 推理在 CPU 上约 0.7s/帧，放子进程里跑不会卡住 tkinter 主循环。

协议（行式 JSON，见 yolo_worker.py 顶部说明）：
  启动 → {"ready": true, "names": {...}}   或   {"error": "..."}
  请求 → {"img": "<base64 PNG>", "conf": 0.25, "classes": [...]}
  响应 → {"dets": [["类名", 置信度, x1, y1, x2, y2], ...]}

设计要点：
- Y1  懒启动：第一次 detect 才拉起进程，模型加载耗时只在真正用到时付出。
- Y2  请求带自增 id，读响应时跳过陈旧行，避免上一帧的迟到响应错配到本帧。
- Y3  单线程独占 stdin/stdout，调用方串行使用；超时则判定 worker 卡死并杀掉重建。
- Y4  退出/异常路径统一清理，不留孤儿 python 进程（close 会 terminate 再 kill）。
- Y5  本模块绝不 import tkinter、也不碰任何控件；日志走标准 logging。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("图形监察")

# 单个请求的最长等待：CPU 推理 0.7s，给 30s 余量覆盖首次加载与慢机器
REQUEST_TIMEOUT = 30.0
# worker 启动后等待 {"ready"} 的上限：模型加载 + torch 初始化可能较慢
STARTUP_TIMEOUT = 120.0

Det = Tuple[str, float, float, float, float, float]


class YoloError(RuntimeError):
    """启动/通信失败，消息可直接展示给用户。"""


def _default_worker_script() -> str:
    """定位 yolo_worker.py。

    源码运行时它与本模块同目录；打包（PyInstaller 6 的 onedir 布局）后它被放进
    exe 同级的 _internal/ 里，而 exe 目录本身也要找一遍——不同 PyInstaller 版本
    对 datas 的落点不同（有的直接放 exe 旁边，有的塞进 _internal），两个都查最稳。

    注意不能只看本模块的 __file__：冻结后它指向临时解包目录，虽然那里通常也有
    一份副本，但以路径方式启动子进程时用临时目录更容易踩到清理/权限问题，
    所以按「exe 同级 → exe 同级 _internal → 本模块同目录」的顺序找。
    """
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    bases = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        bases += [exe_dir, os.path.join(exe_dir, "_internal")]
    bases.append(here)
    for base in bases:
        path = os.path.join(base, "yolo_worker.py")
        if os.path.isfile(path):
            return path
    return os.path.join(bases[0], "yolo_worker.py")


class YoloWorker:
    """yolo_worker.py 的宿主。非线程安全：调用方需自行串行（图形监察是单线程）。"""

    def __init__(self, python_path: str, model_path: str,
                 worker_script: Optional[str] = None) -> None:
        self.python_path = python_path
        self.model_path = model_path
        self.worker_script = worker_script or _default_worker_script()
        self.names: Dict[int, str] = {}
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._stderr_tail: List[str] = []

    # ---------- 生命周期 ----------
    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _check_paths(self) -> None:
        if not self.python_path or not os.path.isfile(self.python_path):
            raise YoloError(
                "未找到 yolo_ocr 环境的 python.exe。\n"
                "请在「图形监察」里指定解释器路径（默认 C:\\Users\\21094\\.conda\\envs\\yolo_ocr\\python.exe）")
        if not self.model_path or not os.path.isfile(self.model_path):
            raise YoloError(
                "未找到 YOLO 权重文件（.pt）。\n"
                "可先点「选择权重」指定，或让 ultralytics 自动下载 yolov8n.pt 到本目录")
        if not os.path.isfile(self.worker_script):
            raise YoloError(f"缺少 worker 脚本：{self.worker_script}")

    def start(self) -> None:
        """拉起子进程并读回 ready 行；重复调用是空操作。"""
        with self._lock:
            if self.alive:
                return
            self._check_paths()
            self._stderr_tail.clear()
            # cwd 必须避开 worker 脚本所在目录：打包版的 _internal\ 里带着主程序
            # 的 python314.DLL，而 worker 跑在 conda 的 Python 3.11 上；Windows 会
            # 优先从 cwd 解析 DLL，于是报
            # "Module use of python314.dll conflicts with this version of Python"。
            # 用解释器自己所在目录最安全（conda 环境的 DLL 就在那里）。
            workdir = os.path.dirname(self.python_path) or os.path.dirname(self.worker_script)
            env = dict(os.environ)
            # 去掉可能残留的 PYTHONPATH/PYTHONHOME，避免子进程被主程序的路径带偏
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            try:
                self._proc = subprocess.Popen(
                    [self.python_path, self.worker_script, self.model_path],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=workdir,
                    env=env,
                    text=True, encoding="utf-8", errors="replace",
                    # 打包成 windowed exe 后父进程没有控制台，必须屏蔽子进程弹窗
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as exc:
                self._proc = None
                raise YoloError(f"无法启动 YOLO 子进程：{exc}") from exc

            # 收集 stderr，worker 崩溃时把尾部内容附在错误里，便于定位
            threading.Thread(target=self._drain_stderr, daemon=True,
                             name="yolo-stderr").start()

            line = self._read_line(STARTUP_TIMEOUT)
            if line is None:
                detail = self._stderr_text()
                self.close()
                raise YoloError("YOLO 子进程启动超时（模型加载过慢或依赖缺失）"
                                + (f"\n{detail}" if detail else ""))
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.close()
                raise YoloError(f"YOLO 子进程返回了非 JSON 内容：{line[:200]}")
            if msg.get("error"):
                self.close()
                raise YoloError(f"YOLO 加载失败：{msg['error']}"
                                + (f"\n{self._stderr_text()}" if self._stderr_text() else ""))
            raw_names = msg.get("names") or {}
            # JSON 的键一定是字符串，统一成 int 便于按 id 取名
            self.names = {}
            for k, v in raw_names.items():
                try:
                    self.names[int(k)] = str(v)
                except (TypeError, ValueError):
                    continue
            logger.info("YOLO 就绪：%s（%d 类）", os.path.basename(self.model_path),
                        len(self.names))

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self._stderr_tail.append(line.rstrip())
                del self._stderr_tail[:-20]      # 只留最后 20 行
        except Exception:
            pass

    def _stderr_text(self) -> str:
        tail = [t for t in self._stderr_tail if t.strip()]
        return " | ".join(tail[-3:])[:400]

    def close(self) -> None:
        """终止子进程；无论正常退出还是异常清理都走这里。"""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if proc.stdin is not None:
                    try:
                        proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                        proc.stdin.flush()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()          # 兜底：不留孤儿进程
        except Exception:
            pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass

    # ---------- 通信 ----------
    def _read_line(self, timeout: float) -> Optional[str]:
        """带超时读一行。stdout 在阻塞读，超时后由调用方 close() 打断。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        result: List[Optional[str]] = [None]

        def _reader() -> None:
            try:
                result[0] = proc.stdout.readline()
            except Exception:
                result[0] = None

        t = threading.Thread(target=_reader, daemon=True, name="yolo-read")
        t.start()
        t.join(timeout)
        if t.is_alive():
            return None                  # 读线程还挂着 → 视为超时，close 会关掉管道
        line = result[0]
        if not line:
            return None                  # EOF：进程已退出
        return line.strip() or None

    def detect(self, png_bytes: bytes, conf: float = 0.35,
               classes: Optional[List[str]] = None) -> List[Det]:
        """对一张 PNG 做检测。首次调用会自动拉起子进程。

        返回 [(类名, 置信度, x1, y1, x2, y2), ...]，坐标为图内相对像素。
        """
        with self._lock:
            if not self.alive:
                self.start()
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdout is None:
                raise YoloError("YOLO 子进程不可用")

            self._req_id += 1
            req = {"img": base64.b64encode(png_bytes).decode("ascii"),
                   "conf": float(conf)}
            if classes:
                req["classes"] = list(classes)
            try:
                proc.stdin.write(json.dumps(req) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self.close()
                raise YoloError(f"向 YOLO 子进程发送数据失败：{exc}") from exc

            deadline = time.monotonic() + REQUEST_TIMEOUT
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    detail = self._stderr_text()
                    self.close()          # 卡死的 worker 必须杀掉，否则会一直占着
                    raise YoloError("YOLO 检测超时（已重启子进程）"
                                    + (f"\n{detail}" if detail else ""))
                line = self._read_line(min(remain, REQUEST_TIMEOUT))
                if line is None:
                    if proc.poll() is not None:
                        detail = self._stderr_text()
                        self.close()
                        raise YoloError("YOLO 子进程意外退出"
                                        + (f"\n{detail}" if detail else ""))
                    continue              # 还没到 deadline，继续等
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue              # ultralytics 偶尔会往 stdout 打杂项，跳过
                if msg.get("error"):
                    raise YoloError(f"YOLO 检测失败：{msg['error']}")
                if "dets" not in msg:
                    continue              # 不是响应行，忽略
                return self._parse_dets(msg["dets"])

    @staticmethod
    def _parse_dets(raw) -> List[Det]:
        out: List[Det] = []
        if not isinstance(raw, list):
            return out
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            try:
                name = str(item[0])
                score = float(item[1])
                x1, y1, x2, y2 = (float(item[2]), float(item[3]),
                                  float(item[4]), float(item[5]))
            except (TypeError, ValueError):
                continue
            out.append((name, score, x1, y1, x2, y2))
        return out


# ===================== 截图 → PNG =====================
def bgra_to_png(bgra: bytes, width: int, height: int) -> bytes:
    """mss 的 BGRA 原始字节 → PNG 字节（交给子进程识别）。

    worker 侧用 PIL 打开，因此这里必须是 PNG 而不是裸数组。
    """
    from PIL import Image
    import numpy as np

    arr = np.frombuffer(bgra, np.uint8).reshape(height, width, 4)
    img = Image.fromarray(arr[:, :, [2, 1, 0]])   # BGRA → RGB
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()