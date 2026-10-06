"""
GloveMouse 2.1 - control the Windows mouse with a gloved hand via webcam.

  * Move the gloved hand          -> cursor follows
  * Quick pinch (thumb + index)   -> left click
  * Pinch and hold / move         -> drag & drop (open the pinch to drop)

Default shortcuts (global, changeable in Settings):
  F6 Start / Pause capture (calibrates the glove at every start)
  F7 Glove requirement ON / OFF
  F8 Settings
  F9 Quit
"""

import ctypes
import json
import math
import os
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
import numpy as np
from PIL import Image, ImageTk
import mediapipe as mp

APP = "GloveMouse"
VERSION = "2.1"

# ---------------- fixed tuning (most settings live in the Settings page) ----------------
GLOVE_MATCH = 0.45      # share of palm pixels that must match the glove colour
ENGAGE_FRAMES = 4       # frames a hand must be seen before it takes control
LOST_GRACE = 0.30       # seconds the hand may vanish before a drag is released
CLICK_MAX_TIME = 0.30   # pinch shorter than this = click
DRAG_MOVE_PX = 40       # moving this far while pinched starts a drag
SMOOTH_BETA = 0.006
CALIB_PREP, CALIB_SAMPLE = 2.0, 1.0   # seconds to get ready, seconds of colour sampling

TRACK_POINTS = {
    "Palm center": (0, 5, 9, 13, 17),
    "Wrist": (0,),
    "Index knuckle": (5,),
    "Middle knuckle": (9,),
    "Ring knuckle": (13,),
    "Pinky knuckle": (17,),
    "Pinky tip": (20,),
}
CORNERS = ["Top left", "Top right", "Bottom left", "Bottom right", "Floater"]
AREA_MODES = ["Whole camera view", "Around my hand (adapts to distance)"]
ACTIONS = [("start", "Start / Pause capture"), ("glove", "Glove requirement ON / OFF"),
           ("settings", "Settings"), ("quit", "Quit app")]
MODIFIER_KEYS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
                 "Win_L", "Win_R", "Super_L", "Super_R", "Caps_Lock", "Num_Lock"}

DEFAULTS = {
    "keys": {"start": {"vk": 0x75, "name": "F6"}, "glove": {"vk": 0x76, "name": "F7"},
             "settings": {"vk": 0x77, "name": "F8"}, "quit": {"vk": 0x78, "name": "F9"}},
    "track_point": "Middle knuckle",
    "glove_required": True,
    "user_tracking": True,
    "area_mode": AREA_MODES[0],
    "long_range": False,
    "camera": 0,
    "corner": "Bottom left",
    "mini_width": 320,
    "movement": 5,     # 1-10: how much hand movement crosses the screen
    "pinch": 5,        # 1-10: pinch sensitivity
    "smoothing": 6,    # 1-10: cursor smoothing
    "float_x": None,
    "float_y": None,
}


# ---------------- Windows layer ----------------
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


IS_WIN = sys.platform == "win32"
if IS_WIN:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    SCREEN_W = user32.GetSystemMetrics(0)
    SCREEN_H = user32.GetSystemMetrics(1)
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]

    def set_cursor(x, y): user32.SetCursorPos(int(x), int(y))
    def mouse_down(): user32.mouse_event(0x0002, 0, 0, 0, 0)
    def mouse_up(): user32.mouse_event(0x0004, 0, 0, 0, 0)
    def key_down(vk): return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)
    def message_box(text): user32.MessageBoxW(None, str(text), APP, 0x10)

    def work_area():
        r = RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
        return r.left, r.top, r.right, r.bottom

    def virtual_screen():
        x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        return x, y, x + user32.GetSystemMetrics(78), y + user32.GetSystemMetrics(79)

    def no_maximize(hwnd):
        style = user32.GetWindowLongW(hwnd, -16)
        user32.SetWindowLongW(hwnd, -16, style & ~0x00010000)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED

    CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
else:  # lets the app run (without mouse control) elsewhere for testing
    SCREEN_W, SCREEN_H = 1920, 1080
    def set_cursor(x, y): pass
    def mouse_down(): pass
    def mouse_up(): pass
    def key_down(vk): return False
    def message_box(text): print(text)
    def work_area(): return 0, 0, SCREEN_W, SCREEN_H
    def virtual_screen(): return 0, 0, SCREEN_W, SCREEN_H
    def no_maximize(hwnd): pass
    CONFIG_DIR = os.path.join(os.path.expanduser("~"), "." + APP)

SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")


class AspectLock:
    """Keeps the preview window's video area at the camera's aspect ratio while
    the user drags its edges (handles the Windows WM_SIZING message)."""
    WM_SIZING = 0x0214

    def __init__(self):
        self.ratio = 4 / 3
        self.extra_h = 0   # height of the button bar under the video
        self._hooks = {}
        if IS_WIN:
            self.WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                              wintypes.WPARAM, wintypes.LPARAM)
            self._set = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            self._set.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            self._set.restype = ctypes.c_void_p
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                               wintypes.WPARAM, wintypes.LPARAM]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t

    def attach(self, hwnd):
        if not IS_WIN or not hwnd or hwnd in self._hooks:
            return
        old = [None]

        def proc(h, msg, wp, lp):
            if msg == self.WM_SIZING and lp:
                try:
                    self._fix(h, wp, lp)
                    return 1
                except Exception:
                    pass
            return user32.CallWindowProcW(old[0], h, msg, wp, lp)

        cb = self.WNDPROC(proc)
        self._hooks[hwnd] = (cb, old)   # keep the callback alive
        old[0] = self._set(hwnd, -4, ctypes.cast(cb, ctypes.c_void_p))
        if not old[0]:
            del self._hooks[hwnd]

    def _fix(self, h, edge, lp):
        r = RECT.from_address(lp)
        wr, cr = RECT(), RECT()
        user32.GetWindowRect(h, ctypes.byref(wr))
        user32.GetClientRect(h, ctypes.byref(cr))
        bw = (wr.right - wr.left) - (cr.right - cr.left)
        bh = (wr.bottom - wr.top) - (cr.bottom - cr.top) + self.extra_h
        if edge in (3, 6):          # top / bottom edge: width follows height
            ih = max(r.bottom - r.top - bh, 150)
            r.right = r.left + bw + int(round(ih * self.ratio))
        else:                       # sides / corners: height follows width
            iw = max(r.right - r.left - bw, 200)
            if edge in (1, 4, 7):
                r.left = r.right - bw - iw
            else:
                r.right = r.left + bw + iw
            ih = int(round(iw / self.ratio))
            if edge in (4, 5):
                r.top = r.bottom - bh - ih
            else:
                r.bottom = r.top + bh + ih


class Hotkey:
    """Edge-triggered global key press."""
    def __init__(self, vk):
        self.vk, self.prev = vk, True   # True: ignore a key already held at creation

    def pressed(self):
        now = key_down(self.vk)
        hit = now and not self.prev
        self.prev = now
        return hit


class OneEuro:
    """One Euro filter: smooth when still, responsive when moving fast."""
    def __init__(self, mincutoff=1.0, beta=0.0, dcutoff=1.0):
        self.mincutoff, self.beta, self.dcutoff = mincutoff, beta, dcutoff
        self.reset()

    def reset(self):
        self.x_prev, self.dx_prev, self.t_prev = None, 0.0, None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, t):
        if self.x_prev is None:
            self.x_prev, self.t_prev = x, t
            return x
        dt = max(t - self.t_prev, 1e-3)
        a_d = self._alpha(self.dcutoff, dt)
        dx = a_d * (x - self.x_prev) / dt + (1 - a_d) * self.dx_prev
        a = self._alpha(self.mincutoff + self.beta * abs(dx), dt)
        x_hat = a * x + (1 - a) * self.x_prev
        self.x_prev, self.dx_prev, self.t_prev = x_hat, dx, t
        return x_hat


# ---------------- settings file ----------------
def load_settings():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            if k == "keys" and isinstance(v, dict):
                for a in cfg["keys"]:
                    if isinstance(v.get(a), dict) and "vk" in v[a] and "name" in v[a]:
                        cfg["keys"][a] = v[a]
            elif k in cfg:
                cfg[k] = v
    except Exception:
        pass
    if cfg["track_point"] not in TRACK_POINTS:
        cfg["track_point"] = DEFAULTS["track_point"]
    if cfg["corner"] not in CORNERS:
        cfg["corner"] = DEFAULTS["corner"]
    if cfg["area_mode"] not in AREA_MODES:
        cfg["area_mode"] = DEFAULTS["area_mode"]
    return cfg


def save_settings(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ---------------- glove colour ----------------
PALM_IDX = (0, 1, 5, 9, 13, 17)


def palm_pixels(hsv, hull):
    """HSV pixels inside a hand's palm (shrunk a bit to avoid background)."""
    H, W = hsv.shape[:2]
    x, y, bw, bh = cv2.boundingRect(hull)
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + bw, W), min(y + bh, H)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return np.empty((0, 3), np.uint8)
    roi = hsv[y0:y1, x0:x1]
    mask = np.zeros(roi.shape[:2], np.uint8)
    cv2.fillConvexPoly(mask, (hull - (x0, y0)).astype(np.int32), 255)
    k = max(3, int(min(bw, bh) * 0.08))     # erosion scales with hand size (works at distance)
    mask = cv2.erode(mask, np.ones((k, k), np.uint8))
    return roi[mask > 0]


def calibrate(px):
    h, s, v = (px[:, i].astype(np.float32) for i in range(3))
    ang = h * (2 * math.pi / 180.0)  # OpenCV hue is 0-180 -> circular mean
    h_mean = (math.degrees(math.atan2(np.sin(ang).mean(), np.cos(ang).mean())) / 2.0) % 180
    s_med, v_med = float(np.median(s)), float(np.median(v))
    dh = np.abs(h - h_mean)
    dh = np.minimum(dh, 180 - dh)
    return {
        "h": float(h_mean), "s": s_med, "v": v_med,
        "h_tol": float(np.clip(np.percentile(dh, 90) * 1.5, 10, 25)),
        "s_tol": float(max(40, np.percentile(np.abs(s - s_med), 90) * 1.5)),
        "v_tol": float(max(50, np.percentile(np.abs(v - v_med), 90) * 1.5)),
        # white/grey gloves: hue is meaningless; black gloves: hue AND saturation are noise
        "use_hue": bool(s_med >= 50 and v_med >= 60),
        "use_sat": bool(v_med >= 60),
    }


def glove_score(px, cal):
    if len(px) < 20:
        return 0.0
    h, s, v = (px[:, i].astype(np.float32) for i in range(3))
    ok = np.abs(v - cal["v"]) <= cal["v_tol"]
    if cal["use_sat"]:
        ok &= np.abs(s - cal["s"]) <= cal["s_tol"]
    if cal["use_hue"]:
        dh = np.abs(h - cal["h"])
        ok &= np.minimum(dh, 180 - dh) <= cal["h_tol"]
    return float(ok.mean())


class Hand:
    def __init__(self, landmarks, w, h):
        self.lms = landmarks
        self.lm = lm = landmarks.landmark
        self.w, self.h = w, h
        pts = np.array([(lm[i].x * w, lm[i].y * h) for i in PALM_IDX], np.float32)
        self.center = pts.mean(axis=0)
        self.hull = cv2.convexHull(pts.astype(np.int32))
        self.size = math.dist(self.px(0), self.px(9)) + 1e-6   # wrist -> middle knuckle
        self.score = None

    def px(self, i):
        return (self.lm[i].x * self.w, self.lm[i].y * self.h)

    def point(self, idxs):
        return np.mean([self.px(i) for i in idxs], axis=0)

    def pinch(self):
        return math.dist(self.px(4), self.px(8)) / self.size


def open_camera(index, long_range):
    backend = cv2.CAP_DSHOW if IS_WIN else cv2.CAP_ANY
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return None
    if long_range:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    else:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    return cap


GREEN, RED, GREY, ORANGE, CYAN, YELLOW = (0, 220, 0), (60, 60, 240), (200, 200, 200), (0, 165, 255), (255, 220, 0), (0, 230, 230)


# ---------------- tracking thread ----------------
class Tracker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.lock = threading.Lock()
        self.out, self.out_id = None, 0
        self.error = None
        self.quit = False
        self.req_start = self.req_stop = False
        self.mode = "idle"            # idle | calib | run
        self.flash = ("", 0.0)
        self.fx, self.fy = OneEuro(1.0, SMOOTH_BETA), OneEuro(1.0, SMOOTH_BETA)
        self.cal = None
        self.lock_pos = self.lock_size = None
        self.lock_t = 0.0
        self.box_center = self.box_size = None
        self.strangers = []
        self.calib_t0, self.calib_px, self.calib_last = 0.0, [], None
        self.last_pinch = None
        self.mp_hands = mp.solutions.hands
        self.mp_draw = mp.solutions.drawing_utils
        self.dim_pt = self.mp_draw.DrawingSpec(color=(150, 150, 150), thickness=1, circle_radius=1)
        self.dim_ln = self.mp_draw.DrawingSpec(color=(110, 110, 110), thickness=1)
        self._reset_control()

    def _reset_control(self):
        self.pinching = self.dragging = False
        self.pinch_t0, self.anchor, self.cursor = 0.0, (0, 0), None
        self.engaged, self.seen, self.lost_t = False, 0, None

    def release(self):
        if self.dragging:
            mouse_up()
        self.pinching = self.dragging = False

    def set_flash(self, text, secs=2.5):
        self.flash = (text, time.monotonic() + secs)

    def run(self):
        try:
            self._loop()
        except Exception:
            self.error = traceback.format_exc()
        finally:
            try:
                self.release()
            except Exception:
                pass

    def _publish(self, frame, status, color, info=""):
        text, until = self.flash
        with self.lock:
            self.out = (frame, status, color, info, text if time.monotonic() < until else "")
            self.out_id += 1

    def _loop(self):
        cap = hands = None
        cam_key = model_key = None
        fails = 0
        try:
            while not self.quit:
                cfg = self.app.cfg
                ck = (int(cfg["camera"]), bool(cfg["long_range"]))
                if ck != cam_key:
                    if cap is not None:
                        cap.release()
                    cap, cam_key = open_camera(*ck), None
                    if cap is None:
                        self._publish(np.zeros((480, 640, 3), np.uint8),
                                      f"Can't open camera {ck[0]}. Close other apps using it, "
                                      f"or pick another camera in Settings.", RED)
                        time.sleep(1.0)
                        continue
                    cam_key, fails = ck, 0
                if ck[1] != model_key:
                    if hands is not None:
                        hands.close()
                    hands = self.mp_hands.Hands(max_num_hands=4, model_complexity=1 if ck[1] else 0,
                                                min_detection_confidence=0.5 if ck[1] else 0.6,
                                                min_tracking_confidence=0.5)
                    model_key = ck[1]
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails > 60:
                        cap.release()
                        cap, cam_key = None, None
                    time.sleep(0.02)
                    continue
                fails = 0
                self._process(cv2.flip(frame, 1), hands, cfg)
        finally:
            if cap is not None:
                cap.release()
            if hands is not None:
                hands.close()

    # ----- per frame -----
    def _process(self, frame, hands, cfg):
        now = time.monotonic()
        h, w = frame.shape[:2]
        start_key = cfg["keys"]["start"]["name"]

        if self.req_stop:
            self.req_stop = False
            self.release()
            self._reset_control()
            self.mode = "idle"
        if self.req_start:
            self.req_start = False
            self.release()
            self._reset_control()
            self.mode = "calib"
            self.calib_t0, self.calib_px, self.calib_last = now, [], None

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        res = hands.process(rgb)
        hs = [Hand(l, w, h) for l in (res.multi_hand_landmarks or [])]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV) if hs else None
        if self.cal is not None:
            for hd in hs:
                hd.score = glove_score(palm_pixels(hsv, hd.hull), self.cal)

        target, info = None, ""
        if self.mode == "idle":
            status, color = f"Press {start_key} to start capture", GREY
        elif self.mode == "calib":
            target = max(hs, key=lambda x: x.size) if hs else None   # closest hand
            el = now - self.calib_t0
            color = CYAN
            if el < CALIB_PREP:
                status = f"Calibrating in {math.ceil(CALIB_PREP - el)}... hold your gloved hand open, palm to camera"
            else:
                status = "Reading glove colour... keep still"
                if target is not None:
                    self.calib_px.append(palm_pixels(hsv, target.hull))
                    self.calib_last = target
            if el >= CALIB_PREP + CALIB_SAMPLE:
                px = np.concatenate(self.calib_px) if self.calib_px else np.empty((0, 3), np.uint8)
                if len(px) >= 200 and self.calib_last is not None:
                    t = self.calib_last
                    self.cal = calibrate(px)
                    self.lock_pos, self.lock_size, self.lock_t = t.center.copy(), t.size, now
                    self.box_center, self.box_size = t.center.copy(), t.size
                    self.strangers = []
                    self.fx.reset()
                    self.fy.reset()
                    self.mode = "run"
                    many = len(hs) > 1 and cfg["user_tracking"]
                    self.set_flash("Calibrated! Locked onto the closest hand." if many
                                   else "Glove calibrated - you're in control!")
                else:
                    self.calib_t0, self.calib_px, self.calib_last = now, [], None
                    self.set_flash("No hand seen - trying again")
        else:
            target = self._pick_target(hs, cfg, now)
            status, color = self._control(target, cfg, now, w, h, start_key)
            parts = []
            if target is not None and target.score is not None:
                parts.append(f"glove {target.score:.0%}")
            if target is not None and self.last_pinch is not None:
                parts.append(f"pinch {self.last_pinch:.2f}")
            if not cfg["glove_required"]:
                parts.append("glove not required")
            if cfg["user_tracking"]:
                parts.append("user lock on")
            info = "   ".join(parts)

        # ----- drawing -----
        for hd in hs:
            if hd is target:
                self.mp_draw.draw_landmarks(frame, hd.lms, self.mp_hands.HAND_CONNECTIONS)
            else:
                self.mp_draw.draw_landmarks(frame, hd.lms, self.mp_hands.HAND_CONNECTIONS,
                                            self.dim_pt, self.dim_ln)
        if target is not None:
            cv2.polylines(frame, [target.hull], True, GREEN if self.mode == "run" else CYAN, 2)
        if self.mode == "run":
            x0, y0, x1, y1 = self.active_box(cfg, w, h)
            cv2.rectangle(frame, (int(x0), int(y0)), (int(x1), int(y1)), (190, 190, 190), 1)
            if target is not None:
                p = target.point(TRACK_POINTS[cfg["track_point"]])
                cv2.circle(frame, (int(p[0]), int(p[1])), 8, YELLOW, 2)
            elif cfg["user_tracking"] and self.lock_pos is not None:
                c = (int(self.lock_pos[0]), int(self.lock_pos[1]))
                cv2.circle(frame, c, int(self._lock_radius(now)), ORANGE, 2)
        self._publish(frame, status, color, info)

    def _lock_radius(self, now):
        return self.lock_size * (1.5 + 2.0 * min(now - self.lock_t, 3.0))

    def _is_stranger(self, c, now):
        return any(np.linalg.norm(c.center - pos) < 1.5 * size
                   for pos, size, t in self.strangers if now - t < 1.0)

    def _pick_target(self, hs, cfg, now):
        if cfg["glove_required"]:
            cands = [x for x in hs if x.score is not None and x.score >= GLOVE_MATCH]
        else:
            cands = hs
        best = None
        if not cfg["user_tracking"] or self.lock_pos is None:
            if cands:
                best = max(cands, key=lambda x: x.size)   # closest hand
            self.strangers = []
        else:
            if cands:
                dist = lambda x: np.linalg.norm(x.center - self.lock_pos)
                near = min(cands, key=dist)
                if now - self.lock_t < 0.15 and dist(near) <= 1.5 * self.lock_size:
                    best = near          # continuous tracking frame to frame
                else:                    # re-acquiring: never take a hand seen as someone else's
                    fresh = [x for x in cands if not self._is_stranger(x, now)]
                    if fresh and dist(min(fresh, key=dist)) <= self._lock_radius(now):
                        best = min(fresh, key=dist)
            # remember every other glove as a "stranger" (kept for 1 s after last seen)
            others = [(x.center.copy(), x.size, now) for x in cands if x is not best]
            kept = [t for t in self.strangers if now - t[2] < 1.0
                    and all(np.linalg.norm(t[0] - o[0]) >= 1.5 * t[1] for o in others)]
            self.strangers = others + kept
        if best is not None:
            self.lock_pos = best.center.copy()
            self.lock_size = 0.8 * self.lock_size + 0.2 * best.size if self.lock_size else best.size
            self.lock_t = now
        return best

    def active_box(self, cfg, w, h):
        m = cfg["movement"]
        if cfg["area_mode"] == AREA_MODES[1] and self.box_center is not None:
            bw = self.box_size * (2 + (m - 1) * 6 / 9)
            bh = bw * SCREEN_H / SCREEN_W
            cx, cy = self.box_center
        else:
            a = 0.3 + (m - 1) * 0.6 / 9
            bw, bh, cx, cy = w * a, h * a, w / 2, h / 2
        return cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2

    def _control(self, target, cfg, now, w, h, start_key):
        if target is not None:
            self.lost_t = None
            self.seen += 1
        else:
            self.seen = 0
            if self.lost_t is None:
                self.lost_t = now

        if target is not None and (self.engaged or self.seen >= ENGAGE_FRAMES):
            self.engaged = True
            self._move(target, cfg, now, w, h)
            if self.dragging:
                return "DRAGGING", GREEN
            return ("PINCH" if self.pinching else "Control ON"), GREEN

        if self.engaged and now - self.lost_t > LOST_GRACE:
            self.release()
            self.engaged = False
            self.fx.reset()
            self.fy.reset()
            self.cursor = None
        if target is not None:
            return "Hand found...", YELLOW
        if cfg["user_tracking"] and now - self.lock_t > 0.5:
            return f"Lost you - bring your glove back to the orange circle ({start_key} to recalibrate)", ORANGE
        return ("No glove detected", RED) if cfg["glove_required"] else ("No hand", GREY)

    def _move(self, t, cfg, now, w, h):
        px, py = t.point(TRACK_POINTS[cfg["track_point"]])
        x0, y0, x1, y1 = self.active_box(cfg, w, h)
        nx = min(max((px - x0) / (x1 - x0), 0.0), 1.0)
        ny = min(max((py - y0) / (y1 - y0), 0.0), 1.0)
        cut = 3.0 * 0.1 ** ((cfg["smoothing"] - 1) / 9)
        self.fx.mincutoff = self.fy.mincutoff = cut
        sx, sy = self.fx(nx * (SCREEN_W - 1), now), self.fy(ny * (SCREEN_H - 1), now)

        pinch_on = 0.18 + (cfg["pinch"] - 1) * 0.24 / 9
        pinch_off = pinch_on + 0.15
        ratio = self.last_pinch = t.pinch()

        if not self.pinching and ratio < pinch_on:
            self.pinching, self.dragging, self.pinch_t0 = True, False, now
            self.anchor = self.cursor if self.cursor else (sx, sy)
        elif self.pinching and ratio > pinch_off:
            if self.dragging:
                mouse_up()
            else:   # short pinch -> click where you were aiming
                set_cursor(*self.anchor)
                mouse_down()
                mouse_up()
            self.pinching = self.dragging = False

        if self.pinching and not self.dragging and (
                now - self.pinch_t0 > CLICK_MAX_TIME or math.dist(self.anchor, (sx, sy)) > DRAG_MOVE_PX):
            set_cursor(*self.anchor)
            mouse_down()
            self.dragging = True

        self.cursor = self.anchor if (self.pinching and not self.dragging) else (sx, sy)
        set_cursor(*self.cursor)


# ---------------- drawing helpers for the preview ----------------
def draw_text_block(img, text, x, y, color, scale, max_w):
    """Word-wrapped outlined text; returns the y after the block."""
    if not text:
        return y
    font, th = cv2.FONT_HERSHEY_SIMPLEX, 1
    line_h = int(cv2.getTextSize("Ag", font, scale, th)[0][1] * 1.8) + 2
    words, line = text.split(), ""
    lines = []
    for wd in words:
        test = (line + " " + wd).strip()
        if cv2.getTextSize(test, font, scale, th)[0][0] > max_w and line:
            lines.append(line)
            line = wd
        else:
            line = test
    lines.append(line)
    for ln in lines:
        y += line_h
        cv2.putText(img, ln, (x, y), font, scale, (0, 0, 0), th + 2, cv2.LINE_AA)
        cv2.putText(img, ln, (x, y), font, scale, color, th, cv2.LINE_AA)
    return y


# ---------------- settings window ----------------
class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        cfg = json.loads(json.dumps(app.cfg))
        self.keys = cfg["keys"]
        self.capturing = None
        self.title(f"{APP} - Settings")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        # --- shortcuts ---
        f = ttk.LabelFrame(body, text="Keyboard shortcuts (work from any app)", padding=8)
        f.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        f.columnconfigure(0, weight=1)
        self.key_btns = {}
        for i, (act, label) in enumerate(ACTIONS):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=2)
            b = ttk.Button(f, width=18, text=self.keys[act]["name"],
                           command=lambda a=act: self.capture_key(a))
            b.grid(row=i, column=1, sticky="e", pady=2)
            self.key_btns[act] = b
        self._hint(f, "Click a button, then press the new key (Esc cancels). F-keys work best:\n"
                      "letter keys would also trigger while you type.", 4)

        # --- tracking ---
        f = ttk.LabelFrame(body, text="Tracking", padding=8)
        f.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        f.columnconfigure(1, weight=1)
        self.v_point = tk.StringVar(value=cfg["track_point"])
        self.v_glove = tk.BooleanVar(value=cfg["glove_required"])
        self.v_user = tk.BooleanVar(value=cfg["user_tracking"])
        self.v_area = tk.StringVar(value=cfg["area_mode"])
        self.v_long = tk.BooleanVar(value=cfg["long_range"])
        self.v_cam = tk.IntVar(value=cfg["camera"])
        ttk.Label(f, text="Point that moves the cursor").grid(row=0, column=0, sticky="w")
        ttk.Combobox(f, textvariable=self.v_point, values=list(TRACK_POINTS), state="readonly",
                     width=28).grid(row=0, column=1, sticky="e", pady=2)
        ttk.Label(f, text="Movement area").grid(row=1, column=0, sticky="w")
        ttk.Combobox(f, textvariable=self.v_area, values=AREA_MODES, state="readonly",
                     width=28).grid(row=1, column=1, sticky="e", pady=2)
        ttk.Checkbutton(f, text="Require a glove (off = any hand controls the mouse)",
                        variable=self.v_glove).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(f, text="User tracking: only follow the hand that calibrated",
                        variable=self.v_user).grid(row=3, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(f, text="Long-range mode (sharper camera + stronger model, uses more CPU)",
                        variable=self.v_long).grid(row=4, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(f, text="Camera number").grid(row=5, column=0, sticky="w")
        ttk.Spinbox(f, from_=0, to=9, textvariable=self.v_cam, width=5,
                    state="readonly").grid(row=5, column=1, sticky="e", pady=2)

        # --- preview ---
        f = ttk.LabelFrame(body, text="Preview while capturing", padding=8)
        f.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        f.columnconfigure(1, weight=1)
        self.v_corner = tk.StringVar(value=cfg["corner"])
        self.v_size = tk.IntVar(value=cfg["mini_width"])
        ttk.Label(f, text="Position").grid(row=0, column=0, sticky="w")
        ttk.Combobox(f, textvariable=self.v_corner, values=CORNERS, state="readonly",
                     width=28).grid(row=0, column=1, sticky="e", pady=2)
        ttk.Label(f, text="Width (pixels)").grid(row=1, column=0, sticky="w")
        tk.Scale(f, variable=self.v_size, from_=200, to=640, resolution=20, orient="horizontal",
                 length=220).grid(row=1, column=1, sticky="e")
        self._hint(f, "Floater: drag the preview with the mouse to place it anywhere.", 2)

        # --- sensitivity ---
        f = ttk.LabelFrame(body, text="Sensitivity", padding=8)
        f.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        f.columnconfigure(1, weight=1)
        self.v_move = tk.IntVar(value=cfg["movement"])
        self.v_pinch = tk.IntVar(value=cfg["pinch"])
        self.v_smooth = tk.IntVar(value=cfg["smoothing"])
        for i, (lbl, var, hint) in enumerate([
                ("Hand movement range", self.v_move, "1 = tiny moves, 10 = big arm moves"),
                ("Pinch sensitivity", self.v_pinch, "higher = pinch triggers more easily"),
                ("Cursor smoothing", self.v_smooth, "higher = steadier, slightly more lag")]):
            ttk.Label(f, text=lbl).grid(row=i * 2, column=0, sticky="w")
            tk.Scale(f, variable=var, from_=1, to=10, resolution=1, orient="horizontal",
                     length=220).grid(row=i * 2, column=1, sticky="e")
            self._hint(f, hint, i * 2 + 1)

        # --- buttons ---
        bf = ttk.Frame(body)
        bf.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(bf, text="Reset to defaults", command=self.reset).pack(side="left")
        ttk.Button(bf, text="Save", command=self.save).pack(side="right")
        ttk.Button(bf, text="Cancel", command=self.close).pack(side="right", padx=6)

        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - self.winfo_width()) // 2}+{max((sh - self.winfo_height()) // 2, 0)}")
        self.lift()
        self.focus_force()

    @staticmethod
    def _hint(parent, text, row):
        ttk.Label(parent, text=text, foreground="gray").grid(row=row, column=0, columnspan=2,
                                                             sticky="w", pady=(0, 4))

    def capture_key(self, act):
        if self.capturing:
            self.key_btns[self.capturing].configure(text=self.keys[self.capturing]["name"])
        self.capturing = act
        self.key_btns[act].configure(text="Press a key...")
        self.focus_set()
        self.bind("<KeyPress>", self.on_key)

    def on_key(self, e):
        act = self.capturing
        if not act or e.keysym in MODIFIER_KEYS:
            return "break"
        self.unbind("<KeyPress>")
        self.capturing = None
        if e.keysym != "Escape":
            name = e.keysym.upper() if len(e.keysym) == 1 else e.keysym
            self.keys[act] = {"vk": int(e.keycode), "name": name}   # Tk keycode = Windows VK code
        self.key_btns[act].configure(text=self.keys[act]["name"])
        return "break"

    def reset(self):
        d = json.loads(json.dumps(DEFAULTS))
        self.keys = d["keys"]
        for act, b in self.key_btns.items():
            b.configure(text=self.keys[act]["name"])
        for var, key in [(self.v_point, "track_point"), (self.v_glove, "glove_required"),
                         (self.v_user, "user_tracking"), (self.v_area, "area_mode"),
                         (self.v_long, "long_range"), (self.v_cam, "camera"),
                         (self.v_corner, "corner"), (self.v_size, "mini_width"),
                         (self.v_move, "movement"), (self.v_pinch, "pinch"),
                         (self.v_smooth, "smoothing")]:
            var.set(d[key])

    def save(self):
        vks = [self.keys[a]["vk"] for a, _ in ACTIONS]
        if len(set(vks)) != len(vks):
            messagebox.showerror("Duplicate shortcut", "Each action needs its own key.", parent=self)
            return
        new = dict(self.app.cfg)
        new.update(keys=self.keys, track_point=self.v_point.get(), glove_required=self.v_glove.get(),
                   user_tracking=self.v_user.get(), area_mode=self.v_area.get(),
                   long_range=self.v_long.get(), camera=int(self.v_cam.get()),
                   corner=self.v_corner.get(), mini_width=int(self.v_size.get()),
                   movement=int(self.v_move.get()), pinch=int(self.v_pinch.get()),
                   smoothing=int(self.v_smooth.get()))
        self.app.apply_settings(new)
        self.close()

    def close(self):
        self.app.settings_win = None
        self.destroy()


# ---------------- main window ----------------
class App:
    def __init__(self):
        self.cfg = load_settings()
        self.root = tk.Tk()
        self.root.title(f"{APP} {VERSION}")
        self.root.configure(bg="black")
        try:
            ttk.Style().theme_use("vista" if IS_WIN else "clam")
        except Exception:
            pass
        self.aspect = AspectLock()
        self.capturing = self.mini = False
        self.settings_win = None
        self.photo, self.last_id, self.saved_geom, self.drag_off = None, -1, None, None
        self.topmost_tick = 0.0

        self.bar = ttk.Frame(self.root, padding=(6, 4))
        self.bar.pack(side="bottom", fill="x")
        self.video = tk.Frame(self.root, bg="black", width=640, height=480)
        self.video.pack(side="top", fill="both", expand=True)
        self.label = tk.Label(self.video, bg="black", bd=0, highlightthickness=0)
        self.label.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.label.bind("<ButtonPress-1>", self.drag_start)
        self.label.bind("<B1-Motion>", self.drag_move)
        self.label.bind("<ButtonRelease-1>", self.drag_end)

        self.btns = {}
        for i, (act, _) in enumerate(ACTIONS):
            b = ttk.Button(self.bar, command=lambda a=act: self.do_action(a))
            b.grid(row=0, column=i, sticky="ew", padx=3)
            self.bar.columnconfigure(i, weight=1, uniform="b")
            self.btns[act] = b
        ttk.Label(self.bar, foreground="gray",
                  text="Processed locally on this PC - no video is ever recorded, saved or sent."
                  ).grid(row=1, column=0, columnspan=len(ACTIONS), pady=(4, 0))
        self.update_buttons()
        self.make_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.root.update_idletasks()
        self.bar_h = self.bar.winfo_reqheight()
        self.root.minsize(560, 420 + self.bar_h)
        self.root.geometry(f"640x{480 + self.bar_h}")

        self.tracker = Tracker(self)
        self.tracker.start()
        self.root.after(300, self.setup_hwnd)
        self.root.after(15, self.render)
        self.root.after(30, self.poll_keys)

    # ----- helpers -----
    def make_hotkeys(self):
        self.hotkeys = {a: Hotkey(self.cfg["keys"][a]["vk"]) for a, _ in ACTIONS}

    def update_buttons(self):
        k = self.cfg["keys"]
        self.btns["start"].configure(
            text=f"{'Pause' if self.capturing else 'Start'} capture ({k['start']['name']})")
        self.btns["glove"].configure(
            text=f"Glove: {'ON' if self.cfg['glove_required'] else 'OFF'} ({k['glove']['name']})")
        self.btns["settings"].configure(text=f"Settings ({k['settings']['name']})")
        self.btns["quit"].configure(text=f"Quit ({k['quit']['name']})")

    def setup_hwnd(self):
        if not IS_WIN or self.mini:
            return
        try:
            self.root.update_idletasks()
            hwnd = int(self.root.wm_frame(), 16)
            self.bar_h = self.bar.winfo_height() or self.bar_h
            self.aspect.extra_h = self.bar_h
            self.aspect.attach(hwnd)
            no_maximize(hwnd)
        except Exception:
            pass

    # ----- actions -----
    def do_action(self, act):
        if act == "start":
            self.toggle_capture()
        elif act == "glove":
            self.cfg = {**self.cfg, "glove_required": not self.cfg["glove_required"]}
            save_settings(self.cfg)
            self.update_buttons()
            self.tracker.set_flash("Glove required" if self.cfg["glove_required"]
                                   else "Glove NOT required - any hand controls the mouse")
        elif act == "settings":
            if self.settings_win is None:
                self.settings_win = SettingsDialog(self)
            else:
                self.settings_win.lift()
        elif act == "quit":
            self.quit()

    def toggle_capture(self):
        self.capturing = not self.capturing
        if self.capturing:
            self.tracker.req_start = True
            self.enter_mini()
        else:
            self.tracker.req_stop = True
            self.exit_mini()
        self.update_buttons()

    def apply_settings(self, new):
        self.cfg = new
        save_settings(new)
        self.make_hotkeys()
        self.update_buttons()
        if self.mini:
            self.place_mini()

    # ----- window modes -----
    def enter_mini(self):
        if self.mini:
            return
        self.saved_geom = self.root.geometry()
        self.mini = True
        self.root.withdraw()
        self.bar.pack_forget()
        self.root.overrideredirect(True)
        self.root.minsize(1, 1)
        self.place_mini()
        self.root.deiconify()
        self.root.attributes("-topmost", True)

    def exit_mini(self):
        if not self.mini:
            return
        self.mini = False
        self.root.withdraw()
        self.root.overrideredirect(False)
        self.root.attributes("-topmost", False)
        self.label.configure(cursor="")
        self.bar.pack(side="bottom", fill="x", before=self.video)
        self.root.minsize(560, int(560 / self.aspect.ratio) + self.bar_h)
        if self.saved_geom:
            self.root.geometry(self.saved_geom)
        self.root.deiconify()
        self.root.after(100, self.setup_hwnd)

    def place_mini(self):
        w = int(self.cfg["mini_width"])
        h = int(round(w / self.aspect.ratio))
        corner = self.cfg["corner"]
        left, top, right, bottom = work_area()
        m = 12
        if corner == "Floater":
            vx0, vy0, vx1, vy1 = virtual_screen()
            x = self.cfg.get("float_x")
            y = self.cfg.get("float_y")
            if x is None or y is None:
                x, y = left + m, bottom - h - m
            x = min(max(int(x), vx0), vx1 - w)
            y = min(max(int(y), vy0), vy1 - h)
            self.label.configure(cursor="fleur")
        else:
            x = left + m if "left" in corner else right - w - m
            y = top + m if "Top" in corner else bottom - h - m
            self.label.configure(cursor="")
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def on_ratio_change(self):
        r = self.aspect.ratio
        if self.mini:
            self.place_mini()
        else:
            self.root.minsize(560, int(560 / r) + self.bar_h)
            w = max(self.root.winfo_width(), 560)
            self.root.geometry(f"{w}x{int(round(w / r)) + self.bar_h}")

    # ----- floater dragging -----
    def drag_start(self, e):
        if self.mini and self.cfg["corner"] == "Floater":
            self.drag_off = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        if self.drag_off:
            self.root.geometry(f"+{e.x_root - self.drag_off[0]}+{e.y_root - self.drag_off[1]}")

    def drag_end(self, e):
        if self.drag_off:
            self.drag_off = None
            self.cfg = {**self.cfg, "float_x": self.root.winfo_x(), "float_y": self.root.winfo_y()}
            save_settings(self.cfg)

    # ----- loops -----
    def poll_keys(self):
        hits = {a: hk.pressed() for a, hk in self.hotkeys.items()}
        if self.settings_win is None:   # paused while editing shortcuts
            for a in ("quit", "start", "glove", "settings"):
                if hits.get(a):
                    self.do_action(a)
                    if a == "quit":
                        return
        now = time.monotonic()
        if self.mini and now - self.topmost_tick > 1.0:
            self.topmost_tick = now
            self.root.attributes("-topmost", True)
        self.root.after(30, self.poll_keys)

    def render(self):
        tr = self.tracker
        if tr.error:
            self.crash(tr.error)
            return
        with tr.lock:
            out, oid = tr.out, tr.out_id
        if out is not None and oid != self.last_id:
            self.last_id = oid
            frame, status, color, info, flash = out
            fh, fw = frame.shape[:2]
            if abs(fw / fh - self.aspect.ratio) > 0.01:
                self.aspect.ratio = fw / fh
                self.on_ratio_change()
            dw, dh = self.video.winfo_width(), self.video.winfo_height()
            if dw > 20 and dh > 20:
                s = min(dw / fw, dh / fh)
                nw, nh = max(1, int(fw * s)), max(1, int(fh * s))
                img = cv2.resize(frame, (nw, nh),
                                 interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
                scale = min(max(nw / 1000, 0.38), 0.75)
                y = draw_text_block(img, status, 8, 0, color, scale, nw - 16)
                draw_text_block(img, flash, 8, y + 2, CYAN, scale, nw - 16)
                if info and nw >= 400:
                    cv2.putText(img, info, (8, nh - 10), cv2.FONT_HERSHEY_SIMPLEX, scale * 0.85,
                                (0, 0, 0), 3, cv2.LINE_AA)
                    cv2.putText(img, info, (8, nh - 10), cv2.FONT_HERSHEY_SIMPLEX, scale * 0.85,
                                (230, 230, 230), 1, cv2.LINE_AA)
                self.photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
                self.label.configure(image=self.photo)
        self.root.after(15, self.render)

    def crash(self, err):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(os.path.join(CONFIG_DIR, "error.log"), "w") as f:
                f.write(err)
        except Exception:
            pass
        messagebox.showerror(APP, "GloveMouse hit an error:\n\n" + err[-1200:])
        self.quit()

    def quit(self):
        self.tracker.quit = True
        self.tracker.join(timeout=2)
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    app = App()
    app.root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(os.path.join(CONFIG_DIR, "error.log"), "w") as f:
                f.write(err)
        except Exception:
            pass
        message_box("GloveMouse crashed:\n\n" + err[-1500:])
