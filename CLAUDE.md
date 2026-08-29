# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Small set of standalone Python/Flask scripts for streaming camera feeds off a Jetson edge device (Xavier). No shared build system, package, or test suite — each script is run directly.

- `multi_camera.py` — original, single-process prototype. Opens local cameras directly (`cv2.VideoCapture(cam_id)`), JPEG-encodes each frame, and serves them itself via `/feed/<cam_id>` (MJPEG). Runs on one machine only, no network hop.
- `client/edge_client.py` + `server/server.py` — the two-machine split of the same idea: `client` runs on the Jetson and captures/compresses/pushes; `server` runs elsewhere and receives/serves/displays. This is the actively developed path; `multi_camera.py` is left untouched as a reference/fallback.

## Running

No installed package — run scripts directly from the repo root or their subdirectory.

```bash
# server (any machine, does not need GStreamer)
pip install -r server/requirements.txt
python3 server/server.py          # dashboard at http://<host>:5001/, prints its LAN IP on startup

# client (must run on the Jetson, needs the system OpenCV build)
pip install -r client/requirements.txt
SERVER_URL=http://<server-lan-ip>:5001 PYTHONPATH=/usr/lib/python3/dist-packages python3 client/edge_client.py
```

Client and server run on different machines on the same LAN, so `SERVER_URL` must be set to wherever `server.py` is actually running - there's no baked-in default. `server.py`'s startup log prints the exact `SERVER_URL=...` value to use. `client/edge_client.py` fails fast with a clear error if `SERVER_URL` is unset (`check_server_url()`).

The `PYTHONPATH` prefix on the client is required: this Jetson's default `python3` resolves `cv2` to a pip-installed `opencv-python` build that lacks GStreamer support, which breaks the hardware H.264 encoder path (`cv2.CAP_GSTREAMER`). The system OpenCV at `/usr/lib/python3/dist-packages` (JetPack-provided, apt-installed) has GStreamer support. Do **not** run the server with this same `PYTHONPATH` override — it shadows pip-installed `flask`'s `click` dependency with an incompatible system version and crashes on import. `client/edge_client.py` fails fast with a clear error (`check_gstreamer_support()`) if run under the wrong interpreter.

Before editing `client/edge_client.py`'s `CAMERA_IDS`, verify device indices with `v4l2-ctl --list-devices` and `v4l2-ctl --device=/dev/videoN --list-formats-ext` — a physical camera exposes a *pair* of `/dev/videoN` nodes (one real capture node, one metadata-only node with no listed formats), and udev can renumber them across reboots/USB replugs. The comment block above `CAMERA_IDS` in that file has the current known-good indices and the reasoning.

## Architecture: client/server split

Two independent Flask processes connected by persistent WebSocket connections (client → server, and server → browser); no shared config. The server discovers cameras dynamically from whatever `cam_id`s show up in ingest traffic — it has no camera list of its own. WebSocket routes on the server are registered via `flask-sock` (`Sock(app)`), running on the same Werkzeug dev server (`threaded=True`) — no eventlet/gevent/ASGI swap.

**`client/edge_client.py`** — one daemon thread per `CAMERA_IDS` entry (`capture_loop`), each running independently:
1. Reads frames from `cv2.VideoCapture(cam_id)` for `BATCH_DURATION_SEC` (0.5s), buffering them in a list. Most cameras are forced to `FRAME_WIDTH`x`FRAME_HEIGHT` (640x480); indices in `NATIVE_RESOLUTION_CAMERA_IDS` skip that (some cameras, e.g. a ZED stereo camera, only support a few fixed discrete modes and fail outright if forced to an unsupported one).
2. Hardware H.264-encodes the batch via a GStreamer pipeline built in `build_gst_pipeline()` and driven through `cv2.VideoWriter(..., cv2.CAP_GSTREAMER, ...)`, writing to a temp `.mp4` file that's read back into bytes and deleted (`encode_batch()`). The pipeline requires `nvvidconv` to move frames into NVMM memory before `nvv4l2h264enc` — plain `videoconvert` output isn't accepted by the encoder. The batch's measured fps is rounded to an int before being embedded in the GStreamer caps string; GStreamer rejects a decimal framerate.
3. Sends the encoded bytes over a WebSocket connection to `<SERVER_URL>/ingest/<cam_id>` (`ws://`), opened once per camera thread and reused across batches — not reopened per batch. Each batch is two WS frames sent back-to-back: a text frame `{"batch_start_time": ...}` (capture time of the batch's first frame, used by the server for latency calculation) followed by a binary frame with the encoded MP4 bytes.
4. On send failure: logs, drops the connection (it will be reopened on the next batch), and moves on (no retry/backoff within a batch). On camera read failure: the thread closes its WS connection and exits (matches `multi_camera.py`'s behavior — no reconnect logic).

**`server/server.py`** — in-memory state per camera guarded by one global `lock` (Flask runs `threaded=True`):
- `/ingest/<cam_id>` (WS): reads each batch as a text frame (JSON metadata) followed by a binary frame (MP4 bytes), then hands off to `process_batch()`, which writes the batch to a temp file, decodes it frame-by-frame with `cv2.VideoCapture`, re-encodes each decoded frame as JPEG, and pushes them onto `frame_queues[cam_id]` (a `deque`). Computes `fps` from decoded-frame-count/`BATCH_DURATION_SEC` and `latency_ms` from `now - batch_start_time`, stored in `stats[cam_id]`.
- `/feed/<cam_id>` (WS): `next_frame()` drains `frame_queues[cam_id]` at the batch's measured fps for smooth playback (holding on `last_frames[cam_id]` when the queue runs dry between batches rather than blocking or erroring); the route handler sends each frame as a binary WS message and ends when the send raises (browser tab closed).
- `GET /stats`: JSON dump of `stats` for every camera seen so far, still plain HTTP (unchanged) — polled by the dashboard every second, decoupled from the per-camera WS transport. A camera that stops sending decays toward `fps: 0` naturally (no explicit "offline" flag).
- `GET /`: dashboard page (`DASHBOARD_HTML`, inline JS) — polls `/stats` every second for the fps/latency chip, and opens a `WebSocket` to `/feed/<cam_id>` the first time it sees a new camera, writing each received binary frame into an `<img>` via `URL.createObjectURL` (revoking the previous object URL each time to avoid leaking blob URLs); reconnects with a 1s retry on close.

The frame queue is shared per camera, not per viewer — if multiple browser tabs open the same `/feed/<cam_id>` simultaneously, they'll split frames from the same queue rather than each getting the full stream.
