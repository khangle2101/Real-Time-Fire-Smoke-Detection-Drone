#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
JETSON NANO RTSP SERVER v2 (Custom FPS + Resolution)
---------------------------------------------------
Stream camera CSI qua RTSP H.264 với khả năng chỉnh:
  --width
  --height
  --fps

Dùng để giảm độ trễ khi YOLO trên Jetson không xử lý được FPS cao.

Ví dụ chạy:
    python3 jetson_rtsp_server_v2.py \
        --camera 0 \
        --port 8554 \
        --path /fire \
        --width 1280 \
        --height 720 \
        --fps 10
"""

from __future__ import print_function
import sys
import argparse

try:
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GstRtspServer', '1.0')
    from gi.repository import Gst, GstRtspServer, GObject
except Exception as e:
    print("ERROR: thiếu thư viện GStreamer RTSP:", e)
    print("Cài thêm:")
    print("  sudo apt-get install -y gstreamer1.0-rtsp gstreamer1.0-plugins-good gstreamer1.0-plugins-bad")
    sys.exit(1)


# ============================================================
#  RTSP Media Factory
# ============================================================
class FireRTSPMediaFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self, camera_id=0, width=1280, height=720, fps=10):
        super(FireRTSPMediaFactory, self).__init__()
        self.camera_id = camera_id
        self.width = width
        self.height = height
        self.fps = fps
        self.set_shared(True)

    def do_create_element(self, url):
        """
        nvarguscamerasrc → NV12 → H264 → RTP
        """
        print(f"📌 Tạo pipeline RTSP cho client (cam={self.camera_id}, {self.width}x{self.height}@{self.fps}fps)")

        pipeline = (
            "nvarguscamerasrc sensor-id={cam} ! "
            "video/x-raw(memory:NVMM), width={w}, height={h}, framerate={fps}/1 ! "
            "nvvidconv ! video/x-raw(memory:NVMM), format=NV12 ! "
            "nvv4l2h264enc bitrate=4000000 preset-level=1 insert-sps-pps=true iframeinterval=15 ! "
            "h264parse config-interval=-1 ! "
            "rtph264pay name=pay0 pt=96 config-interval=1"
        ).format(cam=self.camera_id, w=self.width, h=self.height, fps=self.fps)

        print("GStreamer pipeline:", pipeline)
        return Gst.parse_launch(pipeline)


# ============================================================
#  RTSP Server Wrapper
# ============================================================
class FireRTSPServer(object):
    def __init__(self, camera_id=0, port="8554", mount_point="/fire",
                 width=1280, height=720, fps=10):

        self.camera_id = camera_id
        self.port = port
        self.mount_point = mount_point
        self.width = width
        self.height = height
        self.fps = fps

        Gst.init(None)

        self.server = GstRtspServer.RTSPServer()
        self.server.props.service = self.port

        mounts = self.server.get_mount_points()
        factory = FireRTSPMediaFactory(camera_id=self.camera_id,
                                       width=self.width,
                                       height=self.height,
                                       fps=self.fps)
        mounts.add_factory(self.mount_point, factory)

        self.server.attach(None)

    def run(self):
        print("=" * 70)
        print("🔥  JETSON NANO RTSP SERVER v2 – CUSTOM FPS/RES")
        print("=" * 70)
        print("Camera ID :", self.camera_id)
        print("Port      :", self.port)
        print("Path      :", self.mount_point)
        print("Res       : {}x{}".format(self.width, self.height))
        print("FPS       :", self.fps)
        print("")
        print("➡️  RTSP URL:")
        print("    rtsp://<JETSON_IP>:%s%s" % (self.port, self.mount_point))
        print("")
        print("Địa chỉ Jetson:")
        print("    ifconfig wlan0")
        print("=" * 70)
        print("Ấn Ctrl+C để dừng")
        print("=" * 70)

        loop = GObject.MainLoop()
        try:
            loop.run()
        except KeyboardInterrupt:
            print("\n🛑 Dừng RTSP server…" )


# ============================================================
#  MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Jetson Nano RTSP server v2")
    parser.add_argument("--camera", type=int, default=0,
                        help="sensor-id của nvarguscamerasrc")
    parser.add_argument("--port", type=str, default="8554",
                        help="Cổng RTSP")
    parser.add_argument("--path", type=str, default="/fire",
                        help="Đường dẫn RTSP")
    parser.add_argument("--width", type=int, default=1280,
                        help="Chiều rộng video")
    parser.add_argument("--height", type=int, default=720,
                        help="Chiều cao video")
    parser.add_argument("--fps", type=int, default=10,
                        help="FPS camera")
    args = parser.parse_args()

    server = FireRTSPServer(camera_id=args.camera,
                            port=args.port,
                            mount_point=args.path,
                            width=args.width,
                            height=args.height,
                            fps=args.fps)
    server.run()


if __name__ == "__main__":
    main()
