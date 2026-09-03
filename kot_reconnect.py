"""
kot_reconnect.py - watchdog that clicks through disconnect/reconnect
screens automatically.

REWRITE (session 3): fixes a structural bug in the old version.

    Old version's logic:
        best_match() checks the frame against your CALIBRATED templates.
        If nothing matches -> assumed "connected", keep-alive fires.

        This is wrong. "Nothing matched" does not mean "the game is
        visible". It just means "nothing I have a template for is
        visible". An inactivity popup, an OS dialog, an ad you never
        calibrated, a new BlueStacks nag screen - none of these match
        any template, so the old code silently treats them as normal
        gameplay and keeps clicking keep-alive INTO them.

    New version's logic - ground truth instead of a template blacklist:
        A small ANCHOR template (a HUD element that is on screen only
        during real gameplay - e.g. a corner icon, gold counter, pause
        button) is checked every poll.

        - anchor visible                 -> genuinely connected.
                                             keep-alive allowed to fire.
        - anchor missing + known dialog  -> handled exactly like before
                                             (click the calibrated
                                             reconnect/ad button).
        - anchor missing + NOTHING known -> UNKNOWN interruption. Do
                                             NOT assume connected. After
                                             a short grace period (the
                                             screen might just be
                                             transitioning), run generic
                                             recovery: ESC, Enter, and
                                             blind clicks at the usual
                                             dialog-button screen
                                             positions, re-checking the
                                             anchor after each attempt.
                                             If that fails repeatedly,
                                             escalate to a full
                                             restart_cycle instead of
                                             hanging forever.

    Everything else (resolution-independent capture/template scaling,
    the calibration tool, restart-on-timeout, close-confirmation
    handling) is carried over from the previous version.

Examples:

    python kot_reconnect.py --title BlueStacks --calibrate
    python kot_reconnect.py --title BlueStacks --calibrate-anchor
    python kot_reconnect.py --title BlueStacks --keepalive
    python kot_reconnect.py --title BlueStacks --dry-run --verbose
"""

import argparse
import ctypes
import json
import os
import random
import subprocess
import time
from ctypes import wintypes

import cv2
import keyboard
import mss
import numpy as np
import win32api
import win32con
import win32gui


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WINDOW_TITLE = "Android Device"

GAME_W = 1280
GAME_H = 720

TEMPLATE_DIR = "reconnect_templates"
ANCHOR_PATH = os.path.join(TEMPLATE_DIR, "_anchor.png")
ANCHOR_META = os.path.join(TEMPLATE_DIR, "_anchor.json")
KEEPALIVE_POINT_PATH = os.path.join(TEMPLATE_DIR, "_keepalive_point.json")
APP_ICON_POINT_PATH = os.path.join(TEMPLATE_DIR, "_app_icon_point.json")
LOG_FILE = "reconnect_log.txt"

CONFIRM_DIALOG_TITLE = "close bluestacks"

# Hardcoded default relaunch target, so you don't have to pass
# --restart-exe on the command line every time. This is used whenever
# --restart-exe / --restart-cmd are NOT given on the CLI. Leave it as
# "" to fall back to auto-detecting the emulator's own .exe instead.
#
# This can point at either a normal .exe OR a .lnk shortcut. A .lnk
# that opens straight into King of Thieves is ideal - no icon-tap
# fallback needed at all.
DEFAULT_RESTART_EXE = r"C:\Users\divyp\OneDrive\Desktop\King of Thieves-Android Device.lnk"

STOP = False


def request_stop():
    global STOP
    STOP = True
    print("\n  F9 - stopping after the current step...")


def naptime(seconds, step=0.05):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        if STOP:
            return False
        time.sleep(min(step, max(0.0, end - time.perf_counter())))
    return not STOP


# ---------------------------------------------------------------------------
# Windows plumbing (unchanged from previous version)
# ---------------------------------------------------------------------------

def fix_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def find_window(substring):
    matches = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if substring.lower() in title.lower():
            matches.append((hwnd, title))

    win32gui.EnumWindows(cb, None)
    return matches[0] if matches else (None, None)


def find_confirm_dialog(title_substr=CONFIRM_DIALOG_TITLE):
    matches = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title_substr.lower() in title.lower():
            matches.append(hwnd)

    win32gui.EnumWindows(cb, None)
    return matches[0] if matches else None


def get_exe_path(hwnd):
    """
    Resolve the full path of the .exe that owns this window, so
    restart_cycle() can relaunch BlueStacks automatically without the
    user having to pass --restart-exe every time.
    """
    try:
        import win32process
        _, pid = win32process.GetWindowThreadProcessId(hwnd)

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return None

        buf_len = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(260)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(buf_len)
        )
        ctypes.windll.kernel32.CloseHandle(handle)

        return buf.value if ok else None
    except Exception:
        return None


def ensure_restored(hwnd, timeout=10.0):
    """
    GetClientRect() on a minimized window doesn't error - it silently
    returns a tiny placeholder size, which then flows into a garbage
    capture region (a few hundred pixels instead of the real client
    area). That breaks anchor/template matching and turns generic
    recovery's "safe" fractional blind-click positions into essentially
    random real pixels. Force a restore and wait for it before trusting
    GetClientRect at all.
    """
    if not win32gui.IsIconic(hwnd):
        return True

    log("  window is minimized - restoring before measuring it.")
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception as e:
        log(f"  could not restore window: {e}")
        return False

    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if not win32gui.IsIconic(hwnd):
            time.sleep(0.3)
            return True
        time.sleep(0.2)

    return not win32gui.IsIconic(hwnd)


def game_region(hwnd, args=None):
    ensure_restored(hwnd)
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (left, top))
    right, bottom = win32gui.ClientToScreen(hwnd, (right, bottom))

    cw = right - left
    ch = bottom - top

    ox = getattr(args, "region_x", 0) or 0
    oy = getattr(args, "region_y", 0) or 0

    if ox < 0 or oy < 0:
        raise SystemExit(f"Invalid region offset ({ox},{oy}).")

    capture_w = cw - ox
    capture_h = ch - oy

    if capture_w <= 0 or capture_h <= 0:
        raise SystemExit(
            f"Region offset ({ox},{oy}) is outside the "
            f"BlueStacks client area {cw}x{ch}."
        )

    if cw < 300 or ch < 300:
        raise SystemExit(
            f"BlueStacks client area is only {cw}x{ch} - almost certainly "
            f"still minimized/mid-launch rather than a real game window. "
            f"Refusing to build a capture region from this; make sure the "
            f"emulator is visible and fully open, then retry."
        )

    print(f"  BlueStacks client: {cw}x{ch}")
    print(f"  Capture region:    {capture_w}x{capture_h}")
    print(f"  Capture offset:    ({ox},{oy})")

    if (capture_w, capture_h) != (GAME_W, GAME_H):
        print(
            f"  NOTE: capture differs from the {GAME_W}x{GAME_H} "
            f"reference size. Templates auto-scale to {capture_w}x{capture_h}."
        )

    return {
        "left": left + ox,
        "top": top + oy,
        "width": capture_w,
        "height": capture_h,
    }


def focus_window(hwnd):
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"  could not focus the window ({e}); click the emulator once.")
    time.sleep(0.25)


# ---------------------------------------------------------------------------
# Mouse / keyboard input
# ---------------------------------------------------------------------------

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]


MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000


def _send(flags, x=0, y=0):
    inp = INPUT(type=0, mi=MOUSEINPUT(x, y, 0, flags, 0, None))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def click_at(region, gx, gy, hold=0.05):
    sx = region["left"] + gx
    sy = region["top"] + gy

    vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)

    if vw <= 1 or vh <= 1:
        return

    abs_x = int((sx - vx) * 65535 / (vw - 1))
    abs_y = int((sy - vy) * 65535 / (vh - 1))

    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, abs_x, abs_y)
    time.sleep(0.02)
    _send(MOUSEEVENTF_LEFTDOWN)
    time.sleep(hold)
    _send(MOUSEEVENTF_LEFTUP)


def parse_deny_zones(raw_list):
    """
    Parse --deny-zone entries into fractional rectangles.
    Each entry: "x1,y1,x2,y2" as fractions (0.0-1.0) of the capture
    region, e.g. "0.75,0.05,1.0,0.20" blocks the top-right corner
    (a common spot for ad "buy"/"subscribe"/"X" buttons).
    """
    zones = []
    for raw in raw_list or []:
        try:
            x1, y1, x2, y2 = [float(v) for v in raw.split(",")]
        except Exception:
            print(f"  WARNING: could not parse --deny-zone '{raw}' "
                  f"(expected x1,y1,x2,y2 as 0-1 fractions); skipping it.")
            continue
        zones.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    return zones


def point_in_deny_zone(region, gx, gy, deny_zones):
    if not deny_zones:
        return None
    fx = gx / region["width"]
    fy = gy / region["height"]
    for (x1, y1, x2, y2) in deny_zones:
        if x1 <= fx <= x2 and y1 <= fy <= y2:
            return (x1, y1, x2, y2)
    return None


def safe_click_at(region, gx, gy, args, hold=0.05, why=""):
    """
    Denylist-checked click. Every real click in the bot should go
    through this instead of calling click_at() directly, so a single
    denylist protects blind clicks, ad dismissal, dialog dismissal,
    keep-alive, and the post-restart app-icon tap alike. Returns True
    if the click actually fired, False if it was blocked.
    """
    zone = point_in_deny_zone(region, gx, gy, getattr(args, "deny_zones", None))
    if zone is not None:
        log(f"  BLOCKED click at ({gx},{gy}) [{why}] - inside deny-zone "
            f"{zone} (see --deny-zone). Not clicking.")
        return False

    click_at(region, gx, gy, hold=hold)
    return True


def press_key(hwnd, key):
    """Send a key press to the focused window (used by generic recovery)."""
    focus_window(hwnd)
    try:
        keyboard.send(key)
    except Exception as e:
        log(f"  key press '{key}' failed: {e}")


# ---------------------------------------------------------------------------
# Perception
# ---------------------------------------------------------------------------

def grab(sct, box):
    raw = sct.grab(box)
    arr = np.frombuffer(raw.bgra, dtype=np.uint8)
    return arr.reshape(raw.height, raw.width, 4)[:, :, :3]


def thumb(frame):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (80, 45), interpolation=cv2.INTER_AREA)


def scene_motion(a, b):
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def wait_settled(sct, region, quiet=0.4, timeout=5.0):
    prev = thumb(grab(sct, region))
    still_since = None
    t_end = time.perf_counter() + timeout

    while time.perf_counter() < t_end:
        if STOP:
            return False
        time.sleep(0.05)
        cur = thumb(grab(sct, region))
        motion = scene_motion(prev, cur)
        prev = cur

        if motion < 0.5:
            still_since = still_since or time.perf_counter()
            if time.perf_counter() - still_since >= quiet:
                return True
        else:
            still_since = None

    return False


# ---------------------------------------------------------------------------
# Template scaling
# ---------------------------------------------------------------------------

def scale_image(img, sx, sy):
    if img is None:
        return None
    h, w = img.shape[:2]
    new_w = max(1, int(round(w * sx)))
    new_h = max(1, int(round(h * sy)))
    if new_w == w and new_h == h:
        return img
    interp = cv2.INTER_AREA if (new_w < w or new_h < h) else cv2.INTER_LINEAR
    return cv2.resize(img, (new_w, new_h), interpolation=interp)


def prepare_templates(templates, region):
    any_scaled = False
    for t in templates:
        sx = region["width"] / t["ref_w"]
        sy = region["height"] / t["ref_h"]
        if abs(sx - 1.0) > 0.01 or abs(sy - 1.0) > 0.01:
            any_scaled = True
        t["img"] = scale_image(t["base_img"], sx, sy)
        t["click_dx"] = int(round(t["base_click_dx"] * sx))
        t["click_dy"] = int(round(t["base_click_dy"] * sy))

    if any_scaled:
        print(f"  Rescaled {len(templates)} template(s) to match "
              f"{region['width']}x{region['height']}.")

    return templates


def load_templates(folder):
    templates = []
    if not os.path.isdir(folder):
        return templates

    for fn in sorted(os.listdir(folder)):
        if not fn.endswith(".png") or fn.startswith("_"):
            continue

        name = fn[:-4]
        meta_path = os.path.join(folder, name + ".json")
        if not os.path.isfile(meta_path):
            print(f"  skipping {fn}: no matching {name}.json")
            continue

        img = cv2.imread(os.path.join(folder, fn))
        if img is None:
            print(f"  skipping {fn}: could not read it")
            continue

        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)
        except Exception as e:
            print(f"  skipping {fn}: invalid JSON ({e})")
            continue

        ref_w = int(meta.get("ref_w", GAME_W))
        ref_h = int(meta.get("ref_h", GAME_H))

        templates.append({
            "name": name,
            "base_img": img,
            "ref_w": ref_w,
            "ref_h": ref_h,
            "base_click_dx": meta["click_dx"],
            "base_click_dy": meta["click_dy"],
            "thresh": meta.get("thresh", 0.85),
            "fatal": bool(meta.get("fatal", False)),
            "dismiss_only": bool(meta.get("dismiss_only", False)),
            "img": img,
            "click_dx": meta["click_dx"],
            "click_dy": meta["click_dy"],
        })

    return templates


def load_anchor():
    """Load the ground-truth 'this is real gameplay' template, if any."""
    if not (os.path.isfile(ANCHOR_PATH) and os.path.isfile(ANCHOR_META)):
        return None

    img = cv2.imread(ANCHOR_PATH)
    if img is None:
        return None

    with open(ANCHOR_META, "r") as f:
        meta = json.load(f)

    return {
        "base_img": img,
        "ref_w": int(meta.get("ref_w", GAME_W)),
        "ref_h": int(meta.get("ref_h", GAME_H)),
        "thresh": meta.get("thresh", 0.82),
        "img": img,
    }


def prepare_anchor(anchor, region):
    if anchor is None:
        return None
    sx = region["width"] / anchor["ref_w"]
    sy = region["height"] / anchor["ref_h"]
    anchor["img"] = scale_image(anchor["base_img"], sx, sy)
    return anchor


def find_anchor(frame, tmpl, thresh):
    if tmpl is None:
        return None
    if tmpl.shape[0] > frame.shape[0] or tmpl.shape[1] > frame.shape[1]:
        return None

    res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)

    if score < thresh:
        return None
    return (loc[0], loc[1], score)


def best_match(frame, templates):
    best = None
    for t in templates:
        hit = find_anchor(frame, t["img"], t["thresh"])
        if hit is None:
            continue
        if best is None or hit[2] > best[1][2]:
            best = (t, hit)
    return best


def verify_gone(sct, region, tmpl, thresh, secs, need=2):
    gone = 0
    end = time.perf_counter() + secs
    while time.perf_counter() < end:
        if STOP:
            return False
        time.sleep(0.25)
        if find_anchor(grab(sct, region), tmpl, thresh) is None:
            gone += 1
            if gone >= need:
                return True
        else:
            gone = 0
    return False


# ---------------------------------------------------------------------------
# Keepalive
# ---------------------------------------------------------------------------

def keepalive_click(sct, region, hwnd, tmpl, saved_point, args):
    """
    Fire the keep-alive click every time it's due, regardless of whether
    the keep-alive target was actually found on screen. Priority:

        1. keep-alive template matched        -> click its center.
        2. saved keep-alive point exists       -> auto-scaled from the
                                                   resolution it was
                                                   calibrated at (see
                                                   --set-keepalive-point).
                                                   No flags needed.
        3. --keepalive-x/y given on the CLI    -> scaled using
                                                   --keepalive-ref-w/h
                                                   (manual override,
                                                   rarely needed now).
        4. none of the above                   -> region center.

    Always fires a click and never skips - it's a test click as much as
    a keep-alive tap. Returns (True, description, matched_bool).
    """

    frame = grab(sct, region)
    matched = False
    source = None

    if tmpl is not None:
        hit = find_anchor(frame, tmpl, args.keepalive_thresh)
        if hit is not None:
            th, tw = tmpl.shape[:2]
            cx = hit[0] + tw // 2
            cy = hit[1] + th // 2
            matched = True
            source = "template match"

    if not matched:
        if saved_point is not None:
            sx = region["width"] / saved_point["ref_w"]
            sy = region["height"] / saved_point["ref_h"]
            cx = int(round(saved_point["x"] * sx))
            cy = int(round(saved_point["y"] * sy))
            source = "saved keep-alive point (auto-scaled)"
            reason = "no_template_configured"
        elif args.keepalive_x is not None and args.keepalive_y is not None:
            sx = region["width"] / args.keepalive_ref_w
            sy = region["height"] / args.keepalive_ref_h
            cx = int(round(args.keepalive_x * sx))
            cy = int(round(args.keepalive_y * sy))
            source = (f"fallback --keepalive-x/y scaled from "
                      f"{args.keepalive_ref_w}x{args.keepalive_ref_h} "
                      f"-> {region['width']}x{region['height']}")
            reason = "no_template_configured" if tmpl is None else "template_not_found"
        else:
            cx, cy = region["width"] // 2, region["height"] // 2
            source = "fallback region-center (no target, no saved point, no --keepalive-x/y)"
            reason = "no_template_configured" if tmpl is None else "template_not_found"
    else:
        reason = "matched"

    focus_window(hwnd)
    fired = safe_click_at(region, cx, cy, args, hold=0.05, why="keep-alive")
    if not fired:
        return (False, f"({cx},{cy}) [{source}] - BLOCKED by deny-zone", matched, reason)
    return (True, f"({cx},{cy}) [{source}]", matched, reason)


# ---------------------------------------------------------------------------
# Screenshots / logging
# ---------------------------------------------------------------------------

def shot(sct, region, name):
    fn = f"{name}_{time.strftime('%H%M%S')}.png"
    try:
        cv2.imwrite(fn, grab(sct, region))
        return fn
    except Exception as e:
        return f"(screenshot failed: {e})"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# BlueStacks close confirmation / restart (unchanged)
# ---------------------------------------------------------------------------

def click_dialog_button(dlg_hwnd, label):
    target = None

    def cb(child, _):
        nonlocal target
        if win32gui.GetWindowText(child).strip().lower() == label.lower():
            target = child
        return True

    win32gui.EnumChildWindows(dlg_hwnd, cb, None)

    if target is not None:
        win32gui.SendMessage(target, win32con.BM_CLICK, 0, 0)
        time.sleep(0.3)
        if not win32gui.IsWindow(dlg_hwnd):
            return True
        l, t, r, b = win32gui.GetWindowRect(target)
        cx, cy = (l + r) // 2, (t + b) // 2
    else:
        l, t, r, b = win32gui.GetWindowRect(dlg_hwnd)
        w, h = r - l, b - t
        x_frac, y_frac = (0.85, 0.78) if label.lower() == "close" else (0.60, 0.78)
        cx = l + int(w * x_frac)
        cy = t + int(h * y_frac)

    try:
        win32gui.SetForegroundWindow(dlg_hwnd)
    except Exception:
        pass

    time.sleep(0.1)

    vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)

    if vw <= 1 or vh <= 1:
        return False

    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
          int((cx - vx) * 65535 / (vw - 1)),
          int((cy - vy) * 65535 / (vh - 1)))
    time.sleep(0.02)
    _send(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.05)
    _send(MOUSEEVENTF_LEFTUP)
    return True


def close_window(hwnd, timeout=30.0, confirm_title=CONFIRM_DIALOG_TITLE):
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as e:
        log(f"  WM_CLOSE failed ({e})")
        return False

    confirm_clicked = False
    end = time.perf_counter() + timeout

    while time.perf_counter() < end:
        if not win32gui.IsWindow(hwnd):
            return True

        if not confirm_clicked and confirm_title:
            dlg = find_confirm_dialog(confirm_title)
            if dlg:
                log("  close-confirmation dialog appeared - clicking Close")
                if click_dialog_button(dlg, "Close"):
                    confirm_clicked = True

        time.sleep(0.5)

    return False


def launch_target(path, extra_args=None):
    """
    Launch either a normal .exe or a Windows .lnk shortcut.

    subprocess.Popen([path]) works for .exe but NOT for .lnk - a shortcut
    isn't directly executable, Windows resolves it via the shell
    (ShellExecute / os.startfile), not CreateProcess. Using a .lnk that
    launches straight into a specific app (e.g. a desktop shortcut that
    opens MuMu directly into King of Thieves) is the cleanest fix for
    "restart brings back the emulator but not the game" - no icon-tap
    calibration needed at all if you have one of these.
    """
    try:
        if path.lower().endswith(".lnk"):
            os.startfile(path)
        else:
            subprocess.Popen([path] + (extra_args or []), shell=False)
        return True
    except Exception as e:
        log(f"  launch failed: {e}")
        return False


def restart_cycle(sct, region, hwnd, args, anchor=None, app_icon_point=None):
    log("RESTART: closing the emulator")

    # Grab the exe path from the LIVE window before we close it - this
    # is what lets restart work with zero --restart-exe/--restart-cmd
    # flags. Only falls back to the user-supplied flags if this fails
    # (e.g. permissions) or the user explicitly gave one.
    auto_exe = None
    if not args.restart_exe and not args.restart_cmd:
        auto_exe = get_exe_path(hwnd)
        if auto_exe:
            log(f"  auto-detected launcher: {auto_exe}")
        else:
            log("  could not auto-detect the emulator's .exe path "
                "(and no --restart-exe/--restart-cmd given) - "
                "restart will not be able to relaunch it.")

    before = shot(sct, region, "restart_before")
    log(f"  {before}")

    if not close_window(hwnd, args.close_timeout, args.confirm_title):
        log("  window did not close in time; not force-killing. Stopping.")
        return None

    log("  emulator closed cleanly")
    log(f"  waiting {args.restart_wait:.0f}s before relaunch")

    t0 = time.perf_counter()
    while time.perf_counter() - t0 < args.restart_wait:
        if STOP:
            return None
        time.sleep(0.5)

    if args.restart_exe:
        import shlex
        extra = shlex.split(args.restart_args, posix=False) if args.restart_args else []
        log(f"  launching: {args.restart_exe} {extra}")
        if not launch_target(args.restart_exe, extra):
            return None
    elif args.restart_cmd:
        log(f"  launching: {args.restart_cmd}")
        try:
            subprocess.Popen(args.restart_cmd, shell=True)
        except Exception as e:
            log(f"  launch failed: {e}")
            return None
    elif auto_exe:
        log(f"  launching (auto-detected): {auto_exe}")
        if not launch_target(auto_exe):
            return None
    else:
        log("  nothing to launch - emulator is closed and will stay closed. "
            "Pass --restart-exe, or check auto-detection above.")
        return None

    end = time.perf_counter() + args.relaunch_timeout
    new_hwnd = None

    while time.perf_counter() < end:
        if STOP:
            return None
        h, title = find_window(args.title)
        if h:
            new_hwnd = h
            break
        time.sleep(1.0)

    if not new_hwnd:
        log(f"  window never reappeared within {args.relaunch_timeout:.0f}s.")
        return None

    log(f"  window back after {time.perf_counter() - t0 - args.restart_wait:.0f}s; "
        f"settling for {args.settle_after:.0f}s")
    time.sleep(args.settle_after)

    # If we launched a plain emulator .exe, we're probably sitting on
    # the Android home screen, not inside King of Thieves. Check the
    # anchor; if the game isn't up, tap the calibrated app icon (belt
    # and suspenders - if you used a .lnk that opens straight into the
    # game, the anchor will already be visible and this is skipped).
    if anchor is not None:
        new_region = game_region(new_hwnd, args)
        frame = grab(sct, new_region)

        if find_anchor(frame, anchor["img"], anchor["thresh"]) is not None:
            log("  game already visible after relaunch - no icon tap needed.")
        elif app_icon_point is None:
            log("  game NOT visible after relaunch, and no app icon point "
                "calibrated (--set-app-icon-point). Sitting on the home "
                "screen - you'll need to open the game manually.")
        else:
            cx, cy = scale_point(app_icon_point, new_region)
            log(f"  game not visible - tapping app icon at ({cx},{cy})")
            focus_window(new_hwnd)
            safe_click_at(new_region, cx, cy, args, hold=0.06, why="app-icon tap")

            confirmed = False
            end2 = time.perf_counter() + args.app_launch_timeout
            while time.perf_counter() < end2:
                if STOP:
                    break
                if find_anchor(grab(sct, new_region), anchor["img"],
                                anchor["thresh"]) is not None:
                    confirmed = True
                    break
                time.sleep(1.0)

            if confirmed:
                log("  app launch confirmed.")
            else:
                log(f"  app still not confirmed after {args.app_launch_timeout:.0f}s; "
                    f"retrying tap once.")
                safe_click_at(new_region, cx, cy, args, hold=0.06, why="app-icon tap retry")
                end3 = time.perf_counter() + args.app_launch_timeout
                while time.perf_counter() < end3:
                    if STOP:
                        break
                    if find_anchor(grab(sct, new_region), anchor["img"],
                                    anchor["thresh"]) is not None:
                        log("  app launch confirmed after retry.")
                        break
                    time.sleep(1.0)
                else:
                    log("  still could not confirm the app launched. "
                        "Manual check may be needed.")

    return new_hwnd


# ---------------------------------------------------------------------------
# Calibration (dialog templates + the new anchor template)
# ---------------------------------------------------------------------------

def calibrate(sct, region, hwnd):
    print()
    print("CALIBRATE MODE (dialog templates)")
    print(f"  Region: left={region['left']} top={region['top']} "
          f"{region['width']}x{region['height']}")
    print("  Force a disconnect/popup, wait for it, then press F6.")
    print("  Drag a box around the DIALOG PANEL, then press ENTER.")
    print("  F9 to quit.")

    os.makedirs(TEMPLATE_DIR, exist_ok=True)

    while True:
        if STOP:
            print("Quit.")
            return

        if keyboard.is_pressed("f6"):
            focus_window(hwnd)
            frame = grab(sct, region)

            print()
            print("Drag a box around the DIALOG PANEL, then press ENTER.")
            box = cv2.selectROI("drag the dialog panel, then ENTER", frame, showCrosshair=False)
            cv2.destroyWindow("drag the dialog panel, then ENTER")

            x, y, w, h = [int(v) for v in box]
            if w < 20 or h < 20:
                print("Discarded - no valid box.")
                time.sleep(0.3)
                continue

            crop = frame[y:y + h, x:x + w]

            print()
            print("Hover the mouse over the button to click and press F7. ESC to discard.")
            point = None

            while True:
                if keyboard.is_pressed("esc"):
                    break
                if keyboard.is_pressed("f7"):
                    mx, my = win32gui.GetCursorPos()
                    point = (mx - region["left"], my - region["top"])
                    break
                time.sleep(0.02)

            if point is None:
                print("Discarded.")
                time.sleep(0.3)
                continue

            name = input("Template name (e.g. connection_lost, inactivity_prompt): ").strip()
            if not name:
                print("Empty name, discarded.")
                continue

            dismiss_only = input(
                "Is this a nuisance/ad popup close-X rather than a connection dialog? [y/N]: "
            ).strip().lower().startswith("y")

            fatal = input(
                "Is this UNRECOVERABLE (bot should stop, e.g. ban/kick screen)? [y/N]: "
            ).strip().lower().startswith("y")

            cv2.imwrite(os.path.join(TEMPLATE_DIR, name + ".png"), crop)
            with open(os.path.join(TEMPLATE_DIR, name + ".json"), "w", encoding="utf-8") as f:
                json.dump({
                    "click_dx": point[0] - x,
                    "click_dy": point[1] - y,
                    "thresh": 0.85,
                    "dismiss_only": dismiss_only,
                    "fatal": fatal,
                    "ref_w": region["width"],
                    "ref_h": region["height"],
                }, f, indent=2)

            print(f"Saved {name}.png / {name}.json calibrated at "
                  f"{region['width']}x{region['height']}")
            time.sleep(0.3)

        time.sleep(0.02)


def calibrate_anchor(sct, region, hwnd):
    """
    Calibrate the ground-truth 'game is actually visible' template.

    Pick something on screen ONLY during real gameplay and never during
    any dialog/menu: a HUD icon, gold counter, pause button, minimap
    corner, etc. Small and high-contrast is better than large.
    """
    print()
    print("CALIBRATE ANCHOR MODE")
    print("  Make sure the game is ACTUALLY being played right now (not a")
    print("  menu, not a dialog). Then drag a box around a small HUD")
    print("  element that is ALWAYS visible during gameplay and NEVER")
    print("  visible on any dialog/popup/menu screen. Press ENTER when done.")

    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    focus_window(hwnd)
    frame = grab(sct, region)

    box = cv2.selectROI("drag the gameplay-only HUD element, then ENTER", frame, showCrosshair=False)
    cv2.destroyWindow("drag the gameplay-only HUD element, then ENTER")

    x, y, w, h = [int(v) for v in box]
    if w < 8 or h < 8:
        print("Discarded - no valid box.")
        return

    crop = frame[y:y + h, x:x + w]
    cv2.imwrite(ANCHOR_PATH, crop)
    with open(ANCHOR_META, "w", encoding="utf-8") as f:
        json.dump({
            "thresh": 0.82,
            "ref_w": region["width"],
            "ref_h": region["height"],
        }, f, indent=2)

    print(f"Saved anchor ({w}x{h}) at {ANCHOR_PATH}")


def load_point(path):
    """Generic loader for a saved (x, y, ref_w, ref_h) click point."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "x": int(data["x"]),
            "y": int(data["y"]),
            "ref_w": int(data["ref_w"]),
            "ref_h": int(data["ref_h"]),
        }
    except Exception as e:
        print(f"  could not read {path}: {e}")
        return None


def scale_point(point, region):
    """Scale a saved point from its calibrated resolution to the current one."""
    sx = region["width"] / point["ref_w"]
    sy = region["height"] / point["ref_h"]
    return int(round(point["x"] * sx)), int(round(point["y"] * sy))


def _calibrate_point(path, sct, region, hwnd, header, instructions):
    print()
    print(header)
    print(f"  {instructions}")
    print("  Press F7 over the spot to save it. ESC to cancel.")

    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    focus_window(hwnd)

    point = None
    while True:
        if STOP or keyboard.is_pressed("esc"):
            print("Cancelled.")
            return

        if keyboard.is_pressed("f7"):
            mx, my = win32gui.GetCursorPos()
            point = (mx - region["left"], my - region["top"])
            break

        time.sleep(0.02)

    if not (0 <= point[0] <= region["width"] and 0 <= point[1] <= region["height"]):
        print("  point is outside the capture region; not saved.")
        return

    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "x": point[0],
            "y": point[1],
            "ref_w": region["width"],
            "ref_h": region["height"],
        }, f, indent=2)

    print(f"Saved point {point} at {region['width']}x{region['height']} -> {path}")
    print("Future runs will auto-scale this to whatever resolution the "
          "emulator is at - no flags needed.")


def load_keepalive_point():
    return load_point(KEEPALIVE_POINT_PATH)


def calibrate_keepalive_point(sct, region, hwnd):
    """
    One-time setup: hover over wherever the keep-alive fallback click
    should land, press F7. Auto-scales on every future run.
    """
    _calibrate_point(
        KEEPALIVE_POINT_PATH, sct, region, hwnd,
        "CALIBRATE KEEP-ALIVE POINT",
        "Hover over a harmless spot in the emulator "
        "(somewhere clicking it can't do anything bad).",
    )


def load_app_icon_point():
    return load_point(APP_ICON_POINT_PATH)


def calibrate_app_icon_point(sct, region, hwnd):
    """
    One-time setup: go to the Android home screen and hover over the
    King of Thieves app icon, press F7. This is what lets restart_cycle()
    actually get BACK INTO the game after relaunching the emulator -
    without it, a restart only brings up the home screen and stops there.
    """
    _calibrate_point(
        APP_ICON_POINT_PATH, sct, region, hwnd,
        "CALIBRATE APP ICON POINT",
        "Make sure you're on the Android HOME SCREEN right now, "
        "then hover over the King of Thieves icon.",
    )


# ---------------------------------------------------------------------------
# Generic recovery for UNKNOWN interruptions
# ---------------------------------------------------------------------------

def generic_recovery(sct, region, hwnd, anchor, args):
    """
    Something is covering the game that doesn't match any calibrated
    template, and the anchor confirms we are NOT in gameplay. Try a
    sequence of cheap, generic dismissal actions, re-checking the
    anchor after each one. Returns True if the anchor reappears.
    """

    log("  UNKNOWN interruption (no template matched, anchor not visible). "
        "Starting generic recovery.")

    # 1) Try keyboard dismissal first - cheap and won't mis-click anything.
    for key in args.recovery_keys:
        if STOP:
            return False

        focus_window(hwnd)
        press_key(hwnd, key)
        naptime(0.6)

        frame = grab(sct, region)
        if find_anchor(frame, anchor["img"], anchor["thresh"]) is not None:
            log(f"  recovered via key '{key}'.")
            return True

    # 2) Blind clicks at the screen positions dialog buttons usually sit.
    #    DISABLED BY DEFAULT: an unidentified popup can be an ad with a
    #    purchase/subscribe button in one of these spots, and a blind
    #    click has no way to know that. Pass --allow-blind-recovery to
    #    opt back in. With this off, generic_recovery only ever presses
    #    keys (safe) and otherwise escalates straight to a restart.
    if not args.allow_blind_recovery:
        log("  blind-click recovery is disabled (default). Skipping mouse "
            "fallback to avoid mis-clicking an unknown popup (e.g. an ad's "
            "purchase button). Pass --allow-blind-recovery to re-enable.")
        return False

    # Also refuse to blind-click into a capture region that's suspiciously
    # small - a strong sign the window is minimized and these coordinates
    # don't mean what they normally would.
    if region["width"] < 300 or region["height"] < 300:
        log(f"  capture region looks invalid ({region['width']}x"
            f"{region['height']}, window may be minimized) - refusing to "
            f"blind-click into it.")
        return False

    w, h = region["width"], region["height"]
    blind_points = [
        (int(w * 0.50), int(h * 0.50)),   # center (single OK button)
        (int(w * 0.50), int(h * 0.82)),   # bottom-center
        (int(w * 0.82), int(h * 0.82)),   # bottom-right (common "confirm")
        (int(w * 0.90), int(h * 0.12)),   # top-right (common "X" close)
    ]

    for i, (bx, by) in enumerate(blind_points):
        if STOP:
            return False

        log(f"  generic recovery: blind click attempt {i + 1}/{len(blind_points)} "
            f"at ({bx},{by})")

        focus_window(hwnd)
        naptime(args.pre_click)
        fired = safe_click_at(region, bx, by, args, hold=0.06, why="blind recovery click")
        if not fired:
            continue
        wait_settled(sct, region, quiet=args.quiet, timeout=args.settle_timeout)

        frame = grab(sct, region)
        if find_anchor(frame, anchor["img"], anchor["thresh"]) is not None:
            log(f"  recovered via blind click at ({bx},{by}).")
            return True

    log("  generic recovery exhausted all attempts; anchor still not visible.")
    return False


# ---------------------------------------------------------------------------
# Main watchdog loop
# ---------------------------------------------------------------------------

def run(sct, region, hwnd, templates, anchor, keepalive_point, app_icon_point, args):
    print()
    print(f"Watching with {len(templates)} template(s): "
          f"{', '.join(t['name'] for t in templates) or '(none)'}")
    if anchor is not None:
        print("Anchor template loaded - unknown dialogs will be caught.")
    else:
        print("WARNING: no anchor template calibrated "
              "(run --calibrate-anchor). Unknown dialogs (inactivity "
              "prompts, uncalibrated popups, etc.) will NOT be detected - "
              "the bot can only react to dialogs it has a template for.")
    print(f"Polling every {args.interval}s. F9 to quit.")

    last_fire = {}
    dismissed = 0
    ads_dismissed = 0
    failures = 0
    unknown_since = None
    unknown_recoveries = 0
    unknown_failures = 0

    t_start = time.perf_counter()
    last_heartbeat = time.perf_counter()
    restarted = False
    state = "connected"

    # --- restart circuit-breaker state ---
    # Repeated restarts in a short window almost always mean the bot is
    # stuck looping through the same bad state (or the fix above didn't
    # actually resolve whatever's wrong) rather than genuinely recovering
    # each time. Rather than keep hammering restart/relaunch/click forever,
    # stop and wait for a human once this trips.
    restart_times = []

    def restart_circuit_ok():
        now = time.perf_counter()
        restart_times.append(now)
        window_s = args.restart_window_minutes * 60
        recent = [t for t in restart_times if now - t <= window_s]
        restart_times[:] = recent
        if len(recent) > args.max_restarts:
            log(f"  CIRCUIT BREAKER: {len(recent)} restarts within "
                f"{args.restart_window_minutes:.0f} minute(s) (limit "
                f"{args.max_restarts}). Something is likely stuck in a "
                f"loop the bot can't get out of on its own. Stopping "
                f"entirely rather than restarting again - check on it "
                f"manually. (Tune with --max-restarts/--restart-window-minutes.)")
            return False
        return True

    log(f"session start; {len(templates)} template(s), "
        f"anchor={'yes' if anchor else 'no'}, "
        f"{region['width']}x{region['height']}")

    ka_tmpl_base = None
    ka_tmpl = None
    next_ka = None

    if args.keepalive:
        if args.keepalive_template and os.path.isfile(args.keepalive_template):
            ka_tmpl_base = cv2.imread(args.keepalive_template)
            if ka_tmpl_base is not None:
                sx = region["width"] / GAME_W
                sy = region["height"] / GAME_H
                ka_tmpl = scale_image(ka_tmpl_base, sx, sy)
                print(f"  keep-alive: matching "
                      f"{os.path.basename(args.keepalive_template)} "
                      f"({ka_tmpl.shape[1]}x{ka_tmpl.shape[0]} after scaling)")

        next_ka = time.perf_counter() + random.uniform(args.keepalive_min, args.keepalive_max)
        print(f"  keep-alive every {args.keepalive_min:.0f}-{args.keepalive_max:.0f}s.")
        if anchor is None:
            print("  NOTE: without an anchor, keep-alive cannot verify the "
                  "game is actually visible before clicking.")

    while True:
        if STOP:
            print(f"Quit. {dismissed} dialog(s), {ads_dismissed} ad(s), "
                  f"{unknown_recoveries} unknown-dialog recoveries.")
            return

        # ------------------------------------------------------------
        # Session timeout / scheduled restart
        # ------------------------------------------------------------
        if (args.max_session > 0 and
                time.perf_counter() - t_start > args.max_session * 3600):

            log(f"--max-session {args.max_session}h reached.")
            do_restart = args.restart_loop or (args.restart_test and not restarted)

            if not do_restart:
                log("stopping.")
                return

            restarted = True
            new_hwnd = restart_cycle(sct, region, hwnd, args, anchor, app_icon_point)
            if new_hwnd is None:
                return

            hwnd = new_hwnd
            region = game_region(hwnd, args)
            prepare_templates(templates, region)
            if anchor is not None:
                prepare_anchor(anchor, region)

            if ka_tmpl_base is not None:
                sx = region["width"] / GAME_W
                sy = region["height"] / GAME_H
                ka_tmpl = scale_image(ka_tmpl_base, sx, sy)

            after = shot(sct, region, "restart_after")
            log(f"  after relaunch: {after}")
            t_start = time.perf_counter()
            unknown_since = None
            log("  monitoring resumes.")

        # ------------------------------------------------------------
        # Capture + check known templates first
        # ------------------------------------------------------------
        frame = grab(sct, region)
        best = best_match(frame, templates)

        if best is not None:
            unknown_since = None  # a known template matched; not "unknown" anymore
            t, (x, y, score) = best

            # --- ad / nuisance popup -----------------------------------
            if t.get("dismiss_only"):
                now = time.perf_counter()
                if now - last_fire.get(t["name"], 0) <= args.ad_cooldown:
                    naptime(args.interval)
                    continue

                cx, cy = x + t["click_dx"], y + t["click_dy"]
                log(f"DETECTED ad/popup '{t['name']}' score={score:.3f} "
                    f"at ({x},{y}) -> dismissing at ({cx},{cy})")
                last_fire[t["name"]] = now

                if not args.dry_run:
                    focus_window(hwnd)
                    naptime(args.pre_click)
                    fired = safe_click_at(region, cx, cy, args, hold=0.06,
                                           why=f"dismiss ad '{t['name']}'")
                    if not fired:
                        naptime(0.15)
                        continue
                    wait_settled(sct, region, quiet=args.quiet, timeout=args.settle_timeout)

                    if verify_gone(sct, region, t["img"], t["thresh"], args.verify_secs):
                        ads_dismissed += 1
                        log(f"  CONFIRMED: ad '{t['name']}' closed ({ads_dismissed} total)")
                    else:
                        log(f"  NOT CONFIRMED: '{t['name']}' still showing after click.")

                naptime(0.15)
                continue

            # --- real connection/interruption dialog --------------------
            if state != t["name"]:
                log(f"state: {state} -> {t['name']} "
                    f"(t+{(time.perf_counter() - t_start) / 3600:.2f}h)")
                state = t["name"]

            now = time.perf_counter()
            if now - last_fire.get(t["name"], 0) <= args.cooldown:
                naptime(args.interval)
                continue

            if t.get("fatal"):
                log(f"FATAL '{t['name']}' score={score:.3f}")
                cv2.imwrite("reconnect_fatal.png", frame)
                return

            cx, cy = x + t["click_dx"], y + t["click_dy"]
            log(f"DETECTED dialog '{t['name']}' (confidence {score:.3f}) "
                f"at ({x},{y}) -> clicking at ({cx},{cy})")
            last_fire[t["name"]] = now

            if args.dry_run:
                log("  --dry-run: not clicking.")
                naptime(args.interval)
                continue

            ok = False
            for attempt in range(1, args.max_tries + 1):
                if STOP:
                    break
                focus_window(hwnd)
                naptime(args.pre_click)
                fired = safe_click_at(region, cx, cy, args, hold=0.06,
                                       why=f"dismiss dialog '{t['name']}'")
                if not fired:
                    break
                wait_settled(sct, region, quiet=args.quiet, timeout=args.settle_timeout)

                if verify_gone(sct, region, t["img"], t["thresh"], args.verify_secs):
                    ok = True
                    break
                log(f"  click NOT confirmed - '{t['name']}' still present "
                    f"after attempt {attempt}/{args.max_tries}")

            if ok:
                dismissed += 1
                failures = 0
                log(f"  CONFIRMED: '{t['name']}' dismissed ({dismissed} total)")
            else:
                failures += 1
                stuck_shot = f"reconnect_stuck_{failures}.png"
                cv2.imwrite(stuck_shot, grab(sct, region))
                log(f"  FAILED to dismiss '{t['name']}' after {args.max_tries} "
                    f"attempts. Saved {stuck_shot}.")

                if failures >= args.max_failures:
                    log(f"  giving up after {failures} unresolved dialog(s). "
                        f"Attempting a restart instead of hanging.")
                    if not restart_circuit_ok():
                        return
                    new_hwnd = restart_cycle(sct, region, hwnd, args, anchor, app_icon_point)
                    if new_hwnd is None:
                        return
                    hwnd = new_hwnd
                    region = game_region(hwnd, args)
                    prepare_templates(templates, region)
                    if anchor is not None:
                        prepare_anchor(anchor, region)
                    failures = 0
                    t_start = time.perf_counter()

            naptime(args.cooldown)
            continue

        # ------------------------------------------------------------
        # No known template matched. Is the game actually visible?
        # ------------------------------------------------------------
        game_ok = True
        if anchor is not None:
            game_ok = find_anchor(frame, anchor["img"], anchor["thresh"]) is not None

        if game_ok:
            unknown_since = None
            if state != "connected":
                log(f"state: {state} -> connected "
                    f"(t+{(time.perf_counter() - t_start) / 3600:.2f}h)")
                state = "connected"

            if args.verbose and time.perf_counter() - last_heartbeat >= args.heartbeat:
                log("  connected - watching...")
                last_heartbeat = time.perf_counter()

            if next_ka is not None and time.perf_counter() >= next_ka:
                ok, where, matched, reason = keepalive_click(sct, region, hwnd, ka_tmpl,
                                                               keepalive_point, args)
                gap = random.uniform(args.keepalive_min, args.keepalive_max)
                next_ka = time.perf_counter() + gap
                tag = {
                    "matched": "target found",
                    "no_template_configured": "using saved point (no template set - normal)",
                    "template_not_found": "TEMPLATE NOT FOUND on screen, used fallback",
                }[reason]
                log(f"keep-alive TEST CLICK {where} [{tag}]; next in {gap:.0f}s")

            naptime(args.interval)
            continue

        # ------------------------------------------------------------
        # UNKNOWN interruption: anchor missing, no template matched.
        # ------------------------------------------------------------
        if unknown_since is None:
            unknown_since = time.perf_counter()
            state = "unknown"
            log("state: connected -> unknown (no template matched, "
                "anchor not visible)")

        elapsed = time.perf_counter() - unknown_since

        if elapsed < args.unknown_grace:
            # Might just be a scene transition/loading screen. Wait it out.
            naptime(min(args.interval, 0.5))
            continue

        incident_shot = shot(sct, region, "unknown_dialog")
        log(f"  saved incident screenshot: {incident_shot}")
        recovered = generic_recovery(sct, region, hwnd, anchor, args)

        if recovered:
            unknown_recoveries += 1
            unknown_since = None
            state = "connected"
            naptime(args.interval)
            continue

        unknown_failures += 1
        log(f"  unknown interruption NOT resolved by generic recovery "
            f"({unknown_failures}/{args.max_unknown_failures}).")

        if unknown_failures >= args.max_unknown_failures:
            log("  too many unresolved unknown interruptions; restarting emulator.")
            if not restart_circuit_ok():
                return
            new_hwnd = restart_cycle(sct, region, hwnd, args, anchor, app_icon_point)
            if new_hwnd is None:
                return
            hwnd = new_hwnd
            region = game_region(hwnd, args)
            prepare_templates(templates, region)
            if anchor is not None:
                prepare_anchor(anchor, region)
            unknown_failures = 0
            unknown_since = None
            t_start = time.perf_counter()

        naptime(args.interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)

    ap.add_argument("--title", default=WINDOW_TITLE)
    ap.add_argument("--confirm-title", default=CONFIRM_DIALOG_TITLE, dest="confirm_title",
                     help="title substring of the emulator's own close-confirmation "
                          "popup (e.g. 'close bluestacks' for BlueStacks). Set to "
                          "'' to disable if your emulator (e.g. MuMu) doesn't show one, "
                          "or doesn't need special handling for it.")
    ap.add_argument("--region-x", type=int, default=None, dest="region_x")
    ap.add_argument("--region-y", type=int, default=0, dest="region_y")
    ap.add_argument("--templates", default=TEMPLATE_DIR)

    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--cooldown", type=float, default=6.0)
    ap.add_argument("--ad-cooldown", type=float, default=1.5, dest="ad_cooldown")
    ap.add_argument("--quiet", type=float, default=0.4)
    ap.add_argument("--settle-timeout", type=float, default=5.0, dest="settle_timeout")
    ap.add_argument("--verify-secs", type=float, default=4.0, dest="verify_secs")
    ap.add_argument("--max-tries", type=int, default=3, dest="max_tries")
    ap.add_argument("--max-failures", type=int, default=2, dest="max_failures")
    ap.add_argument("--pre-click", type=float, default=1.0, dest="pre_click")

    # --- anchor / unknown-dialog recovery (new) ---
    ap.add_argument("--anchor-thresh", type=float, default=0.82, dest="anchor_thresh",
                     help="match threshold for the gameplay anchor template")
    ap.add_argument("--unknown-grace", type=float, default=8.0, dest="unknown_grace",
                     help="seconds to wait before treating a missing anchor as a "
                          "real (unknown) interruption, not just a scene transition")
    ap.add_argument("--recovery-keys", default="esc,enter", dest="recovery_keys_raw",
                     help="comma-separated keys to try during generic recovery")
    ap.add_argument("--max-unknown-failures", type=int, default=2, dest="max_unknown_failures")
    ap.add_argument("--max-restarts", type=int, default=3, dest="max_restarts",
                     help="circuit breaker: if a failure-driven restart (stuck dialog "
                          "or unresolved unknown interruption) fires more than this "
                          "many times within --restart-window-minutes, stop the bot "
                          "entirely instead of restarting again. Scheduled restarts "
                          "from --max-session/--restart-loop are not counted.")
    ap.add_argument("--restart-window-minutes", type=float, default=10.0,
                     dest="restart_window_minutes",
                     help="time window (minutes) the --max-restarts circuit breaker "
                          "counts restarts over")
    ap.add_argument("--deny-zone", action="append", default=None, dest="deny_zones_raw",
                     metavar="x1,y1,x2,y2",
                     help="a rectangle (as fractions 0.0-1.0 of the capture region) "
                          "that NO click - blind, ad, dialog, keep-alive, or app-icon "
                          "- is ever allowed to land in. Repeatable. Example: "
                          "--deny-zone 0.75,0.0,1.0,0.20 blocks the top-right corner "
                          "where ad 'buy'/'subscribe' buttons often sit.")
    ap.add_argument("--allow-blind-recovery", action="store_true", dest="allow_blind_recovery",
                     help="allow generic recovery to fall back to blind mouse clicks at "
                          "guessed dialog-button positions when keyboard dismissal (ESC/"
                          "Enter) fails. OFF by default - a blind click can land on an "
                          "unrelated popup's purchase/subscribe button. With this off, "
                          "an unresolved unknown interruption escalates straight to a "
                          "restart instead of guessing with the mouse.")

    ap.add_argument("--keepalive", action="store_true")
    ap.add_argument("--keepalive-template", default=os.path.join(TEMPLATE_DIR, "_mine.png"),
                     dest="keepalive_template")
    ap.add_argument("--keepalive-x", type=int, default=None, dest="keepalive_x",
                     help="fallback click x, measured at --keepalive-ref-w/h resolution")
    ap.add_argument("--keepalive-y", type=int, default=None, dest="keepalive_y",
                     help="fallback click y, measured at --keepalive-ref-w/h resolution")
    ap.add_argument("--keepalive-ref-w", type=int, default=GAME_W, dest="keepalive_ref_w",
                     help="resolution --keepalive-x was measured at (default 1280)")
    ap.add_argument("--keepalive-ref-h", type=int, default=GAME_H, dest="keepalive_ref_h",
                     help="resolution --keepalive-y was measured at (default 720)")
    ap.add_argument("--keepalive-min", type=float, default=30.0, dest="keepalive_min")
    ap.add_argument("--keepalive-max", type=float, default=120.0, dest="keepalive_max")
    ap.add_argument("--keepalive-thresh", type=float, default=0.80, dest="keepalive_thresh")

    ap.add_argument("--max-session", type=float, default=0.0, dest="max_session")
    ap.add_argument("--restart-test", action="store_true", dest="restart_test")
    ap.add_argument("--restart-loop", action="store_true", dest="restart_loop")
    ap.add_argument("--restart-cmd", default="", dest="restart_cmd")
    ap.add_argument("--restart-exe", default=DEFAULT_RESTART_EXE, dest="restart_exe",
                     help="path to the emulator .exe or a .lnk shortcut to relaunch on "
                          "restart. Defaults to DEFAULT_RESTART_EXE set near the top of "
                          "this file, so normally you don't need to pass this at all. "
                          "Pass --restart-exe \"\" to disable the default and fall back "
                          "to auto-detection instead.")
    ap.add_argument("--restart-args", default="", dest="restart_args")
    ap.add_argument("--restart-wait", type=float, default=90.0, dest="restart_wait")
    ap.add_argument("--close-timeout", type=float, default=30.0, dest="close_timeout")
    ap.add_argument("--relaunch-timeout", type=float, default=180.0, dest="relaunch_timeout")
    ap.add_argument("--settle-after", type=float, default=45.0, dest="settle_after")

    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--calibrate-anchor", action="store_true", dest="calibrate_anchor")
    ap.add_argument("--set-keepalive-point", action="store_true", dest="set_keepalive_point",
                     help="one-time: hover the mouse where keep-alive should click, "
                          "press F7 to save it (auto-scales on every future run)")
    ap.add_argument("--set-app-icon-point", action="store_true", dest="set_app_icon_point",
                     help="one-time: on the Android home screen, hover over the game's "
                          "icon and press F7. Used to reopen the game after a restart "
                          "if --restart-exe isn't a .lnk that already does that.")
    ap.add_argument("--app-launch-timeout", type=float, default=25.0, dest="app_launch_timeout",
                     help="seconds to wait for the anchor to appear after tapping "
                          "the app icon post-restart")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--heartbeat", type=float, default=30.0, dest="heartbeat")

    args = ap.parse_args()
    args.recovery_keys = [k.strip() for k in args.recovery_keys_raw.split(",") if k.strip()]
    args.deny_zones = parse_deny_zones(args.deny_zones_raw)
    if args.deny_zones:
        print(f"  {len(args.deny_zones)} deny-zone(s) active - no click will "
              f"ever land inside: {args.deny_zones}")

    if args.region_x is None:
        args.region_x = 0
        print("  --region-x not given; defaulting to 0.")

    fix_dpi()

    try:
        keyboard.add_hotkey("f9", request_stop)
    except Exception as e:
        print(f"  could not register F9 hotkey ({e}); use Ctrl+C.")

    hwnd, title = find_window(args.title)
    if not hwnd:
        print(f"No window matching '{args.title}'. Is the emulator open?")
        return

    print(f"Found: {title}")
    region = game_region(hwnd, args)
    print(f"  Final capture: {region['width']}x{region['height']} "
          f"at screen ({region['left']},{region['top']})")

    if args.restart_exe:
        print(f"  Restart target: {args.restart_exe}")

    # Sanity-check restart capability upfront, not mid-session when a
    # dialog is actually stuck.
    restart_may_be_needed = (
        args.max_session > 0 or args.restart_test or args.restart_loop
        or args.max_failures > 0 or args.max_unknown_failures > 0
    )
    if restart_may_be_needed and not args.restart_exe and not args.restart_cmd:
        detected = get_exe_path(hwnd)
        if detected:
            print(f"  Restart auto-detect OK: {detected}")
        else:
            print("  WARNING: could not auto-detect the emulator's .exe path, "
                  "and no --restart-exe/--restart-cmd was given. If a restart "
                  "is ever triggered, BlueStacks will close and NOT come back "
                  "up on its own. Pass --restart-exe \"C:\\path\\to\\HD-Player.exe\" "
                  "to fix this, or set DEFAULT_RESTART_EXE near the top of the file.")
    print()

    with mss.MSS() as sct:

        if args.calibrate:
            calibrate(sct, region, hwnd)
            return

        if args.calibrate_anchor:
            calibrate_anchor(sct, region, hwnd)
            return

        if args.set_keepalive_point:
            calibrate_keepalive_point(sct, region, hwnd)
            return

        if args.set_app_icon_point:
            calibrate_app_icon_point(sct, region, hwnd)
            return

        templates = load_templates(args.templates)
        if not templates:
            print(f"No templates in '{args.templates}'. Run:")
            print("  python kot_reconnect.py --title BlueStacks --calibrate")

        prepare_templates(templates, region)

        anchor = load_anchor()
        if anchor is not None:
            prepare_anchor(anchor, region)
            anchor["thresh"] = args.anchor_thresh or anchor["thresh"]
        else:
            print("No anchor template found. Run:")
            print("  python kot_reconnect.py --title BlueStacks --calibrate-anchor")

        keepalive_point = load_keepalive_point()
        if args.keepalive:
            if keepalive_point is not None:
                print(f"  keep-alive fallback point loaded from "
                      f"{KEEPALIVE_POINT_PATH} (auto-scaling active).")
            elif args.keepalive_x is None:
                print("  No saved keep-alive point and no --keepalive-x/y given. "
                      "Run --set-keepalive-point once, or the fallback will just "
                      "click the region center.")

        app_icon_point = load_app_icon_point()
        if app_icon_point is not None:
            print(f"  app icon point loaded from {APP_ICON_POINT_PATH} "
                  f"(used to reopen the game after a restart).")
        elif not (args.restart_exe or "").lower().endswith(".lnk"):
            print("  No app icon point calibrated. If a restart ever brings "
                  "the emulator back to the home screen (not straight into "
                  "the game), it will stop there. Either run "
                  "--set-app-icon-point once, or point --restart-exe at a "
                  ".lnk shortcut that opens straight into the game.")

        run(sct, region, hwnd, templates, anchor, keepalive_point, app_icon_point, args)


if __name__ == "__main__":
    main()