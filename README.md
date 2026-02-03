# Real-Time Forest Fire & Smoke Detection System using UAV Drone

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Jetson%20Nano-green?style=for-the-badge&logo=nvidia" alt="Jetson Nano"/>
  <img src="https://img.shields.io/badge/Model-YOLOv11-blue?style=for-the-badge" alt="YOLOv11"/>
  <img src="https://img.shields.io/badge/Framework-TensorRT-red?style=for-the-badge&logo=nvidia" alt="TensorRT"/>
  <img src="https://img.shields.io/badge/Protocol-MAVLink-orange?style=for-the-badge" alt="MAVLink"/>
  <img src="https://img.shields.io/badge/Python-3.6%2B-yellow?style=for-the-badge&logo=python" alt="Python"/>
</p>

<p align="center">
  <b>An end-to-end AI-powered drone system for early forest fire detection and autonomous response</b>
</p>

<p align="center">
  <a href="https://www.youtube.com/watch?v=wso6gZVXSTA">
    <img src="https://img.shields.io/badge/Demo_Video-YouTube-FF0000?style=for-the-badge&logo=youtube" alt="Demo Video"/>
  </a>
</p>

<p align="center">
  <i>Ho Chi Minh City University of Technology and Engineering (HCMUTE) - Graduation Thesis 2025</i>
</p>

---

## Table of Contents

- [Problem Statement](#problem-statement)
- [Solution Overview](#solution-overview)
- [Key Technical Highlights](#key-technical-highlights)
- [System Architecture](#system-architecture)
- [Experimental Results](#experimental-results)
- [Hardware Components](#hardware-components)
- [Software Components](#software-components)
- [Installation & Usage](#installation--usage)
- [API Reference](#api-reference)
- [Demo Video](#demo-video)
- [Authors](#authors)
- [Acknowledgments](#acknowledgments)

---

## Problem Statement

Forest fires cause devastating environmental damage, loss of wildlife habitats, and threaten human communities. Traditional fire detection methods rely on:
- **Satellite imaging**: Low temporal resolution (hours delay)
- **Fixed cameras/sensors**: Limited coverage area
- **Human patrols**: Expensive, dangerous, and inefficient

**Challenge**: How can we detect forest fires **in real-time** with **precise geolocation** and **immediate alerts** to enable rapid response?

---

## Solution Overview

We developed an **autonomous UAV-based fire detection system** that combines:

1. **Edge AI Processing**: YOLOv11 models optimized with TensorRT FP16, running directly on NVIDIA Jetson Nano onboard the drone
2. **Two-Stage Cascaded Detection**: Smoke detection (early warning) followed by fire confirmation (reduces false positives)
3. **Real-Time Data Fusion**: AI detection results + GPS coordinates + drone telemetry = geo-tagged alerts
4. **Autonomous Response**: Automatic mission pause (LOITER mode) when smoke detected, allowing operator to assess the situation
5. **Multi-Channel Alerts**: Instant Telegram notifications with images and Google Maps location links

<p align="center">
  <img src="docs/images/SYSTEM OVERVIEW.jpg" alt="System Overview" width="100%"/>
</p>

---

## Key Technical Highlights

### 1. Edge AI Optimization (TensorRT FP16)
- Converted YOLOv11 models from ONNX to TensorRT engines with FP16 precision
- Achieved **10+ FPS** inference on Jetson Nano 4GB (power-constrained edge device)
- Custom TensorRT inference wrapper with CUDA stream management for asynchronous processing

### 2. Two-Stage Cascaded Detection Pipeline
```
Frame Input -> [Stage 1: Smoke Model] -> Smoke Detected?
                                              |
                                   YES        |        NO
                                    v         v
                        [Stage 2: Fire Model]   Continue monitoring
                                    |
                              Fire Confirmed?
                                    |
                          YES       |       NO
                           v        v
                    FIRE ALERT   SMOKE WARNING
```
- **Stage 1 (Smoke)**: Continuous inference at full FPS, lightweight model (416x416 input)
- **Stage 2 (Fire)**: Triggered only when smoke detected, higher precision model (640x640 input)
- **Benefits**: Reduces computational load, minimizes false positives, enables early warning

### 3. Real-Time MAVLink Integration
- Direct communication with Pixhawk 6C flight controller via pymavlink
- Autonomous mode switching (GUIDED -> LOITER -> GUIDED) based on AI detection
- Mission planning with waypoint actions (Takeoff, Land, RTL, Loiter, Delay)
- Real-time telemetry streaming (GPS, altitude, attitude, battery, speed)

### 4. Non-Blocking Telegram Alert System
- Background worker thread for sending alerts without blocking inference
- Rate limiting to prevent alert spam (configurable cooldown)
- Image attachment with bounding box overlays and detection confidence
- GPS coordinates with clickable Google Maps links

### 5. Multi-Process Architecture
- **Main Process**: Smoke detection + RTSP/MJPEG streaming
- **Fire Worker Process**: Separate CUDA context for fire confirmation (avoids GPU memory conflicts)
- **Flask Server Thread**: Web interface and API endpoints
- **Telegram Worker Thread**: Non-blocking alert delivery

---

## System Architecture

### Hardware Connection Diagram

<p align="center">
  <img src="docs/images/HARDWARE_CONNECTION DIAGRAM.jpg" alt="Hardware Connection Diagram" width="100%"/>
</p>

| Subsystem | Components | Connection |
|-----------|------------|------------|
| **Vision** | Pi Camera V2 (IMX219) -> Jetson Nano | MIPI CSI-2 |
| **Flight Control** | GPS M10 -> Pixhawk 6C -> Air Telemetry | UART |
| **Power** | 4S LiPo -> PM02 -> PDB | DC 14.8V |
| **Propulsion** | Pixhawk -> ESC 40A x4 -> Motors | PWM |
| **Ground Station** | Laptop -> Ground Telemetry Radio | USB |

### Software Architecture

<p align="center">
  <img src="docs/images/SOFTWARE ARCHITECTURE.jpg" alt="Software Architecture" width="100%"/>
</p>

**Jetson Nano (Onboard - Python 3.6.9):**
- `jetson_rtsp_server_v2.py`: GStreamer-based RTSP server with H.264 hardware encoding
- `jetson_yolo11_two_stage_mjpeg_server_v5_4_telegram.py`: Two-stage AI detection pipeline

**Ground Station (Windows/Linux - Python 3.11):**
- `webgcs_loiter.py`: Flask + SocketIO web-based Ground Control Station with mission planning

### Cascaded AI Detection Workflow

<p align="center">
  <img src="docs/images/CASCADED AI DETECTION WORKFLOW.jpg" alt="Cascaded AI Detection Workflow" width="100%"/>
</p>

### Auto Loiter Workflow

<p align="center">
  <img src="docs/images/AUTO LOITER WORKFLOW.jpg" alt="Auto Loiter Workflow" width="100%"/>
</p>

When smoke is detected during an autonomous mission:
1. **Detection**: Jetson Nano detects smoke with confidence above threshold
2. **Alert**: System sends immediate Telegram alert with GPS location
3. **Pause**: GCS commands Pixhawk to enter LOITER mode via MAVLink
4. **Hold Position**: Drone hovers at current location for operator inspection
5. **Resume/RTL**: Operator can resume mission or trigger Return-To-Launch

### Data Fusion (AI + Pixhawk)

<p align="center">
  <img src="docs/images/DATA FUSION (AI + PIXHAWK).jpg" alt="Data Fusion" width="100%"/>
</p>

---

## Experimental Results

### Final Hardware Product

<p align="center">
  <img src="docs/images/FINAL HARDWARE PRODUCT.jpg" alt="Final Hardware Product" width="50%"/>
</p>

The complete drone system with all components integrated and ready for field testing.

### Smoke Detection Alert (Telegram)

<p align="center">
  <img src="docs/images/SMOKE ALERTS VIA TELEGRAM.jpg" alt="Smoke Alerts via Telegram" width="50%"/>
</p>

When smoke is detected, the system immediately sends a Telegram alert with:
- Detection confidence percentage
- Bounding box visualization
- Timestamp
- GPS coordinates (when available)

### Smoke Detection (Ground Control Station)

<p align="center">
  <img src="docs/images/SMOKE ALERTS VIA THE GROUND CONTROL STATION WEBSITE.jpg" alt="Smoke Alerts via GCS" width="100%"/>
</p>

The Web GCS displays real-time detection status with live video feed and telemetry data.

### Fire Confirmation Alert (Telegram)

<p align="center">
  <img src="docs/images/FIRE CONFIRMATION VIA TELEGRAM.jpg" alt="Fire Confirmation via Telegram" width="50%"/>
</p>

When fire is confirmed (Stage 2), an urgent alert is sent with higher priority notification.

### Fire Confirmation (Ground Control Station)

<p align="center">
  <img src="docs/images/FIRE CONFIRMATION VIA THE GROUND CONTROL STATION WEBSITE.jpg" alt="Fire Confirmation via GCS" width="100%"/>
</p>

The Web GCS shows fire confirmation with captured snapshots and detection history.

### Telegram Alert System Flow

<p align="center">
  <img src="docs/images/TELEGRAM ALERT.jpg" alt="Telegram Alert System" width="100%"/>
</p>

**Alert Features:**
- Immediate notification when smoke/fire detected
- GPS coordinates with Google Maps link
- Detection frame with bounding boxes
- Rate limiting to prevent alert spam

---

## Hardware Components

| Component | Specification | Purpose |
|-----------|---------------|---------|
| **NVIDIA Jetson Nano** | 4GB RAM, Maxwell GPU (128 CUDA cores) | Edge AI inference |
| **Pi Camera V2** | IMX219, 8MP, MIPI CSI-2 | Video capture |
| **Pixhawk 6C** | STM32H7, ArduPilot Copter 4.x | Flight control |
| **GPS M10** | u-blox M10, 10Hz update | Position tracking |
| **433MHz Telemetry** | Air + Ground pair | MAVLink communication |
| **4S LiPo Battery** | 14.8V, 5000mAh | Power supply |
| **ESC 40A x4** | 3-Phase brushless | Motor control |

---

## Software Components

### Dependencies

**Jetson Nano:**
- Python 3.6.9
- TensorRT 8.x (pre-installed with JetPack)
- PyCUDA
- OpenCV 4.x
- Flask
- GStreamer (RTSP server)

**Ground Station:**
- Python 3.11+
- Flask + Flask-SocketIO
- pymavlink
- requests

### File Structure

```
.
├── jetson_rtsp_server_v2.py          # RTSP H.264 streaming server
├── jetson_yolo11_two_stage_mjpeg_server_v5_4_telegram.py  # AI detection pipeline
├── webgcs_loiter.py                  # Web Ground Control Station
├── stage1_smoke.onnx                 # Smoke detection model (ONNX)
├── stage2_fire.onnx                  # Fire detection model (ONNX)
├── requirements.txt                  # Python dependencies
└── docs/images/                      # Architecture diagrams
```

---

## Installation & Usage

### 1. Clone Repository

```bash
git clone https://github.com/khangle2101/Real-Time-Fire-Smoke-Detection-Drone.git
cd Real-Time-Fire-Smoke-Detection-Drone
```

### 2. Jetson Nano Setup

```bash
# Install system dependencies
sudo apt-get update
sudo apt-get install -y gstreamer1.0-rtsp gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad python3-gi python3-gst-1.0

# Install Python packages
pip3 install flask opencv-python numpy requests

# Convert models to TensorRT (on Jetson Nano)
trtexec --onnx=stage1_smoke.onnx --saveEngine=stage1_smoke.engine --fp16
trtexec --onnx=stage2_fire.onnx --saveEngine=stage2_fire.engine --fp16
```

### 3. Ground Station Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt
```

### 4. Run the System

**On Jetson Nano:**
```bash
# Terminal 1: Start RTSP Server
python3 jetson_rtsp_server_v2.py --width 1280 --height 720 --fps 10

# Terminal 2: Start AI Detection Server
python3 jetson_yolo11_two_stage_mjpeg_server_v5_4_telegram.py \
    --smoke-engine stage1_smoke.engine \
    --fire-engine stage2_fire.engine \
    --rtsp rtsp://127.0.0.1:8554/fire \
    --telegram-token "YOUR_BOT_TOKEN" \
    --telegram-chat "YOUR_CHAT_ID"
```

**On Ground Station:**
```bash
python webgcs_loiter.py
```

### 5. Access Web Interfaces

| Service | URL | Description |
|---------|-----|-------------|
| Web GCS | `http://localhost:5000` | Drone control & mission planning |
| MJPEG Stream | `http://<JETSON_IP>:5002/video_feed` | Live detection video |
| Detection API | `http://<JETSON_IP>:5002/api/status` | JSON status endpoint |

---

## API Reference

### Detection Server (Jetson Nano)

#### GET `/api/status`
Returns current detection status with smoke/fire confidence, bounding box count, and timestamps.

#### GET `/video_feed`
MJPEG video stream with real-time detection overlays.

#### GET `/snaps/snap_<n>.jpg`
Fire detection snapshots (n = 0, 1, 2) with bounding box annotations.

### Web GCS (Ground Station)

#### POST `/api/missions`
Create a new mission with waypoints and actions.

#### POST `/api/mission/start_sequence`
Start mission execution with automatic waypoint navigation.

#### POST `/api/mission/resume_after_smoke`
Resume mission after smoke detection pause.

#### GET `/api/mission/smoke_pause_status`
Get current smoke pause status and location.

---

## Demo Video

<p align="center">
  <a href="https://www.youtube.com/watch?v=wso6gZVXSTA">
    <img src="https://img.youtube.com/vi/wso6gZVXSTA/maxresdefault.jpg" alt="Demo Video" width="600"/>
  </a>
</p>

<p align="center">
  <b>Click to watch the full demonstration video</b>
</p>

---

## Authors

**Graduation Thesis Project - HCMUTE 2025**

| Name | Role | Contribution |
|------|------|--------------|
| **Le Hoang Khang** | Team Leader | System architecture, AI pipeline, Edge optimization, Web GCS, Hardware integration, Drone assembly, Flight testing |
| **Nguyen Viet Khue** | Member | Flight testing, Web GCS |


---

## Acknowledgments

- [Ultralytics](https://github.com/ultralytics/ultralytics) - YOLOv11 object detection
- [NVIDIA](https://developer.nvidia.com/tensorrt) - TensorRT & Jetson Nano platform
- [ArduPilot](https://ardupilot.org/) - Open-source autopilot firmware
- [pymavlink](https://github.com/ArduPilot/pymavlink) - MAVLink Python library
- [Flask](https://flask.palletsprojects.com/) - Python web framework

---

## License

This project is distributed under the MIT License. See `LICENSE` file for more information.

---

<p align="center">
  <b>Built for forest fire prevention and environmental protection</b>
</p>
