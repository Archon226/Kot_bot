"""
kot_rec.py - record your taps, replay them. Two keys, nothing else.

    F6  record   (wipes the previous recording and starts fresh)

Use --file to give each base its own recording.
    F5  replay
    F9  quit     (ESC aborts a replay mid-run)

Both record and replay send the level-start tap THEMSELVES, and time
everything from that tap. That is the whole trick: t=0 is the same event in
both runs, so the replay lines up. Anchoring to when you pressed a key, or
to a fixed delay, does not - the two runs end up a variable distance apart,
which is what made earlier attempts drift.

Sit on the screen where the level is about to start, press the key, and
take your hand off the mouse. For recording, play the level once the thief
appears; the tool ignores anything in the first --blank seconds so its own
start tap is not recorded as one of yours.

Timing is measured against t0 for every tap, not accumulated between them,
so rounding cannot compound across a run.

Usage:
    python kot_rec.py --title BlueStacks --region-x 228
    python kot_rec.py --title BlueStacks --region-x 228 --no-start-tap
"""

import argparse
import ctypes
import json
import os
import time
from ctypes import wintypes

import cv2
import keyboard
import mss
import numpy as np
import win32api
import win32con
import win32gui

GAME_W, GAME_H = 1280, 720
VK_LBUTTON = 0x01


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


def game_region(hwnd, args):
    l, t, r, b = win32gui.GetClientRect(hwnd)
    l, t = win32gui.ClientToScreen(hwnd, (l, t))
    r, b = win32gui.ClientToScreen(hwnd, (r, b))
    cw, ch = r - l, b - t
    if cw - args.region_x < GAME_W or ch - args.region_y < GAME_H:
        raise SystemExit(f"Window {cw}x{ch} at offset "
                         f"({args.region_x},{args.region_y}) cannot hold "
                         f"{GAME_W}x{GAME_H}.")
    print(f"Client {cw}x{ch}; capturing {GAME_W}x{GAME_H} at "
          f"({args.region_x},{args.region_y})")
    return {"left": l + args.region_x, "top": t + args.region_y,
            "width": GAME_W, "height": GAME_H}


def focus(hwnd):
    """Windows swallows the first click on an inactive window as an
    activation click, so an unfocused emulator ignores the start tap."""
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"  could not focus ({e}); click the emulator once.")
    time.sleep(0.25)


# ------------------------------------------------------------------ input

class MI(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class IN(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", MI)]


MOVE, DOWN, UP, ABS = 0x0001, 0x0002, 0x0004, 0x8000


def _send(flags, x=0, y=0):
    i = IN(type=0, mi=MI(x, y, 0, flags, 0, None))
    ctypes.windll.user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(IN))


def move_to(region, gx, gy):
    sx, sy = region["left"] + gx, region["top"] + gy
    vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    _send(MOVE | ABS, int((sx - vx) * 65535 / (vw - 1)),
          int((sy - vy) * 65535 / (vh - 1)))


def sleep_precise(sec):
    """time.sleep has ~15ms granularity on Windows, useless against a 30ms
    jump window. Sleep most of it, spin the last 2ms."""
    if sec <= 0:
        return
    end = time.perf_counter() + sec
    if sec > 0.002:
        time.sleep(sec - 0.002)
    while time.perf_counter() < end:
        pass


def click(hold):
    t = time.perf_counter()
    _send(DOWN)
    sleep_precise(hold)
    _send(UP)
    return t


# ------------------------------------------------------------- the anchor

def grab(sct, box):
    raw = sct.grab(box)
    a = np.frombuffer(raw.bgra, dtype=np.uint8)
    return a.reshape(raw.height, raw.width, 4)[:, :, :3]


def thumb(f):
    return cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (80, 45),
                      interpolation=cv2.INTER_AREA)


def start(sct, region, hwnd, args):
    """Start the level and return the instant that counts as t=0.

    Returns the perf_counter of the start tap's DOWN edge. Recording and
    replay both produce that event the same way, which is what makes the
    two runs comparable at all.
    """
    focus(hwnd)
    move_to(region, args.x, args.y)
    time.sleep(0.05)
    prev = thumb(grab(sct, region))

    if args.start_tap:
        t0 = click(args.hold)
    else:
        print("  start the level yourself...")
        t0 = None

    end = time.perf_counter() + args.timeout
    hits, peak = 0, 0.0
    while time.perf_counter() < end:
        cur = thumb(grab(sct, region))
        m = float(np.abs(cur.astype(np.int16) - prev.astype(np.int16)).mean())
        peak = max(peak, m)
        prev = cur
        if m >= args.motion:
            hits += 1
            if hits >= 3:
                return t0 if t0 is not None else time.perf_counter()
        else:
            hits = 0
    print(f"  the scene never moved (peak {m:.2f}, need {args.motion:.2f}). "
          f"Level did not start.")
    return None


# ------------------------------------------------------------------- record

def record(sct, region, hwnd, args):
    print("\nRECORD - previous taps wiped. Starting the level; then play.")
    while keyboard.is_pressed("f6"):
        time.sleep(0.01)

    t0 = start(sct, region, hwnd, args)
    if t0 is None:
        return None
    print("  GO. F6 again to stop.\n")

    taps = []
    was = False
    down_t = 0.0
    while not keyboard.is_pressed("f6"):
        d = bool(win32api.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        if d and not was:
            down_t = time.perf_counter()
        elif not d and was:
            rel = down_t - t0
            # Our own start tap is real input and GetAsyncKeyState sees it,
            # so it would be recorded and then replayed twice.
            if rel >= args.blank:
                taps.append({"t": round(rel, 4),
                             "hold": round(time.perf_counter() - down_t, 4)})
                print(f"  tap {len(taps):2d}  t={rel:6.3f}s")
        was = d
        time.sleep(0.001)

    print(f"\n{len(taps)} taps.")
    return taps


def save(taps, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    json.dump({"anchor": "start_tap", "taps": taps}, open(path, "w"),
              indent=1)
    print(f"saved {path}")


def load(path):
    if not os.path.isfile(path):
        return []
    d = json.load(open(path))
    return d.get("taps", d if isinstance(d, list) else [])


# ------------------------------------------------------------------ replay

def replay(sct, region, hwnd, taps, args):
    if not taps:
        print("Nothing recorded. Press F6 first.")
        return
    print(f"\nREPLAY - {len(taps)} taps. ESC aborts.")
    while keyboard.is_pressed("f5"):
        time.sleep(0.01)

    t0 = start(sct, region, hwnd, args)
    if t0 is None:
        return

    for i, tp in enumerate(taps, 1):
        target = t0 + tp["t"] + args.offset
        while True:
            left = target - time.perf_counter()
            if left <= 0.002:
                break
            if left > 0.01:
                if keyboard.is_pressed("esc") or keyboard.is_pressed("f9"):
                    print("aborted.")
                    return
                time.sleep(0.004)
        while time.perf_counter() < target:
            pass
        d = click(tp["hold"])
        print(f"  tap {i:2d}/{len(taps)}  t={tp['t']:6.3f}s  "
              f"drift {((d - t0) - tp['t'] - args.offset) * 1000:+5.1f}ms")
    print("done.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="taps/last.json",
                    help="where to save and load taps. Give each base its "
                         "own file, e.g. taps/base130.json - the campaign "
                         "levels and raid bases are all different runs")
    ap.add_argument("--title", default="LDPlayer",
                    help="emulator window title substring")
    ap.add_argument("--region-x", type=int, default=0, dest="region_x",
                    help="px to shift capture right (BlueStacks ad panel "
                         "is about 228)")
    ap.add_argument("--region-y", type=int, default=0, dest="region_y")
    ap.add_argument("--no-start-tap", action="store_false", dest="start_tap",
                    help="do not send the start tap; start it yourself")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="shift every replayed tap by this many seconds")
    ap.add_argument("--blank", type=float, default=0.25,
                    help="ignore taps this soon after the start tap")
    ap.add_argument("--motion", type=float, default=0.08,
                    help="scene motion that counts as the level starting")
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--hold", type=float, default=0.05)
    ap.add_argument("--x", type=int, default=640)
    ap.add_argument("--y", type=int, default=360)
    args = ap.parse_args()

    fix_dpi()
    hwnd, title = find_window(args.title)
    if not hwnd:
        print(f"No window matching '{args.title}'.")
        return
    region = game_region(hwnd, args)
    print(f"Found: {title}")

    taps = load(args.file)
    if taps:
        print(f"loaded {len(taps)} taps from {args.file}")
    print("\nF6 = record   F5 = replay   F9 = quit\n")

    with mss.MSS() as sct:
        while True:
            if keyboard.is_pressed("f9"):
                print("bye.")
                return
            if keyboard.is_pressed("f6"):
                new = record(sct, region, hwnd, args)
                if new:
                    taps = new
                    save(taps, args.file)
                time.sleep(0.5)
            if keyboard.is_pressed("f5"):
                replay(sct, region, hwnd, taps, args)
                time.sleep(0.5)
            time.sleep(0.01)


if __name__ == "__main__":
    main()