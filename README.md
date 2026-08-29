# ROAR Base Station Feed

Streams camera feeds from a Jetson Xavier (edge device) to a live dashboard on your laptop (base station), over the same LAN.

- **`client/edge_client.py`** runs on the **Xavier**: captures each camera, hardware-encodes it (H.264), and pushes batches to the laptop over a WebSocket.
- **`server/server.py`** runs on the **laptop**: receives those batches, decodes them, and serves a dashboard (`http://<laptop-ip>:5001/`) showing every camera's live feed plus its FPS/latency.

The two machines have different IPs on the same LAN, so the client has to be told where the server is — there's no baked-in default. That's the `SERVER_URL` step below.

## 1. On the laptop (base station / server)

```bash
cd server
pip install -r requirements.txt
python3 server.py
```

It prints something like:

```
Starting camera server...
Dashboard: http://192.168.1.23:5001/
Point edge_client.py at this server with: SERVER_URL=http://192.168.1.23:5001
```

- Open the **Dashboard** URL in a browser — tiles appear automatically as cameras start sending.
- Copy the exact **`SERVER_URL=...`** value — you'll paste it into the command you run on the Xavier in step 3.
- If the laptop reconnects to WiFi or its IP otherwise changes, restart `server.py` and re-copy the new `SERVER_URL` — an old one will just fail to connect.

## 2. On the Xavier: find your camera IDs

`CAMERA_IDS` in `client/edge_client.py` are `/dev/videoN` indices. Each physical camera exposes a *pair* of nodes (one real capture node, one metadata-only node with no formats), and udev can renumber them across reboots/USB replugs — so don't guess, check. Run this on the Xavier to list only the real capture nodes (filters out the metadata-only pair):

```bash
for dev in /dev/video*; do
  if v4l2-ctl --device="$dev" --list-formats-ext 2>/dev/null | grep -q '\[0\]'; then
    name=$(v4l2-ctl --device="$dev" --info 2>/dev/null | awk -F': ' '/Card type/{print $2}')
    echo "$dev  ->  CAMERA_ID ${dev##*video}   ($name)"
  fi
done
```

Each printed line is a usable `CAMERA_IDS` entry. Update the list in `client/edge_client.py` (see the comment above `CAMERA_IDS` for current known-good values and why cam 0 is excluded) before moving on.

## 3. On the Xavier: run the client

```bash
cd client
pip install -r requirements.txt
SERVER_URL=http://<laptop-lan-ip>:5001 PYTHONPATH=/usr/lib/python3/dist-packages python3 edge_client.py
```

Replace `<laptop-lan-ip>:5001` with the exact `SERVER_URL=...` value printed by `server.py` in step 1 — it must be an address reachable over the LAN (not `127.0.0.1` or `localhost`, and not the Xavier's own IP).

If `SERVER_URL` isn't set, `edge_client.py` fails immediately with instructions instead of hanging or silently dropping frames.

**The `PYTHONPATH` prefix is required, every time, on the Xavier.** This Jetson's default `python3` resolves `cv2` to a pip-installed `opencv-python` build with no GStreamer support, which breaks the hardware H.264 encoder. `/usr/lib/python3/dist-packages` is the JetPack-provided system OpenCV that does have GStreamer support. If you forget this, `edge_client.py` fails fast with a clear error rather than a confusing crash.

**Do not** run `server.py` on the laptop with this same `PYTHONPATH` override — it shadows pip-installed `flask`'s dependencies with incompatible system versions and crashes on import. `PYTHONPATH=/usr/lib/python3/dist-packages` is a Xavier-only, client-only thing.

## Troubleshooting

- **A camera never shows up on the dashboard**: check the Xavier's terminal for `[cam N] ws send failed: ...` — usually means `SERVER_URL` is wrong or stale (laptop's IP changed) or the laptop's firewall is blocking port 5001.
- **`edge_client.py` exits with a GStreamer error**: you forgot the `PYTHONPATH` prefix, or ran with a plain `pip install opencv-python` present — see the note in `client/requirements.txt`.
- **Wrong/no camera picture, or capture fails outright**: `/dev/videoN` indices can get renumbered by udev after a reboot or USB replug. Re-run the camera ID script in step 2 and update `CAMERA_IDS` if the indices moved.
- **Single-machine testing, no second device handy**: `multi_camera.py` at the repo root is a standalone, single-process fallback (no client/server split, no network hop) — useful for confirming a camera works at all before wiring up the split setup.

See `CLAUDE.md` for the full architecture writeup (wire protocol, in-memory server state, dashboard internals).
