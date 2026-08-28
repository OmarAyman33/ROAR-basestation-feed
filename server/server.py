import os
import tempfile
import threading
import time
from collections import deque

import cv2
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

BATCH_DURATION_SEC = 0.5

lock = threading.Lock()
frame_queues = {}  # cam_id -> deque[bytes] (JPEG), frames awaiting playback
last_frames = {}  # cam_id -> bytes (JPEG), held between batches
stats = {}  # cam_id -> {"fps": float, "latency_ms": float}


@app.route("/ingest/<int:cam_id>", methods=["POST"])
def ingest(cam_id):
    data = request.get_data()
    batch_start = request.headers.get("X-Batch-Start-Time")

    tmp_path = tempfile.mktemp(suffix=".mp4")
    with open(tmp_path, "wb") as f:
        f.write(data)

    try:
        cap = cv2.VideoCapture(tmp_path)
        decoded_frames = []
        while True:
            success, frame = cap.read()
            if not success:
                break
            ok, buffer = cv2.imencode(".jpg", frame)
            if ok:
                decoded_frames.append(buffer.tobytes())
        cap.release()
    finally:
        os.remove(tmp_path)

    if not decoded_frames:
        return "", 204

    fps = len(decoded_frames) / BATCH_DURATION_SEC
    latency_ms = (time.time() - float(batch_start)) * 1000 if batch_start else None

    with lock:
        frame_queues.setdefault(cam_id, deque()).extend(decoded_frames)
        last_frames[cam_id] = decoded_frames[-1]
        stats[cam_id] = {
            "fps": round(fps, 1),
            "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        }

    return "", 204


def generate_frames(cam_id):
    while True:
        with lock:
            queue = frame_queues.get(cam_id)
            fps = (stats.get(cam_id) or {}).get("fps") or 10
            if queue:
                frame_bytes = queue.popleft()
                last_frames[cam_id] = frame_bytes
            else:
                frame_bytes = last_frames.get(cam_id)

        if frame_bytes is None:
            time.sleep(0.1)
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
        )

        time.sleep(1 / fps)


@app.route("/feed/<int:cam_id>")
def feed(cam_id):
    with lock:
        known = cam_id in last_frames
    if not known:
        return "No frames received yet for this camera", 404
    return Response(
        generate_frames(cam_id),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/stats")
def get_stats():
    with lock:
        return jsonify(stats)


DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
  <title>Camera Dashboard</title>
  <style>
    body { font-family: sans-serif; background: #111; color: #eee; }
    #grid { display: flex; flex-wrap: wrap; gap: 10px; }
    .tile { position: relative; }
    .tile img { width: 320px; display: block; border: 1px solid #333; }
    .overlay {
      position: absolute; top: 4px; left: 4px;
      background: rgba(0,0,0,0.6); padding: 2px 6px;
      font-size: 12px; border-radius: 4px;
    }
  </style>
</head>
<body>
  <h1>Camera Dashboard</h1>
  <div id="grid"></div>
  <script>
    const grid = document.getElementById('grid');
    const tiles = {};

    function ensureTile(camId) {
      if (tiles[camId]) return tiles[camId];
      const tile = document.createElement('div');
      tile.className = 'tile';
      tile.innerHTML =
        '<img src="/feed/' + camId + '">' +
        '<div class="overlay" id="overlay-' + camId + '"></div>';
      grid.appendChild(tile);
      tiles[camId] = tile;
      return tile;
    }

    async function poll() {
      try {
        const res = await fetch('/stats');
        const data = await res.json();
        for (const camId of Object.keys(data)) {
          ensureTile(camId);
          const s = data[camId];
          const overlay = document.getElementById('overlay-' + camId);
          if (overlay) {
            overlay.textContent = 'cam ' + camId + ' | ' + s.fps + ' fps | ' + s.latency_ms + ' ms';
          }
        }
      } catch (e) {
        console.error(e);
      }
    }

    poll();
    setInterval(poll, 1000);
  </script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return DASHBOARD_HTML


if __name__ == "__main__":
    print("Starting camera server on port 5001...")
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
