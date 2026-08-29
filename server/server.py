import json
import os
import socket
import tempfile
import threading
import time
from collections import deque

import cv2
from flask import Flask, jsonify
from flask_sock import Sock

app = Flask(__name__)
sock = Sock(app)

BATCH_DURATION_SEC = 0.5

lock = threading.Lock()
frame_queues = {}  # cam_id -> deque[bytes] (JPEG), frames awaiting playback
last_frames = {}  # cam_id -> bytes (JPEG), held between batches
stats = {}  # cam_id -> {"fps": float, "latency_ms": float}


def process_batch(cam_id, data, batch_start):
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
        return

    fps = len(decoded_frames) / BATCH_DURATION_SEC
    latency_ms = (time.time() - float(batch_start)) * 1000 if batch_start else None

    with lock:
        frame_queues.setdefault(cam_id, deque()).extend(decoded_frames)
        last_frames[cam_id] = decoded_frames[-1]
        stats[cam_id] = {
            "fps": round(fps, 1),
            "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        }


@sock.route("/ingest/<int:cam_id>")
def ingest_ws(ws, cam_id):
    while True:
        meta_raw = ws.receive()
        if meta_raw is None:
            break
        data = ws.receive()
        if data is None:
            break
        batch_start = json.loads(meta_raw).get("batch_start_time")
        process_batch(cam_id, data, batch_start)


def next_frame(cam_id):
    with lock:
        queue = frame_queues.get(cam_id)
        fps = (stats.get(cam_id) or {}).get("fps") or 10
        if queue:
            frame_bytes = queue.popleft()
            last_frames[cam_id] = frame_bytes
        else:
            frame_bytes = last_frames.get(cam_id)
    return frame_bytes, fps


@sock.route("/feed/<int:cam_id>")
def feed_ws(ws, cam_id):
    while True:
        frame_bytes, fps = next_frame(cam_id)
        if frame_bytes is None:
            time.sleep(0.1)
            continue
        ws.send(frame_bytes)
        time.sleep(1 / fps)


@app.route("/stats")
def get_stats():
    with lock:
        return jsonify(stats)


DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
  <title>Camera Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { box-sizing: border-box; }

    html, body {
      margin: 0;
      padding: 0;
      min-height: 100%;
      background: #08080b;
      color: #e8e6ea;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }

    body {
      position: relative;
      overflow-x: hidden;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      z-index: -1;
      background:
        radial-gradient(600px circle at 12% 8%, rgba(255, 23, 68, 0.16), transparent 60%),
        radial-gradient(700px circle at 90% 15%, rgba(255, 23, 68, 0.10), transparent 55%),
        radial-gradient(900px circle at 50% 100%, rgba(255, 23, 68, 0.08), transparent 60%),
        #08080b;
    }

    header {
      position: sticky;
      top: 0;
      z-index: 10;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 18px 28px;
      background: rgba(15, 12, 15, 0.55);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      border-bottom: 1px solid rgba(255, 23, 68, 0.22);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .pulse {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #ff1744;
      box-shadow: 0 0 0 0 rgba(255, 23, 68, 0.7);
      animation: pulse 1.6s infinite;
    }

    @keyframes pulse {
      0%   { box-shadow: 0 0 0 0 rgba(255, 23, 68, 0.55); }
      70%  { box-shadow: 0 0 0 10px rgba(255, 23, 68, 0); }
      100% { box-shadow: 0 0 0 0 rgba(255, 23, 68, 0); }
    }

    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: #f5f0f2;
    }

    h1 span {
      color: #ff1744;
    }

    #cam-count {
      font-size: 12px;
      color: #9a919a;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      padding: 6px 12px;
      border: 1px solid rgba(255, 23, 68, 0.25);
      border-radius: 999px;
      background: rgba(255, 23, 68, 0.06);
    }

    #cam-count b {
      color: #ff5c7a;
    }

    main {
      padding: 28px;
    }

    #grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
      gap: 20px;
    }

    #empty {
      color: #746c74;
      font-size: 14px;
      padding: 40px 0;
      text-align: center;
      letter-spacing: 0.03em;
    }

    .tile {
      position: relative;
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 23, 68, 0.22);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.04);
      overflow: hidden;
      transition: border-color 0.2s ease, box-shadow 0.2s ease, transform 0.2s ease;
    }

    .tile:hover {
      border-color: rgba(255, 23, 68, 0.5);
      box-shadow: 0 8px 30px rgba(255, 23, 68, 0.18), inset 0 1px 0 rgba(255, 255, 255, 0.06);
      transform: translateY(-2px);
    }

    .tile-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 14px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
      font-size: 13px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: #cfc6cf;
    }

    .tile-header .dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #ff1744;
      margin-right: 8px;
      display: inline-block;
      box-shadow: 0 0 6px rgba(255, 23, 68, 0.8);
    }

    .video-wrap {
      position: relative;
      background: #000;
      aspect-ratio: 4 / 3;
    }

    .video-wrap img {
      width: 100%;
      height: 100%;
      display: block;
      object-fit: cover;
    }

    .stat-chip {
      position: absolute;
      top: 10px;
      right: 10px;
      display: flex;
      gap: 10px;
      padding: 8px 12px;
      border-radius: 10px;
      background: rgba(10, 8, 10, 0.55);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      border: 1px solid rgba(255, 23, 68, 0.3);
    }

    .stat {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      line-height: 1.2;
    }

    .stat .label {
      font-size: 9px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #a99ea3;
    }

    .stat .value {
      font-family: "SF Mono", "Cascadia Code", Consolas, monospace;
      font-size: 14px;
      font-weight: 600;
      color: #ff4d6a;
    }

    .stat .value.warn {
      color: #ffb84d;
    }

    .stat .value.bad {
      color: #ff1744;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span class="pulse"></span>
      <h1>ROAR <span>/ Camera Feed</span></h1>
    </div>
    <div id="cam-count"><b id="cam-count-n">0</b> cameras online</div>
  </header>

  <main>
    <div id="grid"></div>
    <div id="empty">Waiting for camera feeds&hellip;</div>
  </main>

  <script>
    const grid = document.getElementById('grid');
    const emptyMsg = document.getElementById('empty');
    const camCountEl = document.getElementById('cam-count-n');
    const tiles = {};

    function connectFeed(camId, imgEl) {
      const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(scheme + '//' + location.host + '/feed/' + camId);
      ws.binaryType = 'blob';
      let lastUrl = null;
      ws.onmessage = (ev) => {
        const url = URL.createObjectURL(ev.data);
        imgEl.src = url;
        if (lastUrl) URL.revokeObjectURL(lastUrl);
        lastUrl = url;
      };
      ws.onclose = () => setTimeout(() => connectFeed(camId, imgEl), 1000);
    }

    function ensureTile(camId) {
      if (tiles[camId]) return tiles[camId];
      const tile = document.createElement('div');
      tile.className = 'tile';
      tile.innerHTML =
        '<div class="tile-header"><span><span class="dot"></span>Camera ' + camId + '</span></div>' +
        '<div class="video-wrap">' +
          '<img id="img-' + camId + '">' +
          '<div class="stat-chip">' +
            '<div class="stat"><span class="label">FPS</span><span class="value" id="fps-' + camId + '">--</span></div>' +
            '<div class="stat"><span class="label">Latency</span><span class="value" id="latency-' + camId + '">--</span></div>' +
          '</div>' +
        '</div>';
      grid.appendChild(tile);
      tiles[camId] = tile;
      connectFeed(camId, tile.querySelector('img'));
      return tile;
    }

    function latencyClass(ms) {
      if (ms === null || ms === undefined) return '';
      if (ms > 500) return 'bad';
      if (ms > 200) return 'warn';
      return '';
    }

    async function poll() {
      try {
        const res = await fetch('/stats');
        const data = await res.json();
        const camIds = Object.keys(data);

        emptyMsg.style.display = camIds.length ? 'none' : 'block';
        camCountEl.textContent = camIds.length;

        for (const camId of camIds) {
          ensureTile(camId);
          const s = data[camId];

          const fpsEl = document.getElementById('fps-' + camId);
          if (fpsEl) fpsEl.textContent = (s.fps ?? 0).toFixed(1);

          const latencyEl = document.getElementById('latency-' + camId);
          if (latencyEl) {
            latencyEl.textContent = (s.latency_ms === null || s.latency_ms === undefined)
              ? '--'
              : Math.round(s.latency_ms) + ' ms';
            latencyEl.className = 'value ' + latencyClass(s.latency_ms);
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


def lan_ip():
    # Doesn't actually send anything - UDP connect() just asks the OS to pick
    # the local interface/IP it would use to reach that address, which is
    # this machine's real LAN IP rather than the loopback address.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    ip = lan_ip() or "<this machine's LAN IP>"
    print("Starting camera server...")
    print(f"Dashboard: http://{ip}:5001/")
    print(f"Point edge_client.py at this server with: SERVER_URL=http://{ip}:5001")
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
