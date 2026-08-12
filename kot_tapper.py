"""
kot_tapper.py - step 2 of the King of Thieves bot. (v2)

Records your taps on a level with millisecond timestamps, then replays
them. Because campaign levels are fixed and the physics are deterministic,
a good recording can clear a level on its own.

v2 fixes:
  - drift is now measured at the mouse-DOWN edge, not after mouse-up.
    v1 included the tap's hold duration in the drift figure, so a 100ms
    hold looked like 100ms of lateness. It wasn't. The down edge is what
    the game reacts to, so that's what we measure.
  - the tight wait loop no longer calls keyboard.is_pressed() on every
    iteration. That call is slow and was adding real jitter. ESC is now
    polled only during the coarse sleep phase.
  - replay prints a drift summary at the end so you can see at a glance
    whether error is accumulating or just noisy.

Install:
    pip install keyboard pywin32

Run:
    python kot_tapper.py

Keys:
    F6  - start / stop recording
    F5  - replay the last recording
    F4  - list saved recordings
    F9  - quit  (also: ESC aborts a replay mid-run)

Recordings are saved as JSON in taps/.
"""

import argparse
import ctypes
import json
import os
import time
from ctypes import wintypes

import keyboard
import win32api
import win32con
import win32gui

WINDOW_TITLE = "LDPlayer"
GAME_W = 1280
GAME_H = 720
TAP_DIR = "taps"

VK_LBUTTON = 0x01


# ---------------------------------------------------------------- windows

def fix_dpi():
    """Same as the probe - stop Windows virtualising our coordinates."""
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
    """Game surface only, chrome stripped - identical logic to the probe."""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (left, top))
    right, bottom = win32gui.ClientToScreen(hwnd, (right, bottom))
    cw, ch = right - left, bottom - top

    pad_top = ch - GAME_H
    if pad_top < 0 or (cw - GAME_W) < 0:
        raise SystemExit(f"Window {cw}x{ch} smaller than game {GAME_W}x{GAME_H}.")

    print(f"Chrome: {pad_top}px top, {cw - GAME_W}px right")
    return {"left": left, "top": top + pad_top, "width": GAME_W, "height": GAME_H}


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
    """One SendInput call. SendInput is the modern replacement for the
    deprecated mouse_event, and unlike it, events can't be interleaved
    with real hardware input mid-sequence."""
    inp = INPUT(type=INPUT_MOUSE,
                mi=MOUSEINPUT(x, y, 0, flags, 0, None))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def to_absolute(sx, sy):
    """Screen pixels -> SendInput's 0..65535 virtual-desktop space."""
    vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    return (int((sx - vx) * 65535 / (vw - 1)),
            int((sy - vy) * 65535 / (vh - 1)))


def move_to(region, gx, gy):
    """Park the cursor without clicking. Doing the move ahead of time
    keeps it out of the timing-critical path."""
    ax, ay = to_absolute(region["left"] + gx, region["top"] + gy)
    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, ax, ay)


def press_release(hold):
    """Mouse down, hold, up. Returns the perf_counter at the DOWN edge,
    which is the moment the game actually responds to."""
    t_down = time.perf_counter()
    _send(MOUSEEVENTF_LEFTDOWN)
    precise_sleep(hold)
    _send(MOUSEEVENTF_LEFTUP)
    return t_down


def precise_sleep(seconds):
    """Sleep accurately enough for a platformer.

    time.sleep() on Windows has ~15ms granularity by default - useless
    when a jump window is 30ms wide. So we sleep most of the way, then
    busy-spin the last 2ms. Costs a little CPU, buys real precision.
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
    """Block until perf_counter reaches target. Returns False if the user
    hit ESC during the coarse phase.

    The tight spin at the end deliberately contains nothing but a clock
    read - no keyboard polling, no function calls. v1 checked ESC on
    every spin iteration and that alone cost real milliseconds.
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


# -------------------------------------------------------------- recording

def record(region):
    """Capture taps until F6 again. Returns list of {t, x, y, hold}."""
    print("\nRECORDING. Play the level. F6 to stop.\n")
    taps = []
    t0 = time.perf_counter()
    was_down = False
    down_t = 0.0
    down_pos = (0, 0)

    while keyboard.is_pressed("f6"):
        time.sleep(0.01)

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
            if 0 <= gx < GAME_W and 0 <= gy < GAME_H:
                tap = {
                    "t": round(down_t - t0, 4),
                    "x": gx,
                    "y": gy,
                    "hold": round(time.perf_counter() - down_t, 4),
                }
                taps.append(tap)
                print(f"  tap {len(taps):3d}  t={tap['t']:7.3f}s  "
                      f"({gx:4d},{gy:3d})  hold={tap['hold'] * 1000:.0f}ms")

        was_down = down
        time.sleep(0.001)

    print(f"\nStopped. {len(taps)} taps captured.")
    return taps


def save(taps):
    os.makedirs(TAP_DIR, exist_ok=True)
    name = f"run_{time.strftime('%H%M%S')}.json"
    path = os.path.join(TAP_DIR, name)
    with open(path, "w") as f:
        json.dump(taps, f, indent=1)
    print(f"Saved {path}")
    return path


def load_taps(path=None):
    """Load a tap file. With no argument, take the MOST RECENTLY MODIFIED
    file in taps/.

    Previously this sorted filenames alphabetically and took the last,
    which repeatedly loaded a stale file - 'test.json' beat 'mine.json',
    'run_233941.json' beat 'g2.json' - and silently replayed the wrong
    sequence. Modification time is what the user actually means by "the
    one I just made".
    """
    if path:
        if not os.path.isfile(path):
            print(f"No such file: {path}")
            return None, None
        with open(path) as f:
            return json.load(f), path

    if not os.path.isdir(TAP_DIR):
        return None, None
    files = [os.path.join(TAP_DIR, f) for f in os.listdir(TAP_DIR)
             if f.endswith(".json")]
    if not files:
        return None, None
    path = max(files, key=os.path.getmtime)
    with open(path) as f:
        return json.load(f), path


# ---------------------------------------------------------------- replay

def replay(region, taps, lead_in=2.0):
    """Fire the recorded taps back at the same relative times.

    Timing is anchored to a single start point rather than accumulated
    per-tap. Sleeping 'gap' seconds between taps would let rounding error
    compound across a run; measuring every tap against t0 does not.
    """
    if not taps:
        print("Nothing recorded yet. Press F6 first.")
        return

    print(f"\nREPLAY in {lead_in:.0f}s - {len(taps)} taps. "
          f"ESC aborts.\nGet the level to its start position.")
    precise_sleep(lead_in)

    drifts = []
    t0 = time.perf_counter()

    for i, tap in enumerate(taps, 1):
        target = t0 + tap["t"]

        # Park the cursor early so the move isn't in the critical path.
        move_to(region, tap["x"], tap["y"])

        if not wait_until(target):
            print("\nAborted.")
            return

        t_down = press_release(tap["hold"])
        drift = (t_down - t0) - tap["t"]
        drifts.append(drift * 1000)
        print(f"  tap {i:3d}/{len(taps)}  drift {drift * 1000:+6.1f}ms")

    lo, hi = min(drifts), max(drifts)
    avg = sum(drifts) / len(drifts)
    trend = drifts[-1] - drifts[0]
    print(f"\ndrift  avg {avg:+.1f}ms   range {lo:+.1f} to {hi:+.1f}ms   "
          f"first-to-last {trend:+.1f}ms")
    if abs(trend) > 20:
        print("  -> error is ACCUMULATING. Open-loop replay will not hold "
              "on longer levels; you need visual resync.")
    else:
        print("  -> error is noise, not accumulation. Open-loop replay is "
              "viable at this length.")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("taps", nargs="?", help="tap JSON to load (default: "
                                            "most recent in taps/)")
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

    print("\nF6 = record   F5 = replay   F4 = list   F9 = quit\n")

    while True:
        if keyboard.is_pressed("f9"):
            print("Bye.")
            break

        if keyboard.is_pressed("f6"):
            taps = record(region)
            if taps:
                save(taps)
            time.sleep(0.4)

        if keyboard.is_pressed("f5"):
            replay(region, taps)
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