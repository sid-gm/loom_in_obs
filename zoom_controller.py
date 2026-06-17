"""
ZoomCast — Smart OBS Zoom Controller
Watches keystrokes, zooms into the active text area in OBS, zooms back out on inactivity.
Cmd+Shift+Z toggles manual lock.
"""

import sys
import time
import threading
import subprocess
import re
import logging

# ── CONFIG ────────────────────────────────────────────────────────────────────
OBS_HOST        = "localhost"
OBS_PORT        = 4455
OBS_PASSWORD    = ""          # set if OBS WebSocket has a password

# Canvas / capture dimensions
CANVAS_W        = 1920
CANVAS_H        = 1080
SCREEN_W        = 2560        # your physical screen width
SCREEN_H        = 1440        # your physical screen height

ZOOM_FACTOR     = 2.2         # how much to zoom in
ZOOM_OUT_DELAY  = 3.0         # seconds of inactivity before zoom-out
ZOOM_ANIM_MS    = 450         # animation duration in ms
ZOOM_ANIM_FPS   = 40          # frames in the animation (higher = smoother)

# Keyboard shortcut to toggle manual zoom lock (pynput Key names or chars)
SHORTCUT_MODS   = {"cmd", "shift"}
SHORTCUT_KEY    = "z"

# Source auto-detection: kinds that count as "screen capture"
SCREEN_SOURCE_KINDS = {
    "screen_capture",
    "monitor_capture",
    "display_capture",
    "av_capture_input",          # macOS screen capture
    "coreaudio_input_capture",
}

# Manual override: set to a source name string to skip auto-detection
SOURCE_NAME: str | None = None

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zoomcast")

# ── OBSWS IMPORT ─────────────────────────────────────────────────────────────
try:
    import obsws_python as obs
except ImportError:
    log.error("obsws-python not installed. Run ./start.sh or: pip install obsws-python")
    sys.exit(1)

# ── PYNPUT IMPORT ─────────────────────────────────────────────────────────────
try:
    from pynput import keyboard as kb
    from pynput import mouse as ms
except ImportError:
    log.error("pynput not installed. Run ./start.sh or: pip install pynput")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Caret / focus position detection
# ─────────────────────────────────────────────────────────────────────────────

APPLESCRIPT_FOCUSED_ELEMENT = """
tell application "System Events"
    set frontProc to first process whose frontmost is true
    try
        set fe to focused UI element of front window of frontProc
        set pos to position of fe
        set sz to size of fe
        return (item 1 of pos) & "," & (item 2 of pos) & "," & (item 1 of sz) & "," & (item 2 of sz)
    on error
        try
            set wins to windows of frontProc
            if (count of wins) > 0 then
                set pos to position of front window of frontProc
                set sz to size of front window of frontProc
                return (item 1 of pos) & "," & (item 2 of pos) & "," & (item 1 of sz) & "," & (item 2 of sz)
            end if
        end try
        return "error"
    end try
end tell
"""

APPLESCRIPT_MOUSE_POS = """
tell application "System Events"
    set mp to do shell script "python3 -c \\"import Quartz; loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None)); print(int(loc.x), int(loc.y))\\""
    return mp
end tell
"""


def _run_applescript(script: str) -> str:
    """Run an AppleScript and return stdout, or '' on failure."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=1.5
        )
        return result.stdout.strip()
    except Exception:
        return ""


def get_mouse_position() -> tuple[int, int]:
    """Return current mouse position via Quartz (most reliable)."""
    try:
        import Quartz
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return int(loc.x), int(loc.y)
    except Exception:
        pass
    # fallback via osascript
    raw = _run_applescript('do shell script "python3 -c \\"import Quartz; loc=Quartz.CGEventGetLocation(Quartz.CGEventCreate(None)); print(int(loc.x),int(loc.y))\\""')
    parts = raw.split()
    if len(parts) == 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            pass
    return SCREEN_W // 2, SCREEN_H // 2


def get_focus_region() -> tuple[int, int, int]:
    """
    Returns (center_x, center_y, radius) in screen coordinates.
    Tries AppleScript accessibility first, falls back to mouse position.
    """
    raw = _run_applescript(APPLESCRIPT_FOCUSED_ELEMENT)

    if raw and raw != "error":
        # AppleScript returns comma-separated: x,y,w,h
        # Sometimes with spaces like "123, 456, 789, 200"
        parts = re.split(r"[,\s]+", raw.strip())
        nums = []
        for p in parts:
            p = p.strip()
            if p:
                try:
                    nums.append(int(float(p)))
                except ValueError:
                    pass
        if len(nums) >= 4:
            x, y, w, h = nums[0], nums[1], nums[2], nums[3]
            cx = x + w // 2
            cy = y + h // 2
            radius = max(300, min(w, h) // 2 + 150)
            return cx, cy, radius

    # Fallback: mouse position
    mx, my = get_mouse_position()
    return mx, my, 400


# ─────────────────────────────────────────────────────────────────────────────
# OBS transform helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_transform(cx: int, cy: int, zoom: float,
                    canvas_w: int = CANVAS_W, canvas_h: int = CANVAS_H,
                    screen_w: int = SCREEN_W, screen_h: int = SCREEN_H) -> dict:
    """
    Build an OBS SetSceneItemTransform payload that zooms into (cx, cy)
    on the physical screen.

    The screen capture source fills the canvas at 1:1 when zoom=1.
    When zooming, we scale the source up and shift it so (cx,cy) maps
    to the canvas centre.
    """
    # Scale factors from screen coords → canvas coords
    sx = canvas_w / screen_w
    sy = canvas_h / screen_h

    # Canvas-space coordinates of the zoom centre
    ccx = cx * sx
    ccy = cy * sy

    # We want the zoomed source centred so that ccx,ccy sits at canvas centre.
    # positionX = canvas_centre_x - ccx * zoom
    pos_x = (canvas_w / 2) - ccx * zoom
    pos_y = (canvas_h / 2) - ccy * zoom

    return {
        "positionX": pos_x,
        "positionY": pos_y,
        "scaleX": zoom,
        "scaleY": zoom,
        "rotation": 0.0,
        "boundsWidth": canvas_w,
        "boundsHeight": canvas_h,
        "boundsType": "OBS_BOUNDS_NONE",
        "cropLeft": 0,
        "cropRight": 0,
        "cropTop": 0,
        "cropBottom": 0,
    }


TRANSFORM_OUT = build_transform(SCREEN_W // 2, SCREEN_H // 2, zoom=1.0)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def ease_out_expo(t: float) -> float:
    """Snappy start, soft settle — the 'premium' zoom feel (Screen Studio-like)."""
    t = max(0.0, min(1.0, t))
    if t >= 1.0:
        return 1.0
    return 1.0 - 2.0 ** (-10.0 * t)


# Active easing curve for the zoom animation. Swap to `smoothstep` for a
# gentler, symmetric ramp.
EASING = ease_out_expo


def interp_transform(t_from: dict, t_to: dict, alpha: float) -> dict:
    keys = ["positionX", "positionY", "scaleX", "scaleY"]
    result = dict(t_to)
    s = EASING(alpha)
    for k in keys:
        result[k] = lerp(t_from.get(k, t_to[k]), t_to[k], s)
    return result


def transforms_close(a: dict, b: dict,
                     pos_tol: float = 8.0, scale_tol: float = 0.05) -> bool:
    """True if two transforms are visually indistinguishable (absorbs caret jitter)."""
    if abs(a.get("scaleX", 0.0) - b.get("scaleX", 0.0)) > scale_tol:
        return False
    if abs(a.get("scaleY", 0.0) - b.get("scaleY", 0.0)) > scale_tol:
        return False
    if abs(a.get("positionX", 0.0) - b.get("positionX", 0.0)) > pos_tol:
        return False
    if abs(a.get("positionY", 0.0) - b.get("positionY", 0.0)) > pos_tol:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# ZoomAnimator
# ─────────────────────────────────────────────────────────────────────────────

class ZoomAnimator:
    """Smoothly interpolates OBS scene item transform between states."""

    def __init__(self, obs_client, scene_name: str, scene_item_id: int):
        self.client       = obs_client
        self.scene_name   = scene_name
        self.item_id      = scene_item_id

        self.current      = dict(TRANSFORM_OUT)
        self.target       = dict(TRANSFORM_OUT)

        self._lock        = threading.Lock()       # guards current / target
        self._anim_lock   = threading.Lock()        # serializes animate_to calls
        self._cancel_evt  = threading.Event()
        self._thread: threading.Thread | None = None

    def animate_to(self, target: dict):
        """Cancel any running animation and start a new one toward `target`."""
        with self._anim_lock:
            # Already heading to (or resting at) essentially this target → leave
            # the in-flight animation alone instead of restarting it from scratch.
            with self._lock:
                if transforms_close(self.target, target):
                    return
                self.target = dict(target)

            # Cancel the running animation. Do NOT hold self._lock across the
            # join — the animation thread needs it to record where it stopped.
            self._cancel_evt.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=0.5)
            self._cancel_evt.clear()

            with self._lock:
                start = dict(self.current)
            t = threading.Thread(
                target=self._run_animation,
                args=(start, dict(target)),
                daemon=True,
            )
            self._thread = t
            t.start()

    def _run_animation(self, t_from: dict, t_to: dict):
        frames = ZOOM_ANIM_FPS
        frame_ms = ZOOM_ANIM_MS / frames / 1000.0
        last = dict(t_from)
        for i in range(frames + 1):
            if self._cancel_evt.is_set():
                # Save where we actually stopped so the next animation starts
                # from here instead of snapping back to the old position.
                with self._lock:
                    self.current = dict(last)
                return
            alpha = i / frames
            tr = interp_transform(t_from, t_to, alpha)
            try:
                self.client.set_scene_item_transform(
                    self.scene_name,
                    self.item_id,
                    tr,
                )
            except Exception as e:
                log.debug("OBS transform error: %s", e)
                with self._lock:
                    self.current = dict(last)
                return
            last = tr
            if i < frames:
                time.sleep(frame_ms)
        with self._lock:
            self.current = dict(t_to)

    def zoom_in(self, cx: int, cy: int, factor: float = ZOOM_FACTOR):
        target = build_transform(cx, cy, zoom=factor)
        log.info("Zoom IN  → screen(%d, %d) × %.1f", cx, cy, factor)
        self.animate_to(target)

    def zoom_out(self):
        log.info("Zoom OUT → full screen")
        self.animate_to(TRANSFORM_OUT)


# ─────────────────────────────────────────────────────────────────────────────
# OBS connection + source discovery
# ─────────────────────────────────────────────────────────────────────────────

def connect_obs() -> obs.ReqClient:
    """Block until OBS WebSocket connects, retrying every 5 s."""
    while True:
        try:
            client = obs.ReqClient(
                host=OBS_HOST,
                port=OBS_PORT,
                password=OBS_PASSWORD,
                timeout=5,
            )
            version = client.get_version()
            log.info("Connected to OBS %s (WebSocket %s)",
                     version.obs_version, version.obs_web_socket_version)
            return client
        except Exception as e:
            log.warning("OBS not reachable (%s) — retrying in 5 s…", e)
            time.sleep(5)


def find_screen_source(client: obs.ReqClient) -> tuple[str, int, str]:
    """
    Returns (scene_name, scene_item_id, source_name) for the first
    screen capture source found in the current scene.
    Raises RuntimeError if none found.
    """
    scene_resp = client.get_current_program_scene()
    scene_name = scene_resp.current_program_scene_name

    items_resp = client.get_scene_item_list(scene_name)
    items = items_resp.scene_items

    log.info("Scene '%s' has %d item(s):", scene_name, len(items))
    for item in items:
        kind = item.get("inputKind", item.get("sourceKind", ""))
        name = item.get("sourceName", "")
        item_id = item.get("sceneItemId", -1)
        log.info("  [%d] '%s'  kind=%s", item_id, name, kind)

    # Manual override
    if SOURCE_NAME:
        for item in items:
            if item.get("sourceName") == SOURCE_NAME:
                return scene_name, item["sceneItemId"], SOURCE_NAME
        raise RuntimeError(
            f"SOURCE_NAME='{SOURCE_NAME}' not found in scene '{scene_name}'. "
            "Check the source names logged above."
        )

    # Auto-detect
    for item in items:
        kind = item.get("inputKind", item.get("sourceKind", ""))
        if any(k in kind.lower() for k in SCREEN_SOURCE_KINDS):
            name = item["sourceName"]
            item_id = item["sceneItemId"]
            log.info("Auto-selected source '%s' (id=%d, kind=%s)", name, item_id, kind)
            return scene_name, item_id, name

    names = [f"'{i.get('sourceName')}' (kind={i.get('inputKind', i.get('sourceKind', '?'))})" for i in items]
    raise RuntimeError(
        f"No screen capture source found in scene '{scene_name}'.\n"
        f"Sources present: {', '.join(names)}\n"
        "Set SOURCE_NAME at the top of zoom_controller.py to one of these."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main controller
# ─────────────────────────────────────────────────────────────────────────────

class ZoomController:
    def __init__(self):
        self.animator: ZoomAnimator | None = None
        self._zoom_out_timer: threading.Timer | None = None
        self._lock = threading.Lock()

        self._manual_locked = False   # True = Cmd+Shift+Z lock is active
        self._locked_pos: tuple[int, int] | None = None

        self._pressed_mods: set[str] = set()
        self._last_key_time = 0.0
        self._debounce_timer: threading.Timer | None = None
        self._debounce_delay = 0.15   # 150 ms

    # ── timer helpers ──────────────────────────────────────────────────────

    def _cancel_zoom_out_timer(self):
        if self._zoom_out_timer:
            self._zoom_out_timer.cancel()
            self._zoom_out_timer = None

    def _schedule_zoom_out(self):
        self._cancel_zoom_out_timer()
        self._zoom_out_timer = threading.Timer(ZOOM_OUT_DELAY, self._do_zoom_out)
        self._zoom_out_timer.daemon = True
        self._zoom_out_timer.start()

    def _do_zoom_out(self):
        if self._manual_locked:
            return
        if self.animator:
            self.animator.zoom_out()

    # ── debounce ───────────────────────────────────────────────────────────

    def _cancel_debounce(self):
        if self._debounce_timer:
            self._debounce_timer.cancel()
            self._debounce_timer = None

    def _schedule_zoom_in(self):
        """After debounce delay, trigger a zoom-in to current focus region."""
        self._cancel_debounce()
        self._debounce_timer = threading.Timer(self._debounce_delay, self._do_zoom_in)
        self._debounce_timer.daemon = True
        self._debounce_timer.start()

    def _zoom_to(self, cx: int, cy: int):
        """Zoom into (cx, cy) in screen coords, then arm the auto zoom-out timer."""
        if self._manual_locked:
            return
        self._cancel_zoom_out_timer()
        if self.animator:
            self.animator.zoom_in(cx, cy)
        self._schedule_zoom_out()

    def _do_zoom_in(self):
        if self._manual_locked:
            return
        cx, cy, _ = get_focus_region()
        self._zoom_to(cx, cy)

    # ── keyboard callbacks ─────────────────────────────────────────────────

    def _mod_name(self, key) -> str | None:
        """Return canonical modifier name for pynput Key, or None."""
        key_map = {
            kb.Key.cmd: "cmd", kb.Key.cmd_l: "cmd", kb.Key.cmd_r: "cmd",
            kb.Key.shift: "shift", kb.Key.shift_l: "shift", kb.Key.shift_r: "shift",
            kb.Key.alt: "alt", kb.Key.alt_l: "alt", kb.Key.alt_r: "alt",
            kb.Key.ctrl: "ctrl", kb.Key.ctrl_l: "ctrl", kb.Key.ctrl_r: "ctrl",
        }
        return key_map.get(key)

    def _is_printable(self, key) -> bool:
        try:
            c = key.char
            return c is not None and c.isprintable()
        except AttributeError:
            return False

    def on_key_press(self, key):
        mod = self._mod_name(key)
        if mod:
            self._pressed_mods.add(mod)
            return

        # Check shortcut: Cmd+Shift+Z
        try:
            char = key.char
        except AttributeError:
            char = None

        if char and char.lower() == SHORTCUT_KEY and SHORTCUT_MODS <= self._pressed_mods:
            self._toggle_manual_lock()
            return

        # Normal printable key → debounce zoom
        if self._is_printable(key) and not self._manual_locked:
            self._cancel_zoom_out_timer()  # typing in progress
            self._schedule_zoom_in()

    def on_key_release(self, key):
        mod = self._mod_name(key)
        if mod:
            self._pressed_mods.discard(mod)

    # ── mouse callbacks ────────────────────────────────────────────────────

    def on_mouse_click(self, x, y, button, pressed):
        """Scroll-wheel (middle button) click → zoom into the click point."""
        if not pressed or button != ms.Button.middle:
            return
        if self._manual_locked:
            return
        log.info("🖱  Scroll-wheel click")
        self._zoom_to(int(x), int(y))

    # ── manual lock ────────────────────────────────────────────────────────

    def _toggle_manual_lock(self):
        with self._lock:
            if not self._manual_locked:
                self._manual_locked = True
                cx, cy, _ = get_focus_region()
                self._locked_pos = (cx, cy)
                log.info("🔒 Zoom LOCKED at screen(%d, %d)  [Cmd+Shift+Z to unlock]", cx, cy)
                self._cancel_zoom_out_timer()
                if self.animator:
                    self.animator.zoom_in(cx, cy)
            else:
                self._manual_locked = False
                self._locked_pos = None
                log.info("🔓 Zoom UNLOCKED — resuming auto-zoom  [Cmd+Shift+Z to lock]")
                if self.animator:
                    self.animator.zoom_out()

    # ── run ────────────────────────────────────────────────────────────────

    def run(self):
        log.info("Connecting to OBS at %s:%d …", OBS_HOST, OBS_PORT)
        client = connect_obs()

        try:
            scene_name, item_id, src_name = find_screen_source(client)
        except RuntimeError as e:
            log.error("%s", e)
            sys.exit(1)

        self.animator = ZoomAnimator(client, scene_name, item_id)
        log.info("Monitoring source '%s' in scene '%s'", src_name, scene_name)
        log.info("Controls: type or scroll-wheel click to zoom | Cmd+Shift+Z to lock/unlock | Ctrl+C to quit")
        log.info("─" * 60)

        listener = kb.Listener(
            on_press=self.on_key_press,
            on_release=self.on_key_release,
        )
        listener.start()

        mouse_listener = ms.Listener(on_click=self.on_mouse_click)
        mouse_listener.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("\nShutting down…")
        finally:
            listener.stop()
            mouse_listener.stop()
            if self.animator:
                self.animator.zoom_out()
            time.sleep(0.4)
            log.info("Bye.")


# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("""
╔══════════════════════════════════════════════════════╗
║           ZoomCast — OBS Smart Zoom Controller       ║
╚══════════════════════════════════════════════════════╝
""")

    # Check accessibility
    result = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to get name of every process'],
        capture_output=True, text=True, timeout=3,
    )
    if result.returncode != 0:
        log.warning(
            "Accessibility may not be granted.\n"
            "  → Open System Settings → Privacy & Security → Accessibility\n"
            "  → Add Terminal (or your terminal app) and enable it.\n"
            "Caret detection will fall back to mouse position."
        )

    ZoomController().run()


if __name__ == "__main__":
    main()
