"""
kot_agent.py - position-triggered replay. The closed-loop agent.

Tracks the thief live and fires a tap when it reaches the next waypoint,
instead of when a clock says so.

WHY POSITION AND NOT TIME

Time-triggered replay failed repeatedly:
  - the replay's t=0 and the demonstration's t=0 were different moments
  - detected launch times carried ~+-100ms error, wider than a jump window
  - one missed tap shifted every tap after it

Position has none of those failure modes, and adds one thing time cannot:
if the thief is nowhere near the next waypoint, the run has ALREADY gone
wrong, and the agent can stop rather than firing taps into a dead run.

SAFETY

  - F9 or ESC aborts instantly, mid-run
  - --dry-run tracks and reports what it WOULD do, without clicking
  - it aborts itself if the thief drifts far from the expected waypoint
  - it aborts if the run exceeds --timeout seconds

ALWAYS run --dry-run first on a new dungeon.

Usage:
    python kot_agent.py waypoints/mine.json --dry-run
    python kot_agent.py waypoints/mine.json
    python kot_agent.py waypoints/mine.json --tol 70 --vtol 400

Keys:
    F7  learn background (dungeon visible, thief NOT placed)
    F8  arm / run
    F9  quit
"""

import argparse
import ctypes
import json
import time
from ctypes import wintypes

import cv2
import keyboard
import mss
import numpy as np
import win32api
import win32con
import win32gui

WINDOW_TITLE = "LDPlayer"
GAME_W, GAME_H = 1280, 720
CROP_TOP, CROP_BOTTOM = 0.06, 0.90


# ---------------------------------------------------------------- windows

def fix_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


def find_window(sub):
    out = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if sub.lower() in t.lower():
                out.append((hwnd, t))

    win32gui.EnumWindows(cb, None)
    return out[0] if out else (None, None)


def game_region(hwnd):
    l, t, r, b = win32gui.GetClientRect(hwnd)
    l, t = win32gui.ClientToScreen(hwnd, (l, t))
    r, b = win32gui.ClientToScreen(hwnd, (r, b))
    cw, ch = r - l, b - t
    pad_top = ch - GAME_H
    if pad_top < 0 or cw - GAME_W < 0:
        raise SystemExit(f"Window {cw}x{ch} smaller than {GAME_W}x{GAME_H}.")
    print(f"Chrome: {pad_top}px top, {cw - GAME_W}px right")
    return {"left": l, "top": t + pad_top, "width": GAME_W, "height": GAME_H}


# ------------------------------------------------------------------ input

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]


MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000


def _send(flags, x=0, y=0):
    inp = INPUT(type=0, mi=MOUSEINPUT(x, y, 0, flags, 0, None))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def park_cursor(region, gx, gy):
    """Move once, up front. Keeping the move out of the firing path means
    a tap costs only the two button events."""
    sx, sy = region["left"] + gx, region["top"] + gy
    vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
          int((sx - vx) * 65535 / (vw - 1)), int((sy - vy) * 65535 / (vh - 1)))


def tap(hold=0.05):
    _send(MOUSEEVENTF_LEFTDOWN)
    end = time.perf_counter() + hold
    time.sleep(max(0, hold - 0.002))
    while time.perf_counter() < end:
        pass
    _send(MOUSEEVENTF_LEFTUP)


# ------------------------------------------------------------ perception

def grab(sct, box):
    raw = sct.grab(box)
    arr = np.frombuffer(raw.bgra, dtype=np.uint8)
    return arr.reshape(raw.height, raw.width, 4)[:, :, :3]


def mask_of(patch, bg_patch, args):
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, np.array([0, 0, args.white_v], np.uint8),
                    np.array([179, args.white_s, 255], np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8),
                         iterations=2)
    if bg_patch is not None:
        d = cv2.absdiff(patch, bg_patch).max(axis=2)
        m = cv2.bitwise_and(m, (d > args.bg_thresh).astype(np.uint8) * 255)
    return m


def biggest_blob(mask, args):
    nl, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    best = None
    for j in range(1, nl):
        a = stats[j, cv2.CC_STAT_AREA]
        if args.minarea <= a <= args.maxarea:
            if best is None or a > best[2]:
                best = (float(cents[j][0]), float(cents[j][1]), int(a))
    return best


def _bg_warn(bg, args):
    y0, y1 = int(GAME_H * CROP_TOP), int(GAME_H * CROP_BOTTOM)
    probe = biggest_blob(mask_of(bg[y0:y1], None, args), args)
    if probe:
        print(f"  note: {probe[2]}px white blob in background at "
              f"({probe[0]:.0f}, {probe[1] + y0:.0f}) - only a problem if "
              f"that is the thief")
    return bg


def learn_background(sct, region, args):
    """Median over frames spread across --bg-secs.

    Pressing F7 on a still screen bakes whatever is there into the
    background - including the thief at its start position, which then
    becomes INVISIBLE exactly where every run begins. That happened: the
    flagged blob sat at (428,370) and waypoint 1 was at (428,380).

    EDIT MODE has no thief, but it also has a banner and a grid overlay
    that the play screen lacks, so every one of those pixels would read as
    "moving" during a run.

    So: sample WHILE THE THIEF IS MOVING. It occupies any given pixel for
    a small fraction of the window, so the median discards it, and the UI
    is identical because it is the same screen.
    """
    span = args.bg_secs
    n = args.bg_frames
    if span <= 0:
        # Instant snapshot. Correct whenever the screen genuinely has no
        # thief on it - a campaign pre-level screen, or TAP TO BREAK IN.
        stack = [grab(sct, region) for _ in range(5)]
        bg = np.median(np.stack(stack), axis=0).astype(np.uint8)
        cv2.imwrite("agent_bg.png", bg)
        return _bg_warn(bg, args)
    print(f"  Collecting background over {span:.0f}s.")
    stack = []
    t_end = time.perf_counter() + span
    while len(stack) < n and time.perf_counter() < t_end:
        stack.append(grab(sct, region))
        time.sleep(max(0.0, span / n - 0.01))
    if len(stack) < 3:
        stack = [grab(sct, region) for _ in range(5)]
    bg = np.median(np.stack(stack), axis=0).astype(np.uint8)
    print(f"  {len(stack)} frames sampled")
    cv2.imwrite("agent_bg.png", bg)
    y0, y1 = int(GAME_H * CROP_TOP), int(GAME_H * CROP_BOTTOM)
    probe = biggest_blob(mask_of(bg[y0:y1], None, args), args)
    if probe:
        print(f"  WARNING: {probe[2]}px white blob in the background at "
              f"({probe[0]:.0f}, {probe[1] + y0:.0f}). If that is the thief "
              f"it will be INVISIBLE there - re-learn with it off screen.")
    return bg


# ---------------------------------------------------------------- the run

def run(sct, region, bg, wps, args):
    y0, y1 = int(GAME_H * CROP_TOP), int(GAME_H * CROP_BOTTOM)
    park_cursor(region, args.tap_x, args.tap_y)

    print(f"\n{'DRY RUN - no clicks' if args.dry_run else 'LIVE'}   "
          f"{len(wps)} waypoints   tol {args.tol}px   F9/ESC aborts\n")

    while keyboard.is_pressed("f8"):
        time.sleep(0.01)

    # Velocity smoothed over a few frames: consecutive-frame dt is ~6ms,
    # so raw finite differences are dominated by 1px centroid jitter.
    hist = []
    last = None
    lost = 0
    idx = 0
    fired = []
    t0 = time.perf_counter()

    while idx < len(wps):
        if keyboard.is_pressed("f9") or keyboard.is_pressed("esc"):
            print("ABORTED by key.")
            return fired

        now = time.perf_counter()
        if now - t0 > args.timeout:
            print(f"ABORT: exceeded {args.timeout}s.")
            return fired

        if last is not None and lost <= args.lost_limit:
            cx, cy = last
            x0 = int(max(0, min(GAME_W - 2 * args.roi, cx - args.roi)))
            yy0 = int(max(y0, min(y1 - 2 * args.roi, cy - args.roi)))
            box = {"left": region["left"] + x0, "top": region["top"] + yy0,
                   "width": 2 * args.roi, "height": 2 * args.roi}
            found = biggest_blob(
                mask_of(grab(sct, box), bg[yy0:yy0 + 2 * args.roi,
                                           x0:x0 + 2 * args.roi], args), args)
            ox, oy = x0, yy0
        else:
            found = biggest_blob(
                mask_of(grab(sct, region)[y0:y1], bg[y0:y1], args), args)
            ox, oy = 0, y0

        if not found:
            lost += 1
            if lost > args.lost_abort:
                print(f"ABORT: thief lost for {lost} frames.")
                return fired
            continue

        x, y = found[0] + ox, found[1] + oy
        hist.append((now, x, y))
        if len(hist) > args.smooth:
            hist.pop(0)
        last, lost = (x, y), 0

        vx = vy = 0.0
        if len(hist) >= 2:
            dt = hist[-1][0] - hist[0][0]
            if dt > 1e-4:
                vx = (hist[-1][1] - hist[0][1]) / dt
                vy = (hist[-1][2] - hist[0][2]) / dt

        w = wps[idx]
        d = float(np.hypot(x - w["x"], y - w["y"]))

        # Velocity gate: two waypoints can share a position but differ in
        # direction (going up vs coming down through the same point).
        # Without this the agent fires on the wrong pass.
        vd = float(np.hypot(vx - w["vx"], vy - w["vy"]))
        v_ok = args.vtol <= 0 or vd <= args.vtol

        if d <= args.tol and v_ok:
            if not args.dry_run:
                tap(args.hold)
            el = now - t0
            print(f"  wp {idx + 1}/{len(wps)} FIRED at ({x:.0f},{y:.0f}) "
                  f"d={d:.0f}px vd={vd:.0f} t={el:.2f}s")
            fired.append((idx, el, x, y))
            idx += 1
            # Refractory: without it the next frames still satisfy the same
            # waypoint and the agent double-taps.
            rt = time.perf_counter() + args.refractory
            while time.perf_counter() < rt:
                pass
            hist.clear()
            continue

        if d > args.abort_dist:
            print(f"  ABORT: {d:.0f}px from waypoint {idx + 1} "
                  f"({w['x']:.0f},{w['y']:.0f}) - run has diverged.")
            return fired

    print("\nAll waypoints fired.")
    return fired


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("waypoints")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    ap.add_argument("--tol", type=float, default=60,
                    help="px within which a waypoint counts as reached")
    ap.add_argument("--vtol", type=float, default=0,
                    help="velocity match tolerance px/s; 0 disables")
    ap.add_argument("--abort-dist", type=float, default=500,
                    dest="abort_dist",
                    help="give up if this far from the expected waypoint")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--refractory", type=float, default=0.15)
    ap.add_argument("--smooth", type=int, default=4,
                    help="frames over which velocity is averaged")
    ap.add_argument("--hold", type=float, default=0.05)
    ap.add_argument("--tap-x", type=int, default=640, dest="tap_x")
    ap.add_argument("--tap-y", type=int, default=360, dest="tap_y")
    ap.add_argument("--white-v", type=int, default=220, dest="white_v")
    ap.add_argument("--white-s", type=int, default=30, dest="white_s")
    ap.add_argument("--bg-thresh", type=int, default=28, dest="bg_thresh")
    ap.add_argument("--minarea", type=int, default=40)
    ap.add_argument("--maxarea", type=int, default=3000)
    ap.add_argument("--bg-frames", type=int, default=30, dest="bg_frames")
    ap.add_argument("--bg-secs", type=float, default=0.0, dest="bg_secs",
                    help="seconds to spread background sampling over. 0 = "
                         "instant snapshot, correct when the screen has no "
                         "thief on it (campaign pre-level, TAP TO BREAK IN)")
    ap.add_argument("--roi", type=int, default=110)
    ap.add_argument("--lost-limit", type=int, default=6, dest="lost_limit")
    ap.add_argument("--lost-abort", type=int, default=90, dest="lost_abort")
    args = ap.parse_args()

    with open(args.waypoints) as f:
        data = json.load(f)
    wps = data["waypoints"]
    print(f"{len(wps)} waypoints from {data.get('source', '?')}")

    fix_dpi()
    hwnd, title = find_window(WINDOW_TITLE)
    if not hwnd:
        print(f"No window matching '{WINDOW_TITLE}'.")
        return
    region = game_region(hwnd)
    print(f"Found: {title}")

    bg = None
    print("\nF7 = learn background   F8 = arm/run   F9 = quit\n")

    with mss.MSS() as sct:
        while True:
            if keyboard.is_pressed("f9"):
                print("Bye.")
                return
            if keyboard.is_pressed("f7"):
                bg = learn_background(sct, region, args)
                print("Background learned -> agent_bg.png")
                time.sleep(0.4)
            if keyboard.is_pressed("f8"):
                if bg is None:
                    print("Learn a background first (F7).")
                    time.sleep(0.4)
                    continue
                run(sct, region, bg, wps, args)
                time.sleep(0.6)
            time.sleep(0.005)


if __name__ == "__main__":
    main()