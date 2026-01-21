#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
jetson_yolo11_two_stage_mjpeg_server_v5_4_telegram.py
-----------------------------------------------------
Two-stage pipeline for Jetson Nano with TELEGRAM ALERTS:

Stage A (SMOKE): runs continuously on RTSP frames (TensorRT engine, 1-class smoke).
                 → Sends Telegram alert with smoke image immediately!
Stage B (FIRE): runs ONLY when smoke is detected (cooldown gated) in a separate process,
                consumes a small "burst" of recent frames, crops ROI around smoke,
                runs fire engine, writes snapshot images, and SENDS TELEGRAM FIRE ALERT.

Web:
- MJPEG stream:      http://<JETSON_IP>:<PORT>/video_feed
- Snapshots (no-cache): http://<JETSON_IP>:<PORT>/snaps/snap_0.jpg  (and snap_1, snap_2)
- Status JSON:       http://<JETSON_IP>:<PORT>/api/status

Telegram:
- Smoke alert: Sent immediately when smoke detected (with cooldown)
- Fire alert: Sent when fire confirmed in Stage B

Python: compatible with Python 3.6+ (no sys.stdout.reconfigure).

Usage:
    python3 jetson_yolo11_two_stage_mjpeg_server_v5_4_telegram.py \\
        --smoke-engine best_yolo11n_fp16.plan \\
        --fire-engine fire_fp16.engine \\
        --rtsp rtsp://127.0.0.1:8554/fire \\
        --telegram-token "YOUR_BOT_TOKEN" \\
        --telegram-chat "YOUR_CHAT_ID"
"""

from __future__ import print_function

import os
import sys
import time
import argparse
import threading
import queue
import collections
import multiprocessing as mp

import numpy as np
import cv2

# Flask
try:
    from flask import Flask, Response, jsonify, send_from_directory, render_template_string, make_response
except Exception as e:
    print("ERROR: Cannot import Flask:", e)
    sys.exit(1)

# Telegram (using requests - already available on most systems)
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("WARNING: requests not installed. Telegram alerts disabled.")
    print("Install with: pip install requests")


# =========================================================
# TELEGRAM ALERT CLASS (embedded for standalone use)
# =========================================================

class TelegramAlerter(object):
    """Non-blocking Telegram alerter with rate limiting."""

    def __init__(self, bot_token, chat_id, 
                 smoke_cooldown=10.0, fire_cooldown=5.0, 
                 min_confidence=0.3):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.smoke_cooldown = float(smoke_cooldown)
        self.fire_cooldown = float(fire_cooldown)
        self.min_confidence = float(min_confidence)
        
        self._last_smoke_time = 0.0
        self._last_fire_time = 0.0
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=10)
        self._running = True
        
        # Start background sender thread
        self._worker = threading.Thread(target=self._send_worker, daemon=True)
        self._worker.start()
        
        self.enabled = self._validate()

    def _validate(self):
        """Check if Telegram is properly configured."""
        if not HAS_REQUESTS:
            return False
        if not self.bot_token or "YOUR" in str(self.bot_token).upper():
            print("⚠️ Telegram: Bot token not configured")
            return False
        if not self.chat_id or "YOUR" in str(self.chat_id).upper():
            print("⚠️ Telegram: Chat ID not configured")
            return False
        print("✅ Telegram alerts enabled")
        return True

    def _send_worker(self):
        """Background worker to send messages."""
        while self._running:
            try:
                task = self._queue.get(timeout=1.0)
                if task is None:
                    break
                func, args, kwargs = task
                try:
                    func(*args, **kwargs)
                except Exception as e:
                    print("Telegram error:", e)
            except queue.Empty:
                continue

    def _api_url(self, method):
        return "https://api.telegram.org/bot{}/{}".format(self.bot_token, method)

    def _send_photo_sync(self, photo_bytes, caption):
        """Send photo (blocking)."""
        try:
            url = self._api_url("sendPhoto")
            files = {"photo": ("alert.jpg", photo_bytes, "image/jpeg")}
            data = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
            resp = requests.post(url, data=data, files=files, timeout=30)
            if resp.status_code == 200:
                print("✅ Telegram photo sent")
            else:
                print("❌ Telegram error:", resp.status_code)
        except Exception as e:
            print("❌ Telegram send failed:", e)

    def _send_message_sync(self, text):
        """Send text message (blocking)."""
        try:
            url = self._api_url("sendMessage")
            data = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
            resp = requests.post(url, data=data, timeout=10)
            if resp.status_code == 200:
                print("✅ Telegram message sent")
        except Exception as e:
            print("❌ Telegram msg failed:", e)

    def send_smoke_alert(self, frame_bgr, confidence, num_boxes=1, lat=None, lon=None):
        """
        Send smoke alert with image.
        Returns True if sent/queued, False if rate-limited or disabled.
        """
        if not self.enabled:
            return False
        if confidence < self.min_confidence:
            return False
        
        now = time.time()
        with self._lock:
            if now - self._last_smoke_time < self.smoke_cooldown:
                return False
            self._last_smoke_time = now

        # Encode frame to JPEG
        try:
            ok, jpeg = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if not ok:
                return False
            photo_bytes = jpeg.tobytes()
        except Exception as e:
            print("JPEG encode error:", e)
            return False

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        caption = (
            "💨 <b>SMOKE DETECTED!</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            "🎯 Confidence: <b>{:.1f}%</b>\n"
            "📦 Detections: {}\n"
            "🕐 Time: {}\n"
        ).format(confidence * 100, num_boxes, timestamp)

        if lat is not None and lon is not None:
            caption += "📍 Location: {:.6f}, {:.6f}\n".format(lat, lon)

        caption += "━━━━━━━━━━━━━━━━\n"
        caption += "⚠️ <i>Checking for fire...</i>"

        try:
            self._queue.put_nowait((self._send_photo_sync, (photo_bytes, caption), {}))
            print("📨 Smoke alert queued")
            return True
        except queue.Full:
            print("⚠️ Telegram queue full")
            return False

    def send_fire_alert(self, frame_bgr, confidence, num_boxes=1, lat=None, lon=None):
        """
        Send fire confirmation alert with image.
        Returns True if sent/queued, False if rate-limited or disabled.
        """
        if not self.enabled:
            return False
        if confidence < self.min_confidence:
            return False

        now = time.time()
        with self._lock:
            if now - self._last_fire_time < self.fire_cooldown:
                return False
            self._last_fire_time = now

        # Encode frame to JPEG
        try:
            ok, jpeg = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if not ok:
                return False
            photo_bytes = jpeg.tobytes()
        except Exception as e:
            print("JPEG encode error:", e)
            return False

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        caption = (
            "🔥🔥🔥 <b>FIRE CONFIRMED!</b> 🔥🔥🔥\n"
            "━━━━━━━━━━━━━━━━\n"
            "🎯 Confidence: <b>{:.1f}%</b>\n"
            "📦 Detections: {}\n"
            "🕐 Time: {}\n"
        ).format(confidence * 100, num_boxes, timestamp)

        if lat is not None and lon is not None:
            caption += "📍 Location: {:.6f}, {:.6f}\n".format(lat, lon)

        caption += "━━━━━━━━━━━━━━━━\n"
        caption += "🚨 <b>IMMEDIATE ACTION REQUIRED!</b>"

        try:
            self._queue.put_nowait((self._send_photo_sync, (photo_bytes, caption), {}))
            print("📨 FIRE alert queued")
            return True
        except queue.Full:
            print("⚠️ Telegram queue full")
            return False

    def stop(self):
        self._running = False
        try:
            self._queue.put_nowait(None)
        except:
            pass


# =========================================================
# Defaults / Globals
# =========================================================
SNAP_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "static",
    "fire_snaps"
)
os.makedirs(SNAP_DIR, exist_ok=True)

APP_TITLE = "🔥 Jetson Two-Stage Smoke→Fire + Telegram"

# MJPEG queue (keep latest)
frame_queue = queue.Queue(maxsize=2)

status_lock = threading.Lock()
status = {
    "has_smoke": False,
    "has_fire": False,
    "smoke_max_conf": 0.0,
    "fire_max_conf": 0.0,
    "smoke_boxes": 0,
    "fire_boxes": 0,
    "smoke_consec": 0,
    "last_fire_check": None,
    "last_fire_confirm": None,
    "last_fire_snapshot": None,
    "last_smoke_telegram": None,
    "last_fire_telegram": None,
    "timestamp": None,
}

# Colors
COLOR_SMOKE = (180, 180, 180)
COLOR_FIRE = (0, 0, 255)
COLOR_TEXT = (255, 255, 255)

# Flask app
app = Flask(__name__, static_folder="static", static_url_path="/static")

# Global Telegram alerter (set in main)
telegram_alerter = None


# =========================================================
# TensorRT helper (per-process CUDA context)
# =========================================================

class TRTInfer(object):
    def __init__(self, engine_path, input_size):
        self.engine_path = engine_path
        self.input_size = int(input_size)

        import tensorrt as trt
        import pycuda.driver as cuda
        import pycuda.autoinit

        self.trt = trt
        self.cuda = cuda
        self.logger = trt.Logger(trt.Logger.WARNING)

        with open(engine_path, "rb") as f, trt.Runtime(self.logger) as rt:
            self.engine = rt.deserialize_cuda_engine(f.read())

        self.context = self.engine.create_execution_context()

        self.input_idx = None
        self.output_idx = None
        for i in range(self.engine.num_bindings):
            if self.engine.binding_is_input(i):
                self.input_idx = i
            else:
                self.output_idx = i

        in_shape = self.engine.get_binding_shape(self.input_idx)
        if -1 in in_shape:
            in_shape = (1, 3, self.input_size, self.input_size)
            self.context.set_binding_shape(self.input_idx, in_shape)
        self.in_shape = tuple(int(x) for x in in_shape)

        out_shape = self.context.get_binding_shape(self.output_idx)
        self.out_shape = tuple(int(x) for x in out_shape)

        self.h_input = np.empty(self.in_shape, dtype=np.float32)
        self.h_output = np.empty(int(np.prod(self.out_shape)), dtype=np.float32)

        self.d_input = cuda.mem_alloc(self.h_input.nbytes)
        self.d_output = cuda.mem_alloc(self.h_output.nbytes)

        self.bindings = [0] * self.engine.num_bindings
        self.bindings[self.input_idx] = int(self.d_input)
        self.bindings[self.output_idx] = int(self.d_output)

        self.stream = cuda.Stream()

        print("✅ Loaded engine:", engine_path)
        print("   Input shape: ", self.in_shape)
        print("   Output shape:", self.out_shape)

    def preprocess(self, bgr):
        img = cv2.resize(bgr, (self.input_size, self.input_size))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = img.astype(np.float32) / 255.0
        img = np.transpose(img, (2, 0, 1))
        img = np.expand_dims(img, axis=0)
        return np.ascontiguousarray(img)

    def infer(self, bgr):
        import pycuda.driver as cuda
        inp = self.preprocess(bgr)
        cuda.memcpy_htod_async(self.d_input, inp, self.stream)
        self.context.execute_async_v2(self.bindings, self.stream.handle)
        cuda.memcpy_dtoh_async(self.h_output, self.d_output, self.stream)
        self.stream.synchronize()
        return self.h_output.reshape(self.out_shape)


# =========================================================
# Postprocess: NMS + decode
# =========================================================

def nms_xyxy(boxes, scores, iou_thres=0.5):
    if len(boxes) == 0:
        return []
    boxes = np.array(boxes)
    scores = np.array(scores)
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(iou <= iou_thres)[0]
        order = order[inds + 1]
    return keep


def decode_yolov11(out, conf_thres=0.25, img_in_wh=(640, 640), img_out_wh=(640, 640),
                   target_class_id=None, iou_thres=0.45):
    if out is None:
        return []
    arr = np.asarray(out)
    if arr.ndim == 2:
        arr = arr[None, ...]
    if arr.ndim != 3 or arr.shape[0] != 1:
        return []

    cdim = arr.shape[1]
    n = arr.shape[2]
    if n == 0:
        return []

    cx = arr[0, 0, :]
    cy = arr[0, 1, :]
    w = arr[0, 2, :]
    h = arr[0, 3, :]
    conf = arr[0, 4, :]

    if cdim >= 6:
        cls = arr[0, 5, :]
        cls_id = cls.astype(np.int32)
    else:
        cls_id = np.zeros_like(conf, dtype=np.int32)

    keep = conf >= conf_thres
    if (target_class_id is not None) and (cdim >= 6):
        keep = keep & (cls_id == int(target_class_id))

    if not np.any(keep):
        return []

    cx = cx[keep]; cy = cy[keep]; w = w[keep]; h = h[keep]
    conf = conf[keep]
    cls_id = cls_id[keep]

    x1 = cx - w / 2.0
    y1 = cy - h / 2.0
    x2 = cx + w / 2.0
    y2 = cy + h / 2.0

    in_w, in_h = float(img_in_wh[0]), float(img_in_wh[1])
    out_w, out_h = float(img_out_wh[0]), float(img_out_wh[1])
    sx = out_w / in_w
    sy = out_h / in_h

    x1 = x1 * sx; x2 = x2 * sx
    y1 = y1 * sy; y2 = y2 * sy

    boxes = np.stack([x1, y1, x2, y2], axis=1)
    keep_idx = nms_xyxy(boxes, conf, iou_thres=iou_thres)

    dets = []
    for i in keep_idx:
        dets.append({
            "box": [float(boxes[i, 0]), float(boxes[i, 1]), float(boxes[i, 2]), float(boxes[i, 3])],
            "score": float(conf[i]),
            "class_id": int(cls_id[i]),
        })
    return dets


# =========================================================
# ROI utilities
# =========================================================

def union_boxes(dets):
    if not dets:
        return None
    x1 = min(d["box"][0] for d in dets)
    y1 = min(d["box"][1] for d in dets)
    x2 = max(d["box"][2] for d in dets)
    y2 = max(d["box"][3] for d in dets)
    return [x1, y1, x2, y2]


def expand_box(box, margin, W, H):
    x1, y1, x2, y2 = box
    bw = x2 - x1
    bh = y2 - y1
    x1 = x1 - bw * margin
    y1 = y1 - bh * margin
    x2 = x2 + bw * margin
    y2 = y2 + bh * margin
    x1 = max(0, min(W - 1, x1))
    y1 = max(0, min(H - 1, y1))
    x2 = max(0, min(W - 1, x2))
    y2 = max(0, min(H - 1, y2))
    if x2 <= x1 + 2 or y2 <= y1 + 2:
        return [0, 0, W - 1, H - 1]
    return [x1, y1, x2, y2]


def crop_roi(frame_bgr, roi_box):
    x1, y1, x2, y2 = roi_box
    return frame_bgr[int(y1):int(y2), int(x1):int(x2)].copy()


# =========================================================
# Fire worker (separate process) with Telegram support
# =========================================================

def fire_worker_main(fire_engine, fire_input, fire_conf,
                     job_q, out_q, snap_dir, snap_count,
                     iou_thres, fire_class_id,
                     telegram_token, telegram_chat):
    """Fire detection worker process with Telegram alerts."""
    
    # CUDA + TensorRT inside this process
    try:
        fire_trt = TRTInfer(fire_engine, fire_input)
    except Exception as e:
        out_q.put({"ok": False, "error": "Failed to load fire engine: %s" % str(e)})
        return

    # Telegram alerter in this process
    tg = None
    if telegram_token and telegram_chat:
        tg = TelegramAlerter(telegram_token, telegram_chat, 
                             smoke_cooldown=30, fire_cooldown=10)

    try:
        os.makedirs(snap_dir, exist_ok=True)
    except Exception:
        pass

    while True:
        job = job_q.get()
        if job is None:
            break

        t_job = time.time()
        rois_jpeg = job.get("rois_jpeg", [])
        meta = job.get("meta", {})
        results = []
        fire_hits = 0
        best = {"score": 0.0, "img": None, "box": None}

        for jb in rois_jpeg:
            try:
                npbuf = np.frombuffer(jb, dtype=np.uint8)
                roi = cv2.imdecode(npbuf, cv2.IMREAD_COLOR)
                if roi is None:
                    continue

                H, W = roi.shape[:2]
                out = fire_trt.infer(roi)
                cdim = int(out.shape[1]) if hasattr(out, 'shape') and len(out.shape) >= 2 else 5
                tcls = fire_class_id if (fire_class_id is not None and cdim == 6) else None
                dets = decode_yolov11(
                    out,
                    conf_thres=fire_conf,
                    img_in_wh=(fire_input, fire_input),
                    img_out_wh=(W, H),
                    target_class_id=tcls,
                    iou_thres=iou_thres
                )

                dets = [d for d in dets if (d["box"][2] - d["box"][0]) * (d["box"][3] - d["box"][1]) > 4.0]

                if dets:
                    fire_hits += 1
                    top = max(dets, key=lambda d: d["score"])
                    if top["score"] > best["score"]:
                        best["score"] = float(top["score"])
                        best["img"] = roi
                        best["box"] = top["box"]

                results.append({"dets": dets, "W": W, "H": H})

            except Exception:
                continue

        # Write snapshots and send Telegram ONLY when fire found
        saved = []
        if best["img"] is not None:
            img = best["img"].copy()
            bx = best["box"]
            cv2.rectangle(img, (int(bx[0]), int(bx[1])), (int(bx[2]), int(bx[3])), COLOR_FIRE, 2)
            cv2.putText(img, "fire %.1f%%" % (best["score"] * 100.0),
                        (int(bx[0]), max(0, int(bx[1]) - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, COLOR_TEXT, 2)
            # banner
            cv2.rectangle(img, (0, 0), (img.shape[1], 48), COLOR_FIRE, -1)
            cv2.putText(img, "FIRE CONFIRM conf=%.1f%%" % (best["score"] * 100.0),
                        (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, COLOR_TEXT, 2)

            for i in range(int(snap_count)):
                fn = "snap_%d.jpg" % i
                fp = os.path.join(snap_dir, fn)
                try:
                    cv2.imwrite(fp, img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                    saved.append(fn)
                except Exception:
                    pass

            # 🔥 SEND TELEGRAM FIRE ALERT
            if tg and tg.enabled:
                tg.send_fire_alert(img, best["score"], num_boxes=fire_hits)

        out_q.put({
            "ok": True,
            "fire_hits": int(fire_hits),
            "best_score": float(best["score"]),
            "saved": saved,
            "meta": meta,
            "t_job": t_job,
            "t_done": time.time(),
        })


# =========================================================
# Flask routes
# =========================================================

@app.route("/")
def index():
    html = """
    <html>
      <head>
        <title>{{title}}</title>
        <style>
          body { background:#111; color:#eee; font-family: Arial; text-align:center; }
          .wrap { max-width: 1100px; margin: 0 auto; padding: 12px; }
          .row { display:flex; gap:12px; justify-content:center; flex-wrap:wrap; }
          img { border: 2px solid #333; border-radius: 8px; }
          .card { background:#1b1b1b; padding: 10px; border-radius: 10px; }
          .small { color:#9aa; font-size: 12px; }
          a { color:#7bd; }
          .telegram-badge { background: #0088cc; color: white; padding: 5px 10px; border-radius: 5px; margin: 10px; }
        </style>
      </head>
      <body>
        <div class="wrap">
          <h2>🔥 Jetson Two-Stage Smoke→Fire + Telegram</h2>
          <div class="telegram-badge">📱 Telegram Alerts Enabled</div>
          <div class="card">
            <div class="small">
              MJPEG: <a href="/video_feed" target="_blank">/video_feed</a> |
              Status: <a href="/api/status" target="_blank">/api/status</a>
            </div>
            <div style="margin-top:10px;">
              <img src="/video_feed" style="max-width:100%;" />
            </div>
          </div>

          <h3 style="margin-top:18px;">📸 Fire Snapshots</h3>
          <div id="lastFireLabel" style="color:#bbb; font-size:14px; margin-top:-8px; margin-bottom:10px;">
            LAST FIRE SNAPSHOT: --
          </div>
          <div class="row">
            <div class="card">
              <img id="s0" src="/snaps/snap_0.jpg?t=0" width="320"/>
            </div>
            <div class="card">
              <img id="s1" src="/snaps/snap_1.jpg?t=0" width="320"/>
            </div>
            <div class="card">
              <img id="s2" src="/snaps/snap_2.jpg?t=0" width="320"/>
            </div>
          </div>

          <script>
            function refreshSnaps(){
              const t = Date.now();
              document.getElementById("s0").src = "/snaps/snap_0.jpg?t=" + t;
              document.getElementById("s1").src = "/snaps/snap_1.jpg?t=" + t;
              document.getElementById("s2").src = "/snaps/snap_2.jpg?t=" + t;
            }

            async function refreshStatus(){
              try {
                const r = await fetch('/api/status', {cache: 'no-store'});
                const s = await r.json();
                const ts = s.last_fire_snapshot || s.last_fire_confirm || null;
                const el = document.getElementById('lastFireLabel');
                if (!el) return;
                if (ts) {
                  el.textContent = 'LAST FIRE SNAPSHOT: ' + ts;
                } else {
                  el.textContent = 'LAST FIRE SNAPSHOT: (none yet)';
                }
              } catch(e) {}
            }

            setInterval(refreshSnaps, 800);
            setInterval(refreshStatus, 1000);
            refreshStatus();
          </script>
        </div>
      </body>
    </html>
    """
    return render_template_string(html, title=APP_TITLE)


@app.route("/api/status")
def api_status():
    with status_lock:
        return jsonify(status)


@app.route("/api/fire_status")
def api_fire_status():
    with status_lock:
        return jsonify(status)


@app.route("/snaps/<path:filename>")
def snaps(filename):
    resp = make_response(send_from_directory(os.path.join(app.root_path, "static", "fire_snaps"), filename))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/video_feed")
def video_feed():
    return Response(mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


def mjpeg_generator():
    placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(placeholder, "Waiting...", (200, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)

    while True:
        try:
            frame = frame_queue.get(timeout=1.0)
            ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" +
                       jpeg.tobytes() + b"\r\n")
        except queue.Empty:
            ok, jpeg = cv2.imencode(".jpg", placeholder)
            if ok:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" +
                       jpeg.tobytes() + b"\r\n")


# =========================================================
# Main loop (Smoke inference) + trigger Fire checks + Telegram
# =========================================================

def run_loop(args):
    global telegram_alerter
    
    # Smoke engine in main process
    smoke_trt = TRTInfer(args.smoke_engine, args.smoke_input)

    # Telegram alerter in main process (for smoke alerts)
    if args.telegram_token and args.telegram_chat:
        telegram_alerter = TelegramAlerter(
            args.telegram_token, 
            args.telegram_chat,
            smoke_cooldown=args.telegram_smoke_cooldown,
            fire_cooldown=args.telegram_fire_cooldown,
            min_confidence=args.telegram_min_conf
        )
    else:
        telegram_alerter = None
        print("⚠️ Telegram not configured - alerts disabled")

    # RTSP capture
    cap = cv2.VideoCapture(args.rtsp)
    if not cap.isOpened():
        print("❌ Cannot open RTSP:", args.rtsp)
        return

    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass

    # Rolling buffer of recent frames (JPEG bytes)
    ring = collections.deque(maxlen=60)

    # Fire worker queues
    job_q = mp.Queue(maxsize=1)
    out_q = mp.Queue(maxsize=2)

    fire_proc = mp.Process(
        target=fire_worker_main,
        args=(
            args.fire_engine,
            args.fire_input,
            args.fire_conf,
            job_q,
            out_q,
            args.snap_dir,
            args.snap_count,
            args.iou_thres,
            args.fire_class_id,
            args.telegram_token,
            args.telegram_chat,
        ),
        daemon=True
    )

    fire_proc.start()
    print("✅ Fire worker started")

    # Control variables
    smoke_consec = 0
    last_fire_check = 0.0
    fire_hold_until = 0.0
    last_fire_confirm = 0.0
    last_fire_snapshot = 0.0
    last_smoke_telegram = 0.0
    smoke_alert_sent = False  # Track if we already sent smoke alert for this detection
    last_fps_t0 = time.time()
    fps_counter = 0

    print("🚀 Loop started. Snap folder:", args.snap_dir)

    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            time.sleep(0.02)
            continue

        H, W = frame.shape[:2]

        # Store frame in ring buffer
        try:
            okj, jb = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if okj:
                ring.append(jb.tobytes())
        except Exception:
            pass

        # Smoke inference
        out = smoke_trt.infer(frame)
        dets = decode_yolov11(
            out,
            conf_thres=args.smoke_conf,
            img_in_wh=(args.smoke_input, args.smoke_input),
            img_out_wh=(W, H),
            target_class_id=args.smoke_class_id,
            iou_thres=args.iou_thres
        )

        # Filter by min area ratio
        min_area = float(args.smoke_min_area) * float(W * H)
        smoke_dets = []
        smoke_max = 0.0
        for d in dets:
            x1, y1, x2, y2 = d["box"]
            area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
            if area >= min_area:
                smoke_dets.append(d)
                if d["score"] > smoke_max:
                    smoke_max = d["score"]

        has_smoke = len(smoke_dets) > 0

        if has_smoke:
            smoke_consec += 1
        else:
            smoke_consec = 0
            smoke_alert_sent = False  # Reset for next detection

        smoke_confirmed = smoke_consec >= int(args.smoke_consec)

        now = time.time()

        # =====================================================
        # 💨 TELEGRAM SMOKE ALERT - Send immediately on first detection!
        # =====================================================
        if smoke_confirmed and not smoke_alert_sent and telegram_alerter:
            # Draw boxes on frame for alert image
            alert_frame = frame.copy()
            for d in smoke_dets:
                x1, y1, x2, y2 = d["box"]
                cv2.rectangle(alert_frame, (int(x1), int(y1)), (int(x2), int(y2)), COLOR_SMOKE, 2)
                cv2.putText(alert_frame, "smoke %.1f%%" % (d["score"] * 100.0),
                            (int(x1), max(0, int(y1) - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, COLOR_TEXT, 2)
            
            # Add banner
            cv2.rectangle(alert_frame, (0, 0), (W, 40), (80, 80, 80), -1)
            cv2.putText(alert_frame, "SMOKE DETECTED conf=%.1f%%" % (smoke_max * 100.0),
                        (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.85, COLOR_TEXT, 2)
            
            # Send Telegram alert
            if telegram_alerter.send_smoke_alert(alert_frame, smoke_max, num_boxes=len(smoke_dets)):
                smoke_alert_sent = True
                last_smoke_telegram = now
                with status_lock:
                    status["last_smoke_telegram"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        # Decide to trigger fire check
        should_check_fire = smoke_confirmed and (now - last_fire_check >= float(args.fire_check_cooldown))

        # Drain fire results
        fire_hit = 0
        fire_best = 0.0
        try:
            while True:
                res = out_q.get_nowait()
                if res.get("ok"):
                    fire_hit = int(res.get("fire_hits", 0))
                    fire_best = float(res.get("best_score", 0.0))
                    saved_files = res.get("saved", []) or []
                    last_fire_check = now
                    if fire_hit >= int(args.fire_confirm):
                        fire_hold_until = now + float(args.fire_hold)
                        last_fire_confirm = now
                        if saved_files:
                            last_fire_snapshot = now
                            with status_lock:
                                status["last_fire_telegram"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                else:
                    print("⚠️ Fire worker error:", res.get("error"))
        except Exception:
            pass

        has_fire = (now < fire_hold_until)

        if should_check_fire and not has_fire:
            # Build burst from ring
            burst = []
            stride = max(1, int(args.burst_stride))
            need = int(args.burst_frames)

            ub = union_boxes(smoke_dets)
            if ub is None:
                ub = [0, 0, W - 1, H - 1]
            roi_box = expand_box(ub, float(args.roi_margin), W, H)

            idx = len(ring) - 1
            taken = 0
            while idx >= 0 and taken < need:
                try:
                    npbuf = np.frombuffer(ring[idx], dtype=np.uint8)
                    fr = cv2.imdecode(npbuf, cv2.IMREAD_COLOR)
                    if fr is None:
                        idx -= stride
                        continue
                    rr = crop_roi(fr, roi_box)
                    okr, jbr = cv2.imencode(".jpg", rr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                    if okr:
                        burst.append(jbr.tobytes())
                        taken += 1
                except Exception:
                    pass
                idx -= stride

            if len(burst) == 0:
                rr = crop_roi(frame, roi_box)
                okr, jbr = cv2.imencode(".jpg", rr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                if okr:
                    burst = [jbr.tobytes()]

            job = {
                "rois_jpeg": burst,
                "meta": {
                    "ts": now,
                    "roi_box": [float(x) for x in roi_box],
                    "smoke_max": float(smoke_max),
                    "smoke_consec": int(smoke_consec),
                }
            }

            try:
                job_q.put_nowait(job)
                with status_lock:
                    status["last_fire_check"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            except Exception:
                pass

            last_fire_check = now

        # Draw smoke boxes
        for d in smoke_dets:
            x1, y1, x2, y2 = d["box"]
            cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), COLOR_SMOKE, 2)
            cv2.putText(frame, "smoke %.1f%%" % (d["score"] * 100.0),
                        (int(x1), max(0, int(y1) - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, COLOR_TEXT, 2)

        # Banner
        if smoke_confirmed:
            cv2.rectangle(frame, (0, 0), (W, 40), (80, 80, 80), -1)
            cv2.putText(frame, "SMOKE WARNING conf=%.1f%% consec=%d" % (smoke_max * 100.0, smoke_consec),
                        (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.85, COLOR_TEXT, 2)

        if has_fire:
            cv2.rectangle(frame, (0, 40), (W, 80), COLOR_FIRE, -1)
            cv2.putText(frame, "FIRE CONFIRMED hold=%.1fs" % max(0.0, fire_hold_until - now),
                        (10, 68), cv2.FONT_HERSHEY_SIMPLEX, 0.85, COLOR_TEXT, 2)

        # FPS
        fps_counter += 1
        dt = time.time() - last_fps_t0
        if dt >= 1.0:
            fps = fps_counter / dt
            fps_counter = 0
            last_fps_t0 = time.time()
            cv2.putText(frame, "FPS: %.1f" % fps, (10, H - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        # Update status
        with status_lock:
            status["has_smoke"] = bool(smoke_confirmed)
            status["has_fire"] = bool(has_fire)
            status["smoke_max_conf"] = float(smoke_max)
            status["fire_max_conf"] = float(fire_best)
            status["smoke_boxes"] = int(len(smoke_dets))
            status["fire_boxes"] = int(fire_hit)
            status["smoke_consec"] = int(smoke_consec)
            status["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if last_fire_confirm > 0:
                status["last_fire_confirm"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(last_fire_confirm))
            if last_fire_snapshot > 0:
                status["last_fire_snapshot"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(last_fire_snapshot))

        # Push frame to MJPEG queue
        try:
            frame_queue.put_nowait(frame)
        except queue.Full:
            try:
                frame_queue.get_nowait()
                frame_queue.put_nowait(frame)
            except Exception:
                pass

        time.sleep(0.002)


# =========================================================
# CLI / Main
# =========================================================

def main():
    parser = argparse.ArgumentParser(
        description="Jetson Two-Stage MJPEG Server with Telegram Alerts"
    )
    
    # Model settings
    parser.add_argument("--smoke-engine", type=str, required=True, 
                        help="TensorRT engine for smoke")
    parser.add_argument("--fire-engine", type=str, required=True, 
                        help="TensorRT engine for fire")
    parser.add_argument("--rtsp", type=str, default="rtsp://127.0.0.1:8554/fire", 
                        help="RTSP URL")
    parser.add_argument("--port", type=int, default=5002, help="Flask port")

    parser.add_argument("--smoke-input", type=int, default=416, 
                        help="Smoke model input size")
    parser.add_argument("--fire-input", type=int, default=640, 
                        help="Fire model input size")

    parser.add_argument("--smoke-class-id", type=int, default=0,
                        help="Smoke class ID (0 for smoke in wildfire dataset)")
    parser.add_argument("--fire-class-id", type=int, default=0,
                        help="Fire class ID")

    parser.add_argument("--smoke-conf", type=float, default=0.30, 
                        help="Smoke confidence threshold")
    parser.add_argument("--fire-conf", type=float, default=0.50, 
                        help="Fire confidence threshold")

    parser.add_argument("--smoke-min-area", type=float, default=0.002, 
                        help="Min smoke box area ratio")
    parser.add_argument("--smoke-consec", type=int, default=3, 
                        help="Consecutive frames to confirm smoke")

    parser.add_argument("--burst-frames", type=int, default=6, 
                        help="Frames for fire check")
    parser.add_argument("--burst-stride", type=int, default=2, 
                        help="Stride when sampling burst")
    parser.add_argument("--roi-margin", type=float, default=0.35, 
                        help="ROI margin around smoke")

    parser.add_argument("--fire-confirm", type=int, default=2, 
                        help="Fire hits needed to confirm")
    parser.add_argument("--fire-check-cooldown", type=float, default=3.0, 
                        help="Seconds between fire checks")
    parser.add_argument("--fire-hold", type=float, default=3.0, 
                        help="Hold FIRE state duration")

    parser.add_argument("--iou-thres", type=float, default=0.45, 
                        help="NMS IoU threshold")

    parser.add_argument("--snap-dir", type=str, default=None, 
                        help="Snapshot folder")
    parser.add_argument("--snap-count", type=int, default=3, 
                        help="Number of snapshots")

    # ===== TELEGRAM SETTINGS =====
    parser.add_argument("--telegram-token", type=str, default=None,
                        help="Telegram Bot API token")
    parser.add_argument("--telegram-chat", type=str, default=None,
                        help="Telegram Chat ID")
    parser.add_argument("--telegram-smoke-cooldown", type=float, default=15.0,
                        help="Min seconds between smoke Telegram alerts")
    parser.add_argument("--telegram-fire-cooldown", type=float, default=10.0,
                        help="Min seconds between fire Telegram alerts")
    parser.add_argument("--telegram-min-conf", type=float, default=0.3,
                        help="Min confidence for Telegram alerts")

    args = parser.parse_args()

    # Snap dir
    if args.snap_dir is None:
        args.snap_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fire_snaps")
    try:
        os.makedirs(args.snap_dir, exist_ok=True)
    except Exception:
        pass

    # Create placeholder snapshots
    for i in range(int(args.snap_count)):
        p = os.path.join(args.snap_dir, "snap_%d.jpg" % i)
        if not os.path.exists(p):
            ph = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.putText(ph, "NO FIRE SNAPSHOT YET", (20, 200), 
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2, cv2.LINE_AA)
            cv2.imwrite(p, ph)

    print("=" * 72)
    print("🔥 Two-Stage + TELEGRAM ALERTS v5.4")
    print("=" * 72)
    print("RTSP:", args.rtsp)
    print("Port:", args.port)
    print("Smoke engine:", args.smoke_engine, " input=", args.smoke_input)
    print("Fire  engine:", args.fire_engine,  " input=", args.fire_input)
    print("=" * 72)
    
    if args.telegram_token and args.telegram_chat:
        print("📱 TELEGRAM ALERTS: ENABLED")
        print("   Token:", args.telegram_token[:10] + "..." if len(args.telegram_token) > 10 else args.telegram_token)
        print("   Chat ID:", args.telegram_chat)
        print("   Smoke cooldown:", args.telegram_smoke_cooldown, "s")
        print("   Fire cooldown:", args.telegram_fire_cooldown, "s")
    else:
        print("📱 TELEGRAM ALERTS: DISABLED (no token/chat provided)")
    
    print("=" * 72)

    # Start Flask in separate thread
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True),
        daemon=True
    )
    flask_thread.start()

    # Run main loop
    run_loop(args)


if __name__ == "__main__":
    try:
        mp.set_start_method("spawn", force=True)
    except Exception:
        pass
    main()


