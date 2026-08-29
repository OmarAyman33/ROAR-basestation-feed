import json
import os
import tempfile
import threading
import time

import cv2
import websocket

# --- Configuration ---
# cam_id 0 (ZED 2i) is excluded: at its native 4416x1242 resolution the
# Jetson's nvv4l2h264enc fails to create the hardware encoder (and hangs
# rather than failing fast) - needs separate investigation before it can be
# added back. The two Logitech C615 webcams work as expected.
#
# NOTE: these are /dev/videoN indices, which V4L2/udev can renumber when USB
# cameras are unplugged/replugged or the board reboots. If capture fails with
# "can't open camera by index", re-check with `v4l2-ctl --list-devices` and
# `v4l2-ctl --device=/dev/videoN --list-formats-ext` (the correct index is
# the one that actually lists a capture format, not its paired metadata node)
# and update this list.
CAMERA_IDS = [3, 6]
# Client and server run on different machines on the same LAN, so this can't
# be a fixed default - it must match wherever server.py is actually running
# right now (server.py prints its LAN address on startup).
SERVER_URL = os.environ.get("SERVER_URL")
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
# Cameras that don't support the forced FRAME_WIDTH/FRAME_HEIGHT (e.g. the
# ZED 2i stereo camera, which only has a few fixed discrete modes) capture
# at native resolution instead.
NATIVE_RESOLUTION_CAMERA_IDS = {0}
BATCH_DURATION_SEC = 0.5


def build_gst_pipeline(path, width, height, fps):
    # Hardware H.264 encode on Jetson/Tegra via GStreamer. nvv4l2h264enc only
    # accepts NVMM-memory input, so nvvidconv is required to move frames from
    # appsrc's regular system-memory buffers into NVMM before the encoder.
    return (
        f"appsrc ! video/x-raw,format=BGR,width={width},height={height},framerate={fps}/1 ! "
        f"videoconvert ! video/x-raw,format=I420 ! nvvidconv ! "
        f"video/x-raw(memory:NVMM),format=I420 ! nvv4l2h264enc ! h264parse ! "
        f"qtmux ! filesink location={path}"
    )


def encode_batch(frames, fps):
    height, width = frames[0].shape[:2]
    tmp_path = tempfile.mktemp(suffix=".mp4")

    # GStreamer caps require an integer framerate fraction (e.g. "30/1"),
    # while the measured batch fps (frame_count / BATCH_DURATION_SEC) is
    # rarely a whole number.
    enc_fps = max(1, round(fps))
    pipeline = build_gst_pipeline(tmp_path, width, height, enc_fps)
    writer = cv2.VideoWriter(pipeline, cv2.CAP_GSTREAMER, 0, enc_fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError("failed to open GStreamer H.264 encoder pipeline")

    try:
        for frame in frames:
            writer.write(frame)
    finally:
        writer.release()

    with open(tmp_path, "rb") as f:
        data = f.read()
    os.remove(tmp_path)
    return data


def capture_loop(cam_id):
    camera = cv2.VideoCapture(cam_id)
    if cam_id not in NATIVE_RESOLUTION_CAMERA_IDS:
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

    ws_url = SERVER_URL.replace("http://", "ws://", 1) + f"/ingest/{cam_id}"
    ws = None

    while True:
        batch = []
        batch_start_time = None
        deadline = None

        while True:
            success, frame = camera.read()
            if not success:
                print(f"[cam {cam_id}] read failed, stopping capture thread")
                if ws is not None:
                    ws.close()
                return

            now = time.time()
            if batch_start_time is None:
                batch_start_time = now
                deadline = now + BATCH_DURATION_SEC

            batch.append(frame)

            if time.time() >= deadline:
                break

        fps = len(batch) / BATCH_DURATION_SEC

        try:
            payload = encode_batch(batch, fps)
        except RuntimeError as e:
            print(f"[cam {cam_id}] encode failed: {e}")
            continue

        try:
            if ws is None:
                ws = websocket.create_connection(ws_url, timeout=5)
            ws.send(json.dumps({"batch_start_time": batch_start_time}))
            ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
        except (websocket.WebSocketException, OSError) as e:
            print(f"[cam {cam_id}] ws send failed: {e}")
            try:
                ws.close()
            except Exception:
                pass
            ws = None


def check_server_url():
    if not SERVER_URL:
        raise RuntimeError(
            "SERVER_URL is not set. Set it to the base station's address, "
            "e.g.:\n\n"
            "    SERVER_URL=http://<server-lan-ip>:5001 python3 edge_client.py\n\n"
            "Replace <server-lan-ip> with the address from server.py's own "
            "startup log on the base station (it prints the exact "
            "SERVER_URL=... to use) - don't reuse an IP from an old run or "
            "from this message, since it changes whenever the base station's "
            "network changes."
        )


def check_gstreamer_support():
    gst_lines = [l for l in cv2.getBuildInformation().splitlines() if "GStreamer" in l]
    if gst_lines and "YES" in gst_lines[0]:
        return
    raise RuntimeError(
        "This build of OpenCV was built without GStreamer support, so the "
        "hardware H.264 encoder pipeline cannot open. This commonly happens "
        "when a pip-installed opencv-python shadows the JetPack system "
        "OpenCV. Run this script with:\n\n"
        "    PYTHONPATH=/usr/lib/python3/dist-packages python3 edge_client.py\n\n"
        "and do not pip install opencv-python."
    )


def main():
    check_server_url()
    check_gstreamer_support()
    print(f"Starting edge client, pushing {CAMERA_IDS} to {SERVER_URL}")
    threads = [
        threading.Thread(target=capture_loop, args=(cam_id,), daemon=True)
        for cam_id in CAMERA_IDS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
