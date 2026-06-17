# ZoomCast — Smart OBS Zoom Controller

Automatically zooms into your active text area in OBS while you type — like Loom, but live. No post-editing.

---

## How it works

- **pynput** watches all keystrokes globally
- On each keystroke, an AppleScript query gets the focused UI element's screen coordinates
- That maps to an OBS `SetSceneItemTransform` call that scales + repositions the screen capture source — zooming into where you're typing
- A **150 ms debounce** prevents rapid re-zooming mid-word
- After **3 s of inactivity**, it smoothly zooms back out
- All zoom transitions are **300 ms smooth animations** (25-frame smoothstep interpolation) sent over OBS WebSocket

---

## Prerequisites

- **macOS** (Sonoma / Ventura / Monterey)
- **OBS 28+** (WebSocket v5 is built in)
- **Python 3.10+**

---

## Setup

### 1 — Enable OBS WebSocket

In OBS: **Tools → WebSocket Server Settings**
- ✅ Enable WebSocket server
- Port: `4455` (default)
- Set a password if you want (update `OBS_PASSWORD` in `zoom_controller.py`)

### 2 — Configure the OBS scene (once)

```bash
cd ~/Desktop/zoomcast
python3 -m venv .venv && source .venv/bin/activate
pip install obsws-python
python3 setup_obs_scene.py
```

This creates a **ZoomCast** scene with:
- Screen capture source (full canvas)
- Webcam PiP in the bottom-right corner (320×180, 20 px margin)

Open OBS, switch to the ZoomCast scene, and confirm the sources look right. Select your screen/webcam device if they show blank.

### 3 — Grant Accessibility permission

**System Settings → Privacy & Security → Accessibility → add Terminal** (or iTerm/whatever you use) and enable it.

Without this, caret detection falls back to mouse position — zoom still works, just less precise.

### 4 — Run

```bash
cd ~/Desktop/zoomcast
./start.sh
```

The script will create a `.venv`, install all deps, and start the controller. Leave it running in the background.

### 5 — Record

1. In OBS, click **Start Recording**
2. Go type in any app — ZoomCast handles the zoom automatically

---

## Controls

| Action | Effect |
|---|---|
| Type anything | Auto-zoom into focused text area |
| Stop typing (3 s) | Zoom back out |
| **Cmd+Shift+Z** | Lock zoom at current position |
| **Cmd+Shift+Z** again | Unlock, resume auto-zoom |
| **Ctrl+C** in terminal | Quit (zooms out gracefully) |

---

## Configuration

Edit the top of `zoom_controller.py`:

```python
OBS_HOST        = "localhost"
OBS_PORT        = 4455
OBS_PASSWORD    = ""          # set if OBS WebSocket has a password

SCREEN_W        = 2560        # your physical screen width
SCREEN_H        = 1440        # your physical screen height

ZOOM_FACTOR     = 2.2         # how much to zoom in (1.5–3.0 is typical)
ZOOM_OUT_DELAY  = 3.0         # seconds of inactivity before zoom-out
ZOOM_ANIM_MS    = 300         # animation duration in ms

SOURCE_NAME     = None        # override auto-detection with an exact source name
```

If auto-detection can't find your screen capture source, the script will list all sources and tell you to set `SOURCE_NAME`.

---

## Troubleshooting

**"OBS not reachable"** — OBS isn't running, or WebSocket isn't enabled. The script retries every 5 s.

**"No screen capture source found"** — The script lists all sources in your scene. Copy the correct name into `SOURCE_NAME` at the top of `zoom_controller.py`.

**Zoom jumps to wrong position** — Grant Accessibility permission (Step 3 above). Without it, caret detection uses mouse position.

**Animation is choppy** — Lower `ZOOM_ANIM_FPS` in `zoom_controller.py` (default 25). Or check OBS CPU usage.

**PiP webcam shows blank** — In OBS, click the webcam source → Properties, and select your camera device manually.
