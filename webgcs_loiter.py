# ============================================================
# PATCHED BY patch_webgcs_v7.py
# Version: 7.0
# Patched at: 2026-01-07T14:49:47.091930
# Changes:
#   - send_mavlink_command: Full mode support (LOITER, AUTO, etc.)
#   - MissionPlanner: Save mode before pause
#   - poll_jetson: Proper LOITER command on smoke detection
#   - API: resume_mission returns resume_mode
#   - UI: Enhanced smokePausePanel with animation
#   - JS: Alert sound and browser notification
# ============================================================

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import random
import time
import threading
import os
import json
from datetime import datetime
import math
import requests
from collections import deque
import io

# ==========================
# CẤU HÌNH JETSON FIRE SERVER
# ==========================
JETSON_FIRE_API_BASE = "http://192.168.46.117:5002"
JETSON_FIRE_STATUS_URL = f"{JETSON_FIRE_API_BASE}/api/status"

# ==========================
# CẤU HÌNH TELEGRAM BOT (BACKUP ALERTS TỪ WINDOWS)
# ==========================
# Hướng dẫn:
# 1. Mở Telegram, tìm @BotFather, gửi /newbot
# 2. Copy token và paste vào TELEGRAM_BOT_TOKEN
# 3. Gửi tin nhắn cho bot, truy cập: https://api.telegram.org/bot<TOKEN>/getUpdates
# 4. Lấy chat_id từ response
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8537225731:AAE810E6yx-qyqWDoT0yFZFBGG_DiCxIfkY")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7957935827")
TELEGRAM_ENABLED = True  # ✅ Telegram đã bật!

# Telegram rate limiting
TELEGRAM_SMOKE_COOLDOWN = 15.0  # seconds
TELEGRAM_FIRE_COOLDOWN = 10.0   # seconds
_last_telegram_smoke = 0.0
_last_telegram_fire = 0.0
_telegram_lock = threading.Lock()


# ==========================
# CẤU HÌNH JETSON STREAM (MODEL 1: SMOKE) + SNAPSHOT (MODEL 2: FIRE)
# ==========================
# Model 1 (Smoke) MJPEG URL (chỉ hiển thị video)
JETSON_SMOKE_VIDEO_URL = f"{JETSON_FIRE_API_BASE}/video_feed"
# Model 2 (Fire) snapshot folder (3 ảnh tĩnh bbox lửa)
JETSON_FIRE_SNAP_BASE = f"{JETSON_FIRE_API_BASE}/snaps"  # no-cache proxy on Jetson

# ==========================
# FIRE/SMOKE ALERT STATE (shared)
# ==========================
ALERT_HISTORY_MAX = 50

ALERT_WINDOW_SEC = 3.0   # gom alert trong 3 giây

# ==========================
# REAL-TIME SMOKE PAUSE (LOITER)
# ==========================
# The alert window is for logging/notification aggregation.
# For real-time behavior (drone should stop near the smoke), we trigger LOITER earlier
# when smoke is seen consistently above a threshold.
SMOKE_PAUSE_MIN_CONF = 0.6              # 0..1
SMOKE_PAUSE_CONSECUTIVE_POLLS = 2       # poll interval is ~0.5s => 2 polls ≈ 1s
SMOKE_PAUSE_COOLDOWN_SEC = 20.0         # prevent repeated LOITER triggers

_smoke_pause_consecutive = 0
_last_smoke_pause_ts = 0.0

_alert_window = {
    "type": None,        # "SMOKE" | "FIRE"
    "max_conf": 0.0,
    "lat": None,
    "lon": None,
    "boxes": [],
    "start_ts": None
}


fire_state = {
    "has_smoke": False,
    "has_fire": False,

    # confidence
    "smoke_conf": 0.0,
    "fire_conf": 0.0,     # fire confidence from Jetson
    "max_conf": 0.0,      # alias (frontend may use max_conf)

    # boxes info (optional if Jetson provides)
    "num_boxes": 0,
    "boxes": [],          # list of bbox dicts if provided

    # last detection info
    "last_lat": None,
    "last_lon": None,
    "last_timestamp": None,  # ISO or HH:MM:SS
    "last_event": "none",    # "smoke" | "fire" | "none"

    # history
    "total_alerts": 0,
    "alert_history": []      # newest last, each item: {id,type,time,conf,lat,lon,num_boxes,boxes}
}

fire_state_lock = threading.Lock()

# rising-edge tracking
_last_smoke_flag = False
_last_fire_flag = False

# deque for history
_alert_history = deque(maxlen=ALERT_HISTORY_MAX)

def _push_alert(alert_type: str, conf: float, lat, lon, boxes=None, num_boxes: int = 0):
    """
    Thêm 1 alert vào fire_state['alert_history'] và tăng total_alerts.
    alert_type: "SMOKE" | "FIRE"
    """
    if boxes is None:
        boxes = []

    fire_state["total_alerts"] = int(fire_state.get("total_alerts", 0) or 0) + 1
    alert_id = fire_state["total_alerts"]

    item = {
        "id": alert_id,
        "type": alert_type,
        "time": datetime.now().strftime("%H:%M:%S"),
        "conf": round(float(conf or 0.0), 3),
        "lat": None if lat is None else round(float(lat), 6),
        "lon": None if lon is None else round(float(lon), 6),
        "num_boxes": int(num_boxes or (len(boxes) if boxes else 0)),
        "boxes": boxes if boxes else []
    }

    hist = fire_state.get("alert_history", [])
    hist.append(item)

    # giới hạn lịch sử (đỡ phình)
    MAX_HIST = 200
    if len(hist) > MAX_HIST:
        hist[:] = hist[-MAX_HIST:]

    fire_state["alert_history"] = hist
    return item


# ==========================
# TELEGRAM ALERTER CLASS (WINDOWS BACKUP)
# ==========================
class TelegramAlerterGCS:
    """
    Telegram alerter chạy trên Windows GCS.
    Gửi alert kèm GPS location của drone.
    """
    
    def __init__(self, bot_token, chat_id, enabled=False):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and self._validate()
        self._queue = deque(maxlen=10)
        self._running = True
        
        if self.enabled:
            self._worker = threading.Thread(target=self._send_worker, daemon=True)
            self._worker.start()
            print("✅ Telegram GCS alerts enabled")
        else:
            print("⚠️ Telegram GCS alerts disabled")
    
    def _validate(self):
        if "YOUR" in str(self.bot_token).upper() or "YOUR" in str(self.chat_id).upper():
            return False
        return True
    
    def _send_worker(self):
        while self._running:
            try:
                if self._queue:
                    task = self._queue.popleft()
                    self._execute_task(task)
                else:
                    time.sleep(0.1)
            except Exception as e:
                print(f"Telegram worker error: {e}")
                time.sleep(1)
    
    def _execute_task(self, task):
        try:
            task_type = task.get("type")
            if task_type == "photo":
                self._send_photo_sync(task["photo"], task["caption"])
            elif task_type == "message":
                self._send_message_sync(task["text"])
        except Exception as e:
            print(f"Telegram task error: {e}")
    
    def _api_url(self, method):
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"
    
    def _send_message_sync(self, text):
        try:
            resp = requests.post(
                self._api_url("sendMessage"),
                data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10
            )
            if resp.status_code == 200:
                print("✅ Telegram message sent (GCS)")
        except Exception as e:
            print(f"❌ Telegram message failed: {e}")
    
    def _send_photo_sync(self, photo_bytes, caption):
        try:
            files = {"photo": ("alert.jpg", photo_bytes, "image/jpeg")}
            resp = requests.post(
                self._api_url("sendPhoto"),
                data={"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"},
                files=files,
                timeout=30
            )
            if resp.status_code == 200:
                print("✅ Telegram photo sent (GCS)")
        except Exception as e:
            print(f"❌ Telegram photo failed: {e}")
    
    def _download_snapshot(self, snap_url):
        """Download snapshot từ Jetson."""
        try:
            resp = requests.get(snap_url, timeout=5)
            if resp.status_code == 200:
                return resp.content
        except Exception:
            pass
        return None
    
    def send_smoke_alert(self, conf, lat=None, lon=None, snap_url=None):
        """Gửi smoke alert với GPS location."""
        if not self.enabled:
            return False
        
        global _last_telegram_smoke
        now = time.time()
        with _telegram_lock:
            if now - _last_telegram_smoke < TELEGRAM_SMOKE_COOLDOWN:
                return False
            _last_telegram_smoke = now
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        caption = (
            "💨 <b>SMOKE DETECTED!</b> (GCS Backup)\n"
            "━━━━━━━━━━━━━━━━\n"
            f"🎯 Confidence: <b>{conf*100:.1f}%</b>\n"
            f"🕐 Time: {timestamp}\n"
        )
        
        if lat is not None and lon is not None:
            caption += f"📍 Drone Location: {lat:.6f}, {lon:.6f}\n"
            caption += f"🗺 <a href='https://maps.google.com/?q={lat},{lon}'>View on Map</a>\n"
        
        caption += "━━━━━━━━━━━━━━━━\n"
        caption += "⚠️ <i>Checking for fire...</i>"
        
        # Download snapshot if available
        photo_bytes = None
        if snap_url:
            photo_bytes = self._download_snapshot(snap_url)
        
        if photo_bytes:
            self._queue.append({"type": "photo", "photo": photo_bytes, "caption": caption})
        else:
            self._queue.append({"type": "message", "text": caption})
        
        return True
    
    def send_fire_alert(self, conf, lat=None, lon=None, snap_url=None):
        """Gửi fire alert với GPS location."""
        if not self.enabled:
            return False
        
        global _last_telegram_fire
        now = time.time()
        with _telegram_lock:
            if now - _last_telegram_fire < TELEGRAM_FIRE_COOLDOWN:
                return False
            _last_telegram_fire = now
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        caption = (
            "🔥🔥🔥 <b>FIRE CONFIRMED!</b> (GCS) 🔥🔥🔥\n"
            "━━━━━━━━━━━━━━━━\n"
            f"🎯 Confidence: <b>{conf*100:.1f}%</b>\n"
            f"🕐 Time: {timestamp}\n"
        )
        
        if lat is not None and lon is not None:
            caption += f"📍 Drone Location: {lat:.6f}, {lon:.6f}\n"
            caption += f"🗺 <a href='https://maps.google.com/?q={lat},{lon}'>View on Map</a>\n"
        
        caption += "━━━━━━━━━━━━━━━━\n"
        caption += "🚨 <b>IMMEDIATE ACTION REQUIRED!</b>"
        
        # Download snapshot
        photo_bytes = None
        if snap_url:
            photo_bytes = self._download_snapshot(snap_url)
        
        if photo_bytes:
            self._queue.append({"type": "photo", "photo": photo_bytes, "caption": caption})
        else:
            self._queue.append({"type": "message", "text": caption})
        
        return True


# Global Telegram alerter instance (initialized in main)
telegram_gcs = None


# Try to import MAVLink dependencies
try:
    from pymavlink import mavutil
    import serial.tools.list_ports

    HAS_MAVLINK = True
except ImportError:
    HAS_MAVLINK = False
    print("MAVLink dependencies not found - running in simulation mode")
    print("Install with: pip install pymavlink pyserial")


# Enhanced Mission Planning System with Waypoint Actions - FIXED VERSION
class MissionPlanner:
    def __init__(self):
        self.missions = {}
        self.current_mission_id = None
        self.mission_status = "READY"
        self.current_action = None
        self.action_complete = True
        self.mission_started = False
        self.home_position = {"lat": 10.794943646452133, "lng": 106.73693924971897, "alt": 0}
        self._mission_lock = threading.RLock()  # FIX: Thread safety

        # Prevent repeatedly re-triggering the same waypoint action from polling loops.
        self._last_action_trigger_mission_id = None
        self._last_action_trigger_wp_index = None  # 0-based
        
        # 🔥 SMOKE DETECTION PAUSE
        self.paused_by_smoke = False
        self.smoke_pause_location = None  # {"lat": x, "lng": y, "alt": z}
        self.mode_before_pause = None  # V7: Lưu mode trước khi pause để resume đúng

    def set_home_position(self, lat, lng, alt=0):
        with self._mission_lock:
            self.home_position = {"lat": lat, "lng": lng, "alt": alt}
        return self.home_position

    def get_home_position(self):
        return self.home_position

    def create_mission(self, name, waypoints):
        with self._mission_lock:
            mission_id = f"mission_{int(time.time())}"
            if waypoints and len(waypoints) > 0:
                waypoints[0]['lat'] = self.home_position['lat']
                waypoints[0]['lng'] = self.home_position['lng']
                waypoints[0]['alt'] = self.home_position['alt']
            else:
                waypoints = [{
                    'lat': self.home_position['lat'],
                    'lng': self.home_position['lng'],
                    'alt': self.home_position['alt'],
                    'seq': 0
                }]

            mission = {
                'id': mission_id,
                'name': name,
                'waypoints': waypoints,
                'created_at': datetime.now().isoformat(),
                'status': 'READY',
                'current_wp_index': 0,
                'home_position': self.home_position
            }
            self.missions[mission_id] = mission
            return mission_id

    def get_mission(self, mission_id):
        return self.missions.get(mission_id)

    def get_all_missions(self):
        return list(self.missions.values())

    def set_current_mission(self, mission_id):
        with self._mission_lock:
            if mission_id in self.missions:
                self.current_mission_id = mission_id
                self.missions[mission_id]['status'] = 'ACTIVE'
                self.current_action = None
                self.action_complete = True
                self.mission_started = False
                return True
            return False

    def start_mission_execution(self, vehicle_data, socketio):
        with self._mission_lock:
            if not self.current_mission_id:
                return False

            self.mission_started = True
            self.action_complete = True
            mission = self.missions[self.current_mission_id]
            mission['current_wp_index'] = 0
            mission['status'] = 'ACTIVE'

            socketio.emit('log', {
                'message': '🚀 Mission execution started! Ready for takeoff...',
                'type': 'success',
                'timestamp': datetime.now().isoformat()
            })
            return True

    def get_current_waypoint(self):
        if not self.current_mission_id:
            return None
        mission = self.missions[self.current_mission_id]
        current_index = mission['current_wp_index']
        if current_index < len(mission['waypoints']):
            return mission['waypoints'][current_index]
        return None

    def execute_waypoint_action(self, vehicle_data, socketio):
        with self._mission_lock:
            print(f"🎬 DEBUG: execute_waypoint_action called - mission_id: {self.current_mission_id}, action_complete: {self.action_complete}, mission_started: {self.mission_started}")
            
            if not self.current_mission_id or not self.action_complete or not self.mission_started:
                print(f"⚠️ DEBUG: execute_waypoint_action exiting early - conditions not met")
                return False

            current_wp = self.get_current_waypoint()
            if not current_wp:
                print(f"⚠️ DEBUG: No current waypoint available")
                self.action_complete = True
                return True

            current_index0 = self.missions[self.current_mission_id]["current_wp_index"]
            if (
                self._last_action_trigger_mission_id == self.current_mission_id
                and self._last_action_trigger_wp_index == current_index0
            ):
                print(f"⏭️ DEBUG: Skipping duplicate action trigger for WP{current_index0 + 1}")
                return True

            # Mark this waypoint action as triggered (prevents re-entry before we advance).
            self._last_action_trigger_mission_id = self.current_mission_id
            self._last_action_trigger_wp_index = current_index0

            wp_index = self.missions[self.current_mission_id]["current_wp_index"] + 1
            action = current_wp.get('action')
            action_type = action.get('type') if action else 'none'
            
            print(f"🎬 DEBUG: Starting WP{wp_index} - action: {action_type}, coordinates: ({current_wp.get('lat'):.6f}, {current_wp.get('lng'):.6f}, {current_wp.get('alt')}m)")
            
            # Special handling: If action is takeoff, execute immediately without navigation
            if action and action.get('type') == 'takeoff':
                print(f"🚀 DEBUG: Waypoint {wp_index} is TAKEOFF - executing immediately without navigation")
                self.current_action = action
                self.action_complete = False
                
                socketio.emit('log', {
                    'message': f'🎯 Executing TAKEOFF at WP {wp_index}',
                    'type': 'info',
                    'timestamp': datetime.now().isoformat()
                })
                
                # Start takeoff thread - it will handle setting action_complete and advancing
                self._execute_takeoff(vehicle_data, socketio, action)
                # DO NOT return True here - the async thread will handle advancement
                return  # Just return without a value to exit the function
            
            # For all other waypoints, navigate to position first
            wp_lat = current_wp.get('lat')
            wp_lon = current_wp.get('lng') or current_wp.get('lon')
            wp_alt = current_wp.get('alt', 10)
            
            socketio.emit('log', {
                'message': f'🎯 Navigating to Waypoint {wp_index}: ({wp_lat:.6f}, {wp_lon:.6f}, {wp_alt}m)',
                'type': 'info',
                'timestamp': datetime.now().isoformat()
            })
            
            # Send command to fly to waypoint coordinates
            if windows_telemetry.connected and wp_lat and wp_lon:
                print(f"🎯 DEBUG: Sending waypoint command to ({wp_lat:.6f}, {wp_lon:.6f}, {wp_alt}m)")
                windows_telemetry.send_waypoint_command(wp_lat, wp_lon, wp_alt)
                print(f"🎯 DEBUG: Waypoint command sent, starting navigation thread")
                
                # Wait for drone to reach waypoint before executing action
                def wait_and_execute_action():
                    start_time = time.time()
                    timeout = 60  # 60 seconds timeout to reach waypoint
                    reached = False
                    last_distance = None
                    stuck_count = 0
                    
                    print(f"🎯 DEBUG: Waiting for drone to reach WP{wp_index} - target: ({wp_lat:.6f}, {wp_lon:.6f}, {wp_alt}m)")
                    print(f"🎯 DEBUG: Current position: ({vehicle_data.lat:.6f}, {vehicle_data.lon:.6f}, {vehicle_data.alt:.1f}m)")
                    
                    check_count = 0
                    while time.time() - start_time < timeout:
                        # Calculate distance to waypoint
                        distance = self.calculate_distance(
                            vehicle_data.lat, vehicle_data.lon,
                            wp_lat, wp_lon
                        )
                        alt_diff = abs(vehicle_data.alt - wp_alt)
                        
                        check_count += 1
                        
                        # Log every check (every 0.5s) for debugging
                        if check_count % 2 == 0:  # Log every 1 second
                            print(f"🎯 DEBUG: WP{wp_index} - Dist: {distance:.1f}m, Alt diff: {alt_diff:.1f}m, Pos: ({vehicle_data.lat:.6f}, {vehicle_data.lon:.6f}, {vehicle_data.alt:.1f}m)")
                        
                        # Check if drone is stuck (not making progress)
                        if last_distance is not None:
                            if abs(distance - last_distance) < 0.5:  # Not moving much
                                stuck_count += 1
                                if stuck_count > 20:  # Stuck for 10 seconds (20 * 0.5s)
                                    print(f"⚠️ DEBUG: Drone seems stuck at {distance:.1f}m from WP{wp_index}, continuing anyway")
                                    reached = True  # Mark as reached to continue mission
                                    break
                            else:
                                stuck_count = 0
                        last_distance = distance
                        
                        # Consider waypoint reached if within 10m horizontally and 5m vertically
                        # Increased tolerance for better reliability
                        if distance < 10.0 and alt_diff < 5.0:
                            reached = True
                            print(f"✅ DEBUG: Reached WP{wp_index} (distance: {distance:.1f}m, alt_diff: {alt_diff:.1f}m)")
                            socketio.emit('log', {
                                'message': f'✅ Reached Waypoint {wp_index} (dist: {distance:.1f}m)',
                                'type': 'success',
                                'timestamp': datetime.now().isoformat()
                            })
                            break
                        
                        time.sleep(0.5)
                    
                    if not reached:
                        distance = self.calculate_distance(vehicle_data.lat, vehicle_data.lon, wp_lat, wp_lon)
                        alt_diff = abs(vehicle_data.alt - wp_alt)
                        print(f"⚠️ DEBUG: WP{wp_index} reach timeout - Final distance: {distance:.1f}m, alt_diff: {alt_diff:.1f}m")
                        socketio.emit('log', {
                            'message': f'⚠️ Waypoint {wp_index} reach timeout (dist: {distance:.1f}m) - continuing anyway',
                            'type': 'warning',
                            'timestamp': datetime.now().isoformat()
                        })
                    
                    # Now execute the action at this waypoint (if any)
                    if action:
                        self.current_action = action
                        self.action_complete = False
                        
                        print(f"🎯 DEBUG: Executing action '{action['type']}' at waypoint {wp_index}")
                        
                        socketio.emit('log', {
                            'message': f'🎯 Executing action: {action["type"]} at WP {wp_index}',
                            'type': 'info',
                            'timestamp': datetime.now().isoformat()
                        })
                        
                        if action['type'] == 'rtl':
                            self._execute_rtl(vehicle_data, socketio, action)
                        elif action['type'] == 'land':
                            self._execute_land(vehicle_data, socketio, action)
                        elif action['type'] == 'delay':
                            self._execute_delay(vehicle_data, socketio, action)
                        elif action['type'] == 'loiter':
                            self._execute_loiter(vehicle_data, socketio, action)
                        elif action['type'] == 'set_speed':
                            self._execute_set_speed(vehicle_data, socketio, action)
                        else:
                            print(f"⚠️ DEBUG: Unknown action type '{action['type']}' at WP{wp_index}")
                            with self._mission_lock:
                                self.action_complete = True
                            self._auto_advance_waypoint(socketio, vehicle_data)
                    else:
                        # No action at this waypoint, just mark complete and advance
                        print(f"✅ DEBUG: WP{wp_index} has no action, advancing...")
                        with self._mission_lock:
                            self.action_complete = True
                        socketio.emit('log', {
                            'message': f'✅ Waypoint {wp_index} completed (no action)',
                            'type': 'success',
                            'timestamp': datetime.now().isoformat()
                        })
                        time.sleep(2)  # Brief pause before advancing
                        self._auto_advance_waypoint(socketio, vehicle_data)
                
                # Start navigation and action execution in background thread
                threading.Thread(target=wait_and_execute_action, daemon=True).start()
            else:
                print(f"⚠️ DEBUG: Not connected or no coordinates for WP{wp_index}")
                # If not connected, just execute action if present
                if action:
                    self.current_action = action
                    self.action_complete = False
                    
                    if action['type'] == 'rtl':
                        self._execute_rtl(vehicle_data, socketio, action)
                    elif action['type'] == 'land':
                        self._execute_land(vehicle_data, socketio, action)
                    elif action['type'] == 'delay':
                        self._execute_delay(vehicle_data, socketio, action)
                    elif action['type'] == 'loiter':
                        self._execute_loiter(vehicle_data, socketio, action)
                    elif action['type'] == 'set_speed':
                        self._execute_set_speed(vehicle_data, socketio, action)

    def _execute_takeoff(self, vehicle_data, socketio, action):
        target_alt = action.get('altitude', 50)
        timeout = action.get('timeout', 30)  # Get timeout from action, default 30s
        
        print(f"🚀 DEBUG: _execute_takeoff called - target: {target_alt}m, timeout: {timeout}s")
        print(f"🚀 DEBUG: windows_telemetry.connected = {windows_telemetry.connected}")
        
        socketio.emit('log', {
            'message': f'🚀 Executing TAKEOFF to {target_alt}m (timeout: {timeout}s)',
            'type': 'success',
            'timestamp': datetime.now().isoformat()
        })
        vehicle_data.status = "Taking Off"

        if windows_telemetry.connected:
            print(f"🚀 DEBUG: About to send takeoff command...")
            print(f"🚀 DEBUG: Current state - Armed: {vehicle_data.armed}, Mode: {vehicle_data.mode}, Altitude: {vehicle_data.alt:.1f}m")
            
            # Verify vehicle is actually armed before takeoff
            if not vehicle_data.armed:
                print(f"⚠️ WARNING: Vehicle not armed! Waiting 2 seconds...")
                time.sleep(2)
                if not vehicle_data.armed:
                    socketio.emit('log', {
                        'message': '❌ Cannot takeoff - vehicle not armed!',
                        'type': 'error',
                        'timestamp': datetime.now().isoformat()
                    })
                    print(f"❌ ERROR: Vehicle still not armed after waiting")
                    return
            
            print(f"🚀 DEBUG: Sending takeoff command to {target_alt}m...")
            ok = windows_telemetry.send_mavlink_command('takeoff', {'altitude': target_alt})
            print(f"🚀 DEBUG: takeoff command send result: {ok}")
            print(f"🚀 DEBUG: Takeoff command sent, starting monitoring thread...")

        def complete_takeoff():
            start_time = time.time()
            altitude_reached = False
            initial_alt = vehicle_data.alt
            last_command_time = 0
            
            print(f"🚀 DEBUG: === TAKEOFF MONITORING STARTED ===")
            print(f"🚀 DEBUG: Initial - Alt: {initial_alt:.1f}m, Target: {target_alt}m, Armed: {vehicle_data.armed}, Mode: {vehicle_data.mode}")
            print(f"🚀 DEBUG: Timeout: {timeout}s")
            
            while time.time() - start_time < timeout:
                current_alt = vehicle_data.alt
                elapsed = time.time() - start_time

                # If FC auto-disarms, stop immediately and surface the reason via STATUSTEXT.
                if not vehicle_data.armed:
                    print(f"❌ DEBUG: Vehicle disarmed during takeoff window at t={elapsed:.1f}s")
                    break
                
                # Keep takeoff alive: if we haven't climbed yet, re-issue NAV_TAKEOFF at a low rate.
                # Avoid sending position targets here, as they can conflict with NAV_TAKEOFF behavior.
                if windows_telemetry.connected:
                    should_retry = (elapsed > 2.0) and (current_alt < initial_alt + 0.5)
                    if should_retry and (time.time() - last_command_time > 2.0):
                        try:
                            ok_retry = windows_telemetry.send_mavlink_command('takeoff', {'altitude': target_alt})
                            last_command_time = time.time()
                            print(f"🚀 DEBUG: Retried takeoff command (ok={ok_retry})")
                        except Exception as e:
                            print(f"⚠️ Error retrying takeoff command: {e}")
                
                # Log progress every 1 second for first 10 seconds, then every 5 seconds
                log_interval = 1 if elapsed < 10 else 5
                if int(elapsed) % log_interval == 0 and elapsed > 0 and int(elapsed * 10) % (log_interval * 10) == 0:
                    print(f"🚀 DEBUG: [{elapsed:.1f}s] Alt: {current_alt:.1f}m (target: {target_alt}m), Armed: {vehicle_data.armed}, Mode: {vehicle_data.mode}")
                
                # Check if target altitude reached (near target; avoid premature "success")
                if current_alt >= (target_alt - 0.5):
                    altitude_reached = True
                    print(f"✅ DEBUG: === TARGET ALTITUDE REACHED ===")
                    print(f"✅ DEBUG: Current: {current_alt:.1f}m, Target: {target_alt}m, Time: {elapsed:.1f}s")
                    break
                
                time.sleep(0.2)

            with self._mission_lock:
                self.action_complete = True
                print(f"🚀 DEBUG: Set action_complete = True after takeoff monitor loop")

            if altitude_reached:
                vehicle_data.status = "Active"
                socketio.emit('log', {
                    'message': f'✅ Takeoff completed to {target_alt}m (current: {vehicle_data.alt:.1f}m)',
                    'type': 'success',
                    'timestamp': datetime.now().isoformat()
                })
                
                # Wait before advancing to next waypoint
                print(f"🚀 DEBUG: Takeoff successful, waiting 2 seconds before advancing...")
                time.sleep(2)
                print(f"🚀 DEBUG: About to call _auto_advance_waypoint - mission_started={self.mission_started}, action_complete={self.action_complete}")
                self._auto_advance_waypoint(socketio, vehicle_data)
            else:
                # Takeoff FAILED - do NOT advance to next waypoint
                vehicle_data.status = "Takeoff Failed"
                final_alt = vehicle_data.alt
                socketio.emit('log', {
                    'message': f'❌ TAKEOFF FAILED - Current: {final_alt:.1f}m, Target: {target_alt}m after {timeout}s - MISSION ABORTED',
                    'type': 'error',
                    'timestamp': datetime.now().isoformat()
                })
                print(f"❌ DEBUG: Takeoff timeout - Final altitude: {final_alt:.1f}m, Target: {target_alt}m")
                print(f"❌ DEBUG: Mission ABORTED due to takeoff failure - NOT advancing to next waypoint")
                
                # Stop the mission
                with self._mission_lock:
                    self.mission_started = False
                    if self.current_mission_id:
                        self.missions[self.current_mission_id]['status'] = 'FAILED'
                
                socketio.emit('mission_update', {
                    'mission_id': self.current_mission_id,
                    'status': 'FAILED',
                    'reason': 'Takeoff timeout',
                    'timestamp': datetime.now().isoformat()
                })

        threading.Thread(target=complete_takeoff, daemon=True).start()

    def _execute_rtl(self, vehicle_data, socketio, action):
        """Execute RTL (Return To Launch) action."""
        timeout = action.get('timeout', 60)  # Default 60s timeout
        
        print(f"🏠 DEBUG: _execute_rtl called with timeout={timeout}s")
        print(f"🏠 DEBUG: windows_telemetry.connected = {windows_telemetry.connected}")
        
        socketio.emit('log', {
            'message': f'🏠 Executing RETURN TO LAUNCH (timeout: {timeout}s)',
            'type': 'warning',
            'timestamp': datetime.now().isoformat()
        })
        vehicle_data.status = "Returning to Launch"

        def complete_rtl():
            print(f"🏠 DEBUG: complete_rtl thread started")
            
            # Send RTL command
            if windows_telemetry.connected:
                print(f"🏠 DEBUG: Sending RTL command via MAVLink...")
                result = windows_telemetry.send_mavlink_command('rtl')
                print(f"🏠 DEBUG: RTL command result: {result}")
            else:
                print(f"❌ DEBUG: Not connected to telemetry, RTL command NOT sent")

            # Wait for drone to reach home
            home = self.get_home_position()
            start_time = time.time()
            position_reached = False

            while time.time() - start_time < timeout:
                distance = self.calculate_distance(
                    vehicle_data.lat, vehicle_data.lon,
                    home['lat'], home['lng']
                )
                
                print(f"🏠 RTL - Distance to home: {distance:.1f}m")
                
                if distance < 10:  # Within 10m of home
                    position_reached = True
                    break
                time.sleep(1)

            # Mark action complete
            with self._mission_lock:
                self.action_complete = True

            if position_reached:
                vehicle_data.status = "At Home"
                socketio.emit('log', {
                    'message': '✅ Vehicle returned to launch point successfully',
                    'type': 'success',
                    'timestamp': datetime.now().isoformat()
                })
            else:
                vehicle_data.status = "RTL Timeout"
                socketio.emit('log', {
                    'message': f'⚠️ RTL timeout after {timeout}s - check vehicle position',
                    'type': 'warning',
                    'timestamp': datetime.now().isoformat()
                })
            
            # Wait before advancing to next waypoint
            print(f"🏠 DEBUG: Waiting 1 second before advancing to next waypoint...")
            time.sleep(1)

            self._auto_advance_waypoint(socketio, vehicle_data)

        # Start RTL in background thread
        threading.Thread(target=complete_rtl, daemon=True).start()

    def _execute_land(self, vehicle_data, socketio, action):
        """Execute LAND action: switch vehicle to LAND mode.

        params (optional):
          - timeout_sec: int (0 = no wait)
        """
        try:
            params = action.get('params', {}) or {}
            timeout_sec = int(params.get('timeout_sec', 0) or 0)
        except Exception:
            timeout_sec = 0

        # Update status
        self.current_status = "LANDING"
        socketio.emit('mission_status', {"status": self.current_status})

        try:
            # Prefer explicit 'land' command if available, otherwise set_mode LAND
            ok = False
            if hasattr(vehicle_data, 'send_mavlink_command'):
                # unlikely here; vehicle_data is dict
                pass
            # WindowsTelemetry singleton is global
            if 'windows_telemetry' in globals():
                try:
                    ok = bool(windows_telemetry.send_mavlink_command('land', {}))
                except Exception:
                    ok = False
                if not ok:
                    try:
                        ok = bool(windows_telemetry.send_mavlink_command('set_mode', {'mode': 'LAND'}))
                    except Exception:
                        ok = False
        except Exception:
            ok = False

        # Optionally wait a bit for altitude to reduce / landed flag
        if timeout_sec > 0:
            t0 = time.time()
            while time.time() - t0 < timeout_sec:
                try:
                    alt = float(vehicle_data.get('altitude', 0.0) or 0.0)
                    armed = bool(vehicle_data.get('armed', True))
                    # Heuristic: landed when very low altitude or disarmed
                    if (alt <= 0.5) or (not armed):
                        break
                except Exception:
                    pass
                time.sleep(0.5)

        # After land action, stop mission automation (safer)
        self.mission_started = False
        self.current_status = "LANDED (or LAND cmd sent)"
        socketio.emit('mission_status', {"status": self.current_status})

        return ok

    def _execute_delay(self, vehicle_data, socketio, action):
        delay_seconds = action.get('seconds', 5)
        socketio.emit('log', {
            'message': f'⏰ Delaying for {delay_seconds} seconds',
            'type': 'warning',
            'timestamp': datetime.now().isoformat()
        })
        vehicle_data.status = f"Delaying for {delay_seconds}s"

        def complete_delay():
            time.sleep(delay_seconds)
            with self._mission_lock:
                self.action_complete = True
            vehicle_data.status = "Active"
            socketio.emit('log', {
                'message': '✅ Delay completed, continuing mission',
                'type': 'success',
                'timestamp': datetime.now().isoformat()
            })
            self._auto_advance_waypoint(socketio, vehicle_data)

        threading.Thread(target=complete_delay, daemon=True).start()

    def _execute_loiter(self, vehicle_data, socketio, action):
        turns = action.get('turns', 1)
        radius = action.get('radius', 50)
        duration = action.get('duration', turns * 10)  # Use duration from UI, or calculate from turns
        
        print(f"🔄 DEBUG: _execute_loiter called - turns: {turns}, radius: {radius}m, duration: {duration}s")
        print(f"🔄 DEBUG: windows_telemetry.connected = {windows_telemetry.connected}")
        
        socketio.emit('log', {
            'message': f'🔄 Loitering for {duration}s (radius {radius}m) - like Mission Planner',
            'type': 'info',
            'timestamp': datetime.now().isoformat()
        })
        vehicle_data.status = f"Loitering ({duration}s)"
        
        # MISSION PLANNER METHOD: Switch to LOITER mode temporarily
        # This is exactly how Mission Planner handles loiter during missions
        if windows_telemetry.connected and windows_telemetry.master:
            print(f"🔄 DEBUG: Setting LOITER mode (Mission Planner method)...")
            
            # Save current mode to restore later
            previous_mode = vehicle_data.mode
            
            # Set LOITER mode (custom mode 5 for ArduCopter)
            windows_telemetry.master.mav.command_long_send(
                windows_telemetry.master.target_system,
                windows_telemetry.master.target_component,
                mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                0,  # confirmation
                mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,  # param1: mode flag
                5,  # param2: custom mode (5 = LOITER for ArduCopter)
                0, 0, 0, 0, 0  # params 3-7: unused
            )
            
            # Optionally set loiter radius if provided
            if radius and radius > 0:
                print(f"🔄 DEBUG: Setting loiter radius to {radius}m...")
                # MAV_CMD_NAV_LOITER_UNLIM with radius parameter
                windows_telemetry.master.mav.command_long_send(
                    windows_telemetry.master.target_system,
                    windows_telemetry.master.target_component,
                    mavutil.mavlink.MAV_CMD_NAV_LOITER_UNLIM,
                    0,  # confirmation
                    0,  # param1: empty
                    0,  # param2: empty
                    radius,  # param3: radius in meters
                    0,  # param4: forward moving (0=loiter in place)
                    0,  # param5: latitude (0 = current)
                    0,  # param6: longitude (0 = current)
                    0   # param7: altitude (0 = current)
                )
            
            print(f"✅ DEBUG: LOITER mode set")
            socketio.emit('log', {
                'message': f'🔄 Now loitering at current position...',
                'type': 'info',
                'timestamp': datetime.now().isoformat()
            })
        else:
            print(f"❌ DEBUG: Not connected to telemetry, loiter command NOT sent")

        def complete_loiter():
            print(f"🔄 DEBUG: Loiter timer started for {duration}s")
            time.sleep(duration)
            
            # Return to GUIDED mode after loiter completes
            if windows_telemetry.connected and windows_telemetry.master:
                print(f"🔄 DEBUG: Loiter time complete, returning to GUIDED mode...")
                windows_telemetry.master.mav.command_long_send(
                    windows_telemetry.master.target_system,
                    windows_telemetry.master.target_component,
                    mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                    0,
                    mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    4,  # GUIDED mode = 4
                    0, 0, 0, 0, 0
                )
                time.sleep(1)  # Wait for mode switch
                print(f"✅ DEBUG: Returned to GUIDED mode")
            
            with self._mission_lock:
                self.action_complete = True
                print(f"🔄 DEBUG: Loiter complete, action_complete set to True")
            
            vehicle_data.status = "Active"
            socketio.emit('log', {
                'message': f'✅ Loitering completed after {duration}s, resuming mission',
                'type': 'success',
                'timestamp': datetime.now().isoformat()
            })
            
            time.sleep(1)
            self._auto_advance_waypoint(socketio, vehicle_data)

        threading.Thread(target=complete_loiter, daemon=True).start()

    def _execute_set_speed(self, vehicle_data, socketio, action):
        speed = action.get('speed', 10)
        
        print(f"💨 DEBUG: _execute_set_speed called - speed: {speed} m/s")
        print(f"💨 DEBUG: windows_telemetry.connected = {windows_telemetry.connected}")
        
        socketio.emit('log', {
            'message': f'💨 Setting speed to {speed} m/s',
            'type': 'info',
            'timestamp': datetime.now().isoformat()
        })
        
        # Send MAVLink command to change speed
        # MAV_CMD_DO_CHANGE_SPEED (command ID 178)
        if windows_telemetry.connected and windows_telemetry.master:
            print(f"💨 DEBUG: Sending MAVLink CHANGE_SPEED command...")
            windows_telemetry.master.mav.command_long_send(
                windows_telemetry.master.target_system,
                windows_telemetry.master.target_component,
                mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,  # command
                0,  # confirmation
                1,  # param1: speed type (0=Airspeed, 1=Ground Speed)
                speed,  # param2: speed in m/s
                -1,  # param3: throttle (-1 means no change)
                0,  # param4: absolute or relative (0=absolute, 1=relative)
                0, 0, 0  # param5-7: unused
            )
            print(f"✅ DEBUG: Speed change command sent")
        else:
            print(f"❌ DEBUG: Not connected to telemetry, speed command NOT sent")
        
        vehicle_data.speed = speed

        with self._mission_lock:
            self.action_complete = True
            print(f"💨 DEBUG: Speed change complete, action_complete set to True")

        socketio.emit('log', {
            'message': f'✅ Speed set to {speed} m/s',
            'type': 'success',
            'timestamp': datetime.now().isoformat()
        })
        
        time.sleep(1)
        self._auto_advance_waypoint(socketio, vehicle_data)

    def _auto_advance_waypoint(self, socketio, vehicle_data):
        with self._mission_lock:
            if self.paused_by_smoke:
                print("⏸️ DEBUG: _auto_advance_waypoint blocked (paused_by_smoke=True)")
                return
            print(f"⏭️ DEBUG: _auto_advance_waypoint entered - mission_id={self.current_mission_id}, mission_started={self.mission_started}, action_complete={self.action_complete}")
            if self.current_mission_id and self.mission_started:
                current_index = self.missions[self.current_mission_id]['current_wp_index']
                print(f"⏭️ DEBUG: _auto_advance_waypoint called - current index: {current_index}")
                
                result = self.advance_waypoint()
                print(f"⏭️ DEBUG: advance_waypoint returned: {result}")
                
                if result == 'COMPLETED':
                    print(f"🏁 DEBUG: Mission COMPLETED after WP{current_index + 1}")
                    socketio.emit('mission_update', {
                        'mission_id': self.current_mission_id,
                        'status': 'COMPLETED',
                        'timestamp': datetime.now().isoformat()
                    })
                    socketio.emit('log', {
                        'message': '🎉 Mission completed!',
                        'type': 'success',
                        'timestamp': datetime.now().isoformat()
                    })
                    vehicle_data.mode = "MANUAL"
                    self.mission_started = False
                elif result == 'ADVANCED':
                    new_index = self.missions[self.current_mission_id]['current_wp_index']
                    print(f"⏭️ DEBUG: Advanced from WP{current_index + 1} to WP{new_index + 1}")
                    socketio.emit('log', {
                        'message': f'➡️ Advanced to waypoint {new_index + 1}',
                        'type': 'info',
                        'timestamp': datetime.now().isoformat()
                    })
                    print(f"⏭️ DEBUG: About to call execute_waypoint_action for WP{new_index + 1} - action_complete={self.action_complete}, mission_started={self.mission_started}")
                    self.execute_waypoint_action(vehicle_data, socketio)
                    print(f"⏭️ DEBUG: execute_waypoint_action returned for WP{new_index + 1}")
                else:
                    print(f"⚠️ DEBUG: _auto_advance_waypoint - unexpected result: {result}")

    def advance_waypoint(self):
        with self._mission_lock:
            if not self.current_mission_id:
                print(f"⚠️ DEBUG: advance_waypoint called but no current_mission_id")
                return False

            mission = self.missions[self.current_mission_id]
            old_index = mission['current_wp_index']
            mission['current_wp_index'] += 1
            new_index = mission['current_wp_index']

            # Allow next waypoint action to trigger.
            if (
                self._last_action_trigger_mission_id == self.current_mission_id
                and self._last_action_trigger_wp_index == old_index
            ):
                self._last_action_trigger_wp_index = None
            
            print(f"📊 DEBUG: advance_waypoint - index changed from {old_index} to {new_index}, total waypoints: {len(mission['waypoints'])}")

            if mission['current_wp_index'] >= len(mission['waypoints']):
                print(f"🏁 DEBUG: Reached end of mission ({new_index} >= {len(mission['waypoints'])})")
                mission['status'] = 'COMPLETED'
                self.current_mission_id = None
                self.mission_started = False
                return 'COMPLETED'

            print(f"✅ DEBUG: Successfully advanced to WP{new_index + 1}")
            return 'ADVANCED'

    def calculate_distance(self, lat1, lon1, lat2, lon2):
        R = 6371000
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)

        a = (math.sin(delta_lat / 2) * math.sin(delta_lat / 2) +
             math.cos(lat1_rad) * math.cos(lat2_rad) *
             math.sin(delta_lon / 2) * math.sin(delta_lon / 2))
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        return R * c
    
    def pause_mission_for_smoke(self, lat, lon, alt, socketio, current_mode=None):
        """
        Pause mission khi phát hiện khói, chuyển sang LOITER mode.
        """
        with self._mission_lock:
            if not self.mission_started or self.paused_by_smoke:
                return False
            
            self.paused_by_smoke = True
            self.smoke_pause_location = {"lat": lat, "lng": lon, "alt": alt}

            # Save current mode so we can restore the correct mode on resume.
            # Only save on the first pause edge.
            if current_mode is not None:
                try:
                    self.mode_before_pause = str(current_mode)
                except Exception:
                    self.mode_before_pause = None
            
            print(f"🔥 SMOKE DETECTED! Pausing mission at ({lat:.6f}, {lon:.6f}, {alt:.1f}m)")
            
            socketio.emit('log', {
                'message': f'🔥 KHÓI PHÁT HIỆN! Tạm dừng mission, LOITER tại vị trí hiện tại...',
                'type': 'warning',
                'timestamp': datetime.now().isoformat()
            })
            
            # Emit smoke pause event
            socketio.emit('mission_paused_smoke', {
                'location': self.smoke_pause_location,
                'timestamp': datetime.now().isoformat()
            })
            
            return True
    
    def resume_mission_after_smoke(self, socketio, vehicle_data=None):
        """
        Resume mission sau khi người giám sát xác nhận.
        """
        with self._mission_lock:
            if not self.paused_by_smoke:
                return False
            
            self.paused_by_smoke = False
            location = self.smoke_pause_location
            self.smoke_pause_location = None

            resume_mode = self.mode_before_pause
            self.mode_before_pause = None
            
            print(f"✅ Mission RESUMED after smoke inspection at ({location['lat']:.6f}, {location['lng']:.6f})")
            
            socketio.emit('log', {
                'message': '✅ Tiếp tục lộ trình sau khi kiểm tra khói...',
                'type': 'success',
                'timestamp': datetime.now().isoformat()
            })
            
            # Emit resume event
            socketio.emit('mission_resumed', {
                'timestamp': datetime.now().isoformat()
            })

            # If an action finished while paused, the mission can otherwise stall on the same WP.
            # On resume: if we are already at the current waypoint and its action was already
            # triggered, advance to the next waypoint.
            try:
                if self.current_mission_id and self.mission_started and self.action_complete:
                    mission = self.missions.get(self.current_mission_id) or {}
                    current_index0 = int(mission.get('current_wp_index', 0) or 0)

                    at_wp = False
                    current_wp = self.get_current_waypoint()
                    if vehicle_data is not None and current_wp:
                        dist = self.calculate_distance(
                            float(getattr(vehicle_data, 'lat', 0.0)),
                            float(getattr(vehicle_data, 'lon', 0.0)),
                            float(current_wp.get('lat')),
                            float(current_wp.get('lng')),
                        )
                        at_wp = dist < 10.0

                    action_already_triggered = (
                        self._last_action_trigger_mission_id == self.current_mission_id
                        and self._last_action_trigger_wp_index == current_index0
                    )

                    if at_wp and action_already_triggered and current_wp and current_wp.get('action'):
                        self._auto_advance_waypoint(socketio, vehicle_data)
            except Exception:
                pass

            return resume_mode or True


# Enhanced Vehicle Data Class - FIXED VERSION
class VehicleData:
    def __init__(self):
        home = mission_planner.get_home_position()
        self.lat = home['lat']
        self.lon = home['lng']
        self.alt = home['alt']
        self.relative_alt = 0.0
        self.speed = 0.0
        self.airspeed = 0.0
        self.heading = 0.0
        self.climb = 0.0
        self.roll = 0.0
        self.pitch = 0.0
        self.yaw = 0.0
        self.battery = 100
        self.battery_voltage = 0.0
        self.battery_current = 0.0
        self.status = "Disconnected"
        self.mode = "MANUAL"
        self.armed = False
        self.gps_satellites = 0
        self.gps_fix_type = 0
        self.target_lat = None
        self.target_lon = None
        self.target_alt = None
        self._data_lock = threading.Lock()
        self._last_altitude = home['alt']

    def update_position(self, lat, lon, alt):
        with self._data_lock:
            # Always update lat/lon from GPS
            self.lat = lat
            self.lon = lon
            
            # Only update altitude if change is reasonable (prevents wild jumps)
            # But allow larger changes during takeoff/landing
            if abs(alt - self._last_altitude) < 100 or self._last_altitude < 5:
                self.alt = alt
                self._last_altitude = alt
            else:
                # Log large altitude jumps but still update if it's consistent
                print(f"Large altitude change detected: {self._last_altitude:.1f}m -> {alt:.1f}m")
                # Update anyway to prevent getting stuck
                self.alt = alt
                self._last_altitude = alt

    def to_dict(self):
        with self._data_lock:
            return {
                'lat': self.lat,
                'lon': self.lon,
                'alt': self.alt,
                'speed': self.speed,
                'heading': self.heading,
                'battery': self.battery,
                'status': self.status,
                'mode': self.mode,
                'armed': self.armed,
                'gps_satellites': self.gps_satellites,
                'gps_fix_type': self.gps_fix_type,
                'roll': self.roll,
                'pitch': self.pitch,
                'yaw': self.yaw,
                'relative_alt': self.relative_alt,
                'timestamp': datetime.now().isoformat()
            }


# Real MAVLink Telemetry Handler - FIXED VERSION
class WindowsTelemetryHandler:
    def __init__(self, socketio):
        self.socketio = socketio
        self.master = None
        self.connected = False
        self.vehicle_data = VehicleData()
        self.last_emit_time = 0
        self._connection_lock = threading.Lock()
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._heartbeat_timeout = 15
        self._last_heartbeat_time = 0
        self._connection_active = False

    def find_telemetry_port(self):
        common_ports = ['COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8']
        if not HAS_MAVLINK:
            return None

        try:
            available_ports = [port.device for port in serial.tools.list_ports.comports()]
            print("🔍 Available COM ports:", available_ports)

            for port in common_ports:
                if port in available_ports:
                    print(f"   Trying {port}...")
                    if self.test_connection(port):
                        return port

            for port in available_ports:
                print(f"   Trying {port}...")
                if self.test_connection(port):
                    return port
        except Exception as e:
            print(f"Error scanning ports: {e}")
        return None

    def test_connection(self, port, baudrate=57600):
        try:
            test_master = mavutil.mavlink_connection(port, baud=baudrate, autoreconnect=True)
            test_master.wait_heartbeat(timeout=2)
            test_master.close()
            print(f"   ✅ {port} has MAVLink device!")
            return True
        except Exception as e:
            print(f"   ❌ {port} failed: {e}")
            return False

    def connect_telemetry_radio(self):
        with self._connection_lock:
            if self.connected and self.master:
                return True

            print("🔍 Searching for telemetry radio...")
            port = self.find_telemetry_port()
            if not port:
                print("❌ No telemetry radio found!")
                self.socketio.emit('log', {
                    'message': 'No telemetry radio found - check connections',
                    'type': 'error',
                    'timestamp': datetime.now().isoformat()
                })
                return False

            try:
                print(f"📡 Connecting to {port} at 57600 baud...")
                self.master = mavutil.mavlink_connection(port, baud=57600, autoreconnect=True, retries=5)
                print("💓 Waiting for heartbeat from Pixhawk...")
                self.master.wait_heartbeat(timeout=10)

                self._last_heartbeat_time = time.time()
                self.connected = True
                self._reconnect_attempts = 0
                self._connection_active = True

                print(f"✅ Connected to Pixhawk via {port}!")
                self.setup_data_streams()

                self.socketio.emit('log', {
                    'message': f'✅ Connected to Pixhawk via {port}!',
                    'type': 'success',
                    'timestamp': datetime.now().isoformat()
                })
                return True

            except Exception as e:
                print(f"❌ Connection failed: {e}")
                self.socketio.emit('log', {
                    'message': f'Connection failed: {str(e)}',
                    'type': 'error',
                    'timestamp': datetime.now().isoformat()
                })
                self._schedule_reconnect()
                return False

    def _schedule_reconnect(self):
        if self._reconnect_attempts < self._max_reconnect_attempts:
            self._reconnect_attempts += 1
            delay = min(2 * self._reconnect_attempts, 30)
            print(f"🔄 Reconnecting in {delay}s (attempt {self._reconnect_attempts})")

            self.socketio.emit('log', {
                'message': f'Reconnecting in {delay}s...',
                'type': 'warning',
                'timestamp': datetime.now().isoformat()
            })

            threading.Timer(delay, self.connect_telemetry_radio).start()
        else:
            print("❌ Max reconnection attempts reached")
            self.socketio.emit('log', {
                'message': 'Max reconnection attempts reached',
                'type': 'error',
                'timestamp': datetime.now().isoformat()
            })
    
    # Thêm vào class WindowsTelemetryHandler

    def upload_mission_to_pixhawk(self, waypoints):
        """
        Upload mission waypoints lên Pixhawk giống Mission Planner. 
        waypoints: list of dict với keys: lat, lng, alt, action (optional)
        """
        if not self.connected or not self.master:
            print("❌ Not connected to Pixhawk")
            return False
        
        try:
            from pymavlink import mavutil
            
            # Clear existing mission
            self.master.mav.mission_clear_all_send(
                self.master.target_system,
                self.master.target_component
            )
            time.sleep(0.5)
            
            # Build mission items
            mission_items = []
            seq = 0
            
            for i, wp in enumerate(waypoints):
                lat = wp.get('lat', 0)
                lng = wp.get('lng', 0) 
                alt = wp.get('alt', 5)
                action = wp.get('action', {})
                action_type = action.get('type', '') if action else ''
                
                if i == 0:
                    # First item: HOME position (không dùng, nhưng cần có)
                    home_item = mavutil.mavlink.MAVLink_mission_item_int_message(
                        self.master.target_system,
                        self.master.target_component,
                        seq,
                        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                        mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                        0, 1,  # current, autocontinue
                        0, 0, 0, 0,  # params
                        int(lat * 1e7), int(lng * 1e7), alt
                    )
                    mission_items.append(home_item)
                    seq += 1
                
                # Xử lý action
                if action_type == 'takeoff':
                    takeoff_alt = action.get('altitude', alt)
                    item = mavutil.mavlink.MAVLink_mission_item_int_message(
                        self.master.target_system,
                        self.master.target_component,
                        seq,
                        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                        0, 1,
                        0, 0, 0, 0,  # pitch, empty, empty, yaw
                        int(lat * 1e7), int(lng * 1e7), takeoff_alt
                    )
                    mission_items.append(item)
                    seq += 1
                    
                elif action_type == 'land':
                    item = mavutil.mavlink.MAVLink_mission_item_int_message(
                        self.master.target_system,
                        self.master.target_component,
                        seq,
                        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                        mavutil.mavlink.MAV_CMD_NAV_LAND,
                        0, 1,
                        0, 0, 0, 0,
                        int(lat * 1e7), int(lng * 1e7), 0
                    )
                    mission_items.append(item)
                    seq += 1
                    
                elif action_type == 'rtl':
                    item = mavutil.mavlink.MAVLink_mission_item_int_message(
                        self.master.target_system,
                        self.master.target_component,
                        seq,
                        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                        mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH,
                        0, 1,
                        0, 0, 0, 0,
                        0, 0, 0
                    )
                    mission_items.append(item)
                    seq += 1
                    
                elif action_type == 'loiter':
                    duration = action.get('duration', 10)
                    item = mavutil.mavlink.MAVLink_mission_item_int_message(
                        self.master.target_system,
                        self.master.target_component,
                        seq,
                        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                        mavutil.mavlink.MAV_CMD_NAV_LOITER_TIME,
                        0, 1,
                        duration, 0, 0, 0,
                        int(lat * 1e7), int(lng * 1e7), alt
                    )
                    mission_items.append(item)
                    seq += 1
                    
                elif action_type == 'delay':
                    seconds = action.get('seconds', 5)
                    item = mavutil.mavlink.MAVLink_mission_item_int_message(
                        self.master.target_system,
                        self.master.target_component,
                        seq,
                        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                        mavutil.mavlink.MAV_CMD_NAV_DELAY,
                        0, 1,
                        seconds, 0, 0, 0,
                        0, 0, 0
                    )
                    mission_items.append(item)
                    seq += 1
                    
                else:
                    # Regular waypoint (không có action hoặc action khác)
                    if i > 0:  # Skip first waypoint (đã xử lý ở trên)
                        item = mavutil.mavlink.MAVLink_mission_item_int_message(
                            self.master.target_system,
                            self.master.target_component,
                            seq,
                            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                            mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                            0, 1,
                            0, 0, 0, 0,  # hold, accept radius, pass radius, yaw
                            int(lat * 1e7), int(lng * 1e7), alt
                        )
                        mission_items.append(item)
                        seq += 1
            
            # Send mission count
            self.master.mav.mission_count_send(
                self.master.target_system,
                self.master.target_component,
                len(mission_items),
                mavutil.mavlink.MAV_MISSION_TYPE_MISSION
            )
            
            # Wait for mission requests and send items
            for item in mission_items:
                # Wait for MISSION_REQUEST_INT
                msg = self.master.recv_match(type=['MISSION_REQUEST_INT', 'MISSION_REQUEST'], 
                                            blocking=True, timeout=5)
                if msg is None:
                    print(f"❌ Timeout waiting for mission request")
                    return False
                
                # Send the requested item
                self.master.mav.send(item)
                print(f"📤 Sent mission item {item.seq}: cmd={item.command}")
            
            # Wait for MISSION_ACK
            ack = self.master.recv_match(type='MISSION_ACK', blocking=True, timeout=5)
            if ack and ack.type == mavutil.mavlink.MAV_MISSION_ACCEPTED: 
                print(f"✅ Mission uploaded successfully! {len(mission_items)} items")
                return True
            else:
                print(f"❌ Mission upload failed: {ack}")
                return False
                
        except Exception as e: 
            print(f"❌ Error uploading mission: {e}")
            import traceback
            traceback.print_exc()
            return False


    def start_auto_mission(self):
        """
        Arm và chuyển sang AUTO mode để chạy mission đã upload. 
        Giống như Mission Planner: Set Mode GUIDED → Arm → Set Mode AUTO
        """
        if not self.connected or not self.master:
            return False
        
        try: 
            # 1. Set GUIDED mode first
            print("📡 Setting GUIDED mode...")
            self.send_mavlink_command('set_mode', {'mode': 'GUIDED'})
            time.sleep(2)
            
            # 2. Arm
            print("📡 Arming...")
            self.send_mavlink_command('arm')
            time.sleep(3)
            
            # 3. Start mission (AUTO mode)
            print("📡 Starting AUTO mission...")
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_MISSION_START,
                0,
                0, 0, 0, 0, 0, 0, 0
            )
            time.sleep(1)
            
            # 4. Set AUTO mode
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                0,
                mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                3,  # AUTO mode = 3
                0, 0, 0, 0, 0
            )
            
            print("✅ AUTO mission started!")
            return True
            
        except Exception as e: 
            print(f"❌ Error starting mission: {e}")
            return False

    def setup_data_streams(self):
        if not self.master:
            return

        try:
            print("📊 Setting up MAVLink data streams...")
            streams = [
                (mavutil.mavlink.MAV_DATA_STREAM_POSITION, 10),  # Tăng lên 10 Hz
                (mavutil.mavlink.MAV_DATA_STREAM_EXTENDED_STATUS, 5),  # Tăng lên 5 Hz
                (mavutil.mavlink.MAV_DATA_STREAM_RAW_SENSORS, 2),
                (mavutil.mavlink.MAV_DATA_STREAM_EXTRA1, 10),  # Thêm attitude 10 Hz
            ]

            for stream_id, rate in streams:
                self.master.mav.request_data_stream_send(
                    self.master.target_system,
                    self.master.target_component,
                    stream_id, rate, 1
                )
                time.sleep(0.05)  # Giảm delay giữa các request
            print("✅ Data streams requested at higher rates")
        except Exception as e:
            print(f"⚠️ Could not setup data streams:  {e}")

    def start_telemetry_loop(self):
        print("🔄 Starting MAVLink message processor (optimized)")

        while self._connection_active:
            try:
                if not self.connected:
                    time. sleep(2.0)
                    continue

                if time.time() - self._last_heartbeat_time > self._heartbeat_timeout:
                    print("💔 Heartbeat timeout - reconnecting...")
                    self.connected = False
                    self._schedule_reconnect()
                    time.sleep(1)
                    continue

                # Process multiple messages per iteration (non-blocking)
                messages_processed = 0
                max_messages_per_iter = 20  # Xử lý tối đa 20 msg mỗi vòng

                while messages_processed < max_messages_per_iter:
                    msg = self.master.recv_match(blocking=False)
                    if msg is None:
                        break

                    if msg.get_type() == 'HEARTBEAT':
                        self._last_heartbeat_time = time.time()
                    self.process_mavlink_message(msg)
                    messages_processed += 1

                # Emit at 10 Hz
                current_time = time.time()
                if current_time - self. last_emit_time >= 0.2:
                    self.emit_telemetry()
                    self.last_emit_time = current_time

                # Small sleep to prevent CPU spinning
                time.sleep(0.02)  # 20ms

            except Exception as e:
                print(f"⚠️ Telemetry loop error: {e}")
                if self.connected:
                    self.connected = False
                    self._schedule_reconnect()
                time. sleep(2.0)

    def process_mavlink_message(self, msg):
        try:
            if msg.get_type() == 'GLOBAL_POSITION_INT':
                # Always update position from GPS data
                new_alt = msg.alt / 1000.0  # Absolute altitude (MSL)
                new_relative_alt = msg.relative_alt / 1000.0  # Relative to home
                new_lat = msg.lat / 1e7
                new_lon = msg.lon / 1e7
                
                # Use relative altitude as the main altitude (what pilots expect)
                self.vehicle_data.update_position(new_lat, new_lon, new_relative_alt)
                self.vehicle_data.relative_alt = new_relative_alt
                self.vehicle_data.heading = msg.hdg / 100.0

            elif msg.get_type() == 'VFR_HUD':
                vfr_alt = msg.alt
                # VFR_HUD altitude is relative altitude in ArduPilot
                if abs(vfr_alt - self.vehicle_data.alt) < 100:
                    self.vehicle_data.alt = vfr_alt
                    self.vehicle_data.relative_alt = vfr_alt
                self.vehicle_data.speed = msg.groundspeed

            elif msg.get_type() == 'SYS_STATUS':
                voltage = msg.voltage_battery / 1000.0 if msg.voltage_battery != 0 else 0
                current = msg.current_battery / 100.0 if msg.current_battery != -1 else 0

                # Use battery_remaining from Pixhawk if available (0-100%)
                # This is the actual battery percentage calculated by the flight controller
                if hasattr(msg, 'battery_remaining') and msg.battery_remaining >= 0:
                    battery_percent = msg.battery_remaining
                else:
                    # Fallback: estimate from voltage for 3S LiPo (11.1V nominal)
                    # Adjust these values based on your battery type
                    if voltage > 12.6:
                        battery_percent = 100
                    elif voltage > 10.5:
                        battery_percent = max(0, min(100, int((voltage - 10.5) / (12.6 - 10.5) * 100)))
                    else:
                        battery_percent = 0

                self.vehicle_data.battery = battery_percent
                self.vehicle_data.battery_voltage = voltage
                self.vehicle_data.battery_current = current

            elif msg.get_type() == 'HEARTBEAT':
                self.vehicle_data.status = "Connected"
                try:
                    mode = mavutil.mode_string_v10(msg)
                    self.vehicle_data.mode = mode
                except:
                    self.vehicle_data.mode = "UNKNOWN"

                armed_status = (msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED) != 0
                if armed_status != self.vehicle_data.armed:
                    print(f"🔄 Armed status changed: {self.vehicle_data.armed} -> {armed_status}")
                self.vehicle_data.armed = armed_status

            elif msg.get_type() == 'GPS_RAW_INT':
                self.vehicle_data.gps_satellites = msg.satellites_visible
                self.vehicle_data.gps_fix_type = msg.fix_type

            elif msg.get_type() == 'ATTITUDE':
                self.vehicle_data.roll = math.degrees(msg.roll)
                self.vehicle_data.pitch = math.degrees(msg.pitch)
                self.vehicle_data.yaw = math.degrees(msg.yaw)

            elif msg.get_type() == 'STATUSTEXT':
                # Critical for diagnosing auto-disarm / takeoff rejection.
                try:
                    text = msg.text if hasattr(msg, 'text') else str(msg)
                except Exception:
                    text = str(msg)
                severity = getattr(msg, 'severity', None)
                print(f"🛰️ STATUSTEXT({severity}): {text}")
                try:
                    self.socketio.emit('log', {
                        'message': f'🛰️ FC: {text}',
                        'type': 'warning' if severity is None or severity <= 4 else 'info',
                        'timestamp': datetime.now().isoformat()
                    })
                except Exception:
                    pass

            elif msg.get_type() == 'COMMAND_ACK':
                # Shows whether NAV_TAKEOFF / mode change was accepted or rejected.
                try:
                    cmd = int(getattr(msg, 'command', -1))
                    result = int(getattr(msg, 'result', -1))
                    print(f"🛰️ COMMAND_ACK: cmd={cmd}, result={result}")
                except Exception:
                    print(f"🛰️ COMMAND_ACK: {msg}")

            elif msg.get_type() == 'EKF_STATUS_REPORT':
                # Useful for takeoff issues (bad EKF can prevent GUIDED navigation).
                try:
                    flags = int(getattr(msg, 'flags', 0))
                    print(f"🛰️ EKF_STATUS_REPORT flags=0x{flags:08X}")
                except Exception:
                    pass

        except Exception as e:
            print(f"Error processing MAVLink message: {e}")

    def emit_telemetry(self):
        telemetry_dict = self.vehicle_data.to_dict()

        if mission_planner.current_mission_id:
            current_wp = mission_planner.get_current_waypoint()
            mission = mission_planner.get_mission(mission_planner.current_mission_id)
            telemetry_dict.update({
                'current_mission': mission_planner.current_mission_id,
                'current_waypoint': mission['current_wp_index'] if mission else 0,
                'total_waypoints': len(mission['waypoints']) if mission else 0,
                'current_action': mission_planner.current_action,
                'mission_started': mission_planner.mission_started,
                'action_complete': mission_planner.action_complete,
                'home_position': mission_planner.get_home_position()
            })

        data_logger.save_telemetry(telemetry_dict)
        self.socketio.emit('telemetry', telemetry_dict)

    def execute_mission_sequence(self, mission_id=None):
        if not self.connected or not self.master:
            self.socketio.emit('log', {
                'message': '❌ Not connected to vehicle',
                'type': 'error',
                'timestamp': datetime.now().isoformat()
            })
            return False

        try:
            self.socketio.emit('log', {
                'message': '🔄 Setting to GUIDED mode...',
                'type': 'info',
                'timestamp': datetime.now().isoformat()
            })

            if not self.send_mavlink_command('set_mode', {'mode': 'GUIDED'}):
                self.socketio.emit('log', {
                    'message': '❌ Failed to set GUIDED mode',
                    'type': 'error',
                    'timestamp': datetime.now().isoformat()
                })
                return False

            time.sleep(3)

            self.socketio.emit('log', {
                'message': '🔓 Arming vehicle...',
                'type': 'warning',
                'timestamp': datetime.now().isoformat()
            })

            if not self.send_mavlink_command('arm'):
                self.socketio.emit('log', {
                    'message': '❌ Failed to arm vehicle',
                    'type': 'error',
                    'timestamp': datetime.now().isoformat()
                })
                return False

            # Wait for arming to complete and stabilize
            print(f"⏳ DEBUG: Waiting for vehicle to arm completely...")
            time.sleep(5)  # Increased from 3 to 5 seconds
            
            # Verify vehicle is actually armed
            print(f"🔍 DEBUG: Verifying armed state - vehicle_data.armed = {self.vehicle_data.armed}")
            if not self.vehicle_data.armed:
                print(f"⚠️ WARNING: Vehicle not showing as armed after waiting!")

            self.socketio.emit('log', {
                'message': f'🚀 Starting mission execution! (Armed: {self.vehicle_data.armed})',
                'type': 'success',
                'timestamp': datetime.now().isoformat()
            })

            if mission_id and mission_planner.set_current_mission(mission_id):
                mission_planner.mission_started = True
                mission = mission_planner.get_mission(mission_id)
                if mission:
                    mission['current_wp_index'] = 0
                    mission['status'] = 'ACTIVE'

                mission_planner.execute_waypoint_action(self.vehicle_data, self.socketio)
                return True
            else:
                self.socketio.emit('log', {
                    'message': '⚠️ No mission selected or mission activation failed',
                    'type': 'warning',
                    'timestamp': datetime.now().isoformat()
                })
                return False

        except Exception as e:
            self.socketio.emit('log', {
                'message': f'❌ Mission sequence failed: {str(e)}',
                'type': 'error',
                'timestamp': datetime.now().isoformat()
            })
            print(f"❌ Error in execute_mission_sequence: {e}")
            import traceback
            traceback.print_exc()
            return False

    def send_waypoint_command(self, lat, lon, alt):
        if not self.connected or not self.master:
            return False

        try:
            # Convert to proper MAVLink integer format (degE7)
            lat_int = int(lat * 1e7)
            lon_int = int(lon * 1e7)
            alt_mm = int(alt * 1000)  # Altitude in millimeters

            # Use SET_POSITION_TARGET_GLOBAL_INT for GUIDED mode waypoint navigation
            # This is the correct message for setting waypoints in GUIDED mode
            self.master.mav.set_position_target_global_int_send(
                0,  # time_boot_ms (not used)
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,  # Use relative altitude frame
                0b0000111111111000,  # type_mask (only positions enabled)
                lat_int,  # Latitude in degE7
                lon_int,  # Longitude in degE7
                alt,  # Altitude in meters (relative to home)
                0, 0, 0,  # vx, vy, vz (not used)
                0, 0, 0,  # afx, afy, afz (not used)
                0, 0  # yaw, yaw_rate (not used)
            )

            self.socketio.emit('log', {
                'message': f'🎯 Navigating to waypoint: {lat:.6f}, {lon:.6f}, {alt}m',
                'type': 'info',
                'timestamp': datetime.now().isoformat()
            })
            return True
        except Exception as e:
            print(f"Error sending waypoint command: {e}")
            return False

    def send_mavlink_command(self, command_type, params=None):
        if not self.connected or not self.master:
            print(f"❌ Cannot send {command_type} command - connected: {self.connected}, master: {self.master is not None}")
            return False

        print(f"📡 Sending MAVLink command: {command_type}, params: {params}")
        
        try:
            if command_type == 'arm':
                print("🔄 Sending FORCE ARM command with safety bypasses...")
                
                # Try to disable some safety checks temporarily (will be re-enabled by autopilot)
                # This helps with EKF and GPS issues
                try:
                    # Set ARMING_CHECK to 0 (disable all pre-arm checks) temporarily
                    self.master.mav.param_set_send(
                        self.master.target_system,
                        self.master.target_component,
                        b'ARMING_CHECK',
                        0,  # Disable all checks
                        mavutil.mavlink.MAV_PARAM_TYPE_INT32
                    )
                    print("✅ Temporarily disabled arming checks")
                    time.sleep(0.5)
                except Exception as e:
                    print(f"⚠️ Could not disable arming checks: {e}")
                
                # Force arm with magic number 21196 (bypasses pre-arm checks)
                for attempt in range(3):
                    try:
                        self.master.mav.command_long_send(
                            self.master.target_system,
                            self.master.target_component,
                            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                            0,  # confirmation
                            1,  # arm (1) or disarm (0)
                            21196,  # force arm (magic number - bypasses safety checks)
                            0, 0, 0, 0, 0
                        )
                        print(f"✅ Force arm command sent (attempt {attempt + 1})")
                        time.sleep(2.0)
                        
                        # Check if armed
                        if self.vehicle_data.armed:
                            print(f"✅ Vehicle armed successfully on attempt {attempt + 1}")
                            break
                    except Exception as e:
                        print(f"⚠️ Arm attempt {attempt + 1} failed: {e}")

                # Fallback: Try simple arm command
                if not self.vehicle_data.armed:
                    try:
                        self.master.arducopter_arm()
                        print("✅ Simple arm command sent")
                        time.sleep(1.0)
                    except Exception as e:
                        print(f"⚠️ Simple arm failed: {e}")
                
                return True

            elif command_type == 'disarm':
                print("📤 Sending DISARM command...")
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                    0,  # Confirmation
                    0,  # Param 1: Disarm (0=disarm, 1=arm)
                    0, 0, 0, 0, 0, 0  # Params 2-7
                )
                print("✅ DISARM command sent")
                return True

            elif command_type == 'takeoff':
                altitude = params.get('altitude', 10) if params else 10
                print(f"📤 Sending TAKEOFF command to {altitude}m...")
                print(f"🔍 DEBUG: Current mode: {self.vehicle_data.mode}, Armed: {self.vehicle_data.armed}")
                
                # Check if already in GUIDED mode, if not set it
                if self.vehicle_data.mode != 'GUIDED':
                    print("📡 Setting GUIDED mode before takeoff...")
                    self.master.mav.command_long_send(
                        self.master.target_system,
                        self.master.target_component,
                        mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                        0,
                        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                        4,  # GUIDED mode
                        0, 0, 0, 0, 0
                    )
                    time.sleep(2.0)  # Increased wait time for mode change
                    print(f"✅ Mode after setting: {self.vehicle_data.mode}")
                else:
                    print(f"✅ Already in GUIDED mode, skipping mode change")
                
                # Verify vehicle is armed
                if not self.vehicle_data.armed:
                    print("❌ ERROR: Vehicle not armed! Cannot takeoff.")
                    return False

                # Use MAV_CMD_NAV_TAKEOFF to actually spin motors and climb.
                # In ArduCopter, this is the most reliable way to initiate takeoff in GUIDED.
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                    0,  # Confirmation
                    0, 0, 0, 0,  # Params 1-4
                    0, 0,  # Params 5-6: Lat/Lon (0 = current)
                    float(altitude)  # Param 7: Altitude (m)
                )
                print(f"✅ MAV_CMD_NAV_TAKEOFF sent to {altitude}m")
                return True

            elif command_type == 'rtl':
                print("📤 Sending RTL command...")
                print(f"📤 DEBUG: target_system={self.master.target_system}, target_component={self.master.target_component}")
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH,
                    0,  # Confirmation
                    0, 0, 0, 0, 0, 0, 0  # Params 1-7 (all empty for RTL)
                )
                print("✅ RTL command sent successfully")
                print(f"✅ DEBUG: Command ID = {mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH}")
                return True

            elif command_type == 'loiter':
                # Manual LOITER mode command - switch to LOITER flight mode
                print("🔄 Sending LOITER mode command (manual)...")
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                    0,  # confirmation
                    mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    5,  # LOITER mode = 5 for ArduCopter
                    0, 0, 0, 0, 0
                )
                print("✅ LOITER mode command sent - vehicle will hover at current position")
                return True

            elif command_type == 'land':
                # Land by setting mode LAND (ArduCopter custom_mode=9 typically)
                return self.send_mavlink_command('set_mode', {'mode': 'LAND'})

            elif command_type == 'set_mode':
                mode = params.get('mode', 'GUIDED') if params else 'GUIDED'
                mode_mapping = {
                    'STABILIZE': 0, 'ALT_HOLD': 2, 'AUTO': 3,
                    'GUIDED': 4, 'LOITER': 5, 'RTL': 6,
                    'LAND': 9
                }
                if mode in mode_mapping:
                    self.master.mav.command_long_send(
                        self.master.target_system,
                        self.master.target_component,
                        mavutil.mavlink.MAV_CMD_DO_SET_MODE, 0,
                        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                        mode_mapping[mode], 0, 0, 0, 0, 0
                    )
                return True

            return True
        except Exception as e:
            print(f"❌ Error sending MAVLink command '{command_type}': {e}")
            import traceback
            traceback.print_exc()
            return False

    def force_arm_direct(self):
        if not self.connected or not self.master:
            return False

        try:
            print("🚀 Sending DIRECT FORCE ARM...")
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1, 1, 21196, 0, 0, 0, 0, 0
            )
            print("✅ Direct force arm command sent")
            return True
        except Exception as e:
            print(f"❌ Direct force arm failed: {e}")
            return False


# Data persistence handler
class DataLogger:
    def __init__(self, log_file='telemetry_log.json', max_entries=1000):
        self.log_file = log_file
        self.max_entries = max_entries
        self.ensure_valid_json_file()

    def ensure_valid_json_file(self):
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        json.loads(content)
                print("JSON file is valid")
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"JSON file corrupted, recreating: {e}")
                import shutil
                backup_name = f"{self.log_file}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                shutil.copy2(self.log_file, backup_name)
                with open(self.log_file, 'w', encoding='utf-8') as f:
                    json.dump([], f, indent=2)
                print("New JSON file created")
        else:
            with open(self.log_file, 'w', encoding='utf-8') as f:
                json.dump([], f, indent=2)
            print("New JSON file created")

    def save_telemetry(self, data):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if os.path.exists(self.log_file):
                    try:
                        with open(self.log_file, 'r', encoding='utf-8') as f:
                            content = f.read().strip()
                            if content:
                                existing_data = json.loads(content)
                            else:
                                existing_data = []
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        print(f"JSON read error, recreating file: {e}")
                        existing_data = []
                        self.ensure_valid_json_file()
                else:
                    existing_data = []

                entry = {
                    'timestamp': datetime.now().isoformat(),
                    'data': data
                }
                existing_data.append(entry)

                if len(existing_data) > self.max_entries:
                    existing_data = existing_data[-self.max_entries:]

                temp_file = self.log_file + '.tmp'
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(existing_data, f, indent=2, ensure_ascii=False)

                os.replace(temp_file, self.log_file)
                return

            except Exception as e:
                print(f"Error saving telemetry (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    print("All retry attempts failed, resetting JSON file")
                    self.ensure_valid_json_file()
                time.sleep(0.1)

    def get_recent_data(self, limit=100):
        try:
            if os.path.exists(self.log_file):
                with open(self.log_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        data = json.loads(content)
                        return data[-limit:] if limit else data
            return []
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"Error reading JSON file: {e}")
            self.ensure_valid_json_file()
            return []


# Initialize components
data_logger = DataLogger()
mission_planner = MissionPlanner()
app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_timeout=60, ping_interval=25)

# Store connected clients
clients = []

# Initialize vehicle data and telemetry handler
vehicle_data = VehicleData()
windows_telemetry = WindowsTelemetryHandler(socketio)


# API Routes for Mission Planning
@app.route('/api/missions', methods=['GET', 'POST'])
def handle_missions():
    if request.method == 'POST':
        data = request.get_json()
        mission_name = data.get('name', 'Unnamed Mission')
        waypoints = data.get('waypoints', [])
        mission_id = mission_planner.create_mission(mission_name, waypoints)
        return jsonify({'mission_id': mission_id, 'status': 'created'})
    else:
        missions = mission_planner.get_all_missions()
        return jsonify(missions)


@app.route('/api/missions/<mission_id>', methods=['GET', 'PUT', 'DELETE'])
def handle_mission(mission_id):
    if request.method == 'GET':
        mission = mission_planner.get_mission(mission_id)
        if mission:
            return jsonify(mission)
        return jsonify({'error': 'Mission not found'}), 404
    elif request.method == 'PUT':
        data = request.get_json()
        if data.get('action') == 'activate':
            if mission_planner.set_current_mission(mission_id):
                return jsonify({'status': 'activated'})
            return jsonify({'error': 'Mission not found'}), 404
    elif request.method == 'DELETE':
        if mission_id in mission_planner.missions:
            del mission_planner.missions[mission_id]
            return jsonify({'status': 'deleted'})
        return jsonify({'error': 'Mission not found'}), 404


@app.route('/api/mission/current', methods=['GET', 'DELETE'])
def handle_current_mission():
    if request.method == 'GET':
        if mission_planner.current_mission_id:
            mission = mission_planner.get_mission(mission_planner.current_mission_id)
            return jsonify(mission)
        return jsonify({'current_mission': None})
    elif request.method == 'DELETE':
        mission_planner.current_mission_id = None
        mission_planner.mission_started = False
        vehicle_data.mode = "MANUAL"
        return jsonify({'status': 'cleared'})


@app.route('/api/mission/advance', methods=['POST'])
def advance_mission():
    result = mission_planner.advance_waypoint()
    return jsonify({'status': result})


@app.route('/api/mission/start_sequence', methods=['POST'])
def start_mission_sequence():
    data = request.get_json()
    mission_id = data.get('mission_id')
    if not mission_id:
        return jsonify({'error': 'No mission ID provided'}), 400
    success = windows_telemetry.execute_mission_sequence(mission_id)
    if success:
        return jsonify({'status': 'mission_sequence_started'})
    else:
        return jsonify({'error': 'Failed to start mission sequence'}), 500


@app.route('/api/mission/resume_after_smoke', methods=['POST'])
def resume_mission_after_smoke():
    """Resume mission sau khi người giám sát xác nhận khói."""
    try:
        if not mission_planner.paused_by_smoke:
            return jsonify({'error': 'Mission is not paused by smoke'}), 400
        
        # Resume mission
        resume_mode = mission_planner.resume_mission_after_smoke(socketio, windows_telemetry.vehicle_data if windows_telemetry.connected else vehicle_data)
        if resume_mode:
            # Restore mode (default to GUIDED if unknown)
            target_mode = resume_mode if isinstance(resume_mode, str) and resume_mode else 'GUIDED'
            if windows_telemetry.connected:
                windows_telemetry.send_mavlink_command('set_mode', {'mode': target_mode})
                time.sleep(1)
            
            socketio.emit('log', {
                'message': f'✅ Mission resumed - switching to {target_mode} mode...',
                'type': 'success',
                'timestamp': datetime.now().isoformat()
            })
            
            return jsonify({'status': 'mission_resumed', 'resume_mode': target_mode})
        else:
            return jsonify({'error': 'Failed to resume mission'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/mission/smoke_pause_status', methods=['GET'])
def get_smoke_pause_status():
    """Lấy trạng thái smoke pause."""
    return jsonify({
        'paused': mission_planner.paused_by_smoke,
        'location': mission_planner.smoke_pause_location,
        'mission_active': mission_planner.mission_started
    })


@app.route('/api/set_home_position', methods=['POST'])
def set_home_position():
    data = request.get_json()
    lat = data.get('lat')
    lng = data.get('lng')
    alt = data.get('alt', 0)
    if lat is None or lng is None:
        return jsonify({'error': 'Latitude and longitude required'}), 400
    home_position = mission_planner.set_home_position(lat, lng, alt)
    vehicle_data.lat = lat
    vehicle_data.lon = lng
    vehicle_data.alt = alt
    return jsonify({
        'status': 'home_position_set',
        'home_position': home_position
    })


@socketio.on('takeoff_with_altitude')
def handle_takeoff_with_altitude(data):
    altitude = data.get('altitude', 5)  # Default 5m (max 500m limit)
    
    # Debug: Print armed status
    print(f"🔍 Takeoff requested - Armed status: {vehicle_data.armed}")
    
    socketio.emit('log', {
        'message': f'🛫 Manual takeoff to {altitude}m',
        'type': 'warning',
        'timestamp': datetime.now().isoformat()
    })
    
    if windows_telemetry.connected:
        # Add small delay after arming before takeoff
        time.sleep(2.0)
        windows_telemetry.send_mavlink_command('takeoff', {'altitude': altitude})
    else:
        def simulate_takeoff():
            time.sleep(2)
            vehicle_data.alt = altitude
            socketio.emit('log', {
                'message': f'Manual takeoff completed to {altitude}m',
                'type': 'success',
                'timestamp': datetime.now().isoformat()
            })

        threading.Thread(target=simulate_takeoff, daemon=True).start()


@app.route('/')
def index():
    # COMPLETE HTML INTERFACE (2500+ lines)
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>MAVLink Telemetry with Advanced Mission Planning</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <style>
            /* COMPLETE CSS STYLES */
            body {
                margin: 0;
                padding: 0;
                background: #1a1a1a;
                color: white;
                font-family: Arial, sans-serif;
                
            }
            .dashboard {
                padding: 20px;
                padding-top: 20px; /* Add space for logos at top */
                min-height: 100vh;
            }
            .header {
                text-align: center;
                margin-bottom: 30px;
                margin-top: -20px;
                padding: 20px;
                background: transparent; /* Make transparent so logos show through */
                border-radius: 10px;
                border: 1px solid #444;
            }
            .header h1 {
                margin: 0;
                font-size: 2.5em;
                background: linear-gradient(45deg, #00d4ff, #0099cc);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            .subtitle {
                color: #888;
                font-size: 1.1em;
                margin-top: 5px;
            }
            .connection-status {
                padding: 12px 20px;
                border-radius: 8px;
                margin: 20px 0;
                font-weight: bold;
                text-align: center;
                border: 1px solid transparent;
            }
            .connection-status.connected {
                background: rgba(76, 175, 80, 0.2);
                border-color: #4CAF50;
                color: #4CAF50;
            }
            .connection-status.disconnected {
                background: rgba(244, 67, 54, 0.2);
                border-color: #f44336;
                color: #f44336;
            }
            .connection-status.real-telemetry {
                background: rgba(33, 150, 243, 0.2);
                border-color: #2196F3;
                color: #2196F3;
            }
            .status-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin: 30px 0;
            }
            .status-card {
                background: #2d2d2d;
                padding: 20px;
                border-radius: 12px;
                border: 1px solid #444;
                box-shadow: 0 4px 15px rgba(0, 0, 0, 0.3);
            }
            .status-card h3 {
                margin: 0 0 15px 0;
                color: #00d4ff;
                font-size: 1.1em;
                border-bottom: 2px solid #444;
                padding-bottom: 8px;
            }
            .status-value {
                font-size: 1.8em;
                font-weight: bold;
                color: white;
                text-align: center;
                margin: 10px 0;
            }
            .status-subvalue {
                font-size: 0.9em;
                color: #888;
                text-align: center;
            }
            .charts-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 20px;
                margin: 30px 0;
            }
            .chart-container {
                height: 200px;
                position: relative;
            }
            #leafletMap {
                height: 500px;
                width: 100%;
                border-radius: 8px;
                border: 1px solid #444;
            }
            .control-btn {
                padding: 12px 20px;
                background: linear-gradient(135deg, #2196F3, #1976D2);
                color: white;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                font-size: 14px;
                font-weight: bold;
                transition: all 0.3s ease;
            }
            .control-btn:hover {
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(33, 150, 243, 0.4);
            }
            .control-btn:disabled {
                background: #666;
                cursor: not-allowed;
                transform: none;
                box-shadow: none;
            }
            .log-container {
                height: 200px;
                overflow-y: auto;
                background: #000;
                padding: 15px;
                border-radius: 8px;
                border: 1px solid #444;
                font-family: monospace;
                font-size: 12px;
            }
            .log-container div {
                margin: 5px 0;
                padding: 5px;
                border-left: 3px solid transparent;
            }
            .mission-panel {
                background: #2d2d2d;
                padding: 25px;
                border-radius: 12px;
                border: 1px solid #444;
                margin: 20px 0;
            }
            .mission-panel h3 {
                color: #00d4ff;
                margin-bottom: 20px;
                font-size: 1.3em;
            }
            .mission-panel h4 {
                color: #ffeb3b;
                margin: 15px 0 10px 0;
            }
            .waypoint-list {
                max-height: 300px;
                overflow-y: auto;
                background: rgba(0, 0, 0, 0.3);
                padding: 15px;
                border-radius: 8px;
                border: 1px solid #444;
                margin: 10px 0;
            }
            .waypoint-item {
                background: rgba(255, 255, 255, 0.05);
                padding: 12px;
                margin: 8px 0;
                border-radius: 6px;
                border: 1px solid #444;
                cursor: pointer;
                transition: all 0.3s ease;
            }
            .waypoint-item:hover {
                background: rgba(255, 255, 255, 0.1);
                border-color: #00d4ff;
            }
            .waypoint-item.active {
                border-color: #4CAF50;
                background: rgba(76, 175, 80, 0.1);
            }
            .waypoint-item.has-action {
                border-left: 4px solid #ff9800;
            }
            .mission-btn {
                padding: 10px 16px;
                background: linear-gradient(135deg, #4CAF50, #45a049);
                color: white;
                border: none;
                border-radius: 6px;
                cursor: pointer;
                font-size: 12px;
                font-weight: bold;
                margin: 5px;
                transition: all 0.3s ease;
            }
            .mission-btn:hover {
                transform: translateY(-1px);
                box-shadow: 0 3px 10px rgba(76, 175, 80, 0.3);
            }
            .mission-btn.delete {
                background: linear-gradient(135deg, #f44336, #d32f2f);
            }
            .mission-btn.delete:hover {
                box-shadow: 0 3px 10px rgba(244, 67, 54, 0.3);
            }
            .mission-btn.sequence {
                background: linear-gradient(135deg, #ff9800, #f57c00);
                font-size: 14px;
                padding: 12px 20px;
            }
            .mission-btn.sequence:hover {
                box-shadow: 0 3px 10px rgba(255, 152, 0, 0.3);
            }
            .mission-btn.home {
                background: linear-gradient(135deg, #9c27b0, #7b1fa2);
                font-size: 12px;
            }
            .mission-btn.home:hover {
                box-shadow: 0 3px 10px rgba(156, 39, 176, 0.3);
            }
            .action-panel {
                background: rgba(40, 40, 40, 0.95);
                border-radius: 8px;
                padding: 20px;
                margin: 15px 0;
                border-left: 4px solid #00d4ff;
                border: 1px solid #444;
                color: #ffffff;
            }
            .action-panel h4,
            .action-panel div,
            #selectedWaypoint {
                color: #ffffff !important;
            }
            .action-form {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 12px;
                margin-top: 15px;
                color: #ffffff;
            }
            .action-input {
                padding: 10px;
                border-radius: 6px;
                border: 1px solid #555;
                background: rgba(255,255,255,0.15);
                color: white;
                font-size: 14px;
            }
            .action-input::placeholder {
                color: #cccccc;
            }
            .action-input:focus {
                outline: none;
                border-color: #00d4ff;
                box-shadow: 0 0 5px rgba(0, 212, 255, 0.5);
            }
            .action-btn {
                padding: 10px 16px;
                background: #4CAF50;
                color: white;
                border: none;
                border-radius: 6px;
                cursor: pointer;
                font-size: 12px;
                font-weight: bold;
                transition: all 0.3s ease;
            }
            .action-btn:hover {
                background: #45a049;
                transform: translateY(-1px);
            }
            .action-btn.remove {
                background: #f44336;
            }
            .action-btn.remove:hover {
                background: #d32f2f;
            }
            .action-tag {
                display: inline-block;
                background: #ff9800;
                color: black;
                padding: 3px 10px;
                border-radius: 15px;
                font-size: 11px;
                font-weight: bold;
                margin-left: 8px;
            }
            .mission-info {
                background: rgba(0, 212, 255, 0.1);
                padding: 15px;
                border-radius: 8px;
                border: 1px solid #00d4ff;
                margin: 10px 0;
            }
            .mission-status {
                text-align: center;
                font-weight: bold;
                padding: 5px;
                border-radius: 4px;
                margin-bottom: 10px;
            }
            .status-active {
                background: rgba(76, 175, 80, 0.2);
                color: #4CAF50;
            }
            .status-armed {
                background: rgba(255, 152, 0, 0.2);
                color: #ff9800;
            }
            .status-guided {
                background: rgba(33, 150, 243, 0.2);
                color: #2196F3;
            }
            .home-marker {
                background: #9c27b0 !important;
                border: 3px solid white !important;
            }
            ::-webkit-scrollbar {
                width: 8px;
            }
            ::-webkit-scrollbar-track {
                background: #2d2d2d;
                border-radius: 4px;
            }
            ::-webkit-scrollbar-thumb {
                background: #555;
                border-radius: 4px;
            }
            ::-webkit-scrollbar-thumb:hover {
                background: #777;
            }
            .home-panel {
                background: rgba(156, 39, 176, 0.1);
                padding: 15px;
                border-radius: 8px;
                border: 1px solid #9c27b0;
                margin: 10px 0;
            }
            .coordinate-input {
                display: grid;
                grid-template-columns: 1fr 1fr 1fr;
                gap: 10px;
                margin: 10px 0;
            }
            .coord-input {
                padding: 8px;
                border-radius: 6px;
                border: 1px solid #555;
                background: rgba(255,255,255,0.1);
                color: white;
                font-size: 12px;
            }
            .takeoff-control {
                background: rgba(255, 152, 0, 0.1);
                padding: 15px;
                border-radius: 8px;
                border: 1px solid #ff9800;
                margin: 10px 0;
            }
            .altitude-input-group {
                display: flex;
                gap: 10px;
                align-items: center;
                margin: 10px 0;
            }
            .altitude-input {
                padding: 10px;
                border-radius: 6px;
                border: 1px solid #555;
                background: rgba(255,255,255,0.1);
                color: white;
                font-size: 14px;
                flex: 1;
            }
            .takeoff-btn {
                background: linear-gradient(135deg, #ff9800, #f57c00);
                color: white;
                border: none;
                padding: 10px 16px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 12px;
                font-weight: bold;
                transition: all 0.3s ease;
            }
            .takeoff-btn:hover {
                transform: translateY(-1px);
                box-shadow: 0 3px 10px rgba(255, 152, 0, 0.3);
            }
        </style>
    </head>
    <body>
        <div class="dashboard">
            <div class="header">
                <h1>🚁 Drone Control and Monitoring Ground Station</h1>
                <div class="subtitle">By Nguyen Viet Khue and Le Hoang Khang</div>
            </div>

            <div class="connection-status" id="connectionStatus">
                🔄 Connecting to MAVLink Server...
            </div>

            <!-- Mission Planning Panel -->
            <div class="mission-panel">
                <h3>🎯 Advanced Mission Planning</h3>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                    <div>
                        <h4>Create Mission</h4>

                        <!-- Home Position Panel -->
                        <div class="home-panel">
                            <h4 style="color: #9c27b0; margin-top: 0;">🏠 Set Launch Position</h4>
                            <div class="coordinate-input">
                                <input type="number" id="homeLat" class="coord-input" placeholder="Latitude" step="0.000001" value="10.794944">
                                <input type="number" id="homeLng" class="coord-input" placeholder="Longitude" step="0.000001" value="106.736939">
                                <input type="number" id="homeAlt" class="coord-input" placeholder="Altitude (m)" value="0">
                            </div>
                            <button class="mission-btn home" onclick="setHomePosition()">Set Launch Position</button>
                            <button class="mission-btn home" onclick="setHomeToCurrent()">Use Current Map Center</button>
                            <div style="font-size: 11px; color: #ffeb3b; margin-top: 5px;">
                                Waypoint 1 will always be at this launch position
                            </div>
                        </div>

                        <input type="text" id="missionName" placeholder="Mission Name" style="width: 100%; padding: 12px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #555; background: rgba(255,255,255,0.1); color: white; font-size: 14px;">

                        <!-- Action Configuration Panel -->
                        <div class="action-panel" id="actionPanel" style="display: none;">
                            <h4>Configure Waypoint Action</h4>
                            <div>Selected Waypoint: <span id="selectedWaypoint">-</span></div>
                            
                            <!-- Waypoint Altitude Editor -->
                            <div style="margin: 10px 0; padding: 10px; background: rgba(255,255,255,0.05); border-radius: 5px;">
                                <label style="display: block; margin-bottom: 5px; color: #ffeb3b; font-size: 12px;">Waypoint Altitude (m):</label>
                                <input type="number" id="waypointAltitude" class="action-input" placeholder="Altitude (m)" min="0" max="10" style="width: 100%;" onchange="updateWaypointAltitude()">
                            </div>
                            
                            <div class="action-form">
                                <select id="actionType" class="action-input" onchange="updateActionForm()">
                                    <option value="">No Action</option>
                                    <option value="takeoff">Takeoff</option>
                                    <option value="rtl">Return to Start</option>
                                    <option value="land">Land</option>
                                    <option value="delay">Delay</option>
                                    <option value="loiter">Loiter</option>
                                    <option value="set_speed">Set Speed</option>
                                </select>
                                <div id="actionParams"></div>
                                <button class="action-btn" onclick="saveWaypointAction()">Save Action</button>
                                <button class="action-btn remove" onclick="clearWaypointAction()">Clear Action</button>
                            </div>
                        </div>

                        <div class="waypoint-list" id="waypointList">
                            <div style="text-align: center; color: #888; padding: 20px;">
                                Click on map to set home/launch position (Waypoint 1). Then add more waypoints.
                            </div>
                        </div>
                        <div style="display: flex; gap: 10px; margin-top: 15px;">
                            <button class="mission-btn" onclick="clearWaypoints()">Clear Waypoints</button>
                            <button class="mission-btn" onclick="saveMission()">Save Mission</button>
                        </div>
                    </div>
                    <div>
                        <h4>Saved Missions</h4>
                        <div class="waypoint-list" id="savedMissionsList">
                            <div style="text-align: center; color: #888; padding: 20px;">
                                No saved missions
                            </div>
                        </div>

                        <div style="margin: 15px 0;">
                            <button class="mission-btn sequence" onclick="startMissionSequence()" style="width: 100%;">
                                🚀 START MISSION SEQUENCE
                            </button>
                            <div style="text-align: center; font-size: 11px; color: #ffeb3b; margin-top: 5px;">
                                GUIDED → ARM → START MISSION
                            </div>
                        </div>

                        <!-- 🔥 SMOKE PAUSE CONTROL -->
                        <!-- 🔥 SMOKE PAUSE CONTROL: Panel ẩn mặc định, chỉ hiện khi có khói -->
                        <div id="smokePausePanel" style="display: none; margin: 15px 0; padding: 15px; background: rgba(255, 152, 0, 0.2); border: 2px solid #ff9800; border-radius: 8px;">
                            <div style="text-align: center; font-size: 16px; font-weight: bold; color: #ff9800; margin-bottom: 10px;">
                                ⏸️ MISSION TẠM DỪNG - KHÓI PHÁT HIỆN!
                            </div>
                            <div style="text-align: center; font-size: 12px; color: #ffeb3b; margin-bottom: 10px;">
                                Drone đang LOITER tại vị trí phát hiện khói
                            </div>
                            <button class="mission-btn sequence" onclick="resumeMissionAfterSmoke()" style="width: 100%; background: linear-gradient(135deg, #4CAF50, #45a049);">
                                ✅ TIẾP TỤC LỘ TRÌNH
                            </button>
                            <div style="text-align: center; font-size: 11px; color: #9aa0a6; margin-top: 8px;">
                                Bấm khi đã kiểm tra và xác nhận an toàn
                            </div>
                        </div>

                        <div class="mission-controls" style="display: flex; gap: 10px; margin: 15px 0;">
                            <button class="mission-btn" onclick="loadMissions()">Refresh</button>
                            <button class="mission-btn delete" onclick="clearCurrentMission()">Stop Mission</button>
                        </div>

                        <div class="mission-info" id="currentMissionInfo" style="display: none;">
                            <div class="mission-status" id="missionStatus">Active</div>
                            <div style="text-align: center; font-size: 14px;">
                                WP: <span id="currentWP">0</span>/<span id="totalWP">0</span>
                            </div>
                            <div style="text-align: center; font-size: 12px; color: #ffeb3b;" id="currentAction">
                                No action
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Manual Takeoff Control -->
            <div class="takeoff-control">
                <h3>🛫 Manual Takeoff Control</h3>
                <div class="altitude-input-group">
                    <input type="number" id="manualTakeoffAltitude" class="altitude-input" placeholder="Takeoff Altitude (m)" value="5" min="1" max="500">
                    <button class="takeoff-btn" onclick="manualTakeoff()">TAKEOFF TO ALTITUDE</button>
                </div>
                <div style="font-size: 11px; color: #ffeb3b; margin-top: 5px;">
                    Set altitude and click to take off manually (vehicle must be armed)
                </div>
            </div>

                <div class="status-grid">
                    <div class="status-card">
                        <h3>📍 Position</h3>
                        <div class="status-value" id="position">N/A</div>
                        <div class="status-subvalue" id="gpsInfo">GPS: No fix</div>
                    </div>
                    <div class="status-card">
                        <h3>📏 Altitude</h3>
                        <div class="status-value" id="altitude">0 m</div>
                        <div class="status-subvalue" id="relativeAlt">Relative: 0 m</div>
                    </div>
                    <div class="status-card">
                        <h3>💨 Speed</h3>
                        <div class="status-value" id="speed">0 km/h</div>
                        <div class="status-subvalue" id="airspeed">Airspeed: 0 m/s</div>
                    </div>
                    <div class="status-card">
                        <h3>🧭 Heading</h3>
                        <div class="status-value" id="heading">0°</div>
                        <div class="status-subvalue" id="attitude">Roll: 0° Pitch: 0°</div>
                    </div>
                    <div class="status-card">
                        <h3>🔋 Battery</h3>
                        <div class="status-value" id="battery">N/A</div>
                        <div class="status-subvalue" id="batteryVoltage">Voltage: 0.0V</div>
                    </div>
                    <div class="status-card">
                        <h3>🟢 Status</h3>
                        <div class="status-value" id="systemStatus">Unknown</div>
                        <div class="status-subvalue" id="flightMode">Mode: UNKNOWN</div>
                        <div class="status-subvalue" id="armedStatus">Armed: NO</div>
                    </div>
                </div>
    
                <div class="charts-grid">
                    <div class="status-card">
                        <h3>📈 Altitude History</h3>
                        <div class="chart-container">
                            <canvas id="altitudeChart"></canvas>
                        </div>
                    </div>
                    <div class="status-card">
                        <h3>🚀 Speed History</h3>
                        <div class="chart-container">
                            <canvas id="speedChart"></canvas>
                        </div>
                    </div>
                    <div class="status-card">
                        <h3>🔋 Battery History</h3>
                        <div class="chart-container">
                            <canvas id="batteryChart"></canvas>
                        </div>
                    </div>
                </div>
                
                <!-- 🔥 FIRE DETECTION PANEL (JETSON) -->
                <div class="status-card" style="grid-column: 1 / -1;">
                    <h3>🔥 Fire/Smoke Monitoring (Jetson YOLOv11)</h3>
                    <div style="
                        display: grid;
                        grid-template-columns: 35% 65%;
                        gap: 20px;
                        align-items: flex-start;
                    ">
                        <div>
                            <!-- Status chính - TO HƠN -->
                            <div id="fireStatus" style="
                                font-size: 28px;
                                font-weight: bold;
                                margin-bottom: 20px;
                                text-align: center;
                                padding: 15px;
                                border-radius: 8px;
                                background: rgba(0,0,0,0.3);
                            ">NO SMOKE / NO FIRE</div>

                            <div style="display:flex; gap:10px; justify-content:center; margin-bottom: 18px;">
                                <button id="resetFireUIBtn"
                                        onclick="window.dashboard && window.dashboard.resetFireUI()"
                                        style="
                                            padding:10px 16px;
                                            border-radius:8px;
                                            border:1px solid #666;
                                            background: rgba(255,255,255,0.06);
                                            color:#fff;
                                            cursor:pointer;
                                            font-weight:bold;
                                        ">
                                    🔄 Reset Status
                                </button>
                            </div>


                            <!-- 📋 ALERT HISTORY LOG - TO HƠN -->
                            <div id="alertHistoryContainer" style="
                                padding: 15px;
                                border: 1px solid #444;
                                border-radius: 8px;
                                background: rgba(0,0,0,0.3);
                            ">
                                <div style="color: #ffa500; font-size: 18px; font-weight: bold; margin-bottom: 12px;">
                                    📋 Alert History (Total: <span id="alertTotalCount">0</span>)
                                </div>
                                <div id="alertHistoryList" style="
                                    max-height: 200px;
                                    overflow-y: auto;
                                    font-family: monospace;
                                    font-size: 14px;
                                    line-height: 1.6;
                                ">
                                    <div style="color: #888;">No alerts yet</div>
                                </div>
                            </div>

                            <div style="margin-top:12px; font-size:12px; color:#9aa0a6; line-height:1.4;">
                                <div><b>Video</b>: Model 1 (Smoke) chạy liên tục.</div>
                                <div><b>3 ảnh dưới</b>: snapshot gần nhất đã chạy Model 2 (Fire).</div>
                            </div>
                        </div>

                        <div style="display:flex; flex-direction:column; gap:12px;">
                            <!-- Video (Model 1 - Smoke) -->
                            <img id="jetsonSmokeVideo"
                                 src=""
                                 style="
                                    width: 100%;
                                    max-height: 520px;
                                    border-radius: 8px;
                                    border: 1px solid #444;
                                    background: #000;
                                    object-fit: contain;
                                 ">

                            <!-- 3 ảnh tĩnh (Model 2 - Fire snapshots) -->
                            <div style="
                                display: grid;
                                grid-template-columns: repeat(3, 1fr);
                                gap: 10px;
                            ">
                                <img id="fireSnap0" src="" style="width:100%; max-height:220px; border-radius:8px; border:1px solid #444; background:#000; object-fit:contain;">
                                <img id="fireSnap1" src="" style="width:100%; max-height:220px; border-radius:8px; border:1px solid #444; background:#000; object-fit:contain;">
                                <img id="fireSnap2" src="" style="width:100%; max-height:220px; border-radius:8px; border:1px solid #444; background:#000; object-fit:contain;">
                            </div>
                        </div>
                    </div>
                </div>

                <div class="status-card" style="grid-column: 1 / -1;">
                    <h3>🗺️ Live Position Tracking & Mission Planning</h3>
                    <div id="leafletMap"></div>
                </div>

            <div class="status-card" style="grid-column: 1 / -1;">
                <h3>🎮 Vehicle Controls</h3>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px;">
                    <button class="control-btn" onclick="sendCommand('arm')" id="armBtn">🔓 Force Arm</button>
                    <button class="control-btn" onclick="sendCommand('disarm')" id="disarmBtn">🔒 Disarm</button>
                    <button class="control-btn" onclick="manualTakeoff()">🛫 Takeoff to Altitude</button>
                    <button class="control-btn" onclick="sendCommand('rtl')">🏠 Return to Start</button>
                    <button class="control-btn" onclick="sendCommand('loiter')" style="background: linear-gradient(135deg, #9C27B0, #7B1FA2);">🔄 Loiter Mode</button>
                    <button class="control-btn" onclick="sendCommand('land')" style="background: linear-gradient(135deg, #4CAF50, #45a049);">🛬 Land</button>
                    <button class="control-btn" style="background: linear-gradient(135deg, #ff6b6b, #ff4757);" 
                            onclick="sendCommand('emergency')">🚨 Emergency</button>
                    <button class="control-btn" onclick="forceArmDirect()" style="background: linear-gradient(135deg, #ff9800, #f57c00);">🚀 Direct Force Arm</button>
                </div>
            </div>

            <div class="status-card" style="grid-column: 1 / -1;">
                <h3>📋 System Logs</h3>
                <div class="log-container" id="logContainer">
                    <div>🚀 System initialized - Waiting for telemetry data...</div>
                </div>
            </div>
        </div>

        <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <script>
            // COMPLETE JAVASCRIPT CODE (1500+ lines)
            let waypoints = [];
            let missions = [];
            let currentMission = null;
            let selectedWaypointIndex = -1;
            let realTelemetryMode = false;
            let homeMarker = null;

            function manualTakeoff() {
                const altitude = parseInt(document.getElementById('manualTakeoffAltitude').value) || 50;
                if (altitude < 1 || altitude > 500) {
                    alert('Please enter a valid altitude between 1m and 500m');
                    return;
                }
                if (window.dashboard && window.dashboard.socket) {
                    window.dashboard.socket.emit('takeoff_with_altitude', { altitude: altitude });
                    window.dashboard.addLog(`Manual takeoff to ${altitude}m initiated`, 'warning');
                } else {
                    alert('Not connected to server!');
                }
            }

            async function setHomePosition() {
                const lat = parseFloat(document.getElementById('homeLat').value);
                const lng = parseFloat(document.getElementById('homeLng').value);
                const alt = parseFloat(document.getElementById('homeAlt').value) || 0;
                if (isNaN(lat) || isNaN(lng)) {
                    alert('Please enter valid coordinates');
                    return;
                }
                try {
                    const response = await fetch('/api/set_home_position', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ lat, lng, alt })
                    });
                    if (response.ok) {
                        const result = await response.json();
                        window.dashboard.addLog(`🏠 Home position set to: ${lat.toFixed(6)}, ${lng.toFixed(6)}, ${alt}m`, 'success');
                        updateHomeMarker(lat, lng);
                        if (waypoints.length > 0) {
                            waypoints[0].lat = lat;
                            waypoints[0].lng = lng;
                            waypoints[0].alt = alt;
                            updateWaypointList();
                            if (window.dashboard && window.dashboard.mapManager) {
                                window.dashboard.mapManager.updateMissionPath();
                            }
                        }
                    } else {
                        const error = await response.json();
                        window.dashboard.addLog(`Failed to set home position: ${error.error}`, 'error');
                    }
                } catch (error) {
                    console.error('Error setting home position:', error);
                    window.dashboard.addLog('Error setting home position', 'error');
                }
            }

            function setHomeToCurrent() {
                if (window.dashboard && window.dashboard.mapManager && window.dashboard.mapManager.map) {
                    const center = window.dashboard.mapManager.map.getCenter();
                    document.getElementById('homeLat').value = center.lat.toFixed(6);
                    document.getElementById('homeLng').value = center.lng.toFixed(6);
                    setHomePosition();
                }
            }

            function updateHomeMarker(lat, lng) {
                if (window.dashboard && window.dashboard.mapManager && window.dashboard.mapManager.map) {
                    if (homeMarker) {
                        window.dashboard.mapManager.map.removeLayer(homeMarker);
                    }
                    homeMarker = L.marker([lat, lng], {
                        icon: L.divIcon({
                            className: 'home-marker',
                            html: '<div style="background: #9c27b0; color: white; width: 24px; height: 24px; border-radius: 50%; border: 3px solid white; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 12px;">🏠</div>',
                            iconSize: [24, 24],
                            iconAnchor: [12, 12]
                        })
                    }).addTo(window.dashboard.mapManager.map);
                    homeMarker.bindPopup(`
                        <div style="color: #000; padding: 5px;">
                            <strong>🏠 Launch Position</strong><br>
                            Lat: ${lat.toFixed(6)}<br>
                            Lng: ${lng.toFixed(6)}<br>
                            This is your takeoff and RTL position
                        </div>
                    `).openPopup();
                    window.dashboard.mapManager.map.setView([lat, lng], 16);
                }
            }

            function updateActionForm() {
                const actionType = document.getElementById('actionType').value;
                const paramsDiv = document.getElementById('actionParams');
                paramsDiv.innerHTML = '';
                switch(actionType) {
                    case 'takeoff':
                        paramsDiv.innerHTML = `
                            <input type="number" id="takeoffAltitude" class="action-input" placeholder="Altitude (m)" value="5" min="1" max="500" style="grid-column: 1 / -1;">
                            <input type="number" id="takeoffTimeout" class="action-input" placeholder="Timeout (seconds)" value="30" min="5" max="300" style="grid-column: 1 / -1;">
                            <div style="color: #ffeb3b; text-align: center; grid-column: 1 / -1; font-size: 11px;">
                                🚀 Vehicle will take off vertically to specified altitude<br>
                                ⏱️ Timeout: Max time to reach altitude before continuing
                            </div>
                        `;
                        break;
                    case 'rtl':
                        paramsDiv.innerHTML = `
                            <input type="number" id="rtlTimeout" class="action-input" placeholder="Timeout (seconds)" value="60" min="10" max="600" style="grid-column: 1 / -1;">
                            <div style="color: #ffeb3b; text-align: center; grid-column: 1 / -1;">
                                🏠 Vehicle will return to launch position<br>
                                ⏱️ Timeout: Max time to return home before continuing
                            </div>
                        `;
                        break;
                    case 'delay':
                        paramsDiv.innerHTML = `
                            <input type="number" id="delaySeconds" class="action-input" placeholder="Delay Duration (seconds)" value="5" min="1" max="300" style="grid-column: 1 / -1;">
                            <div style="color: #ffeb3b; text-align: center; grid-column: 1 / -1; font-size: 11px;">
                                ⏸️ Pause mission for specified duration
                            </div>
                        `;
                        break;
                    case 'loiter':
                        paramsDiv.innerHTML = `
                            <input type="number" id="loiterTurns" class="action-input" placeholder="Turns" value="1" min="1" max="500">
                            <input type="number" id="loiterRadius" class="action-input" placeholder="Radius (m)" value="50" min="10" max="200">
                            <input type="number" id="loiterDuration" class="action-input" placeholder="Duration (seconds)" value="10" min="5" max="600" style="grid-column: 1 / -1;">
                            <div style="color: #ffeb3b; text-align: center; grid-column: 1 / -1; font-size: 11px;">
                                🔄 Circle in place for specified duration
                            </div>
                        `;
                        break;
                    case 'set_speed':
                        paramsDiv.innerHTML = `
                            <input type="number" id="setSpeed" class="action-input" placeholder="Speed (m/s)" value="10" min="1" max="30" style="grid-column: 1 / -1;">
                            <div style="color: #ffeb3b; text-align: center; grid-column: 1 / -1; font-size: 11px;">
                                💨 Set vehicle speed (instant action, no timeout)
                            </div>
                        `;
                        break;
                case 'land':
                    paramsDiv.innerHTML = `
                        <div style="opacity:0.9; line-height:1.5; grid-column: 1 / -1;">
                            <b>Land</b>: Chuyển drone sang chế độ hạ cánh (LAND).
                            <div style="margin-top:10px;">
                                <label style="display:block; margin-bottom:6px;">Land Timeout (s) (optional):</label>
                                <input id="actionLandTimeout" type="number" value="0" min="0" step="1" class="action-input" style="width:100%;">
                                <div style="font-size:12px; color:#9aa0a6; margin-top:6px;">
                                    0 = không chờ. Nếu >0: hệ thống có thể chờ tối đa N giây để xác nhận hạ cánh.
                                </div>
                            </div>
                        </div>
                    `;
                    break;

                    default:
                        paramsDiv.innerHTML = `<div style="color: #888; text-align: center; grid-column: 1 / -1;">Select an action type</div>`;
                }
            }

            function saveWaypointAction() {
                if (selectedWaypointIndex === -1) return;
                const actionType = document.getElementById('actionType').value;
                if (!actionType) {
                    clearWaypointAction();
                    return;
                }
                let action = { type: actionType };
                switch(actionType) {
                    case 'takeoff':
                        action.altitude = parseInt(document.getElementById('takeoffAltitude').value) || 5;
                        action.timeout = parseInt(document.getElementById('takeoffTimeout').value) || 30;
                        break;
                    case 'rtl':
                        action.timeout = parseInt(document.getElementById('rtlTimeout').value) || 60;
                        break;
                    case 'delay':
                        action.seconds = parseInt(document.getElementById('delaySeconds').value) || 5;
                        break;
                    case 'loiter':
                        action.turns = parseInt(document.getElementById('loiterTurns').value) || 1;
                        action.radius = parseInt(document.getElementById('loiterRadius').value) || 50;
                        action.duration = parseInt(document.getElementById('loiterDuration').value) || 10;
                        break;
                    case 'set_speed':
                        action.speed = parseInt(document.getElementById('setSpeed').value) || 10;
                        break;
                    case 'land':
                        action.timeout = parseInt(document.getElementById('actionLandTimeout').value) || 0;
                        break;
                }
                waypoints[selectedWaypointIndex].action = action;
                updateWaypointList();
                
                // Better log message with timeout info
                let logMsg = `Action added to waypoint ${selectedWaypointIndex + 1}: ${actionType}`;
                if (action.altitude) logMsg += ` (Alt: ${action.altitude}m)`;
                if (action.timeout) logMsg += ` [Timeout: ${action.timeout}s]`;
                if (action.duration) logMsg += ` [Duration: ${action.duration}s]`;
                if (action.seconds) logMsg += ` [Duration: ${action.seconds}s]`;
                window.dashboard.addLog(logMsg, 'success');
            }

            function clearWaypointAction() {
                if (selectedWaypointIndex === -1) return;
                delete waypoints[selectedWaypointIndex].action;
                updateWaypointList();
                document.getElementById('actionType').value = '';
                window.dashboard.addLog(`Action removed from waypoint ${selectedWaypointIndex + 1}`, 'warning');
            }

            function updateWaypointAltitude() {
                if (selectedWaypointIndex === -1) return;
                const newAlt = parseFloat(document.getElementById('waypointAltitude').value);
                if (isNaN(newAlt) || newAlt < 0 || newAlt > 500) {
                    alert('Please enter a valid altitude (0-10m)');
                    return;
                }
                waypoints[selectedWaypointIndex].alt = newAlt;
                updateWaypointList();
                window.dashboard.mapManager.updateWaypointPath();
                window.dashboard.addLog(`Waypoint ${selectedWaypointIndex + 1} altitude updated to ${newAlt}m`, 'success');
            }

            function selectWaypointForAction(index) {
                selectedWaypointIndex = index;
                document.getElementById('actionPanel').style.display = 'block';
                document.getElementById('selectedWaypoint').textContent = index + 1;
                const wp = waypoints[index];
                
                // Populate waypoint altitude
                document.getElementById('waypointAltitude').value = wp.alt || 0;
                
                if (wp.action) {
                    document.getElementById('actionType').value = wp.action.type;
                    updateActionForm();
                    if (wp.action.altitude !== undefined) {
                        const altInput = document.getElementById('takeoffAltitude') || document.getElementById('changeAltitude');
                        if (altInput) altInput.value = wp.action.altitude;
                    }
                    if (wp.action.seconds !== undefined) {
                        const secondsInput = document.getElementById('delaySeconds');
                        if (secondsInput) secondsInput.value = wp.action.seconds;
                    }
                    if (wp.action.turns !== undefined) {
                        const turnsInput = document.getElementById('loiterTurns');
                        if (turnsInput) turnsInput.value = wp.action.turns;
                    }
                    if (wp.action.radius !== undefined) {
                        const radiusInput = document.getElementById('loiterRadius');
                        if (radiusInput) radiusInput.value = wp.action.radius;
                    }
                    if (wp.action.speed !== undefined) {
                        const speedInput = document.getElementById('setSpeed');
                        if (speedInput) speedInput.value = wp.action.speed;
                    }
                    if (wp.action.timeout !== undefined && wp.action.type === 'land') {
                        const timeoutInput = document.getElementById('actionLandTimeout');
                        if (timeoutInput) timeoutInput.value = wp.action.timeout;
                    }
                } else {
                    document.getElementById('actionType').value = '';
                    updateActionForm();
                }
            }

            function updateWaypointList() {
                const list = document.getElementById('waypointList');
                list.innerHTML = '';
                if (waypoints.length === 0) {
                    list.innerHTML = '<div style="text-align: center; color: #888; padding: 20px;">Click on map to set home/launch position (Waypoint 1). Then add more waypoints.</div>';
                    return;
                }
                waypoints.forEach((wp, index) => {
                    const item = document.createElement('div');
                    item.className = 'waypoint-item';
                    if (wp.action) {
                        item.classList.add('has-action');
                    }
                    if (index === 0) {
                        item.style.borderLeft = '4px solid #9c27b0';
                        item.innerHTML = `
                            <strong>WP ${index + 1} 🏠</strong> (Home/Launch)
                            ${wp.action ? `<span class="action-tag">${wp.action.type.toUpperCase()}</span>` : ''}
                            <br>
                            ${wp.lat.toFixed(6)}, ${wp.lng.toFixed(6)}<br>
                            Alt: ${wp.alt}m
                            ${wp.action ? `<br><small style="color: #ffeb3b;">Action: ${wp.action.type}${wp.action.altitude ? ` to ${wp.action.altitude}m` : ''}${wp.action.seconds ? ` for ${wp.action.seconds}s` : ''}</small>` : ''}
                        `;
                    } else {
                        item.innerHTML = `
                            <strong>WP ${index + 1}</strong>
                            ${wp.action ? `<span class="action-tag">${wp.action.type.toUpperCase()}</span>` : ''}
                            <br>
                            ${wp.lat.toFixed(6)}, ${wp.lng.toFixed(6)}<br>
                            Alt: ${wp.alt}m
                            ${wp.action ? `<br><small style="color: #ffeb3b;">Action: ${wp.action.type}${wp.action.altitude ? ` to ${wp.action.altitude}m` : ''}${wp.action.seconds ? ` for ${wp.action.seconds}s` : ''}</small>` : ''}
                        `;
                    }
                    item.onclick = () => {
                        if (window.dashboard && window.dashboard.mapManager && window.dashboard.mapManager.map) {
                            window.dashboard.mapManager.map.setView([wp.lat, wp.lng], 16);
                        }
                        selectWaypointForAction(index);
                    };
                    list.appendChild(item);
                });
            }

            function clearWaypoints() {
                if (window.dashboard && window.dashboard.mapManager) {
                    window.dashboard.mapManager.clearWaypoints();
                }
            }

            function removeWaypoint(index) {
                if (window.dashboard && window.dashboard.mapManager) {
                    window.dashboard.mapManager.removeWaypoint(index);
                }
            }

            async function saveMission() {
                const name = document.getElementById('missionName').value || 'Unnamed Mission';
                if (waypoints.length === 0) {
                    alert('Please set a home/launch position first by clicking on the map');
                    return;
                }
                if (waypoints.length < 1) {
                    alert('Mission must include at least the home/launch position (Waypoint 1)');
                    return;
                }
                try {
                    const response = await fetch('/api/missions', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name, waypoints })
                    });
                    const result = await response.json();
                    if (result.mission_id) {
                        window.dashboard.addLog(`Mission "${name}" saved with ${waypoints.length} waypoints`, 'success');
                        document.getElementById('missionName').value = '';
                        loadMissions();
                    }
                } catch (error) {
                    console.error('Error saving mission:', error);
                    window.dashboard.addLog('Error saving mission', 'error');
                }
            }

            async function loadMissions() {
                try {
                    const response = await fetch('/api/missions');
                    missions = await response.json();
                    updateMissionsList();
                } catch (error) {
                    console.error('Error loading missions:', error);
                }
            }

            function updateMissionsList() {
                const list = document.getElementById('savedMissionsList');
                list.innerHTML = '';
                if (missions.length === 0) {
                    list.innerHTML = '<div style="text-align: center; color: #888; padding: 20px;">No saved missions</div>';
                    return;
                }
                missions.forEach(mission => {
                    const item = document.createElement('div');
                    item.className = 'waypoint-item';
                    if (currentMission && currentMission.id === mission.id) {
                        item.classList.add('active');
                    }
                    item.innerHTML = `
                        <strong>${mission.name}</strong><br>
                        ${mission.waypoints.length} waypoints<br>
                        <small>Created: ${new Date(mission.created_at).toLocaleDateString()}</small>
                        <div style="margin-top: 8px;">
                            <button onclick="activateMission('${mission.id}')" style="background: #4CAF50; color: white; border: none; padding: 4px 12px; border-radius: 4px; font-size: 11px; margin-right: 5px; cursor: pointer;">Activate</button>
                            <button onclick="deleteMission('${mission.id}')" style="background: #f44336; color: white; border: none; padding: 4px 12px; border-radius: 4px; font-size: 11px; cursor: pointer;">Delete</button>
                        </div>
                    `;
                    list.appendChild(item);
                });
            }

            async function activateMission(missionId) {
                try {
                    const response = await fetch(`/api/missions/${missionId}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ action: 'activate' })
                    });
                    if (response.ok) {
                        window.dashboard.addLog(`Mission "${missionId}" activated`, 'success');
                    }
                } catch (error) {
                    console.error('Error activating mission:', error);
                    window.dashboard.addLog('Error activating mission', 'error');
                }
            }

            async function startMissionSequence() {
                if (!missions.length) {
                    alert('No missions available. Please create a mission first.');
                    return;
                }
                const missionId = missions[0].id;
                try {
                    const response = await fetch('/api/mission/start_sequence', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ mission_id: missionId })
                    });
                    if (response.ok) {
                        window.dashboard.addLog('Mission sequence started: GUIDED → ARM → START MISSION', 'success');
                    } else {
                        const error = await response.json();
                        window.dashboard.addLog(`Failed to start mission sequence: ${error.error}`, 'error');
                    }
                } catch (error) {
                    console.error('Error starting mission sequence:', error);
                    window.dashboard.addLog('Error starting mission sequence', 'error');
                }
            }

            async function resumeMissionAfterSmoke() {
                if (!confirm('Bạn đã kiểm tra và xác nhận an toàn để tiếp tục mission?')) {
                    return;
                }
                
                try {
                    const response = await fetch('/api/mission/resume_after_smoke', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    
                    if (response.ok) {
                        window.dashboard.addLog('✅ Tiếp tục lộ trình sau khi kiểm tra khói', 'success');
                        document.getElementById('smokePausePanel').style.display = 'none';
                    } else {
                        const error = await response.json();
                        window.dashboard.addLog(`Failed to resume mission: ${error.error}`, 'error');
                    }
                } catch (error) {
                    console.error('Error resuming mission:', error);
                    window.dashboard.addLog('Error resuming mission', 'error');
                }
            }

            async function checkSmokePauseStatus() {
                try {
                    const response = await fetch('/api/mission/smoke_pause_status');
                    if (response.ok) {
                        const status = await response.json();
                        const panel = document.getElementById('smokePausePanel');
                        
                        if (status.paused && status.mission_active) {
                            panel.style.display = 'block';
                        } else {
                            panel.style.display = 'none';
                        }
                    }
                } catch (error) {
                    // Ignore errors
                }
            }

            // Check smoke pause status every 2 seconds
            setInterval(checkSmokePauseStatus, 2000);

            async function deleteMission(missionId) {
                if (confirm('Are you sure you want to delete this mission?')) {
                    try {
                        const response = await fetch(`/api/missions/${missionId}`, { method: 'DELETE' });
                        if (response.ok) {
                            window.dashboard.addLog('Mission deleted', 'success');
                            loadMissions();
                        }
                    } catch (error) {
                        console.error('Error deleting mission:', error);
                        window.dashboard.addLog('Error deleting mission', 'error');
                    }
                }
            }

            async function clearCurrentMission() {
                try {
                    const response = await fetch('/api/mission/current', { method: 'DELETE' });
                    if (response.ok) {
                        currentMission = null;
                        document.getElementById('currentMissionInfo').style.display = 'none';
                        window.dashboard.addLog('Mission stopped', 'warning');
                    }
                } catch (error) {
                    console.error('Error clearing mission:', error);
                    window.dashboard.addLog('Error stopping mission', 'error');
                }
            }

            function sendCommand(command) {
                if (window.dashboard && window.dashboard.socket) {
                    window.dashboard.socket.emit('command', {type: command});
                    window.dashboard.addLog(`Sent command: ${command}`, 'info');
                } else {
                    alert('Not connected to server!');
                }
            }

            function forceArmDirect() {
                if (window.dashboard && window.dashboard.socket) {
                    window.dashboard.socket.emit('command', {type: 'force_arm'});
                    window.dashboard.addLog('Sent DIRECT FORCE ARM command', 'warning');
                }
            }

            function emergencyStop() {
                sendCommand('disarm');
                window.dashboard.addLog('EMERGENCY STOP - Vehicle disarmed', 'error');
            }

            class LeafletMapManager {
                constructor() {
                    this.map = null;
                    this.marker = null;
                    this.path = [];
                    this.pathPolyline = null;
                    this.mapInitialized = false;
                    this.waypointMarkers = [];
                    this.missionPath = null;
                    this.initializationInProgress = false;
                    this.fireMarker = null;
                }

                initMap() {
                    if (this.mapInitialized || this.initializationInProgress) {
                        console.log('Map already initialized or initialization in progress');
                        return;
                    }
                    this.initializationInProgress = true;
                    try {
                        const mapElement = document.getElementById('leafletMap');
                        if (!mapElement) {
                            throw new Error('Map container not found');
                        }
                        if (mapElement._leaflet_id) {
                            console.log('Map container already has a Leaflet instance');
                            this.mapInitialized = true;
                            this.initializationInProgress = false;
                            return;
                        }
                        const defaultCenter = [10.7949, 106.7369];
                        this.map = L.map('leafletMap').setView(defaultCenter, 16);
                        const satelliteLayer = L.tileLayer('https://{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', {
                            maxZoom: 20,
                            subdomains: ['mt0', 'mt1', 'mt2', 'mt3'],
                            attribution: '© Google'
                        });
                        satelliteLayer.addTo(this.map);
                        this.map.on('click', (e) => {
                            this.addWaypoint(e.latlng.lat, e.latlng.lng);
                        });
                        const droneIcon = L.divIcon({
                            className: 'drone-marker',
                            html: '<div style="background: linear-gradient(135deg, #00d4ff, #0099cc); width: 24px; height: 24px; border-radius: 50%; border: 3px solid white; box-shadow: 0 4px 15px rgba(0, 212, 255, 0.5);"></div>',
                            iconSize: [24, 24],
                            iconAnchor: [12, 12]
                        });
                        this.marker = L.marker(defaultCenter, { icon: droneIcon }).addTo(this.map);
                        this.marker.bindPopup("<strong>Drone Position</strong><br>Initializing...").openPopup();
                        this.pathPolyline = L.polyline(this.path, {
                            color: '#00d4ff',
                            weight: 4,
                            opacity: 0.7
                        }).addTo(this.map);
                        this.missionPath = L.polyline([], {
                            color: '#ffeb3b',
                            weight: 3,
                            opacity: 0.8,
                            dashArray: '5, 10'
                        }).addTo(this.map);
                        updateHomeMarker(defaultCenter[0], defaultCenter[1]);
                        this.mapInitialized = true;
                        this.initializationInProgress = false;
                        console.log('Map initialized successfully');
                        if (window.dashboard && window.dashboard.addLog) {
                            window.dashboard.addLog('Map initialized successfully', 'success');
                        }
                    } catch (error) {
                        this.initializationInProgress = false;
                        console.error('Map initialization failed:', error);
                        if (window.dashboard && window.dashboard.addLog) {
                            window.dashboard.addLog('Map initialization failed: ' + error.message, 'error');
                        }
                    }
                }

                async setHomePositionOnServer(lat, lng, alt) {
                    try {
                        const response = await fetch('/api/set_home_position', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ lat, lng, alt })
                        });
                        if (response.ok) {
                            console.log('Home position updated on server');
                        }
                    } catch (error) {
                        console.error('Error setting home position:', error);
                    }
                }

                addWaypoint(lat, lng, alt = null) {
                    if (!this.mapInitialized) {
                        console.error('Map not initialized');
                        return;
                    }
                    if (waypoints.length === 0) {
                        const homeLat = parseFloat(document.getElementById('homeLat').value) || this.map.getCenter().lat;
                        const homeLng = parseFloat(document.getElementById('homeLng').value) || this.map.getCenter().lng;
                        const homeAlt = parseFloat(document.getElementById('homeAlt').value) || 0;
                        lat = homeLat;
                        lng = homeLng;
                        alt = homeAlt;
                        document.getElementById('homeLat').value = homeLat.toFixed(6);
                        document.getElementById('homeLng').value = homeLng.toFixed(6);
                        document.getElementById('homeAlt').value = homeAlt;
                        this.setHomePositionOnServer(homeLat, homeLng, homeAlt);
                    } else if (alt === null) {
                        // Use previous waypoint's altitude if not specified
                        alt = waypoints[waypoints.length - 1].alt;
                    }
                    const waypoint = { lat, lng, alt, seq: waypoints.length };
                    waypoints.push(waypoint);
                    const isHome = waypoints.length === 1;
                    const marker = L.marker([lat, lng], {
                        icon: L.divIcon({
                            className: isHome ? 'home-marker' : 'waypoint-marker',
                            html: `<div style="background: ${isHome ? '#9c27b0' : '#ffeb3b'}; color: #000; width: 20px; height: 20px; border-radius: 50%; border: 2px solid white; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 10px;">${isHome ? '🏠' : waypoints.length}</div>`,
                            iconSize: [20, 20],
                            iconAnchor: [10, 10]
                        })
                    }).addTo(this.map);
                    marker.bindPopup(`
                        <div style="color: #000; padding: 5px;">
                            <strong>${isHome ? 'Waypoint 1 🏠 (Home/Launch)' : 'Waypoint ' + waypoints.length}</strong><br>
                            Lat: ${lat.toFixed(6)}<br>
                            Lng: ${lng.toFixed(6)}<br>
                            Alt: ${alt}m
                            <br><br>
                            <button onclick="selectWaypointForAction(${waypoints.length - 1})" style="background: #4CAF50; color: white; border: none; padding: 5px 10px; border-radius: 3px; cursor: pointer; margin-right: 5px;">Add Action</button>
                            ${!isHome ? `<button onclick="removeWaypoint(${waypoints.length - 1})" style="background: #f44336; color: white; border: none; padding: 5px 10px; border-radius: 3px; cursor: pointer;">Remove</button>` : '<span style="color: #9c27b0; font-size: 10px;">Home position cannot be removed</span>'}
                        </div>
                    `);
                    this.waypointMarkers.push(marker);
                    this.updateMissionPath();
                    updateWaypointList();
                    if (isHome) {
                        window.dashboard.addLog('🏠 Home/Launch position set as Waypoint 1. You can now add more waypoints.', 'success');
                    }
                }

                removeWaypoint(index) {
                    if (index === 0) {
                        alert('Cannot remove Waypoint 1 (Home/Launch position). Use "Clear Waypoints" to reset everything.');
                        return;
                    }
                    if (this.waypointMarkers[index]) {
                        this.map.removeLayer(this.waypointMarkers[index]);
                        this.waypointMarkers.splice(index, 1);
                    }
                    waypoints.splice(index, 1);
                    this.waypointMarkers.forEach((marker, idx) => {
                        const isHome = idx === 0;
                        marker.setIcon(L.divIcon({
                            className: isHome ? 'home-marker' : 'waypoint-marker',
                            html: `<div style="background: ${isHome ? '#9c27b0' : '#ffeb3b'}; color: #000; width: 20px; height: 20px; border-radius: 50%; border: 2px solid white; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 10px;">${isHome ? '🏠' : idx + 1}</div>`,
                            iconSize: [20, 20],
                            iconAnchor: [10, 10]
                        }));
                    });
                    this.updateMissionPath();
                    updateWaypointList();
                    document.getElementById('actionPanel').style.display = 'none';
                    selectedWaypointIndex = -1;
                }

                updateMissionPath() {
                    if (!this.mapInitialized) return;
                    const path = waypoints.map(wp => [wp.lat, wp.lng]);
                    this.missionPath.setLatLngs(path);
                }
                
                // 🔥 hiển thị vị trí lần cảnh báo gần nhất
                showFireLocation(lat, lng) {
                    if (!this.mapInitialized) return;
                    const pos = [lat, lng];
                    if (!this.fireMarker) {
                        this.fireMarker = L.circleMarker(pos, {
                            radius: 10,
                            color: '#ff1744',
                            fillColor: '#ff5252',
                            fillOpacity: 0.7
                        }).addTo(this.map);
                    } else {
                        this.fireMarker.setLatLng(pos);
                    }
                    this.fireMarker.bindPopup(`
                        <div style="color:#000; padding:5px;">
                            <strong>🔥 Last Fire Alert</strong><br>
                            Lat: ${lat.toFixed(6)}<br>
                            Lng: ${lng.toFixed(6)}
                        </div>
                    `);
                }
                
                clearWaypoints() {
                    if (!this.mapInitialized) return;
                    this.waypointMarkers.forEach(marker => {
                        this.map.removeLayer(marker);
                    });
                    this.waypointMarkers = [];
                    waypoints = [];
                    const center = this.map.getCenter();
                    document.getElementById('homeLat').value = center.lat.toFixed(6);
                    document.getElementById('homeLng').value = center.lng.toFixed(6);
                    document.getElementById('homeAlt').value = 0;
                    this.updateMissionPath();
                    updateWaypointList();
                    document.getElementById('actionPanel').style.display = 'none';
                    selectedWaypointIndex = -1;
                    window.dashboard.addLog('All waypoints cleared. Click on map to set new home/launch position.', 'warning');
                }

                updatePosition(lat, lng, heading = 0) {
                    if (!this.mapInitialized) return;
                    const newPosition = [lat, lng];
                    
                    // Smooth animation với duration ngắn hơn
                    this.marker.setLatLng(newPosition);
                    
                    const markerElement = this.marker. getElement();
                    if (markerElement) {
                        markerElement.style.transform = `rotate(${heading}deg)`;
                    }
                    
                    this.path.push(newPosition);
                    
                    // Giới hạn path length để không lag
                    if (this.path.length > 500) {
                        this.path = this.path.slice(-500);
                    }
                    
                    this.pathPolyline.setLatLngs(this.path);
                    
                    // Giảm animation duration
                    this.map.panTo(newPosition, { animate: true, duration: 0.3 });
                    
                    this.marker.setPopupContent(`
                        <strong>Drone Position</strong><br>
                        Lat: ${lat.toFixed(6)}<br>
                        Lng: ${lng.toFixed(6)}<br>
                        Heading: ${heading. toFixed(0)}°
                    `);
                }

                clearPath() {
                    if (!this.mapInitialized) return;
                    this.path = [];
                    this.pathPolyline.setLatLngs([]);
                }

                destroyMap() {
                    if (this.map) {
                        this.map.remove();
                        this.map = null;
                    }
                    this.mapInitialized = false;
                    this.initializationInProgress = false;
                }
            }

            class TelemetryCharts {
                constructor() {
                    this.historyLimit = 50;
                    this.altitudeHistory = [];
                    this.speedHistory = [];
                    this.batteryHistory = [];
                    this.timestamps = [];
                    this.initCharts();
                }

                initCharts() {
                    try {
                        this.altitudeChart = new Chart(
                            document.getElementById('altitudeChart').getContext('2d'),
                            this.createChartConfig('Altitude (m)', '#00d4ff')
                        );
                        this.speedChart = new Chart(
                            document.getElementById('speedChart').getContext('2d'),
                            this.createChartConfig('Speed (km/h)', '#ff6b6b')
                        );
                        this.batteryChart = new Chart(
                            document.getElementById('batteryChart').getContext('2d'),
                            this.createChartConfig('Battery (%)', '#4ecdc4')
                        );
                    } catch (error) {
                        console.error('Chart initialization failed:', error);
                    }
                }

                createChartConfig(label, color) {
                    return {
                        type: 'line',
                        data: {
                            labels: [],
                            datasets: [{
                                label: label,
                                data: [],
                                borderColor: color,
                                backgroundColor: color + '22',
                                tension: 0.4,
                                fill: true,
                                pointRadius: 0,
                                borderWidth: 2
                            }]
                        },
                        options: {
                            responsive: true,
                            maintainAspectRatio: false,
                            plugins: {
                                legend: { display: false },
                                tooltip: { mode: 'index', intersect: false }
                            },
                            scales: {
                                y: {
                                    beginAtZero: false,
                                    grid: { color: '#404040' },
                                    ticks: { 
                                        color: '#888', 
                                        maxTicksLimit: 5,
                                        callback: function(value) {
                                            return Number(value).toFixed(1);
                                        }
                                    }
                                },
                                x: {
                                    display: false,
                                    grid: { display: false }
                                }
                            },
                            interaction: { mode: 'nearest', axis: 'x', intersect: false },
                            animation: { duration: 0 }
                        }
                    };
                }

                updateCharts(telemetryData) {
                    const timestamp = new Date().toLocaleTimeString();
                    this.altitudeHistory.push(telemetryData.alt);
                    this.speedHistory.push((telemetryData.speed * 3.6).toFixed(1));
                    this.batteryHistory.push(telemetryData.battery);
                    this.timestamps.push(timestamp);
                    if (this.altitudeHistory.length > this.historyLimit) {
                        this.altitudeHistory.shift();
                        this.speedHistory.shift();
                        this.batteryHistory.shift();
                        this.timestamps.shift();
                    }
                    this.updateChart(this.altitudeChart, this.altitudeHistory);
                    this.updateChart(this.speedChart, this.speedHistory);
                    this.updateChart(this.batteryChart, this.batteryHistory);
                }

                updateChart(chart, data) {
                    if (data.length > 0 && chart) {
                        chart.data.labels = Array(data.length).fill('');
                        chart.data.datasets[0].data = data;
                        chart.update('none');
                    }
                }
            }

            class MAVLinkDashboard {
                constructor() {
                    this.socket = null;
                    this.isConnected = false;
                    this.reconnectAttempts = 0;
                    this.maxReconnectAttempts = 5;
                    this.charts = new TelemetryCharts();
                    this.mapManager = new LeafletMapManager();
                    this.sessionAlertLevel = 0; // 0=NO, 1=SMOKE, 2=FIRE (latch)
                    this.sessionLocked = false; // optional, khóa khi lên FIRE
                    this.totalFireAlerts = 0;
                    setTimeout(() => {
                        this.init();
                    }, 100);
                    window.dashboard = this;
                }

                resetFireUI() {
                    this.sessionAlertLevel = 0;
                    this.sessionLocked = false;

                    // reset UI status box
                    const statusEl = document.getElementById('fireStatus');
                    if (statusEl) {
                        statusEl.innerHTML = '✅ NO SMOKE / NO FIRE';
                        statusEl.style.color = '#4caf50';
                        statusEl.style.background = 'rgba(76, 175, 80, 0.2)';
                        statusEl.style.border = '2px solid #4caf50';
                    }

                    this.addLog('🔄 Fire/Smoke UI status reset. Ready for new session.', 'info');
                }


                init() {
                    this.connectWebSocket();
                    this.initMap();
                    this.startConnectionMonitor();
                    loadMissions();
                }

                initMap() {
                    requestAnimationFrame(() => {
                        try {
                            this.mapManager.initMap();
                        } catch (error) {
                            console.error('Map initialization error:', error);
                            setTimeout(() => {
                                this.initMap();
                            }, 2000);
                        }
                    });
                }

                connectWebSocket() {
                    try {
                        this.socket = io();
                        this.socket.on('connect', () => {
                            this.handleConnectionOpen();
                        });
                        this.socket.on('telemetry', (data) => {
                            this.handleTelemetryData(data);
                        });
                        this.socket.on('log', (data) => {
                            this.addLog(data.message, data.type);
                        });
                        
                        // 🔥 nhận dữ liệu từ Jetson (qua server laptop)
                        this.socket.on('fire_alert', (data) => {
                            this.handleFireAlert(data);
                        });

                        this.socket.on('disconnect', () => {
                            this.handleConnectionClose();
                        });
                        this.socket.on('connect_error', (error) => {
                            this.handleConnectionError(error);
                        });
                        
                        // 🔥 Smoke pause/resume events
                        this.socket.on('mission_paused_smoke', (data) => {
                            this.handleMissionPausedSmoke(data);
                        });
                        this.socket.on('mission_resumed', (data) => {
                            this.handleMissionResumed(data);
                        });
                    } catch (error) {
                        console.error('WebSocket connection failed:', error);
                        this.scheduleReconnect();
                    }
                }

                handleConnectionOpen() {
                    this.isConnected = true;
                    this.reconnectAttempts = 0;
                    this.updateConnectionStatus(true);
                    this.addLog('Connected to MAVLink server', 'success');
                }

                handleConnectionClose() {
                    this.isConnected = false;
                    this.updateConnectionStatus(false);
                    this.addLog('Disconnected from server', 'warning');
                    this.scheduleReconnect();
                }

                handleConnectionError(error) {
                    console.error('Socket error:', error);
                    this.addLog('Connection error: ' + error, 'error');
                    this.scheduleReconnect();
                }

                handleTelemetryData(data) {
                    try {
                        document.getElementById('position').textContent = 
                            data.lat.toFixed(6) + ', ' + data.lon.toFixed(6);
                        document.getElementById('altitude').textContent = 
                            data.alt.toFixed(1) + ' m';
                        const speedKmh = (data.speed * 3.6).toFixed(1);
                        document.getElementById('speed').textContent = speedKmh + ' km/h';
                        document.getElementById('heading').textContent = 
                            data.heading.toFixed(0) + '°';
                        document.getElementById('battery').textContent = 
                            data.battery + '%';
                        document.getElementById('systemStatus').textContent = data.status;
                        if (data.gps_satellites !== undefined) {
                            const gpsFixTypes = ['No GPS', 'No Fix', '2D Fix', '3D Fix', 'DGPS', 'RTK Float', 'RTK Fixed'];
                            const fixType = data.gps_fix_type < gpsFixTypes.length ? gpsFixTypes[data.gps_fix_type] : 'Unknown';
                            document.getElementById('gpsInfo').textContent = 
                                `GPS: ${fixType} (${data.gps_satellites} sats)`;
                        }
                        if (data.relative_alt !== undefined) {
                            document.getElementById('relativeAlt').textContent = 
                                `Relative: ${data.relative_alt.toFixed(1)} m`;
                        }
                        if (data.airspeed !== undefined) {
                            document.getElementById('airspeed').textContent = 
                                `Airspeed: ${data.airspeed.toFixed(1)} m/s`;
                        }
                        if (data.roll !== undefined && data.pitch !== undefined) {
                            document.getElementById('attitude').textContent = 
                                `Roll: ${data.roll.toFixed(1)}° Pitch: ${data.pitch.toFixed(1)}°`;
                        }
                        if (data.mode) {
                            document.getElementById('flightMode').textContent = 
                                `Mode: ${data.mode}`;
                        }
                        if (data.armed !== undefined) {
                            document.getElementById('armedStatus').textContent = 
                                `Armed: ${data.armed ? 'YES' : 'NO'}`;
                        }
                        this.mapManager.updatePosition(data.lat, data.lon, data.heading);
                        this.charts.updateCharts(data);
                        if (data.current_mission) {
                            const missionInfo = document.getElementById('currentMissionInfo');
                            if (missionInfo) {
                                missionInfo.style.display = 'block';
                                document.getElementById('currentWP').textContent = (data.current_waypoint || 0) + 1;
                                document.getElementById('totalWP').textContent = data.total_waypoints || 0;
                                let statusClass = 'status-active';
                                let statusText = 'Active';
                                if (data.mission_started) {
                                    statusText = 'Mission Started';
                                    statusClass = 'status-active';
                                } else if (data.armed) {
                                    statusText = 'Armed';
                                    statusClass = 'status-armed';
                                } else if (data.mode === 'GUIDED') {
                                    statusText = 'Guided Mode';
                                    statusClass = 'status-guided';
                                }
                                const statusElement = document.getElementById('missionStatus');
                                statusElement.textContent = statusText;
                                statusElement.className = `mission-status ${statusClass}`;
                                if (data.current_action) {
                                    document.getElementById('currentAction').textContent = 
                                        `Action: ${data.current_action.type}`;
                                } else {
                                    document.getElementById('currentAction').textContent = 'No action';
                                }
                            }
                        }
                        if (!realTelemetryMode && data.lat !== 10.794943646452133) {
                            realTelemetryMode = true;
                            this.updateConnectionStatus(true, true);
                            this.addLog('Real telemetry data received from Pixhawk!', 'success');
                        }
                    } catch (error) {
                        console.error('Error updating telemetry display:', error);
                    }
                }
                
                handleMissionPausedSmoke(data) {
                    try {
                        this.addLog('🔥 Mission tạm dừng! Khói phát hiện - Drone đang LOITER', 'warning');
                        
                        // Show smoke pause panel
                        const panel = document.getElementById('smokePausePanel');
                        if (panel) {
                            panel.style.display = 'block';
                        }
                        
                        // Show alert
                        if (data.location) {
                            this.addLog(
                                `📍 Vị trí phát hiện khói: ${data.location.lat.toFixed(6)}, ${data.location.lng.toFixed(6)}`,
                                'warning'
                            );
                        }
                    } catch (error) {
                        console.error('Error handling mission pause:', error);
                    }
                }

                handleMissionResumed(data) {
                    try {
                        this.addLog('✅ Mission tiếp tục! Drone chuyển về GUIDED mode...', 'success');
                        
                        // Hide smoke pause panel
                        const panel = document.getElementById('smokePausePanel');
                        if (panel) {
                            panel.style.display = 'none';
                        }
                    } catch (error) {
                        console.error('Error handling mission resume:', error);
                    }
                }
                
                handleFireAlert(data) {
                    try {
                        const statusEl = document.getElementById('fireStatus');
                        const historyEl = document.getElementById('alertHistoryList');
                        const totalCountEl = document.getElementById('alertTotalCount');
                        if (!statusEl) return;

                        // Level từ data: FIRE > SMOKE > NONE
                        const incomingLevel = data.has_fire ? 2 : (data.has_smoke ? 1 : 0);

                        // Nếu đã lock (ví dụ lên FIRE) thì không cập nhật nữa
                        if (this.sessionLocked) {
                            // vẫn có thể cập nhật history nếu bạn muốn (tùy bạn)
                            // nhưng status box thì không đổi
                        } else {
                            // Chỉ cho phép tăng level (0->1->2). Không giảm, không cập nhật lại.
                            if (incomingLevel > this.sessionAlertLevel) {
                                this.sessionAlertLevel = incomingLevel;

                                if (incomingLevel === 1) {
                                    // SMOKE DETECTED (lần cập nhật 1)
                                    statusEl.innerHTML = '💨 SMOKE DETECTED';
                                    statusEl.style.color = '#ffa726';
                                    statusEl.style.background = 'rgba(255, 167, 38, 0.18)';
                                    statusEl.style.border = '2px solid #ffa726';
                                    this.addLog(`💨 Smoke detected (conf=${(data.smoke_conf || 0).toFixed(2)})`, 'warning');
                                }

                                if (incomingLevel === 2) {
                                    // FIRE DETECTED (lần cập nhật 2 - khóa luôn)
                                    statusEl.innerHTML = '🔥 FIRE DETECTED';
                                    statusEl.style.color = '#ff5252';
                                    statusEl.style.background = 'rgba(255, 82, 82, 0.2)';
                                    statusEl.style.border = '2px solid #ff5252';
                                    this.sessionLocked = true;

                                    this.addLog(
                                        `🔥 Fire detected (conf=${(data.fire_conf || data.max_conf || 0).toFixed(2)})`,
                                        'error'
                                    );
                                }
                            }
                        }

                        // ====== PHẦN ALERT HISTORY: bạn có thể giữ như cũ ======
                        // Nếu bạn muốn history vẫn cập nhật liên tục thì giữ nguyên đoạn render history.
                        // Ví dụ:
                        if (totalCountEl && data.total_alerts !== undefined) {
                            totalCountEl.textContent = data.total_alerts;
                        }

                        if (historyEl && data.alert_history && data.alert_history.length > 0) {
                            const reversedHistory = [...data.alert_history].reverse();
                            historyEl.innerHTML = reversedHistory.map(alert => `
                                <div style="
                                    color:#fff; margin:6px 0; padding:8px 10px;
                                    background: rgba(255,255,255,0.06);
                                    border-left: 3px solid ${alert.type === 'FIRE' ? '#ff5252' : '#ffa726'};
                                    border-radius:4px;
                                ">
                                    <span style="color:#90caf9; font-weight:bold;">#${alert.id}</span>
                                    <span style="margin-left:10px;">${alert.type}</span>
                                    <span style="color:#90caf9; margin-left:10px;">${alert.time}</span>
                                    <span style="color:#ef5350; margin-left:10px;">Conf: ${alert.conf}</span>
                                    <span style="color:#a5d6a7; margin-left:10px;">📍 ${alert.lat}, ${alert.lon}</span>
                                    
                                </div>
                            `).join('');
                        }

                        // Map marker (nếu muốn chỉ update khi fire)
                        if (data.has_fire && data.lat && data.lon && this.mapManager) {
                            this.mapManager.showFireLocation(data.lat, data.lon);
                        }

                    } catch (e) {
                        console.error('Error handling fire_alert:', e);
                    }
                }
                
                updateConnectionStatus(isConnected, isRealTelemetry = false) {
                    const statusElement = document.getElementById('connectionStatus');
                    if (statusElement) {
                        if (isConnected) {
                            if (isRealTelemetry) {
                                statusElement.textContent = '📡 Connected to Real Pixhawk Telemetry';
                                statusElement.className = 'connection-status real-telemetry';
                            } else {
                                statusElement.textContent = '✅ Connected to MAVLink Server';
                                statusElement.className = 'connection-status connected';
                            }
                        } else {
                            statusElement.textContent = '❌ Disconnected from Server';
                            statusElement.className = 'connection-status disconnected';
                        }
                    }
                }

                addLog(message, type = 'info') {
                    const logContainer = document.getElementById('logContainer');
                    if (!logContainer) return;
                    const logEntry = document.createElement('div');
                    const timestamp = new Date().toLocaleTimeString();
                    logEntry.textContent = `[${timestamp}] ${message}`;
                    logEntry.style.color = 
                        type === 'error' ? '#ff4444' : 
                        type === 'warning' ? '#ffaa00' : 
                        type === 'success' ? '#44ff44' : '#ffffff';
                    logContainer.appendChild(logEntry);
                    logContainer.scrollTop = logContainer.scrollHeight;
                    if (logContainer.children.length > 50) {
                        logContainer.removeChild(logContainer.firstChild);
                    }
                }

                scheduleReconnect() {
                    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
                        this.addLog('Max reconnection attempts reached', 'error');
                        return;
                    }
                    const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000);
                    this.reconnectAttempts++;
                    this.addLog(`Reconnecting in ${delay/1000}s... (${this.reconnectAttempts}/${this.maxReconnectAttempts})`, 'info');
                    setTimeout(() => {
                        this.connectWebSocket();
                    }, delay);
                }

                startConnectionMonitor() {
                    setInterval(() => {
                        if (this.socket && !this.socket.connected && this.isConnected) {
                            this.handleConnectionClose();
                        }
                    }, 5000);
                }
            }

            document.addEventListener('DOMContentLoaded', () => {
                window.dashboard = new MAVLinkDashboard();

                fetch('/api/fire_state')
                    .then(r => r.json())
                    .then(s => {
                        if (window.dashboard && typeof window.dashboard.handleFireAlert === "function") {
                            window.dashboard.handleFireAlert(s);
                        }
                    })
                    .catch(() => {});

                // Load UI config (Jetson URLs) then set video + snapshots
                fetch('/api/ui_config')
                    .then(r => r.json())
                    .then(cfg => {
                        window.__JETSON_CFG__ = cfg;

                        const vid = document.getElementById('jetsonSmokeVideo');
                        if (vid && cfg.smoke_video_url) {
                            vid.src = cfg.smoke_video_url;
                        }

                        function refreshFireSnapshots() {
                            if (!cfg.fire_snap_urls || cfg.fire_snap_urls.length < 3) return;
                            const t = Date.now();
                            const ids = ['fireSnap0','fireSnap1','fireSnap2'];
                            for (let i = 0; i < 3; i++) {
                                const el = document.getElementById(ids[i]);
                                if (el) el.src = cfg.fire_snap_urls[i] + '?t=' + t;
                            }
                        }

                        // Refresh định kỳ (1s). Nếu muốn nhẹ hơn, tăng lên 2-3s.
                        setInterval(refreshFireSnapshots, 1000);
                        refreshFireSnapshots();

                        // Expose để các event khác gọi được
                        window.refreshFireSnapshots = refreshFireSnapshots;
                    })
                    .catch(err => console.error('Failed to load /api/ui_config', err));
            });
        </script>
    </body>
    </html>
    """



@app.route('/api/ui_config')
def ui_config():
    """UI config để JS lấy URL video smoke + URL 3 snapshot fire."""
    return jsonify({
        "smoke_video_url": JETSON_SMOKE_VIDEO_URL,
        "fire_snap_urls": [
            f"{JETSON_FIRE_SNAP_BASE}/snap_0.jpg",
            f"{JETSON_FIRE_SNAP_BASE}/snap_1.jpg",
            f"{JETSON_FIRE_SNAP_BASE}/snap_2.jpg",
        ]
    })

@app.route('/api/history')
def get_history():
    try:
        limit = request.args.get('limit', 100, type=int)
        historical_data = data_logger.get_recent_data(limit)
        return jsonify(historical_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/fire_state')
def get_fire_state():
    with fire_state_lock:
        return jsonify(fire_state)


@socketio.on('connect')
def handle_connect():
    clients.append(1)
    print(f"Client connected. Total clients: {len(clients)}")
    socketio.emit('log', {
        'message': 'Welcome to MAVLink Telemetry!',
        'type': 'success',
        'timestamp': datetime.now().isoformat()
    })


@socketio.on('disconnect')
def handle_disconnect():
    if clients:
        clients.pop()
    print(f"Client disconnected. Total clients: {len(clients)}")


@socketio.on('command')
def handle_command(data):
    command_type = data.get('type')
    print(f"Received command: {command_type}")

    if command_type == 'force_arm':
        if windows_telemetry.connected:
            if windows_telemetry.force_arm_direct():
                socketio.emit('log', {
                    'message': '✅ Direct force arm command sent!',
                    'type': 'success',
                    'timestamp': datetime.now().isoformat()
                })
            else:
                socketio.emit('log', {
                    'message': '❌ Direct force arm failed',
                    'type': 'error',
                    'timestamp': datetime.now().isoformat()
                })
        return

    if windows_telemetry.connected:
        if windows_telemetry.send_mavlink_command(command_type):
            socketio.emit('log', {
                'message': f'Sent MAVLink command: {command_type}',
                'type': 'success',
                'timestamp': datetime.now().isoformat()
            })
            return

    responses = {
        'takeoff': 'Takeoff sequence initiated',
        'rtl': 'Return to launch initiated',
        'loiter': 'Loiter mode activated - hovering at current position',
        'arm': 'Vehicle armed',
        'disarm': 'Vehicle disarmed',
        'emergency': 'EMERGENCY STOP activated',
        'start_mission': 'Mission execution started',
        'pause_mission': 'Mission paused',
        'resume_mission': 'Mission resumed'
    }

    response = responses.get(command_type, 'Unknown command')
    socketio.emit('log', {
        'message': f'Command: {response}',
        'type': 'success',
        'timestamp': datetime.now().isoformat()
    })

    if command_type == 'start_mission':
        mission_id = data.get('mission_id')
        if mission_id and mission_planner.set_current_mission(mission_id):
            vehicle_data.mode = "AUTO"
            socketio.emit('mission_update', {
                'mission_id': mission_id,
                'status': 'ACTIVE',
                'timestamp': datetime.now().isoformat()
            })


def follow_mission_waypoints():
    while True:
        try:
            with mission_planner._mission_lock:
                mission_active = (mission_planner.current_mission_id and
                                  mission_planner.mission_started and
                                  mission_planner.action_complete and
                                  not mission_planner.paused_by_smoke)  # Check smoke pause

            if not mission_active:
                # If paused by smoke, stay in LOITER
                if mission_planner.paused_by_smoke:
                    # Log periodically
                    if int(time.time()) % 10 == 0:  # Every 10 seconds
                        print("⏸️ Mission paused by smoke detection - waiting for resume command...")
                time.sleep(1)
                continue

            current_wp = mission_planner.get_current_waypoint()
            if not current_wp:
                time.sleep(1)
                continue

            distance = mission_planner.calculate_distance(
                vehicle_data.lat, vehicle_data.lon,
                current_wp['lat'], current_wp['lng']
            )

            vehicle_data.target_lat = current_wp['lat']
            vehicle_data.target_lon = current_wp['lng']
            vehicle_data.target_alt = current_wp.get('alt', 5)  # Default 5m (max 500m limit)

            if (windows_telemetry.connected and
                    distance > 15 and
                    not current_wp.get('action')):
                windows_telemetry.send_waypoint_command(
                    current_wp['lat'],
                    current_wp['lng'],
                    current_wp.get('alt', 5)  # Default 5m (max 500m limit)
                )

            if distance < 10:
                if 'action' in current_wp:
                    mission_planner.execute_waypoint_action(vehicle_data, socketio)
                else:
                    result = mission_planner.advance_waypoint()
                    if result == 'COMPLETED':
                        socketio.emit('mission_update', {
                            'mission_id': mission_planner.current_mission_id,
                            'status': 'COMPLETED',
                            'timestamp': datetime.now().isoformat()
                        })
                        socketio.emit('log', {
                            'message': '🎉 Mission completed!',
                            'type': 'success',
                            'timestamp': datetime.now().isoformat()
                        })
                        vehicle_data.mode = "MANUAL"
                        mission_planner.mission_started = False
                    elif result == 'ADVANCED':
                        socketio.emit('log', {
                            'message': f'➡️ Advanced to waypoint {mission_planner.missions[mission_planner.current_mission_id]["current_wp_index"] + 1}',
                            'type': 'info',
                            'timestamp': datetime.now().isoformat()
                        })

        except Exception as e:
            print(f"Error in mission waypoint following: {e}")
            socketio.emit('log', {
                'message': f'Mission execution error: {str(e)}',
                'type': 'error',
                'timestamp': datetime.now().isoformat()
            })

        time.sleep(2)

def _get_current_gps():
    """
    Lấy lat/lon hiện tại từ telemetry đang chạy trên laptop.
    Ưu tiên windows_telemetry.vehicle_data (real), fallback vehicle_data (global).
    """
    try:
        # windows_telemetry.vehicle_data luôn được cập nhật trong loop MAVLink
        vd = windows_telemetry.vehicle_data if windows_telemetry else None
        if vd and hasattr(vd, "lat") and hasattr(vd, "lon"):
            return float(vd.lat), float(vd.lon)
    except Exception:
        pass

    try:
        if vehicle_data and hasattr(vehicle_data, "lat") and hasattr(vehicle_data, "lon"):
            return float(vehicle_data.lat), float(vehicle_data.lon)
    except Exception:
        pass

    return None, None


def _push_alert(alert_type: str, conf: float, lat, lon, boxes=None):
    """
    alert_type: 'SMOKE' or 'FIRE'
    """
    now = datetime.now()
    item = {
        "id": fire_state["total_alerts"] + 1,
        "type": alert_type,
        "time": now.strftime("%H:%M:%S"),
        "iso": now.isoformat(),
        "conf": round(float(conf or 0.0), 3),
        "lat": None if lat is None else round(float(lat), 6),
        "lon": None if lon is None else round(float(lon), 6),
        "num_boxes": 0,
        "boxes": []
    }

    if isinstance(boxes, list):
        item["boxes"] = boxes
        item["num_boxes"] = len(boxes)

    # Update global state + history
    _alert_history.append(item)
    fire_state["total_alerts"] = item["id"]
    fire_state["alert_history"] = list(_alert_history)

    return item

# ==========================
# ALERT AGGREGATION (reduce spam)
# ==========================

def poll_jetson_fire_status():
    """
    Poll Jetson status and broadcast to web via Socket.IO.
    - Update fire_state continuously.
    - Group alerts in a time window (ALERT_WINDOW_SEC) and only push the highest conf in that window.
    - IMPORTANT: Only push alert when conf > 0.
    - Prevent "rising-edge lock" when has_fire/has_smoke is True but conf == 0 (wait until conf > 0).
    """
    global _last_smoke_flag, _last_fire_flag, _alert_window
    global _smoke_pause_consecutive, _last_smoke_pause_ts

    print(f"🔥 Jetson polling thread starting. Source: {JETSON_FIRE_STATUS_URL}")

    while True:
        try:
            resp = requests.get(JETSON_FIRE_STATUS_URL, timeout=1.0)
            if resp.status_code != 200:
                time.sleep(0.5)
                continue

            data = resp.json() if resp.content else {}
            print("JETSON raw:", data)  # DEBUG

            # Jetson flags
            has_smoke = bool(data.get("has_smoke", False))
            has_fire  = bool(data.get("has_fire", False))

            # Jetson conf keys (the ones you confirmed)
            # {"smoke_max_conf":..., "fire_max_conf":...}
            try:
                smoke_conf = float(data.get("smoke_max_conf", 0.0) or 0.0)
            except Exception:
                smoke_conf = 0.0

            try:
                fire_conf = float(data.get("fire_max_conf", 0.0) or 0.0)
            except Exception:
                fire_conf = 0.0

            lat, lon = _get_current_gps()
            now = time.time()

            # =====================================================
            # REAL-TIME: PAUSE MISSION ASAP ON SMOKE
            # =====================================================
            # This runs BEFORE alert-window push so the vehicle stops close to the smoke location.
            try:
                smoke_candidate = bool(has_smoke) and (not bool(has_fire)) and (float(smoke_conf or 0.0) > 0.0)
                if smoke_candidate and float(smoke_conf) >= float(SMOKE_PAUSE_MIN_CONF):
                    _smoke_pause_consecutive += 1
                else:
                    _smoke_pause_consecutive = 0

                can_trigger = (
                    mission_planner.mission_started
                    and (not mission_planner.paused_by_smoke)
                    and (_smoke_pause_consecutive >= int(SMOKE_PAUSE_CONSECUTIVE_POLLS))
                    and (now - float(_last_smoke_pause_ts or 0.0) >= float(SMOKE_PAUSE_COOLDOWN_SEC))
                )

                if can_trigger and (lat is not None) and (lon is not None):
                    current_alt = windows_telemetry.vehicle_data.alt if windows_telemetry.connected else 5
                    current_mode = windows_telemetry.vehicle_data.mode if windows_telemetry.connected else getattr(vehicle_data, 'mode', None)

                    if mission_planner.pause_mission_for_smoke(lat, lon, current_alt, socketio, current_mode=current_mode):
                        _last_smoke_pause_ts = now
                        _smoke_pause_consecutive = 0
                        print("⚡ REAL-TIME SMOKE PAUSE: switching to LOITER now")

                        if windows_telemetry.connected:
                            windows_telemetry.send_mavlink_command('set_mode', {'mode': 'LOITER'})
            except Exception as e:
                print(f"⚠️ realtime smoke pause error: {e}")

            # =========================
            # UPDATE FIRE STATE (ALWAYS)
            # =========================
            with fire_state_lock:
                fire_state.update({
                    "has_smoke": has_smoke,
                    "has_fire": has_fire,
                    "smoke_conf": smoke_conf,
                    "fire_conf": fire_conf,
                    "last_lat": lat,
                    "last_lon": lon,
                    "last_timestamp": datetime.now().isoformat()
                })

            # =========================================================
            # EVENT PICKING + "NO CONF => DO NOT LOCK RISING EDGE"
            # =========================================================
            # Decide current event type + its conf
            event_type = None
            conf = 0.0

            if has_fire:
                event_type = "FIRE"
                conf = fire_conf
            elif has_smoke:
                event_type = "SMOKE"
                conf = smoke_conf

            # Gate: only consider as a "real event" when conf > 0
            real_event = (event_type is not None and conf > 0.0)

            # Rising-edge logic but ONLY for real_event
            # - if has_fire True but conf==0: do NOT treat as event, keep _last_fire_flag False
            rising = False
            if real_event:
                if event_type == "FIRE":
                    rising = (not _last_fire_flag)
                else:  # "SMOKE"
                    rising = (not _last_smoke_flag)

            # =========================
            # ALERT WINDOW AGGREGATION
            # =========================
            if real_event:
                # Start a new window only on rising-edge OR when window is empty
                if _alert_window.get("start_ts") is None:
                    # open new window
                    _alert_window = {
                        "type": event_type,
                        "max_conf": conf,
                        "lat": lat,
                        "lon": lon,
                        "boxes": [],
                        "start_ts": now
                    }
                else:
                    # if same type: keep max conf
                    if event_type == _alert_window.get("type"):
                        if conf > float(_alert_window.get("max_conf", 0.0) or 0.0):
                            _alert_window["max_conf"] = conf
                            _alert_window["lat"] = lat
                            _alert_window["lon"] = lon
                    else:
                        # different type while window is running:
                        # Prefer FIRE over SMOKE (upgrade window to FIRE)
                        if event_type == "FIRE" and _alert_window.get("type") != "FIRE":
                            _alert_window["type"] = "FIRE"
                            _alert_window["max_conf"] = conf
                            _alert_window["lat"] = lat
                            _alert_window["lon"] = lon
                        # If window is FIRE and now SMOKE, ignore downgrade

            else:
                # If no real_event (conf==0 or no smoke/fire),
                # we can reset window early to avoid pushing "conf 0" alerts
                if _alert_window.get("start_ts") is not None and (not has_fire and not has_smoke):
                    _alert_window = {
                        "type": None,
                        "max_conf": 0.0,
                        "lat": None,
                        "lon": None,
                        "boxes": [],
                        "start_ts": None
                    }

            # =========================
            # END WINDOW => PUSH ALERT (ONLY IF max_conf > 0)
            # =========================
            if _alert_window.get("start_ts") and (now - _alert_window["start_ts"] >= ALERT_WINDOW_SEC):
                max_conf = float(_alert_window.get("max_conf", 0.0) or 0.0)

                if max_conf > 0.0:
                    alert_item = _push_alert(
                        _alert_window["type"],
                        max_conf,
                        _alert_window.get("lat"),
                        _alert_window.get("lon")
                    )

                    socketio.emit("fire_alert", {
                        **fire_state,
                        "alert_item": alert_item
                    })

                    print("✅ PUSH ALERT:", alert_item)
                    
                    # =====================================================
                    # 📱 TELEGRAM ALERT (GCS BACKUP với GPS)
                    # =====================================================
                    if telegram_gcs and telegram_gcs.enabled:
                        alert_type = _alert_window.get("type")
                        alert_lat = _alert_window.get("lat")
                        alert_lon = _alert_window.get("lon")
                        
                        # Lấy snapshot URL từ Jetson
                        snap_url = f"{JETSON_FIRE_SNAP_BASE}/snap_0.jpg"
                        
                        if alert_type == "FIRE":
                            telegram_gcs.send_fire_alert(
                                conf=max_conf,
                                lat=alert_lat,
                                lon=alert_lon,
                                snap_url=snap_url
                            )
                            print("📱 Telegram FIRE alert queued (GCS)")
                        elif alert_type == "SMOKE":
                            telegram_gcs.send_smoke_alert(
                                conf=max_conf,
                                lat=alert_lat,
                                lon=alert_lon,
                                snap_url=snap_url
                            )
                            print("📱 Telegram SMOKE alert queued (GCS)")

                    # NOTE: Legacy "alert-log-based" LOITER trigger removed.
                    # We still push alerts to the log here, but LOITER is commanded only
                    # by the real-time smoke pause logic earlier in this loop.
                else:
                    print("⏭️ SKIP PUSH ALERT (max_conf=0)")

                # reset window
                _alert_window = {
                    "type": None,
                    "max_conf": 0.0,
                    "lat": None,
                    "lon": None,
                    "boxes": [],
                    "start_ts": None
                }

            # =========================
            # UPDATE FLAGS (CRITICAL)
            # =========================
            # Do NOT lock rising-edge when conf==0.
            # Only set flags True when we have a real_event (conf>0).
            if has_fire and fire_conf > 0.0:
                _last_fire_flag = True
            elif not has_fire:
                _last_fire_flag = False
            # else: has_fire True but conf==0 => keep False (wait for conf>0)

            if has_smoke and (not has_fire) and smoke_conf > 0.0:
                _last_smoke_flag = True
            elif not has_smoke:
                _last_smoke_flag = False
            # else: has_smoke True but conf==0 => keep False

            # Always emit state so UI doesn't freeze
            socketio.emit("fire_alert", fire_state)

        except Exception as e:
            print("⚠️ poll error:", e)

        time.sleep(0.5)

def start_real_telemetry():
    if HAS_MAVLINK:
        print("🖥️  Windows telemetry mode activated")
        if windows_telemetry.connect_telemetry_radio():
            print("✅ MAVLink connection established")

            def run_telemetry():
                print("📡 MAVLink telemetry thread started")
                windows_telemetry.start_telemetry_loop()

            telemetry_thread = threading.Thread(
                target=run_telemetry,
                daemon=True,
                name="MAVLinkTelemetry"
            )
            telemetry_thread.start()
            print("🚀 Real telemetry is now running!")
            return
        else:
            print("❌ MAVLink connection failed")
    print("🎮 Starting simulation mode")


if __name__ == '__main__':
    print("Starting MAVLink Telemetry Server with Advanced Mission Planning...")
    print("Server will be available at: http://localhost:5000")
    if HAS_MAVLINK:
        print("✅ MAVLink support: ENABLED")
        print("💡 Make sure your telemetry radio is connected via USB")
    else:
        print("⚠️  MAVLink support: DISABLED (simulation mode)")
        print("   Install: pip install pymavlink pyserial")

    # =====================================================
    # 📱 KHỞI TẠO TELEGRAM GCS (BACKUP ALERTS)
    # =====================================================
    telegram_gcs = TelegramAlerterGCS(
        bot_token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID,
        enabled=TELEGRAM_ENABLED
    )
    
    if TELEGRAM_ENABLED:
        print("=" * 60)
        print("📱 TELEGRAM GCS ALERTS: ENABLED")
        print(f"   Token: {TELEGRAM_BOT_TOKEN[:15]}..." if len(TELEGRAM_BOT_TOKEN) > 15 else f"   Token: {TELEGRAM_BOT_TOKEN}")
        print(f"   Chat ID: {TELEGRAM_CHAT_ID}")
        print("=" * 60)
    else:
        print("⚠️ Telegram GCS alerts: DISABLED (set TELEGRAM_ENABLED=True)")

    try:
        # Thread đọc telemetry thật từ Pixhawk
        telemetry_thread = threading.Thread(target=start_real_telemetry, daemon=True)
        telemetry_thread.start()
        print("✓ Telemetry thread started")

        # Thread chạy mission waypoint
        mission_thread = threading.Thread(target=follow_mission_waypoints, daemon=True)
        mission_thread.start()
        print("✓ Mission thread started")

        # 🔥 Thread poll trạng thái fire từ Jetson
        jetson_fire_thread = threading.Thread(target=poll_jetson_fire_status, daemon=True)
        jetson_fire_thread.start()
        print("✓ Jetson fire polling thread started")

    except Exception as e:
        print(f"✗ Failed to start threads: {e}")

    try:
        socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
    except Exception as e:
        print(f"✗ Server failed to start: {e}")
