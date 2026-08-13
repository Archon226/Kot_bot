"""
kot_reconnect.py - watchdog that clicks through disconnect/reconnect
screens automatically, so a dropped connection doesn't leave automation
sitting in front of a dialog that will never clear on its own.

WHY TEMPLATE MATCHING, NOT A FIXED "RECONNECT BUTTON" COORD

Different disconnect states show different dialogs at different
positions ("Connection lost", "Session expired", a plain reconnect
spinner, a login-again screen). A single hardcoded (x, y) only survives
the one dialog you tested against. This matches N saved templates
against the live frame every poll and clicks whichever one appears,
same as kot_skipper.py's find_anchor() but polling continuously
instead of once per base.

CROP TEMPLATES TO THE DIALOG PANEL, NOT THE WHOLE FRAME

A full-frame template is mostly background - blurred base, HUD, gold,
countdown - none of which identifies the dialog and all of which
changes. Measured on the two real dialogs from this account:

    full frame:  own dialog 0.997, other dialog 0.813   (margin 0.18)
    panel only:  own dialog 1.000, other dialog 0.655   (margin 0.35)

A full-frame template also always matches at (0,0), because there is
only one place a 1280x720 template can sit, and it would fail outright
if a disconnect happened on a different screen than the one captured.

The margin matters because the two dialogs put RECONNECT 126px apart -
CONNECTION LOST at (768,540), the inactivity one at (642,522) - so a
cross-match clicks bare panel and achieves nothing.

CALIBRATION IS MANUAL AND ON PURPOSE

There's no way to know what your disconnect screens look like without
seeing one, so this ships with no templates. Run --calibrate, force a
disconnect (turn off wifi on the host for a few seconds), press F6 when
the dialog appears, drag a box around the DIALOG PANEL, then click the
button to record where to tap.

WHY POLL SLOWLY

Unlike kot_agent's jump timing, nothing here is time-critical to the
millisecond - a disconnect dialog sits there until clicked. Polling
every 2s costs nothing and avoids catching a half-drawn transition.

Install:
    pip install keyboard pywin32 mss opencv-python numpy

Run:
    python kot_reconnect.py --calibrate      # teach it a dialog
    python kot_reconnect.py                  # run the watchdog
    python kot_reconnect.py --dry-run        # detect only, never click

Keys (calibrate mode):
    F6  capture the current frame and mark a template
    F9  quit

Keys (watchdog mode):
    F9  quit
"""

import argparse
import ctypes
import json
import os
import random
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
TEMPLATE_DIR = "reconnect_templates"
LOG_FILE = "reconnect_log.txt"

# Set by the F9 hotkey. A POLLED keyboard.is_pressed() only sees the key
# if it happens to be down at that instant, and this loop sleeps 2s
# between checks and up to ~30s during a dismissal sequence - so a normal
# press was simply missed. A hotkey fires on the keypress itself, whatever
# the loop is doing.
STOP = False


def request_stop():
    global STOP
    STOP = True
    print("\n  F9 - stopping after the current step...")


def naptime(seconds, step=0.05):
    """Sleep, but notice F9. Long sleeps are where quit requests died."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        if STOP:
            return False
        time.sleep(min(step, max(0.0, end - time.perf_counter())))
    return not STOP


# ---------------------------------------------------------------- windows

def fix_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


def find_window(substring):
    matches = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if substring.lower() in win32gui.GetWindowText(hwnd).lower():
            matches.append((hwnd, win32gui.GetWindowText(hwnd)))

    win32gui.EnumWindows(cb, None)
    return matches[0] if matches else (None, None)


def game_region(hwnd, args=None):
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (left, top))
    right, bottom = win32gui.ClientToScreen(hwnd, (right, bottom))
    cw, ch = right - left, bottom - top
    ox = getattr(args, "region_x", 0) or 0
    oy = getattr(args, "region_y", 0) or 0
    if cw - ox < GAME_W or ch - oy < GAME_H:
        raise SystemExit(f"Window {cw}x{ch} at offset ({ox},{oy}) cannot "
                         f"hold {GAME_W}x{GAME_H}.")
    if ox or oy:
        print(f"  capturing at offset ({ox},{oy})")
    return {"left": left + ox, "top": top + oy,
            "width": GAME_W, "height": GAME_H}


def focus_window(hwnd):
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"  could not focus the window ({e}); click the emulator once.")
    time.sleep(0.25)


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


def click_at(region, gx, gy, hold=0.05):
    sx, sy = region["left"] + gx, region["top"] + gy
    vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
          int((sx - vx) * 65535 / (vw - 1)), int((sy - vy) * 65535 / (vh - 1)))
    time.sleep(0.02)
    _send(MOUSEEVENTF_LEFTDOWN)
    time.sleep(hold)
    _send(MOUSEEVENTF_LEFTUP)


# ------------------------------------------------------------- perception

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
    """Wait until the screen stops changing before/after a click.

    Clicking or reading mid-transition either misses the dialog or
    double-fires on a half-drawn frame. Same idea as kot_skipper's
    wait_settled - just reused here for a different transition.

    NOTE this says the screen is STILL, not that the dialog is GONE. A
    swallowed click settles exactly as well as a successful one, so it
    cannot be used as proof the click worked - that is verify_gone().
    """
    prev = thumb(grab(sct, region))
    still_since = None
    t_end = time.perf_counter() + timeout
    while time.perf_counter() < t_end:
        if STOP:
            return False
        time.sleep(0.05)
        cur = thumb(grab(sct, region))
        m = scene_motion(prev, cur)
        prev = cur
        if m < 0.5:
            still_since = still_since or time.perf_counter()
            if time.perf_counter() - still_since >= quiet:
                return True
        else:
            still_since = None
    return False


# -------------------------------------------------------------- templates

def load_templates(folder):
    """Each template is <name>.png plus <name>.json holding the click
    point relative to the template's top-left match position."""
    templates = []
    if not os.path.isdir(folder):
        return templates
    for fn in sorted(os.listdir(folder)):
        if not fn.endswith(".png") or fn.startswith("_"):
            continue          # _name.png is a helper image, not a dialog
        name = fn[:-4]
        meta_path = os.path.join(folder, name + ".json")
        if not os.path.isfile(meta_path):
            print(f"  skipping {fn}: no matching {name}.json")
            continue
        img = cv2.imread(os.path.join(folder, fn))
        if img is None:
            print(f"  skipping {fn}: could not read it")
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        if img.shape[0] >= GAME_H and img.shape[1] >= GAME_W:
            print(f"  NOTE {name}.png is a full frame ({img.shape[1]}x"
                  f"{img.shape[0]}). It can only ever match at (0,0), its "
                  f"score is dominated by the background rather than the "
                  f"dialog, and it will fail if the disconnect happens on "
                  f"a different screen. Re-cut it to the panel.")
        templates.append({"name": name, "img": img,
                          "click_dx": meta["click_dx"],
                          "click_dy": meta["click_dy"],
                          "thresh": meta.get("thresh", 0.85)})
    return templates


def find_anchor(frame, tmpl, thresh):
    if tmpl is None or tmpl.shape[0] > frame.shape[0] \
            or tmpl.shape[1] > frame.shape[1]:
        return None
    res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < thresh:
        return None
    return loc[0], loc[1], score


def best_match(frame, templates):
    """Score EVERY template and return the strongest.

    Taking the first template over threshold makes the choice a coin
    flip decided by filename order when two dialogs resemble each other.
    Measured cross-match on the real pair was 0.813 against a 0.85
    threshold - close enough to fire on the wrong one - and their
    RECONNECT buttons sit 126px apart, so the wrong match clicks bare
    panel and the dialog stays put. One extra matchTemplate per poll is
    nothing at a 2s interval.
    """
    best = None
    for t in templates:
        hit = find_anchor(frame, t["img"], t["thresh"])
        if hit is None:
            continue
        if best is None or hit[2] > best[1][2]:
            best = (t, hit)
    return best


def verify_gone(sct, region, tmpl, thresh, secs, need=2):
    """Confirm the dialog actually disappeared.

    A click that never reached the application looks identical to one
    that worked - Windows silently eats the first click on an unfocused
    window, which cost several runs elsewhere in this project. Without
    this the watchdog cannot tell "reconnected" from "clicked into the
    void", and a failure becomes a silent click loop rather than a
    message.
    """
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


def keepalive_click(sct, region, hwnd, tmpl, args):
    """Poke the screen so the client does not time out as idle.

    The logs show the inactivity dialog arriving on a fixed 184s cycle,
    so anything under ~3 minutes prevents it. The interval is RANDOM
    between --keepalive-min and --keepalive-max: a click at a constant
    period is a stronger pattern than the disconnect it avoids, and
    trading one regular signature for another gains nothing.

    Matched by template rather than fixed coordinates because the panel
    position shifted 33px between a saved screenshot and the live
    capture - the dialog templates hit that already.
    """
    frame = grab(sct, region)
    if tmpl is not None:
        hit = find_anchor(frame, tmpl, args.keepalive_thresh)
        if hit is None:
            return False, "target not on screen"
        th, tw = tmpl.shape[:2]
        cx, cy = hit[0] + tw // 2, hit[1] + th // 2
    elif args.keepalive_x is not None and args.keepalive_y is not None:
        cx, cy = args.keepalive_x, args.keepalive_y
    else:
        return False, "no template and no --keepalive-x/y"

    focus_window(hwnd)
    click_at(region, cx, cy, hold=0.05)
    return True, f"({cx},{cy})"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# -------------------------------------------------------------- calibrate

def calibrate(sct, region, hwnd):
    print("CALIBRATE MODE")
    print(f"  Region: left={region['left']} top={region['top']} "
          f"{region['width']}x{region['height']} - sanity-check this "
          f"against the emulator's actual on-screen position.")
    print("  Force a disconnect (e.g. turn off wifi on this PC for a few")
    print("  seconds), wait for the dialog, then press F6.")
    print("  You'll drag a box around the DIALOG PANEL, then click the")
    print("  button to record where to tap. F9 to quit.")
    print("  The emulator is brought to the front before every capture -")
    print("  keep it visible, don't alt-tab away right before F6.")
    os.makedirs(TEMPLATE_DIR, exist_ok=True)

    while True:
        if STOP:
            print("Quit.")
            return
        if keyboard.is_pressed("f6"):
            # Bring the emulator to the front FIRST - mss grabs raw screen
            # pixels at these coordinates regardless of which window is
            # actually showing there. If VS Code (or anything else) is
            # covering the game area when we grab, we capture the editor,
            # not the dialog. This is what produced a template with line
            # numbers and a Problems panel baked into it last time.
            focus_window(hwnd)
            frame = grab(sct, region)

            print("\nDrag a box around the DIALOG PANEL, then press ENTER.")
            print("Crop the panel, NOT the whole screen: a full-frame")
            print("template scores on the background, which changes.")
            box = cv2.selectROI("drag the dialog panel, then ENTER", frame,
                                showCrosshair=False)
            cv2.destroyWindow("drag the dialog panel, then ENTER")
            x, y, w, h = [int(v) for v in box]
            if w < 20 or h < 20:
                print("Discarded (no box drawn).")
                time.sleep(0.3)
                continue
            crop = frame[y:y + h, x:x + w]

            print("Now hover the mouse over the button to tap and press F7 "
                  "(ESC to discard).")
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
            if not (x <= point[0] <= x + w and y <= point[1] <= y + h):
                print(f"  WARNING: click point {point} is OUTSIDE the box "
                      f"you drew. The offset is stored relative to the box "
                      f"so it still works, but check you marked the right "
                      f"button.")

            name = input("Template name (e.g. 'connection_lost'): ").strip()
            if not name:
                print("Empty name, discarded.")
                continue
            cv2.imwrite(os.path.join(TEMPLATE_DIR, name + ".png"), crop)
            with open(os.path.join(TEMPLATE_DIR, name + ".json"), "w") as f:
                json.dump({"click_dx": point[0] - x, "click_dy": point[1] - y,
                           "thresh": 0.85}, f, indent=2)
            print(f"Saved {name}.png ({w}x{h}) / {name}.json — click offset "
                  f"({point[0] - x}, {point[1] - y}).")
            print(f"  Open {os.path.join(TEMPLATE_DIR, name + '.png')} and "
                  f"confirm it's the dialog panel - not your editor, not "
                  f"the whole screen - before trusting it.")
            time.sleep(0.3)
        time.sleep(0.02)


# -------------------------------------------------------------------- run

def run(sct, region, hwnd, templates, args):
    print(f"Watching with {len(templates)} template(s): "
          f"{', '.join(t['name'] for t in templates)}")
    print(f"Polling every {args.interval}s. F9 to quit.")
    last_fire = {}
    dismissed = 0
    failures = 0
    ka_tmpl = None
    next_ka = None
    if args.keepalive:
        if args.keepalive_template:
            ka_tmpl = cv2.imread(args.keepalive_template)
            if ka_tmpl is None:
                print(f"  keep-alive template {args.keepalive_template} "
                      f"not found - falling back to --keepalive-x/y")
            else:
                print(f"  keep-alive: matching "
                      f"{os.path.basename(args.keepalive_template)} "
                      f"({ka_tmpl.shape[1]}x{ka_tmpl.shape[0]})")
        next_ka = time.perf_counter() + random.uniform(args.keepalive_min,
                                                       args.keepalive_max)
        print(f"  keep-alive every {args.keepalive_min:.0f}-"
              f"{args.keepalive_max:.0f}s (random). The idle dialog was "
              f"arriving on a 184s cycle.")

    while True:
        if STOP:
            print(f"Quit. {dismissed} dialog(s) dismissed.")
            return

        frame = grab(sct, region)
        best = best_match(frame, templates)
        if best is None:
            # Only poke the screen when no dialog is up. Clicking into a
            # dialog would either dismiss it by accident or do nothing.
            if next_ka is not None and time.perf_counter() >= next_ka:
                ok, where = keepalive_click(sct, region, hwnd, ka_tmpl, args)
                gap = random.uniform(args.keepalive_min, args.keepalive_max)
                next_ka = time.perf_counter() + gap
                log(f"keep-alive {'clicked ' + where if ok else 'SKIPPED: ' + where}"
                    f"; next in {gap:.0f}s")
            naptime(args.interval)
            continue

        t, (x, y, score) = best
        now = time.perf_counter()
        if now - last_fire.get(t["name"], 0) <= args.cooldown:
            naptime(args.interval)
            continue

        cx, cy = x + t["click_dx"], y + t["click_dy"]
        log(f"MATCH '{t['name']}' score={score:.3f} at ({x},{y}) "
            f"-> click ({cx},{cy})"
            + (" [dry-run, not clicking]" if args.dry_run else ""))
        last_fire[t["name"]] = now

        if args.dry_run:
            naptime(args.interval)
            continue

        ok = False
        for attempt in range(1, args.max_tries + 1):
            if STOP:
                break
            focus_window(hwnd)
            # Let the dialog finish animating in. Clicking the instant it
            # is first matched can land while the panel is still sliding
            # or fading, and the button may not be live yet - which looks
            # exactly like a swallowed click from here.
            naptime(args.pre_click)
            click_at(region, cx, cy, hold=0.06)
            wait_settled(sct, region, quiet=args.quiet,
                         timeout=args.settle_timeout)
            if verify_gone(sct, region, t["img"], t["thresh"],
                           args.verify_secs):
                ok = True
                break
            log(f"  '{t['name']}' still present after attempt "
                f"{attempt}/{args.max_tries}")

        if ok:
            dismissed += 1
            failures = 0
            log(f"  dismissed '{t['name']}' ({dismissed} total)")
        else:
            failures += 1
            shot = f"reconnect_stuck_{failures}.png"
            cv2.imwrite(shot, grab(sct, region))
            log(f"  FAILED to dismiss '{t['name']}' after {args.max_tries} "
                f"attempts. Wrote {shot}.")
            # Stop rather than click forever. If the button moved, the
            # template is wrong, or the game wants something else first,
            # more clicks will not fix it - and an unattended click loop
            # is worse than a stopped watchdog.
            if failures >= args.max_failures:
                log(f"  giving up after {failures} unresolved dialog(s). "
                    f"Check the screenshots and the click point.")
                return

        naptime(args.cooldown)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--title", default=WINDOW_TITLE,
                    help=f"window title substring (default '{WINDOW_TITLE}')")
    ap.add_argument("--region-x", type=int, default=0, dest="region_x",
                    help="px to shift capture right; BlueStacks puts an "
                         "advert panel about 228px wide on the left")
    ap.add_argument("--region-y", type=int, default=0, dest="region_y")
    ap.add_argument("--templates", default=TEMPLATE_DIR,
                    help=f"template folder (default '{TEMPLATE_DIR}')")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between polls (default 2.0)")
    ap.add_argument("--cooldown", type=float, default=6.0,
                    help="min seconds between clicks on the same template")
    ap.add_argument("--quiet", type=float, default=0.4,
                    help="seconds of stillness that count as settled")
    ap.add_argument("--settle-timeout", type=float, default=5.0,
                    dest="settle_timeout")
    ap.add_argument("--verify-secs", type=float, default=4.0,
                    dest="verify_secs",
                    help="how long to wait for the dialog to actually "
                         "disappear before calling the click failed")
    ap.add_argument("--max-tries", type=int, default=3, dest="max_tries",
                    help="click attempts per dialog before giving up")
    ap.add_argument("--max-failures", type=int, default=2,
                    dest="max_failures",
                    help="unresolved dialogs before the watchdog stops")
    ap.add_argument("--pre-click", type=float, default=1.0,
                    dest="pre_click",
                    help="seconds to wait after the dialog is matched "
                         "before clicking, so it can finish animating in")
    ap.add_argument("--keepalive", action="store_true",
                    help="click a harmless target periodically so the "
                         "client does not drop you for inactivity")
    ap.add_argument("--keepalive-template",
                    default=os.path.join(TEMPLATE_DIR, "_mine.png"),
                    dest="keepalive_template",
                    help="image of the thing to click; matched each time "
                         "so it survives the layout shifting")
    ap.add_argument("--keepalive-x", type=int, default=None,
                    dest="keepalive_x",
                    help="fixed click point, if no template")
    ap.add_argument("--keepalive-y", type=int, default=None,
                    dest="keepalive_y")
    ap.add_argument("--keepalive-min", type=float, default=30.0,
                    dest="keepalive_min")
    ap.add_argument("--keepalive-max", type=float, default=120.0,
                    dest="keepalive_max",
                    help="upper bound of the random gap. Keep it well "
                         "under the 184s idle timeout")
    ap.add_argument("--keepalive-thresh", type=float, default=0.80,
                    dest="keepalive_thresh")
    ap.add_argument("--calibrate", action="store_true",
                    help="teach the tool a new disconnect dialog")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and log matches, never click")
    args = ap.parse_args()

    fix_dpi()
    try:
        keyboard.add_hotkey("f9", request_stop)
    except Exception as e:
        print(f"  could not register the F9 hotkey ({e}) - use Ctrl+C.")
    hwnd, title = find_window(args.title)
    if not hwnd:
        print(f"No window matching '{args.title}'. Is the emulator open?")
        return
    print(f"Found: {title}")
    region = game_region(hwnd, args)

    with mss.MSS() as sct:
        if args.calibrate:
            calibrate(sct, region, hwnd)
            return

        templates = load_templates(args.templates)
        if not templates:
            print(f"No templates in '{args.templates}'. Run with "
                  f"--calibrate first to teach it your disconnect dialogs.")
            return

        run(sct, region, hwnd, templates, args)


if __name__ == "__main__":
    main()