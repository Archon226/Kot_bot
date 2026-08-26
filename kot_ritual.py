"""
kot_ritual.py - close the game when the ritual finishes.

Watches for the ritual timer reading 00:00:00 and shuts the emulator down
cleanly, so a run does not sit open for hours after the thing it was
waiting for is done.

WHY MATCH THE WHOLE BAR

The template is the green bar AND the zeros together, not just the text.
A partially-complete ritual shows a part-filled bar with a non-zero time,
so the two together are specific in a way neither is alone - and the
level badge is cropped out because that number differs per dungeon.

The bar sits on the chest, and the defender's layout decides where the
chest is, so it is matched anywhere in the play area rather than at fixed
coordinates.

CONFIRM BEFORE ACTING

A false positive here closes the emulator, so a match must persist for
--confirm consecutive polls before anything happens. A single frame that
happens to score high - mid-transition, or during an animation - is not
enough. It also screenshots what it saw, so a wrong close can be
diagnosed after the fact rather than guessed at.

BLUESTACKS ASKS BEFORE CLOSING

WM_CLOSE only *asks*; BlueStacks puts up its own confirmation window and
waits. That popup is a separate top-level window, so it has to be found
and answered or the close silently times out.

Usage:
    python kot_ritual.py --title BlueStacks --region-x 225 --dry-run
    python kot_ritual.py --title BlueStacks --region-x 225

Keys:
    F9  quit
"""

import argparse
import ctypes
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
PLAY_TOP, PLAY_BOTTOM = 0.06, 0.92
LOG_FILE = "ritual_log.txt"

STOP = False


def request_stop():
    global STOP
    STOP = True
    print("\n  F9 - stopping...")


def naptime(seconds, step=0.05):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        if STOP:
            return False
        time.sleep(min(step, max(0.0, end - time.perf_counter())))
    return not STOP


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


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


def game_region(hwnd, args):
    l, t, r, b = win32gui.GetClientRect(hwnd)
    l, t = win32gui.ClientToScreen(hwnd, (l, t))
    r, b = win32gui.ClientToScreen(hwnd, (r, b))
    cw, ch = r - l, b - t
    ox, oy = args.region_x or 0, args.region_y or 0
    if cw - ox < GAME_W or ch - oy < GAME_H:
        raise SystemExit(f"Window {cw}x{ch} at offset ({ox},{oy}) cannot "
                         f"hold {GAME_W}x{GAME_H}.")
    print(f"Client {cw}x{ch}; capturing {GAME_W}x{GAME_H} at ({ox},{oy})")
    if cw > GAME_W + 100 and ox == 0:
        print("  NOTE: the client is much wider than the game. If your "
              "emulator shows an advert panel on the left, pass "
              "--region-x or you are matching against adverts.")
    return {"left": l + ox, "top": t + oy,
            "width": GAME_W, "height": GAME_H}


def grab(sct, box):
    raw = sct.grab(box)
    a = np.frombuffer(raw.bgra, dtype=np.uint8)
    return a.reshape(raw.height, raw.width, 4)[:, :, :3]


# ------------------------------------------------------------------ close

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


def click_screen(sx, sy, hold=0.06):
    vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    _send(MOVE | ABS, int((sx - vx) * 65535 / (vw - 1)),
          int((sy - vy) * 65535 / (vh - 1)))
    time.sleep(0.05)
    _send(DOWN)
    time.sleep(hold)
    _send(UP)


def find_confirm(title_substr):
    out = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and \
                title_substr.lower() in win32gui.GetWindowText(h).lower():
            out.append(h)

    win32gui.EnumWindows(cb, None)
    return out[0] if out else None


def answer_confirm(args, timeout=10.0):
    """Click 'Close' on the emulator's exit confirmation.

    Qt draws these buttons inside one window rather than creating child
    controls, so there is usually nothing to send BM_CLICK to - the
    position is a fraction of the dialog so it survives the dialog being
    a different size.
    """
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        h = find_confirm(args.close_prompt)
        if h:
            l, t, r, b = win32gui.GetWindowRect(h)
            w, ht = r - l, b - t
            x = l + int(w * args.close_btn_x)
            y = t + int(ht * args.close_btn_y)
            log(f"  confirmation window {w}x{ht}; clicking ({x},{y})")
            try:
                win32gui.SetForegroundWindow(h)
            except Exception:
                pass
            time.sleep(0.15)
            click_screen(x, y)
            return True
        time.sleep(0.3)
    return False


def close_emulator(hwnd, args):
    log("closing the emulator")
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as e:
        log(f"  WM_CLOSE failed: {e}")
        return False
    if args.close_prompt:
        answer_confirm(args)
    end = time.perf_counter() + args.close_timeout
    while time.perf_counter() < end:
        if not win32gui.IsWindow(hwnd):
            log("  emulator closed")
            return True
        if args.close_prompt and int(time.perf_counter() * 2) % 8 == 0:
            answer_confirm(args, timeout=1.0)
        time.sleep(0.5)
    log(f"  still open after {args.close_timeout:.0f}s. Not force-killing - "
        f"a half-killed emulator is worse than one left running.")
    return False


# -------------------------------------------------------------------- run

def run(sct, region, hwnd, tmpl, args):
    y0, y1 = int(GAME_H * PLAY_TOP), int(GAME_H * PLAY_BOTTOM)
    log(f"watching for a finished ritual every {args.interval:.0f}s "
        f"(need {args.confirm} consecutive matches). F9 to quit.")
    hits = 0
    seen_since = None

    while True:
        if STOP:
            log("stopped by key.")
            return

        frame = grab(sct, region)
        sub = frame[y0:y1]
        if tmpl.shape[0] > sub.shape[0] or tmpl.shape[1] > sub.shape[1]:
            raise SystemExit("template is larger than the play area - it was "
                             "cut from a differently sized capture.")
        res = cv2.matchTemplate(sub, tmpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)

        if score >= args.thresh:
            hits += 1
            seen_since = seen_since or time.perf_counter()
            log(f"ritual timer reads 00:00:00 - score {score:.3f} at "
                f"({loc[0]},{loc[1] + y0})  [{hits}/{args.confirm}]")
            if hits >= args.confirm:
                shot = f"ritual_done_{time.strftime('%H%M%S')}.png"
                cv2.imwrite(shot, frame)
                log(f"  confirmed over {time.perf_counter() - seen_since:.0f}s"
                    f"; wrote {shot}")
                if args.dry_run:
                    log("  dry run - NOT closing. Remove --dry-run to act.")
                    return
                close_emulator(hwnd, args)
                return
        else:
            if hits:
                log(f"  match lost (best {score:.3f}) - counter reset")
            hits = 0
            seen_since = None

        naptime(args.interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="LDPlayer",
                    help="emulator window title substring")
    ap.add_argument("--region-x", type=int, default=0, dest="region_x",
                    help="px to shift capture right; BlueStacks puts an "
                         "advert panel on the left")
    ap.add_argument("--region-y", type=int, default=0, dest="region_y")
    ap.add_argument("--template",
                    default=os.path.join("reconnect_templates",
                                         "ritual_done.png"))
    ap.add_argument("--thresh", type=float, default=0.85,
                    help="match score to accept")
    ap.add_argument("--confirm", type=int, default=3,
                    help="consecutive matches before acting. A false "
                         "positive closes the emulator, so one frame is "
                         "not enough")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="seconds between checks. A finished ritual is not "
                         "going anywhere, so this can be slow")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="report and screenshot, never close")
    ap.add_argument("--close-prompt", default="Close BlueStacks",
                    dest="close_prompt",
                    help="title of the exit-confirmation window. Empty "
                         "string if your emulator does not ask")
    ap.add_argument("--close-btn-x", type=float, default=0.71,
                    dest="close_btn_x")
    ap.add_argument("--close-btn-y", type=float, default=0.75,
                    dest="close_btn_y")
    ap.add_argument("--close-timeout", type=float, default=45.0,
                    dest="close_timeout")
    args = ap.parse_args()

    tmpl = cv2.imread(args.template)
    if tmpl is None:
        raise SystemExit(f"No template at {args.template}")
    print(f"template {args.template} {tmpl.shape[1]}x{tmpl.shape[0]}")

    fix_dpi()
    try:
        keyboard.add_hotkey("f9", request_stop)
    except Exception as e:
        print(f"  could not register F9 ({e}) - use Ctrl+C.")
    hwnd, title = find_window(args.title)
    if not hwnd:
        print(f"No window matching '{args.title}'.")
        return
    print(f"Found: {title}")
    region = game_region(hwnd, args)

    with mss.MSS() as sct:
        run(sct, region, hwnd, tmpl, args)


if __name__ == "__main__":
    main()