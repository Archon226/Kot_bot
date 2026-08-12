"""
kot_tapper.py v3 - record your taps, replay them, anchored to the level start.

WHY THIS APPROACH CAME BACK

Session 2 abandoned time-triggered replay. Those reasons were sound, but
read what they were actually about:

  - the replay's t=0 and the demonstration's t=0 were different moments
  - detected launch times carried ~+-100ms error, wider than a jump window
  - one missed or spurious detection shifted every tap after it
  - KoT ignores taps made mid-air, so a tap log and what the game did are
    different things

EVERY ONE of those is about INFERRING taps from video. Not one of them
applies when the taps are recorded directly:

  - t=0 is now the same event in both runs (see below)
  - there is no detection, so no detection error
  - nothing can be missed, because nothing is being inferred
  - the game discards the same mid-air taps on replay that it discarded
    when you made them - identical input, identical result

restart-archive/kot-solver clears real dungeons this way with no screen
capture at all: hardcoded jump intervals and nothing else. Its author's
workflow is to log the gaps between his own key presses and paste them
into the source. This file does the same thing automatically, at
millisecond resolution, without the transcription step.

WHY THE WAYPOINT AGENT STRUGGLED AND THIS DOES NOT

kot_agent.py replays STATES: "tap when the thief is here, moving like
this". A state is not exactly reproducible - putting the thief at (553,598)
moving at 70 px/s having just left a wall at a particular angle is not
something an agent can arrange. Measured velocity mismatches ran 300-500
px/s and the run diverged by the seventh waypoint.

This file replays INPUTS. 550ms is 550ms. The game turns identical inputs
into identical outcomes because campaign physics are deterministic.

THE ANCHOR, WHICH IS THE WHOLE TRICK

t=0 must mean the same thing when recording and when replaying, so the
tool sends the level-start tap ITSELF in both modes and measures from that
tap. Not from when you pressed F6, not from the first detected motion -
from an event it controls and can reproduce exactly.

It then confirms the level really started by watching for scene motion,
because a click on an unfocused window is swallowed by Windows as an
activation click and starts nothing.

Timing is anchored to that single point rather than accumulated per tap.
Sleeping 'gap' seconds between taps lets rounding compound across a run;
measuring every tap against t0 does not.

Install:
    pip install keyboard pywin32 mss opencv-python numpy

Run:
    python kot_tapper.py                 # F6 record, F5 replay
    python kot_tapper.py taps/lvl28.json # load a specific file

Keys:
    F6  record   (sit on TAP TO START, then hands off - it starts the level)
    F5  replay   (sit on TAP TO START, then hands off)
    F4  list saved recordings
    F9  quit     (ESC aborts a replay mid-run)
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

WINDOW_TITLE = "LDPlayer"
GAME_W, GAME_H = 1280, 720
TAP_DIR = "taps"
VK_LBUTTON = 0x01


# ---------------------------------------------------------------- windows

def fix_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


def find_window(substring):
    matches = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if substring.lower() in win32gui.GetWindowText(hwnd).lower():
            matches.append((hwnd, win32gui.GetWindowText(hwnd)))

    win32gui.EnumWindows(callback, None)
    return matches[0] if matches else (None, None)


def game_region(hwnd):
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (left, top))
    right, bottom = win32gui.ClientToScreen(hwnd, (right, bottom))
    cw, ch = right - left, bottom - top
    pad_top = ch - GAME_H
    if pad_top < 0 or (cw - GAME_W) < 0:
        raise SystemExit(f"Window {cw}x{ch} smaller than game "
                         f"{GAME_W}x{GAME_H}.")
    print(f"Chrome: {pad_top}px top, {cw - GAME_W}px right")
    return {"left": left, "top": top + pad_top,
            "width": GAME_W, "height": GAME_H}


def focus_window(hwnd):
    """Windows consumes the first click on an inactive window as an
    activation click - the app never sees it. An agent that politely sends
    one tap from a focused console starts nothing at all."""
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"  could not focus the window ({e}); click LDPlayer once.")
    time.sleep(0.25)


# ------------------------------------------------------------------ input

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]


INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000


def _send(flags, x=0, y=0):
    """One SendInput call. Unlike the deprecated mouse_event, these cannot
    be interleaved with real hardware input mid-sequence."""
    inp = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(x, y, 0, flags, 0, None))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def to_absolute(sx, sy):
    vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    return (int((sx - vx) * 65535 / (vw - 1)),
            int((sy - vy) * 65535 / (vh - 1)))


def move_to(region, gx, gy):
    """Park the cursor without clicking. Doing the move ahead of time keeps
    it out of the timing-critical path."""
    ax, ay = to_absolute(region["left"] + gx, region["top"] + gy)
    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, ax, ay)


def press_release(hold):
    """Mouse down, hold, up. Returns the perf_counter at the DOWN edge,
    which is the moment the game reacts to."""
    t_down = time.perf_counter()
    _send(MOUSEEVENTF_LEFTDOWN)
    precise_sleep(hold)
    _send(MOUSEEVENTF_LEFTUP)
    return t_down


def precise_sleep(seconds):
    """Sleep accurately enough for a platformer.

    time.sleep() on Windows has ~15ms granularity by default - useless when
    a jump window is 30ms wide. Sleep most of the way, busy-spin the last
    2ms. Costs a little CPU, buys real precision.
    """
    if seconds <= 0:
        return
    end = time.perf_counter() + seconds
    coarse = seconds - 0.002
    if coarse > 0:
        time.sleep(coarse)
    while time.perf_counter() < end:
        pass


def wait_until(target):
    """Block until perf_counter reaches target. False if ESC was pressed.

    The tight spin at the end contains nothing but a clock read - no
    keyboard polling, no function calls. v1 checked ESC on every spin
    iteration and that alone cost real milliseconds.
    """
    while True:
        remaining = target - time.perf_counter()
        if remaining <= 0.002:
            break
        if remaining > 0.01:
            if keyboard.is_pressed("esc"):
                return False
            time.sleep(0.004)
    while time.perf_counter() < target:
        pass
    return True


# ------------------------------------------------------------- the anchor

def grab(sct, box):
    raw = sct.grab(box)
    arr = np.frombuffer(raw.bgra, dtype=np.uint8)
    return arr.reshape(raw.height, raw.width, 4)[:, :, :3]


def thumb(frame):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (80, 45), interpolation=cv2.INTER_AREA)


def scene_motion(a, b):
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def start_level(sct, region, hwnd, args):
    """Send the level-start tap and confirm the level actually started.

    RETURNS the perf_counter of the tap's DOWN edge. That instant is t=0
    for both recording and replay, and it is the entire reason this works:
    the same event, produced the same way, in both runs. Anchoring to when
    F6 was pressed, or to the first frame of detected motion, would put the
    two runs a variable distance apart - which is precisely the failure the
    session-2 notes recorded as '--start-gap was a guess'.

    Motion is then checked because a swallowed click looks identical to a
    successful one from the caller's side.
    """
    focus_window(hwnd)
    move_to(region, args.x, args.y)
    time.sleep(0.05)
    prev = thumb(grab(sct, region))
    t0 = press_release(args.hold)

    t_end = time.perf_counter() + args.start_timeout
    hits, peak = 0, 0.0
    while time.perf_counter() < t_end:
        cur = thumb(grab(sct, region))
        m = scene_motion(prev, cur)
        peak = max(peak, m)
        prev = cur
        if m >= args.start_motion:
            hits += 1
            if hits >= 3:
                return t0
        else:
            hits = 0
    print(f"  the scene never started moving (peak {peak:.2f}, need "
          f"{args.start_motion:.2f}).")
    print("  The level did not start. Either the start tap was swallowed "
          "as a window-activation click, or the game is not on the TAP TO "
          "START screen.")
    return None


# -------------------------------------------------------------- recording

def record(sct, region, hwnd, args):
    """Start the level, then capture taps relative to that start."""
    print("\nRECORDING. Sit on TAP TO START - the tool starts the level.")
    while keyboard.is_pressed("f6"):
        time.sleep(0.01)

    t0 = start_level(sct, region, hwnd, args)
    if t0 is None:
        return []
    print("  level running - play it. F6 to stop.\n")

    taps = []
    was_down = False
    down_t = 0.0
    down_pos = (0, 0)

    while True:
        if keyboard.is_pressed("f6"):
            break

        down = bool(win32api.GetAsyncKeyState(VK_LBUTTON) & 0x8000)

        if down and not was_down:
            mx, my = win32gui.GetCursorPos()
            down_t = time.perf_counter()
            down_pos = (mx - region["left"], my - region["top"])

        elif not down and was_down:
            gx, gy = down_pos
            t_rel = down_t - t0
            # Our own start tap is real input, so GetAsyncKeyState sees it
            # too. Recording it would replay it twice: once as the anchor,
            # once as a tap.
            if t_rel < args.blank:
                pass
            elif 0 <= gx < GAME_W and 0 <= gy < GAME_H:
                tap = {"t": round(t_rel, 4), "x": gx, "y": gy,
                       "hold": round(time.perf_counter() - down_t, 4)}
                taps.append(tap)
                print(f"  tap {len(taps):3d}  t={tap['t']:7.3f}s  "
                      f"({gx:4d},{gy:3d})  "
                      f"hold={tap['hold'] * 1000:.0f}ms")
            was_down = down
            time.sleep(0.001)
            continue

        was_down = down
        time.sleep(0.001)

    print(f"\nStopped. {len(taps)} taps captured.")
    if taps:
        gaps = np.diff([0.0] + [t["t"] for t in taps]) * 1000
        print(f"gaps from start: " + "  ".join(f"{g:.0f}" for g in gaps))
    return taps


def save(taps):
    os.makedirs(TAP_DIR, exist_ok=True)
    name = f"run_{time.strftime('%H%M%S')}.json"
    path = os.path.join(TAP_DIR, name)
    with open(path, "w") as f:
        json.dump({"anchor": "start_tap", "taps": taps}, f, indent=1)
    print(f"Saved {path}")
    return path


def load_taps(path=None):
    """Load a tap file. With no argument, the MOST RECENTLY MODIFIED file.

    v1 sorted filenames alphabetically and took the last, which repeatedly
    loaded a stale file - 'test.json' beat 'mine.json' - and silently
    replayed the wrong sequence.
    """
    def _read(p):
        with open(p) as f:
            d = json.load(f)
        if isinstance(d, list):
            print("  (old-format file: no start anchor. Re-record it - "
                  "replay will not line up.)")
            return d
        return d.get("taps", [])

    if path:
        if not os.path.isfile(path):
            print(f"No such file: {path}")
            return None, None
        return _read(path), path

    if not os.path.isdir(TAP_DIR):
        return None, None
    files = [os.path.join(TAP_DIR, f) for f in os.listdir(TAP_DIR)
             if f.endswith(".json")]
    if not files:
        return None, None
    path = max(files, key=os.path.getmtime)
    return _read(path), path


# ----------------------------------------------------------------- replay

def replay(sct, region, hwnd, taps, args):
    if not taps:
        print("Nothing recorded yet. Press F6 first.")
        return

    print(f"\nREPLAY - {len(taps)} taps. Sit on TAP TO START. ESC aborts.")
    while keyboard.is_pressed("f5"):
        time.sleep(0.01)

    t0 = start_level(sct, region, hwnd, args)
    if t0 is None:
        return

    drifts = []
    for i, tap in enumerate(taps, 1):
        target = t0 + tap["t"]

        # Park the cursor early so the move is not in the critical path.
        move_to(region, tap["x"], tap["y"])

        if not wait_until(target):
            print("\nAborted.")
            return

        t_down = press_release(tap["hold"])
        drift = (t_down - t0) - tap["t"]
        drifts.append(drift * 1000)
        print(f"  tap {i:3d}/{len(taps)}  t={tap['t']:7.3f}s  "
              f"drift {drift * 1000:+6.1f}ms")

    lo, hi = min(drifts), max(drifts)
    avg = sum(drifts) / len(drifts)
    trend = drifts[-1] - drifts[0]
    print(f"\ndrift  avg {avg:+.1f}ms   range {lo:+.1f} to {hi:+.1f}ms   "
          f"first-to-last {trend:+.1f}ms")
    if abs(trend) > 20:
        print("  -> error is ACCUMULATING. Something is wrong: every tap "
              "is measured against t0, so drift should not grow.")
    else:
        print("  -> error is noise, not accumulation. Timing is sound; if "
              "the run still failed, the cause is the game state, not the "
              "clock.")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("taps", nargs="?",
                    help="tap JSON to load (default: most recent in taps/)")
    ap.add_argument("--x", type=int, default=640,
                    help="tap x in game coords (tap-anywhere; avoid buttons)")
    ap.add_argument("--y", type=int, default=360)
    ap.add_argument("--hold", type=float, default=0.05,
                    help="hold for the start tap")
    ap.add_argument("--blank", type=float, default=0.25,
                    help="seconds after the start tap during which taps are "
                         "ignored while recording - our own start tap is "
                         "real input and would otherwise be recorded too")
    ap.add_argument("--start-motion", type=float, default=0.08,
                    dest="start_motion",
                    help="scene motion that counts as 'the level started'")
    ap.add_argument("--start-timeout", type=float, default=6.0,
                    dest="start_timeout")
    cli = ap.parse_args()

    fix_dpi()
    hwnd, title = find_window(WINDOW_TITLE)
    if not hwnd:
        print(f"No window matching '{WINDOW_TITLE}'. Is the emulator open?")
        return

    region = game_region(hwnd)
    print(f"Found: {title}")
    print(f"Game:   {region['width']}x{region['height']} "
          f"at ({region['left']}, {region['top']})")

    taps, path = load_taps(cli.taps)
    if taps:
        print(f"Loaded {len(taps)} taps from {path}")

    print("\nF6 = record   F5 = replay   F4 = list   F9 = quit")
    print("Both record and replay start the level themselves - sit on "
          "TAP TO START and let go of the mouse.\n")

    with mss.MSS() as sct:
        while True:
            if keyboard.is_pressed("f9"):
                print("Bye.")
                break

            if keyboard.is_pressed("f6"):
                new = record(sct, region, hwnd, cli)
                if new:
                    taps = new
                    save(taps)
                time.sleep(0.4)

            if keyboard.is_pressed("f5"):
                replay(sct, region, hwnd, taps, cli)
                time.sleep(0.4)

            if keyboard.is_pressed("f4"):
                if os.path.isdir(TAP_DIR):
                    for f in sorted(os.listdir(TAP_DIR)):
                        print(" ", f)
                else:
                    print("  (no recordings yet)")
                time.sleep(0.4)

            time.sleep(0.01)


if __name__ == "__main__":
    main()