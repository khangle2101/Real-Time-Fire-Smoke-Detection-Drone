# 🔥 Drone Forest Fire Detection System

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Jetson%20Nano-green?style=for-the-badge&logo=nvidia" alt="Jetson Nano"/>
  <img src="https://img.shields.io/badge/Model-YOLOv11-blue?style=for-the-badge" alt="YOLOv11"/>
  <img src="https://img.shields.io/badge/Framework-TensorRT-red?style=for-the-badge&logo=nvidia" alt="TensorRT"/>
  <img src="https://img.shields.io/badge/Protocol-MAVLink-orange?style=for-the-badge" alt="MAVLink"/>
</p>

<p align="center">
  <b>Forest Fire and Smoke Detection System using UAV Drone + YOLOv11 + Jetson Nano</b>
</p>

<p align="center">
  <a href="https://www.youtube.com/watch?v=wso6gZVXSTA">
    <img src="https://img.shields.io/badge/▶️_Demo_Video-YouTube-FF0000?style=for-the-badge&logo=youtube" alt="Demo Video"/>
  </a>
</p>

<p align="center">
  <i>Ho Chi Minh City University of Technology and Engineering (HCMUTE)</i>
</p>

---

## 📋 Table of Contents

- [Introduction](#-introduction)
- [System Architecture](#-system-architecture)
- [Features](#-features)
- [Hardware Requirements](#-hardware-requirements)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [API Reference](#-api-reference)
- [Demo](#-demo)
- [Contributing](#-contributing)
- [License](#-license)

---

## 🎯 Introduction

This project develops an automated forest fire surveillance system using UAV drones, combining Artificial Intelligence (AI) with the **YOLOv11** model optimized by **TensorRT** to run on **NVIDIA Jetson Nano**. The system is capable of:

- 🔍 Real-time smoke and fire detection
- 📡 Video streaming via RTSP/MJPEG
- 📱 Instant alerts via Telegram
- 🗺️ Drone control through Web GCS (Ground Control Station)
- ✈️ Automatic mission pause (LOITER) when smoke is detected
- 🔄 Data fusion between AI detection and Pixhawk flight controller

---

## 🏗 System Architecture

### System Overview

<p align="center">
  <img src="SYSTEM OVERVIEW.jpg" alt="System Overview" width="1500"/>
</p>

The system consists of two main components:
- **Ground Control Station (GCS)**: Laptop running Web GCS with Telegram integration, connected to Ground Telemetry Radio via USB
- **UAV Drone**: Includes Vision System (Jetson Nano + Camera), Flight Control (Pixhawk + GPS), and Power System

Communication channels:
- **RF 433MHz**: MAVLink telemetry between GCS and Pixhawk
- **WiFi/4G**: Video stream and API from Jetson Nano to GCS

---

### Hardware Connection Diagram

<p align="center">
  <img src="HARDWARE_CONNECTION DIAGRAM.jpg" alt="Hardware Connection Diagram" width="1500"/>
</p>

**Key Components:**

| Subsystem | Components | Connection |
|-----------|------------|------------|
| **Vision Processing** | Pi Camera V2 (IMX219) → Jetson Nano | MIPI CSI-2 |
| **Flight Control** | GPS M10 → Pixhawk 6C → Air Telemetry | UART |
| **Power System** | 4S LiPo → PM02 → PDB | DC 14.8V |
| **Propulsion** | Pixhawk → ESC 40A x4 → Motors | PWM / 3-Phase |
| **Ground Station** | Laptop → Ground Telemetry Radio | USB |

---

### Software Architecture

<p align="center">
  <img src="SOFTWARE ARCHITECTURE.jpg" alt="Software Architecture" width="1500"/>
</p>

**Jetson Nano (Python 3.6.9):**
- `jetson_rtsp_server_v2.py`: CSI camera RTSP streaming with H.264 hardware encoding
- `jetson_yolo11_two_stage_mjpeg_server_v5_4_telegram.py`: Two-stage AI detection + MJPEG server

**Ground Station (Python 3.11):**
- `webgcs_loiter.py`: Web-based Ground Control Station with mission planning and Telegram alerts

---

### Cascaded AI Detection Workflow

<p align="center">
  <img src="CASCADED AI DETECTION WORKFLOW.jpg" alt="Cascaded AI Detection Workflow" width="1500"/>
</p>

**Two-Stage Detection Pipeline:**

| Stage | Model | Purpose | Trigger |
|-------|-------|---------|---------|
| **Stage 1** | `stage1_smoke.onnx` | Smoke Detection | Continuous (every frame) |
| **Stage 2** | `stage2_fire.onnx` | Fire Confirmation | Only when smoke detected |

This cascaded approach:
- ✅ Reduces false positives
- ✅ Saves computational resources
- ✅ Enables faster response for smoke (early warning)
- ✅ Confirms fire before critical alerts

---

### Auto Loiter Workflow

<p align="center">
  <img src="AUTO LOITER WORKFLOW.jpg" alt="Auto Loiter Workflow" width="1500"/>
</p>

When smoke is detected during an autonomous mission:
1. **Detection**: Jetson Nano detects smoke with confidence above threshold
2. **Alert**: System sends immediate Telegram alert with GPS location
3. **Pause**: GCS commands Pixhawk to enter LOITER mode
4. **Hold Position**: Drone hovers at current location for inspection
5. **Resume/RTL**: Operator can resume mission or trigger Return-To-Launch

---

### Data Fusion (AI + Pixhawk)

<p align="center">
  <img src="DATA FUSION (AI + PIXHAWK).jpg" alt="Data Fusion" width="1500"/>
</p>

The system fuses data from multiple sources:
- **AI Detection**: Smoke/Fire confidence, bounding boxes
- **Pixhawk Telemetry**: GPS position, altitude, attitude, battery status
- **Combined Output**: Geo-tagged alerts with precise location of detected smoke/fire

---

### Telegram Alert System

<p align="center">
  <img src="TELEGRAM ALERT.jpg" alt="Telegram Alert" width="1500"/>
</p>

**Alert Features:**
- 💨 **Smoke Alert**: Immediate notification when smoke detected
- 🔥 **Fire Alert**: Urgent notification when fire confirmed
- 📍 **GPS Location**: Exact coordinates with Google Maps link
- 📸 **Image Attachment**: Detection frame with bounding boxes
- ⏱️ **Rate Limiting**: Prevents alert spam (configurable cooldown)

---

## ✨ Features

### 🎥 RTSP Server (`jetson_rtsp_server_v2.py`)
- Stream CSI camera via RTSP H.264
- Customizable resolution (width, height)
- Adjustable FPS for performance optimization
- Hardware encoding with NVIDIA nvv4l2h264enc
- Low latency streaming

### 🤖 AI Detection (`jetson_yolo11_two_stage_mjpeg_server_v5_4_telegram.py`)
- **Stage 1 - Smoke Detection**: Continuous inference using `stage1_smoke.onnx`
- **Stage 2 - Fire Detection**: Triggered when smoke detected, using `stage2_fire.onnx`
- TensorRT FP16 inference for high-speed processing
- MJPEG streaming with Flask
- Automatic snapshot saving on fire detection
- NMS (Non-Maximum Suppression) for accurate results
- ROI cropping for efficient fire verification

### 📱 Telegram Alerts
- Instant smoke alerts with images
- Fire confirmation alerts
- Rate limiting to prevent spam
- GPS coordinates with Google Maps link
- Non-blocking background sending

### 🗺️ Web GCS (`webgcs_loiter.py`)
- Web-based drone control interface
- Mission Planning with waypoints
- Supported commands: Takeoff, Land, RTL, Loiter, Delay
- Real-time telemetry (GPS, attitude, battery, etc.)
- Auto-pause (LOITER) on smoke detection
- Resume mission after verification
- Interactive map integration
- MAVLink communication via USB telemetry radio

---

## 🔧 Hardware Requirements

### Jetson Nano (Onboard)
| Component | Specification |
|-----------|---------------|
| Board | NVIDIA Jetson Nano 4GB |
| Camera | Pi Camera V2 (IMX219) - MIPI CSI-2 |
| Storage | MicroSD 64GB+ (Class 10) |
| Power | DC 14.8V from PDB |

### Ground Control Station
| Component | Specification |
|-----------|---------------|
| Device | Laptop/PC |
| OS | Windows/Linux/macOS |
| Python | 3.11+ |
| Connection | USB to Ground Telemetry Radio |

### Flight Control System
| Component | Specification |
|-----------|---------------|
| Flight Controller | Pixhawk 6C |
| Firmware | ArduPilot (Copter 4.x) |
| GPS | GPS M10 (UART) |
| Telemetry | 433MHz Radio (Air + Ground pair) |

### Power System
| Component | Specification |
|-----------|---------------|
| Battery | 4S LiPo (14.8V) |
| Power Module | PM02 |
| Distribution | PDB (Power Distribution Board) |

### Propulsion System
| Component | Specification |
|-----------|---------------|
| ESC | 40A x4 (3-Phase) |
| Motors | Brushless x4 (M1, M2, M3, M4) |
| Frame | Quadcopter frame |

---

## 📦 Installation

### 1. Clone Repository

```bash
git clone https://github.com/YOUR_USERNAME/drone-fire-detection.git
cd drone-fire-detection
```

### 2. Jetson Nano Setup (Python 3.6.9)

```bash
# Install system dependencies
sudo apt-get update
sudo apt-get install -y \
    gstreamer1.0-rtsp \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    python3-gi \
    python3-gst-1.0

# Install Python packages
pip3 install flask opencv-python numpy requests

# Verify TensorRT (pre-installed with JetPack)
python3 -c "import tensorrt; print(tensorrt.__version__)"

# Install PyCUDA if needed
pip3 install pycuda
```

### 3. Ground Station Setup (Python 3.11)

```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# Linux/macOS:
source venv/bin/activate
# Windows:
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 4. Prepare Models

Place your ONNX models in the project directory:
- `stage1_smoke.onnx` - Smoke detection model (Stage 1)
- `stage2_fire.onnx` - Fire detection model (Stage 2)

Convert to TensorRT on Jetson Nano:

```bash
# On Jetson Nano - Convert ONNX to TensorRT engine
# Stage 1: Smoke model (input size: 416x416)
trtexec --onnx=stage1_smoke.onnx --saveEngine=stage1_smoke.engine --fp16

# Stage 2: Fire model (input size: 640x640)
trtexec --onnx=stage2_fire.onnx --saveEngine=stage2_fire.engine --fp16
```

---

## ⚙️ Configuration

### Telegram Bot Setup

1. Create a bot via [@BotFather](https://t.me/botfather)
2. Get the Bot Token
3. Get Chat ID (send a message to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`)

### Environment Variables

Create a `.env` file (do not commit to git):

```env
# Telegram Configuration
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# Jetson Configuration  
JETSON_IP=192.168.1.100

# Flask Configuration
FLASK_SECRET_KEY=your_random_secret_key
```

**⚠️ Important:** Add `.env` to your `.gitignore` file!

---

## 🚀 Usage

### Step 1: Start RTSP Server (Jetson Nano)

```bash
python3 jetson_rtsp_server_v2.py \
    --camera 0 \
    --port 8554 \
    --path /fire \
    --width 1280 \
    --height 720 \
    --fps 10
```

### Step 2: Start AI Detection Server (Jetson Nano)

```bash
python3 jetson_yolo11_two_stage_mjpeg_server_v5_4_telegram.py \
    --smoke-engine stage1_smoke.engine \
    --fire-engine stage2_fire.engine \
    --rtsp rtsp://127.0.0.1:8554/fire \
    --port 5002 \
    --telegram-token "YOUR_BOT_TOKEN" \
    --telegram-chat "YOUR_CHAT_ID" \
    --smoke-conf 0.30 \
    --fire-conf 0.50
```

### Step 3: Start Web GCS (Ground Station Laptop)

Connect the Ground Telemetry Radio to your laptop via USB, then:

```bash
python webgcs_loiter.py
```

### Step 4: Access Interfaces

| Service | URL | Description |
|---------|-----|-------------|
| Web GCS | `http://localhost:5000` | Drone control interface |
| MJPEG Stream | `http://<JETSON_IP>:5002/video_feed` | Live video with detection |
| Detection Status | `http://<JETSON_IP>:5002/api/status` | JSON status endpoint |
| Fire Snapshots | `http://<JETSON_IP>:5002/snaps/snap_0.jpg` | Captured fire images |

---

## 📡 API Reference

### Detection Server API (Jetson Nano)

#### GET `/api/status`
Returns current detection status.

```json
{
  "has_smoke": true,
  "has_fire": false,
  "smoke_max_conf": 0.85,
  "fire_max_conf": 0.0,
  "smoke_boxes": 2,
  "fire_boxes": 0,
  "smoke_consec": 5,
  "last_fire_check": "2025-01-22T10:30:00",
  "last_smoke_telegram": "2025-01-22T10:30:01",
  "timestamp": "2025-01-22T10:30:05"
}
```

#### GET `/video_feed`
MJPEG video stream with detection overlays.

#### GET `/snaps/snap_<n>.jpg`
Fire detection snapshots (n = 0, 1, 2).

### Web GCS API (Ground Station)

#### POST `/api/missions`
Create a new mission.

```json
{
  "name": "Patrol Mission 1",
  "waypoints": [
    {"lat": 10.762622, "lng": 106.660172, "alt": 50, "action": "takeoff"},
    {"lat": 10.763000, "lng": 106.661000, "alt": 50, "action": "waypoint"},
    {"lat": 10.764000, "lng": 106.662000, "alt": 50, "action": "loiter", "duration": 30},
    {"lat": 10.762622, "lng": 106.660172, "alt": 0, "action": "land"}
  ]
}
```

#### POST `/api/mission/start_sequence`
Start mission execution.

#### POST `/api/mission/resume_after_smoke`
Resume mission after smoke detection pause.

#### GET `/api/mission/smoke_pause_status`
Get current smoke pause status.

---

## 🎬 Demo

<p align="center">
  <a href="https://www.youtube.com/watch?v=wso6gZVXSTA">
    <img src="https://img.youtube.com/vi/wso6gZVXSTA/maxresdefault.jpg" alt="Demo Video" width="600"/>
  </a>
</p>

👆 **Click to watch the full demo video**

---

## 🛠 Troubleshooting

### Camera Not Working
```bash
# Test camera
nvgstcapture-1.0

# List devices
v4l2-ctl --list-devices

# Check CSI connection
dmesg | grep -i imx219
```

### TensorRT Errors
```bash
# Check CUDA version
nvcc --version

# Check TensorRT version
python3 -c "import tensorrt; print(tensorrt.__version__)"

# Check PyCUDA
python3 -c "import pycuda.autoinit; print('PyCUDA OK')"
```

### MAVLink Connection Issues
- Verify baudrate (typically 57600)
- Check COM port (Windows) or /dev/ttyUSB* (Linux)
- Ensure Pixhawk is connected to Air Telemetry Radio
- Verify Ground Telemetry Radio LED indicators

### Network/WiFi Issues
```bash
# Check Jetson IP
ifconfig wlan0

# Test connection from laptop
ping <JETSON_IP>

# Test RTSP stream
ffplay rtsp://<JETSON_IP>:8554/fire
```

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a new branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📄 License

This project is distributed under the MIT License. See `LICENSE` file for more information.

---

## 👥 Author

**Graduation Thesis Project - Le Hoang Khang (Leader), Nguyen Viet Khue**

Ho Chi Minh City University of Technology and Engineering (HCMUTE)
---

## 🙏 Acknowledgments

- [Ultralytics](https://github.com/ultralytics/ultralytics) - YOLOv11
- [NVIDIA](https://developer.nvidia.com/tensorrt) - TensorRT & Jetson Nano
- [ArduPilot](https://ardupilot.org/) - Flight Controller Firmware
- [Flask](https://flask.palletsprojects.com/) - Web Framework
- [MAVLink](https://mavlink.io/) - Communication Protocol

---

<p align="center">
  Made with ❤️ for Forest Fire Prevention
</p>
