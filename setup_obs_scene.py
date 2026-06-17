"""
ZoomCast — OBS Scene Setup
Run once to create the "ZoomCast" scene with a full-screen capture source
and a webcam PiP in the bottom-right corner.
"""

import sys
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zoomcast-setup")

# ── CONFIG ────────────────────────────────────────────────────────────────────
OBS_HOST     = "localhost"
OBS_PORT     = 4455
OBS_PASSWORD = ""           # set if OBS WebSocket requires a password

SCENE_NAME   = "ZoomCast"

CANVAS_W     = 1920
CANVAS_H     = 1080

# PiP (webcam) settings
PIP_W        = 320
PIP_H        = 180
PIP_MARGIN   = 20           # px from canvas edge

# ── IMPORT ────────────────────────────────────────────────────────────────────
try:
    import obsws_python as obs
except ImportError:
    log.error("obsws-python not installed. Run: pip install obsws-python")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def connect() -> obs.ReqClient:
    for attempt in range(1, 6):
        try:
            client = obs.ReqClient(
                host=OBS_HOST, port=OBS_PORT,
                password=OBS_PASSWORD, timeout=5,
            )
            v = client.get_version()
            log.info("Connected to OBS %s (WebSocket %s)",
                     v.obs_version, v.obs_web_socket_version)
            return client
        except Exception as e:
            log.warning("Attempt %d/5 failed: %s", attempt, e)
            if attempt < 5:
                time.sleep(3)
    log.error("Could not connect to OBS. Make sure it's running and WebSocket is enabled.")
    sys.exit(1)


def list_scenes(client: obs.ReqClient) -> list[str]:
    resp = client.get_scene_list()
    return [s["sceneName"] for s in resp.scenes]


def ensure_scene(client: obs.ReqClient) -> bool:
    """Create SCENE_NAME if it doesn't exist. Returns True if created."""
    scenes = list_scenes(client)
    if SCENE_NAME in scenes:
        log.info("Scene '%s' already exists — skipping creation.", SCENE_NAME)
        return False
    client.create_scene(SCENE_NAME)
    log.info("Created scene '%s'.", SCENE_NAME)
    return True


def get_scene_items(client: obs.ReqClient) -> list[dict]:
    resp = client.get_scene_item_list(SCENE_NAME)
    return resp.scene_items


def source_exists_in_scene(client: obs.ReqClient, source_name: str) -> bool:
    for item in get_scene_items(client):
        if item.get("sourceName") == source_name:
            return True
    return False


def list_inputs(client: obs.ReqClient) -> list[dict]:
    resp = client.get_input_list()
    return resp.inputs


def find_input_by_kind(client: obs.ReqClient, *kinds: str) -> dict | None:
    """Return the first existing input whose kind matches any of `kinds`."""
    for inp in list_inputs(client):
        k = inp.get("inputKind", "")
        if any(kind in k for kind in kinds):
            return inp
    return None


def get_or_create_screen_input(client: obs.ReqClient) -> str:
    """Return the name of an existing screen-capture input, or create one."""
    SCREEN_KINDS = ["screen_capture", "monitor_capture", "display_capture",
                    "av_capture_input"]
    existing = find_input_by_kind(client, *SCREEN_KINDS)
    if existing:
        name = existing["inputName"]
        log.info("Found existing screen capture input: '%s'", name)
        return name

    # Create a new screen capture input (macOS)
    name = "Screen Capture"
    try:
        client.create_input(SCENE_NAME, name, "screen_capture", {}, True)
        log.info("Created screen capture input '%s'.", name)
        return name
    except Exception as e:
        log.warning("Could not create 'screen_capture' kind (%s); trying 'monitor_capture'…", e)

    try:
        client.create_input(SCENE_NAME, name, "monitor_capture", {}, True)
        log.info("Created monitor capture input '%s'.", name)
        return name
    except Exception as e2:
        log.error("Failed to create screen capture source: %s", e2)
        log.error("Please add a screen capture source manually in OBS and run this script again.")
        sys.exit(1)


def add_source_to_scene(client: obs.ReqClient, input_name: str) -> int:
    """Add input to SCENE_NAME if not already present. Returns scene item id."""
    for item in get_scene_items(client):
        if item.get("sourceName") == input_name:
            log.info("'%s' already in scene — skipping.", input_name)
            return item["sceneItemId"]
    resp = client.create_scene_item(SCENE_NAME, input_name, enabled=True)
    return resp.scene_item_id


def set_item_transform_fullscreen(client: obs.ReqClient, item_id: int):
    """Position and scale a scene item to fill the canvas."""
    transform = {
        "positionX": 0.0,
        "positionY": 0.0,
        "scaleX": 1.0,
        "scaleY": 1.0,
        "rotation": 0.0,
        "boundsType": "OBS_BOUNDS_SCALE_INNER",
        "boundsWidth": float(CANVAS_W),
        "boundsHeight": float(CANVAS_H),
        "cropLeft": 0,
        "cropRight": 0,
        "cropTop": 0,
        "cropBottom": 0,
    }
    client.set_scene_item_transform(SCENE_NAME, item_id, transform)
    log.info("Set item %d to full canvas (%dx%d).", item_id, CANVAS_W, CANVAS_H)


def get_or_create_webcam_input(client: obs.ReqClient) -> str | None:
    """Return name of first webcam/video capture input, or None."""
    WEBCAM_KINDS = ["av_capture_input_v2", "av_capture_input",
                    "video_capture_device", "dshow_input"]
    existing = find_input_by_kind(client, *WEBCAM_KINDS)
    if existing:
        log.info("Found existing webcam input: '%s'", existing["inputName"])
        return existing["inputName"]

    # Try to create one
    name = "Webcam"
    for kind in ["av_capture_input_v2", "av_capture_input", "video_capture_device"]:
        try:
            client.create_input(SCENE_NAME, name, kind, {}, True)
            log.info("Created webcam input '%s' (kind=%s).", name, kind)
            return name
        except Exception:
            continue

    log.warning("Could not auto-create webcam source. Add one manually in OBS as a PiP.")
    return None


def set_item_transform_pip(client: obs.ReqClient, item_id: int):
    """Position a scene item as a PiP in the bottom-right corner."""
    x = float(CANVAS_W - PIP_W - PIP_MARGIN)
    y = float(CANVAS_H - PIP_H - PIP_MARGIN)
    transform = {
        "positionX": x,
        "positionY": y,
        "scaleX": 1.0,
        "scaleY": 1.0,
        "rotation": 0.0,
        "boundsType": "OBS_BOUNDS_SCALE_INNER",
        "boundsWidth": float(PIP_W),
        "boundsHeight": float(PIP_H),
        "cropLeft": 0,
        "cropRight": 0,
        "cropTop": 0,
        "cropBottom": 0,
    }
    client.set_scene_item_transform(SCENE_NAME, item_id, transform)
    log.info("Positioned webcam PiP at (%d, %d) size %dx%d.", int(x), int(y), PIP_W, PIP_H)


def set_scene_active(client: obs.ReqClient):
    """Switch to the ZoomCast scene in the program output."""
    try:
        client.set_current_program_scene(SCENE_NAME)
        log.info("Switched active scene to '%s'.", SCENE_NAME)
    except Exception as e:
        log.warning("Could not switch to scene: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("""
╔══════════════════════════════════════════════════════╗
║           ZoomCast — OBS Scene Setup                 ║
╚══════════════════════════════════════════════════════╝
""")

    client = connect()

    # 1. Create scene
    ensure_scene(client)

    # 2. Screen capture → full canvas
    screen_name = get_or_create_screen_input(client)
    screen_id   = add_source_to_scene(client, screen_name)
    set_item_transform_fullscreen(client, screen_id)

    # 3. Webcam PiP
    webcam_name = get_or_create_webcam_input(client)
    if webcam_name:
        webcam_id = add_source_to_scene(client, webcam_name)
        set_item_transform_pip(client, webcam_id)

    # 4. Switch to scene
    set_scene_active(client)

    print("""
✅ Setup complete!

Next steps:
  1. In OBS, open the "ZoomCast" scene and confirm sources look correct.
  2. Select your screen / webcam device if the captures show blank.
  3. Run:  ./start.sh
  4. Click Record in OBS, then start typing — ZoomCast handles the rest.

Tip: Run zoom_controller.py --help (or read the README) for shortcut info.
""")


if __name__ == "__main__":
    main()
