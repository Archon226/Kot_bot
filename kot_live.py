"""
kot_live.py - closed-loop foundation. Milestone 1: SEE, don't act. (v2)

WHY CLOSED-LOOP

Offline tap recovery hit a ceiling: it infers a CAUSE (the tap) from an
EFFECT (the thief moving), and cannot reliably tell a tap-launch from a
bounce. KoT also ignores taps made mid-air, so a tap log and what the game
actually did are different things. Errors landed near +-100ms, wider than
a jump window. Looking at where the thief IS now sidesteps all of it.

WHAT THIS FILE DOES

Watches only. NO CLICKING. Prove perception fits inside a frame before
giving anything control of the mouse.

v2 - ADAPTIVE ROI (the reason v1 was too slow)

v1 grabbed and processed the whole 1280x720 screen every frame: 18.2ms
median, 22.3ms p95, over a 16.6ms budget. But the thief is ~20px across
and cannot teleport, so nearly all of that work was wasted.

v2 grabs only a window around its last known position. mss can capture an
arbitrary screen rectangle, so a 220x220 grab is far cheaper than a full
frame - and detection then runs on ~1/17th the pixels. Full-frame search
happens only when the thief is lost.

Everything is now full resolution: no downscale in the hot loop at all.

BACKGROUND

KoT shows the dungeon before the run starts. Learn the background there.

  IMPORTANT: the thief must NOT be on screen when you press F7. If it is,
  it becomes part of the background and is invisible wherever it starts.
  v1 hit exactly this - the panda was baked into live_bg.png and the first
  2.2 seconds reported "lost".

Usage:
    python kot_live.py
    python kot_live.py --white-v 220 --white-s 30

Keys:
    F7  learn background (dungeon visible, thief NOT placed)
    F8  start / stop watching
    F9  quit
"""

import argparse
import ctypes
import time

import cv2
import keyboard
import mss
import numpy as np
import win32gui

WINDOW_TITLE = "BlueStacks"
GAME_W, GAME_H = 1280, 720
CROP_TOP, CROP_BOTTOM = 0.06, 0.90   # fractions of GAME_H


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


def grab(sct, box):
    """BGR array for an arbitrary screen rectangle."""
    raw = sct.grab(box)
    arr = np.frombuffer(raw.bgra, dtype=np.uint8)
    return arr.reshape(raw.height, raw.width, 4)[:, :, :3]


def mask_of(patch, bg_patch, args):
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    lo = np.array([0, 0, args.white_v], np.uint8)
    hi = np.array([179, args.white_s, 255], np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8),
                         iterations=2)
    if bg_patch is not None:
        diff = cv2.absdiff(patch, bg_patch).max(axis=2)
        m = cv2.bitwise_and(m, (diff > args.bg_thresh).astype(np.uint8) * 255)
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


def learn_background(sct, region, args):
    """Median of several full frames.

    Median rather than a single snapshot so animated decor - torch
    flicker, drifting particles - does not get baked in as if it were
    static.
    """
    span = args.bg_secs
    n = args.bg_frames
    if span <= 0:
        stack = [grab(sct, region) for _ in range(5)]
        bg = np.median(np.stack(stack), axis=0).astype(np.uint8)
        cv2.imwrite("live_bg.png", bg)
        return bg
    print(f"  Collecting over {span:.0f}s.")
    stack = []
    t_end = time.perf_counter() + span
    while len(stack) < n and time.perf_counter() < t_end:
        stack.append(grab(sct, region))
        time.sleep(max(0.0, span / n - 0.01))
    if len(stack) < 3:
        stack = [grab(sct, region) for _ in range(5)]
    bg = np.median(np.stack(stack), axis=0).astype(np.uint8)
    print(f"  {len(stack)} frames sampled")
    cv2.imwrite("live_bg.png", bg)

    # If a thief-sized white blob is present, the background is
    # contaminated and the thief will be invisible near that spot.
    y0 = int(GAME_H * CROP_TOP)
    y1 = int(GAME_H * CROP_BOTTOM)
    probe = biggest_blob(mask_of(bg[y0:y1], None, args), args)
    if probe:
        print(f"  WARNING: a {probe[2]}px white blob is in the background "
              f"at ({probe[0]:.0f}, {probe[1] + y0:.0f}).")
        print("  If that is the thief, it will be INVISIBLE there. Re-learn "
              "with the thief off screen.")
    return bg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--white-v", type=int, default=220, dest="white_v")
    ap.add_argument("--white-s", type=int, default=30, dest="white_s")
    ap.add_argument("--bg-thresh", type=int, default=28, dest="bg_thresh")
    ap.add_argument("--minarea", type=int, default=40)
    ap.add_argument("--maxarea", type=int, default=3000)
    ap.add_argument("--bg-frames", type=int, default=30, dest="bg_frames")
    ap.add_argument("--bg-secs", type=float, default=0.0, dest="bg_secs")
    ap.add_argument("--roi", type=int, default=110,
                    help="half-width of the tracking window in px; the "
                         "thief cannot move further than this per frame")
    ap.add_argument("--lost-limit", type=int, default=6, dest="lost_limit",
                    help="lost frames before falling back to a full search")
    args = ap.parse_args()

    fix_dpi()
    hwnd, title = find_window(WINDOW_TITLE)
    if not hwnd:
        print(f"No window matching '{WINDOW_TITLE}'.")
        return
    region = game_region(hwnd)
    print(f"Found: {title}")

    bg = None
    print("\nF7 = learn background  (dungeon visible, thief NOT placed)")
    print("F8 = start/stop watching    F9 = quit\n")

    with mss.MSS() as sct:
        while True:
            if keyboard.is_pressed("f9"):
                print("Bye.")
                return

            if keyboard.is_pressed("f7"):
                bg = learn_background(sct, region, args)
                print(f"Background learned from {args.bg_frames} frames "
                      f"-> live_bg.png")
                time.sleep(0.4)

            if keyboard.is_pressed("f8"):
                if bg is None:
                    print("Learn a background first (F7).")
                    time.sleep(0.4)
                    continue
                watch(sct, region, bg, args)
                time.sleep(0.4)

            time.sleep(0.005)


def watch(sct, region, bg, args):
    y0 = int(GAME_H * CROP_TOP)
    y1 = int(GAME_H * CROP_BOTTOM)

    print("\nWATCHING. F8 to stop.\n")
    print(f"{'t(s)':>7} {'x':>6} {'y':>6} {'vx':>7} {'vy':>7} "
          f"{'state':>8} {'mode':>5} {'loop':>7}")

    while keyboard.is_pressed("f8"):
        time.sleep(0.01)

    t0 = time.perf_counter()
    last = None          # (x, y) in game coords
    last_t = None
    lost = 0
    loops = []
    full_scans = 0
    last_print = 0.0

    while not keyboard.is_pressed("f8"):
        ls = time.perf_counter()

        if last is not None and lost <= args.lost_limit:
            # Narrow grab around the last known position. This is the whole
            # speedup: a 220x220 capture plus detection on 48k pixels
            # instead of 920k.
            cx, cy = last
            x0 = int(max(0, min(GAME_W - 2 * args.roi, cx - args.roi)))
            yy0 = int(max(y0, min(y1 - 2 * args.roi, cy - args.roi)))
            wbox = {"left": region["left"] + x0,
                    "top": region["top"] + yy0,
                    "width": 2 * args.roi,
                    "height": 2 * args.roi}
            patch = grab(sct, wbox)
            bgp = bg[yy0:yy0 + 2 * args.roi, x0:x0 + 2 * args.roi]
            found = biggest_blob(mask_of(patch, bgp, args), args)
            mode = "roi"
            ox, oy = x0, yy0
        else:
            frame = grab(sct, region)
            found = biggest_blob(mask_of(frame[y0:y1], bg[y0:y1], args), args)
            mode = "full"
            ox, oy = 0, y0
            full_scans += 1

        now = time.perf_counter()
        vx = vy = 0.0
        state = "lost"

        if found:
            x, y = found[0] + ox, found[1] + oy
            if last is not None and last_t is not None:
                dt = now - last_t
                if dt > 1e-4:
                    vx = (x - last[0]) / dt
                    vy = (y - last[1]) / dt
            speed = float(np.hypot(vx, vy))
            state = "contact" if speed < 80 else "moving"
            last, last_t = (x, y), now
            lost = 0
        else:
            x = y = float("nan")
            lost += 1

        loops.append((time.perf_counter() - ls) * 1000)

        if now - last_print > 0.1:
            print(f"{now - t0:7.2f} {x:6.0f} {y:6.0f} {vx:+7.0f} {vy:+7.0f} "
                  f"{state:>8} {mode:>5} {loops[-1]:6.1f}ms")
            last_print = now

    a = np.array(loops)
    print(f"\nloop: median {np.median(a):.1f}ms  p95 {np.percentile(a, 95):.1f}ms"
          f"  max {a.max():.1f}ms  ({len(a)} iterations)")
    print(f"full-frame scans: {full_scans} of {len(a)} "
          f"({100 * full_scans / max(len(a), 1):.0f}%)")
    if np.percentile(a, 95) < 16:
        print("\n  -> fits inside a 60fps frame. A live agent has time to "
              "decide and act.")
    else:
        print("\n  -> still over budget. Lower --roi, or check the "
              "full-frame scan percentage above: frequent full scans mean "
              "the thief is being lost, not that the loop is slow.")


if __name__ == "__main__":
    main()
