"""
kot_track.py - step 3 (v2, colour) of the King of Thieves bot.

Records a run to disk: downscaled greyscale frames, their timestamps, and
(in own-run mode) your real tap times. Detection happens OFFLINE against
the saved footage, so you can iterate on the detector without replaying
the level every time.

Two modes:
    --own     you play, taps are recorded as ground truth
    --ghost   ghost potion replay, no taps to record

Own-run mode first. Without ground truth you cannot tell a working motion
detector from a broken one - you need runs where you already know exactly
when the taps happened, so you can score the detector against them.

Why frames go to disk rather than RAM: at 640x360 greyscale that is
~230KB/frame, ~15MB/s. A two-minute run is ~1.8GB - too much to hold in
memory, trivial for an SSD to write sequentially.

Why nothing is analysed during capture: adding work to the capture loop
risks dropping frames, and a dropped frame is a jump you can never get
back. Capture cheap, think later.

Install:
    pip install mss numpy opencv-python keyboard pywin32

Run:
    python kot_track.py --own
    python kot_track.py --ghost
    python kot_track.py --list

Keys while armed:
    F6  - start / stop recording
    F9  - quit
"""

import argparse
import ctypes
import json
import os
import time
from collections import deque

import cv2
import keyboard
import mss
import numpy as np
import win32api
import win32gui

WINDOW_TITLE = "BlueStacks"
GAME_W = 1280
GAME_H = 720

# Capture scale. 0.5 keeps the thief ~20px across - plenty to track -
# while cutting bytes and per-frame cost by 4x.
SCALE = 0.5

REC_DIR = "runs"
VK_LBUTTON = 0x01


def fix_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


def find_window(substring):
    matches = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if substring.lower() in t.lower():
                matches.append((hwnd, t))

    win32gui.EnumWindows(cb, None)
    return matches[0] if matches else (None, None)


def game_region(hwnd):
    l, t, r, b = win32gui.GetClientRect(hwnd)
    l, t = win32gui.ClientToScreen(hwnd, (l, t))
    r, b = win32gui.ClientToScreen(hwnd, (r, b))
    cw, ch = r - l, b - t
    pad_top = ch - GAME_H
    if pad_top < 0 or cw - GAME_W < 0:
        raise SystemExit(f"Window {cw}x{ch} smaller than game {GAME_W}x{GAME_H}.")
    print(f"Chrome: {pad_top}px top, {cw - GAME_W}px right")
    return {"left": l, "top": t + pad_top, "width": GAME_W, "height": GAME_H}


def grab_bgr(sct, region, w, h):
    """One frame, downscaled, BGR uint8.

    Colour is kept because the thief wears a bright green costume and hue
    separates it from the desaturated dungeon far more cleanly than
    brightness ever could. Costs 3x the bytes of greyscale; worth it.

    INTER_AREA is the right interpolation for shrinking - it averages the
    source pixels instead of sampling one, so a small sprite does not
    flicker in and out between frames as it crosses pixel boundaries.
    """
    raw = sct.grab(region)
    arr = np.frombuffer(raw.bgra, dtype=np.uint8)
    bgr = arr.reshape(raw.height, raw.width, 4)[:, :, :3]
    return cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA)


def motion_level(prev_small, small):
    """Cheap scene-motion metric: mean abs difference on a tiny greyscale
    thumbnail. Costs ~0.1ms, so it can run inside the capture loop without
    risking dropped frames."""
    return float(np.abs(small.astype(np.int16) - prev_small).mean())


def thumb(frame):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (80, 45), interpolation=cv2.INTER_AREA)


def motion_scan(region):
    """Print the live motion metric so you can pick a threshold.

    Watch it while the game sits idle, then while a run is playing. Set
    --motion-thresh between the two.
    """
    w = int(GAME_W * SCALE)
    h = int(GAME_H * SCALE)
    print("\nMotion scan. Idle value vs running value. F6 to stop.\n")
    while keyboard.is_pressed("f6"):
        time.sleep(0.01)

    with mss.MSS() as sct:
        prev = thumb(grab_bgr(sct, region, w, h))
        last_print = 0.0
        while not keyboard.is_pressed("f6"):
            cur = thumb(grab_bgr(sct, region, w, h))
            m = motion_level(prev, cur)
            prev = cur
            now = time.perf_counter()
            if now - last_print > 0.25:
                bar = "#" * min(60, int(m * 4))
                print(f"  {m:6.2f}  {bar}")
                last_print = now
            time.sleep(0.01)
    print("\nStopped.")


def record(region, mode, args_auto=None):
    w = int(GAME_W * SCALE)
    h = int(GAME_H * SCALE)
    frame_bytes = w * h * 3

    os.makedirs(REC_DIR, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    base = os.path.join(REC_DIR, f"{mode}_{stamp}")
    raw_path = base + ".raw"
    meta_path = base + ".json"

    print(f"\nRECORDING ({mode}). {w}x{h} colour, "
          f"{frame_bytes / 1024:.0f}KB/frame. F6 to stop.\n")

    times = []
    taps = []
    was_down = False
    down_t = 0.0
    dropped_warn = False

    while keyboard.is_pressed("f6"):
        time.sleep(0.01)

    # In auto mode we hold a short pre-roll so the first frames of the run
    # are not lost while motion is still being confirmed. Without it the
    # recording starts a few frames late and the first jump is clipped.
    preroll = deque(maxlen=args_auto["preroll"]) if args_auto else None
    armed = bool(args_auto)
    running = not armed
    quiet_since = None
    prev_small = None

    if armed:
        print("  ARMED - waiting for motion. F6 aborts.")

    t0 = time.perf_counter()

    with open(raw_path, "wb", buffering=1024 * 1024) as fh, mss.MSS() as sct:
        while True:
            if keyboard.is_pressed("f6"):
                break

            loop_start = time.perf_counter()
            frame = grab_bgr(sct, region, w, h)

            if armed:
                small = thumb(frame)
                m = 0.0 if prev_small is None else motion_level(prev_small,
                                                                small)
                prev_small = small

                if not running:
                    preroll.append((loop_start, frame))
                    if m >= args_auto["thresh"]:
                        # Rewind: write the buffered frames first, and
                        # rebase t0 to the earliest of them.
                        t0 = preroll[0][0]
                        for pt, pf in preroll:
                            fh.write(pf.tobytes())
                            times.append(round(pt - t0, 5))
                        preroll.clear()
                        running = True
                        print(f"  motion detected ({m:.1f}) - RECORDING")
                        # The current frame was appended to the pre-roll at
                        # the top of this branch and has just been written
                        # with the rest. Falling through would write it a
                        # SECOND time with an identical timestamp, and a
                        # duplicate timestamp makes np.gradient divide by
                        # zero - corrupting velocity and acceleration for
                        # the entire run.
                        time.sleep(0.001)
                        continue
                    else:
                        time.sleep(0.001)
                        continue
                else:
                    if m < args_auto["thresh"] * 0.4:
                        quiet_since = quiet_since or loop_start
                        if loop_start - quiet_since > args_auto["quiet"]:
                            print(f"  quiet for {args_auto['quiet']}s - "
                                  f"stopping")
                            break
                    else:
                        quiet_since = None

            fh.write(frame.tobytes())
            times.append(round(loop_start - t0, 5))

            # Ground-truth taps. Only meaningful in own-run mode; in ghost
            # mode there is no human input to capture.
            if mode == "own":
                down = bool(win32api.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
                if down and not was_down:
                    down_t = time.perf_counter()
                elif not down and was_down:
                    taps.append({
                        "t": round(down_t - t0, 4),
                        "hold": round(time.perf_counter() - down_t, 4),
                    })
                was_down = down

            spent = time.perf_counter() - loop_start
            if spent > 0.030 and not dropped_warn:
                print(f"  WARNING: a frame took {spent * 1000:.0f}ms. "
                      f"Close other apps - you are losing frames.")
                dropped_warn = True

    total = time.perf_counter() - t0
    n = len(times)
    size_mb = (n * frame_bytes) / 1e6

    meta = {
        "mode": mode,
        "width": w,
        "height": h,
        "scale": SCALE,
        "frames": n,
        "duration": round(total, 3),
        "fps": round(n / total, 1) if total else 0,
        "frame_bytes": frame_bytes,
        "channels": 3,
        "raw": os.path.basename(raw_path),
        "taps": taps,
        # Per-frame capture times. Essential: frame spacing is NOT even
        # (median 16.6ms, p95 20.6ms, stalls to 30ms+), so assuming
        # uniform spacing accumulates hundreds of ms of error across a
        # run and makes any timing analysis downstream wrong.
        "times": times,
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=1)

    print(f"\nStopped. {n} frames in {total:.1f}s "
          f"({meta['fps']} fps, {size_mb:.0f}MB)")
    if mode == "own":
        print(f"{len(taps)} taps recorded as ground truth.")
    print(f"Saved {raw_path}")
    print(f"Saved {meta_path}")

    # Frame intervals tell you whether capture was steady. A detector that
    # assumes even spacing will misread timings if it was not.
    if n > 2:
        gaps = np.diff(times) * 1000
        print(f"Frame gap: median {np.median(gaps):.1f}ms  "
              f"p95 {np.percentile(gaps, 95):.1f}ms  max {gaps.max():.1f}ms")
        if gaps.max() > 50:
            print("  -> big stalls present. Timestamps are saved per frame, "
                  "so analysis can still be correct, but detection will be "
                  "less precise around the gaps.")


def list_runs():
    if not os.path.isdir(REC_DIR):
        print("No runs yet.")
        return
    for f in sorted(os.listdir(REC_DIR)):
        if f.endswith(".json"):
            with open(os.path.join(REC_DIR, f)) as fh:
                m = json.load(fh)
            print(f"  {f:28s} {m['mode']:6s} {m['frames']:5d} frames  "
                  f"{m['duration']:6.1f}s  {m['fps']:5.1f}fps  "
                  f"{len(m['taps']):3d} taps")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--own", action="store_true",
                   help="you play; taps recorded as ground truth")
    g.add_argument("--ghost", action="store_true",
                   help="ghost potion replay; no taps to record")
    g.add_argument("--list", action="store_true", help="list saved runs")
    ap.add_argument("--auto", action="store_true",
                    help="start recording when the scene starts moving, "
                         "stop when it goes quiet")
    ap.add_argument("--motion-thresh", type=float, default=0.08,
                    dest="motion_thresh",
                    help="mean abs frame difference that counts as motion; "
                         "find yours with --motion-scan")
    ap.add_argument("--quiet-secs", type=float, default=1.2,
                    dest="quiet_secs",
                    help="seconds of stillness before auto-stop")
    ap.add_argument("--preroll", type=int, default=20,
                    help="frames of pre-roll kept before motion is "
                         "confirmed, so the start is not clipped")
    ap.add_argument("--motion-scan", action="store_true", dest="motion_scan",
                    help="print the live motion metric and exit")
    args = ap.parse_args()

    if args.list:
        list_runs()
        return

    mode = "ghost" if args.ghost else "own"

    fix_dpi()
    hwnd, title = find_window(WINDOW_TITLE)
    if not hwnd:
        print(f"No window matching '{WINDOW_TITLE}'. Is the emulator open?")
        return

    region = game_region(hwnd)
    print(f"Found: {title}")
    print(f"Mode:  {mode}")
    auto_cfg = None
    if args.auto:
        auto_cfg = {"thresh": args.motion_thresh,
                    "quiet": args.quiet_secs,
                    "preroll": args.preroll}
        print(f"Auto: start above {args.motion_thresh}, stop after "
              f"{args.quiet_secs}s quiet, {args.preroll} frames pre-roll")

    if args.motion_scan:
        motion_scan(region)
        return

    print("\nF6 = start/stop recording   F9 = quit\n")

    while True:
        if keyboard.is_pressed("f9"):
            print("Bye.")
            break
        if keyboard.is_pressed("f6"):
            record(region, mode, auto_cfg)
            time.sleep(0.4)
        time.sleep(0.01)


if __name__ == "__main__":
    main()
