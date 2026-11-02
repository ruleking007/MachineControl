# -*- coding: utf-8 -*-
"""YOLO 检测 worker：由 conda(yolo_ocr) 的 python 以子进程方式运行。

协议（行式 JSON，stdout 单行 flush）：
  启动后先发一行  {"ready": true, "names": {0: "xxx", ...}}
  每条请求       {"img": "<base64 PNG>", "conf": 0.25}
  每条响应       {"dets": [["类名", 置信度, x1, y1, x2, y2], ...]}   # 坐标为区域内相对像素
  退出           {"cmd": "quit"} 或直接关闭 stdin

本文件刻意不依赖 mss/pynput 等项目库：截图由主进程完成后以 PNG 传入，
conda 环境只需要 ultralytics + pillow 即可运行。
用法：python yolo_worker.py <模型路径.pt>
"""
import base64
import io
import json
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: yolo_worker.py <model.pt>"}), flush=True)
        sys.exit(2)
    model_path = sys.argv[1]
    try:
        from ultralytics import YOLO
        model = YOLO(model_path)
    except Exception as exc:  # 加载失败也必须让主进程收到协议行
        print(json.dumps({"error": f"load failed: {exc}"}), flush=True)
        sys.exit(3)

    print(json.dumps({"ready": True, "names": model.names}, ensure_ascii=False), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"error": "bad json"}), flush=True)
            continue
        if req.get("cmd") == "quit":
            break
        try:
            img_b64 = req.get("img", "")
            conf = float(req.get("conf", 0.25))
            classes = req.get("classes")          # 可选：只保留这些类名
            from PIL import Image
            import numpy as np
            image = np.asarray(Image.open(io.BytesIO(base64.b64decode(img_b64))).convert("RGB"))
            results = model.predict(image, conf=conf, verbose=False)
            dets = []
            for r in results:
                names = r.names
                boxes = getattr(r, "boxes", None)
                if boxes is None:
                    continue
                for b in boxes:
                    cls_id = int(b.cls[0])
                    cls_name = names.get(cls_id, str(cls_id))
                    if classes and cls_name not in classes:
                        continue
                    x1, y1, x2, y2 = [round(float(v), 1) for v in b.xyxy[0]]
                    dets.append([cls_name, round(float(b.conf[0]), 3), x1, y1, x2, y2])
            print(json.dumps({"dets": dets}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)


if __name__ == "__main__":
    main()
