"""
kot_reconnect.py - watchdog that clicks through disconnect/reconnect
screens automatically.

Now resolution-independent:

    Old version:
        Required the BlueStacks client area to be (close to) 1280x720.
        Capture region was clamped/shrunk to that fixed size.

    This version:
        Captures whatever the ACTUAL BlueStacks client area is,
        at any size/resolution.

        Every template records the resolution it was calibrated at
        (saved automatically by --calibrate as "ref_w"/"ref_h" in its
        .json file). Before each run, every template's image AND its
        click offset are rescaled to match the current capture size,
        so the same template set works whether BlueStacks is running
        windowed at 1024x576, fullscreen at 1920x1080, or anything
        else.

    Detection/click logging:
        Every poll clearly logs whether a dialog was found or not.
        When a reconnect-style dialog is found, it logs the detection,
        then logs the click, then logs whether the click was CONFIRMED
        (dialog actually disappeared) or not.

Examples:

    python kot_reconnect.py --title BlueStacks
    python kot_reconnect.py --title BlueStacks --keepalive
    python kot_reconnect.py --title BlueStacks --region-x 0 --keepalive
    python kot_reconnect.py --title BlueStacks --calibrate
    python kot_reconnect.py --title BlueStacks --dry-run
    python kot_reconnect.py --title BlueStacks --verbose
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

WINDOW_TITLE = "BlueStacks"

# Reference resolution. This is ONLY used as the fallback "calibrated at"
# size for templates whose .json doesn't specify ref_w/ref_h (e.g. templates
# made with an older version of this script, or the keep-alive template,
# which has no metadata file). It is NOT a required or enforced capture
# size any more - game_region() always captures the real client area,
# whatever size that is.
GAME_W = 1280
GAME_H = 720

TEMPLATE_DIR = "reconnect_templates"
LOG_FILE = "reconnect_log.txt"

CONFIRM_DIALOG_TITLE = "close bluestacks"

STOP = False


# ---------------------------------------------------------------------------
# Stop handling
# ---------------------------------------------------------------------------

def request_stop():
    global STOP
    STOP = True
    print("\n  F9 - stopping after the current step...")


def naptime(seconds, step=0.05):
    """Sleep while still allowing F9 to interrupt."""
    end = time.perf_counter() + seconds

    while time.perf_counter() < end:
        if STOP:
            return False

        time.sleep(
            min(
                step,
                max(0.0, end - time.perf_counter())
            )
        )

    return not STOP


# ---------------------------------------------------------------------------
# Windows
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
    """
    Find BlueStacks' native close-confirmation popup.
    """

    matches = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return

        title = win32gui.GetWindowText(hwnd)

        if title_substr.lower() in title.lower():
            matches.append(hwnd)

    win32gui.EnumWindows(cb, None)

    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Capture region
# ---------------------------------------------------------------------------

def game_region(hwnd, args=None):
    """
    Calculate the screen capture region from the ACTUAL BlueStacks
    client area, at whatever size/resolution it currently is.

    Unlike the old version, this does not require or clamp to
    1280x720 - it always captures the real client area (minus any
    --region-x/--region-y offset). Any BlueStacks window size or
    resolution is supported; templates are rescaled separately in
    prepare_templates() to match this size.
    """

    # Get client rectangle.
    left, top, right, bottom = win32gui.GetClientRect(hwnd)

    # Convert client coordinates into actual screen coordinates.
    left, top = win32gui.ClientToScreen(hwnd, (left, top))
    right, bottom = win32gui.ClientToScreen(hwnd, (right, bottom))

    cw = right - left
    ch = bottom - top

    # Region offsets.
    ox = getattr(args, "region_x", 0) or 0
    oy = getattr(args, "region_y", 0) or 0

    if ox < 0 or oy < 0:
        raise SystemExit(
            f"Invalid region offset ({ox},{oy}). "
            f"Offsets cannot be negative."
        )

    capture_w = cw - ox
    capture_h = ch - oy

    if capture_w <= 0 or capture_h <= 0:
        raise SystemExit(
            f"Region offset ({ox},{oy}) is outside the "
            f"BlueStacks client area {cw}x{ch}."
        )

    print(
        f"  BlueStacks client: {cw}x{ch}"
    )

    print(
        f"  Capture region:    {capture_w}x{capture_h}"
    )

    print(
        f"  Capture offset:    ({ox},{oy})"
    )

    if (capture_w, capture_h) != (GAME_W, GAME_H):
        print(
            f"  NOTE: capture resolution differs from the "
            f"{GAME_W}x{GAME_H} reference size. Templates will be "
            f"auto-scaled to match ({capture_w}x{capture_h})."
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
        print(
            f"  could not focus the window ({e}); "
            f"click the emulator once."
        )

    time.sleep(0.25)


# ---------------------------------------------------------------------------
# Mouse input
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
    _fields_ = [
        ("type", wintypes.DWORD),
        ("mi", MOUSEINPUT),
    ]


MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000


def _send(flags, x=0, y=0):
    inp = INPUT(
        type=0,
        mi=MOUSEINPUT(
            x,
            y,
            0,
            flags,
            0,
            None
        )
    )

    ctypes.windll.user32.SendInput(
        1,
        ctypes.byref(inp),
        ctypes.sizeof(INPUT)
    )


def click_at(region, gx, gy, hold=0.05):
    """
    Click a point expressed relative to the capture region.
    """

    sx = region["left"] + gx
    sy = region["top"] + gy

    vw = win32api.GetSystemMetrics(
        win32con.SM_CXVIRTUALSCREEN
    )

    vh = win32api.GetSystemMetrics(
        win32con.SM_CYVIRTUALSCREEN
    )

    vx = win32api.GetSystemMetrics(
        win32con.SM_XVIRTUALSCREEN
    )

    vy = win32api.GetSystemMetrics(
        win32con.SM_YVIRTUALSCREEN
    )

    # Prevent invalid coordinates.
    if vw <= 1 or vh <= 1:
        return

    abs_x = int(
        (sx - vx) * 65535 / (vw - 1)
    )

    abs_y = int(
        (sy - vy) * 65535 / (vh - 1)
    )

    _send(
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
        abs_x,
        abs_y
    )

    time.sleep(0.02)

    _send(MOUSEEVENTF_LEFTDOWN)

    time.sleep(hold)

    _send(MOUSEEVENTF_LEFTUP)


# ---------------------------------------------------------------------------
# Perception
# ---------------------------------------------------------------------------

def grab(sct, box):
    raw = sct.grab(box)

    arr = np.frombuffer(
        raw.bgra,
        dtype=np.uint8
    )

    return arr.reshape(
        raw.height,
        raw.width,
        4
    )[:, :, :3]


def thumb(frame):
    g = cv2.cvtColor(
        frame,
        cv2.COLOR_BGR2GRAY
    )

    return cv2.resize(
        g,
        (80, 45),
        interpolation=cv2.INTER_AREA
    )


def scene_motion(a, b):
    return float(
        np.abs(
            a.astype(np.int16) -
            b.astype(np.int16)
        ).mean()
    )


def wait_settled(
    sct,
    region,
    quiet=0.4,
    timeout=5.0
):
    prev = thumb(
        grab(sct, region)
    )

    still_since = None

    t_end = (
        time.perf_counter() +
        timeout
    )

    while time.perf_counter() < t_end:

        if STOP:
            return False

        time.sleep(0.05)

        cur = thumb(
            grab(sct, region)
        )

        motion = scene_motion(
            prev,
            cur
        )

        prev = cur

        if motion < 0.5:
            still_since = (
                still_since or
                time.perf_counter()
            )

            if (
                time.perf_counter() -
                still_since >= quiet
            ):
                return True

        else:
            still_since = None

    return False


# ---------------------------------------------------------------------------
# Template scaling (resolution independence)
# ---------------------------------------------------------------------------

def scale_image(img, sx, sy):
    """
    Resize an image by independent x/y scale factors. Returns the same
    image unchanged if the scale factors are effectively 1.0.
    """

    if img is None:
        return None

    h, w = img.shape[:2]

    new_w = max(1, int(round(w * sx)))
    new_h = max(1, int(round(h * sy)))

    if new_w == w and new_h == h:
        return img

    interp = (
        cv2.INTER_AREA
        if (new_w < w or new_h < h)
        else cv2.INTER_LINEAR
    )

    return cv2.resize(
        img,
        (new_w, new_h),
        interpolation=interp
    )


def prepare_templates(templates, region):
    """
    Rescale every loaded template - and its click offset - from the
    resolution it was calibrated at (t["ref_w"]/t["ref_h"]) to the
    CURRENT capture resolution (region["width"]/region["height"]).

    This is what makes a single template set work across any
    BlueStacks window size/resolution: each template is scaled
    independently based on its own recorded calibration size.

    Call this once after computing the capture region, and again any
    time the region changes (e.g. after a --restart-test/--restart-loop
    relaunch, in case BlueStacks reopens at a different size).
    """

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
        print(
            f"  Rescaled {len(templates)} template(s) to match "
            f"{region['width']}x{region['height']}."
        )

    return templates


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def load_templates(folder):
    templates = []

    if not os.path.isdir(folder):
        return templates

    for fn in sorted(os.listdir(folder)):

        if not fn.endswith(".png"):
            continue

        if fn.startswith("_"):
            continue

        name = fn[:-4]

        meta_path = os.path.join(
            folder,
            name + ".json"
        )

        if not os.path.isfile(meta_path):
            print(
                f"  skipping {fn}: "
                f"no matching {name}.json"
            )
            continue

        img = cv2.imread(
            os.path.join(folder, fn)
        )

        if img is None:
            print(
                f"  skipping {fn}: "
                f"could not read it"
            )
            continue

        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)
        except Exception as e:
            print(
                f"  skipping {fn}: "
                f"invalid JSON ({e})"
            )
            continue

        # Resolution this template was calibrated at. Older templates
        # (made before this version) won't have ref_w/ref_h in their
        # .json, so fall back to the legacy 1280x720 reference size.
        ref_w = int(meta.get("ref_w", GAME_W))
        ref_h = int(meta.get("ref_h", GAME_H))

        if (
            img.shape[0] >= ref_h and
            img.shape[1] >= ref_w
        ):
            print(
                f"  NOTE {name}.png is a full-frame "
                f"template ({img.shape[1]}x{img.shape[0]})."
            )

        templates.append(
            {
                "name": name,
                "base_img": img,
                "ref_w": ref_w,
                "ref_h": ref_h,
                "base_click_dx": meta["click_dx"],
                "base_click_dy": meta["click_dy"],
                "thresh": meta.get(
                    "thresh",
                    0.85
                ),
                "fatal": bool(
                    meta.get(
                        "fatal",
                        False
                    )
                ),
                "dismiss_only": bool(
                    meta.get(
                        "dismiss_only",
                        False
                    )
                ),
                # Populated by prepare_templates() before use.
                "img": img,
                "click_dx": meta["click_dx"],
                "click_dy": meta["click_dy"],
            }
        )

    return templates


def find_anchor(frame, tmpl, thresh):
    if tmpl is None:
        return None

    if (
        tmpl.shape[0] >
        frame.shape[0]
    ):
        return None

    if (
        tmpl.shape[1] >
        frame.shape[1]
    ):
        return None

    res = cv2.matchTemplate(
        frame,
        tmpl,
        cv2.TM_CCOEFF_NORMED
    )

    _, score, _, loc = cv2.minMaxLoc(
        res
    )

    if score < thresh:
        return None

    return (
        loc[0],
        loc[1],
        score
    )


def best_match(frame, templates):
    best = None

    for t in templates:

        hit = find_anchor(
            frame,
            t["img"],
            t["thresh"]
        )

        if hit is None:
            continue

        if (
            best is None or
            hit[2] > best[1][2]
        ):
            best = (
                t,
                hit
            )

    return best


def verify_gone(
    sct,
    region,
    tmpl,
    thresh,
    secs,
    need=2
):
    gone = 0

    end = (
        time.perf_counter() +
        secs
    )

    while time.perf_counter() < end:

        if STOP:
            return False

        time.sleep(0.25)

        if (
            find_anchor(
                grab(sct, region),
                tmpl,
                thresh
            )
            is None
        ):
            gone += 1

            if gone >= need:
                return True

        else:
            gone = 0

    return False


# ---------------------------------------------------------------------------
# Keepalive
# ---------------------------------------------------------------------------

def keepalive_click(
    sct,
    region,
    hwnd,
    tmpl,
    args
):
    frame = grab(
        sct,
        region
    )

    if tmpl is not None:

        hit = find_anchor(
            frame,
            tmpl,
            args.keepalive_thresh
        )

        if hit is None:
            return (
                False,
                "target not on screen"
            )

        th, tw = tmpl.shape[:2]

        cx = (
            hit[0] +
            tw // 2
        )

        cy = (
            hit[1] +
            th // 2
        )

    elif (
        args.keepalive_x is not None
        and
        args.keepalive_y is not None
    ):

        cx = args.keepalive_x
        cy = args.keepalive_y

    else:
        return (
            False,
            "no template and no "
            "--keepalive-x/y"
        )

    focus_window(hwnd)

    click_at(
        region,
        cx,
        cy,
        hold=0.05
    )

    return (
        True,
        f"({cx},{cy})"
    )


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------

def shot(
    sct,
    region,
    name
):
    fn = (
        f"{name}_"
        f"{time.strftime('%H%M%S')}.png"
    )

    try:
        cv2.imwrite(
            fn,
            grab(sct, region)
        )

        return fn

    except Exception as e:
        return (
            f"(screenshot failed: {e})"
        )


# ---------------------------------------------------------------------------
# BlueStacks close confirmation
# ---------------------------------------------------------------------------

def click_dialog_button(
    dlg_hwnd,
    label
):
    target = None

    def cb(child, _):
        nonlocal target

        if (
            win32gui.GetWindowText(
                child
            ).strip().lower()
            ==
            label.lower()
        ):
            target = child

        return True

    win32gui.EnumChildWindows(
        dlg_hwnd,
        cb,
        None
    )

    if target is not None:

        win32gui.SendMessage(
            target,
            win32con.BM_CLICK,
            0,
            0
        )

        time.sleep(0.3)

        if not win32gui.IsWindow(
            dlg_hwnd
        ):
            return True

        l, t, r, b = (
            win32gui.GetWindowRect(
                target
            )
        )

        cx = (l + r) // 2
        cy = (t + b) // 2

    else:

        l, t, r, b = (
            win32gui.GetWindowRect(
                dlg_hwnd
            )
        )

        w = r - l
        h = b - t

        if label.lower() == "close":
            x_frac = 0.85
            y_frac = 0.78
        else:
            x_frac = 0.60
            y_frac = 0.78

        cx = l + int(
            w * x_frac
        )

        cy = t + int(
            h * y_frac
        )

    try:
        win32gui.SetForegroundWindow(
            dlg_hwnd
        )
    except Exception:
        pass

    time.sleep(0.1)

    vw = win32api.GetSystemMetrics(
        win32con.SM_CXVIRTUALSCREEN
    )

    vh = win32api.GetSystemMetrics(
        win32con.SM_CYVIRTUALSCREEN
    )

    vx = win32api.GetSystemMetrics(
        win32con.SM_XVIRTUALSCREEN
    )

    vy = win32api.GetSystemMetrics(
        win32con.SM_YVIRTUALSCREEN
    )

    if vw <= 1 or vh <= 1:
        return False

    _send(
        MOUSEEVENTF_MOVE |
        MOUSEEVENTF_ABSOLUTE,
        int(
            (cx - vx) *
            65535 /
            (vw - 1)
        ),
        int(
            (cy - vy) *
            65535 /
            (vh - 1)
        )
    )

    time.sleep(0.02)

    _send(
        MOUSEEVENTF_LEFTDOWN
    )

    time.sleep(0.05)

    _send(
        MOUSEEVENTF_LEFTUP
    )

    return True


def close_window(
    hwnd,
    timeout=30.0
):
    try:
        win32gui.PostMessage(
            hwnd,
            win32con.WM_CLOSE,
            0,
            0
        )

    except Exception as e:
        log(
            f"  WM_CLOSE failed ({e})"
        )

        return False

    confirm_clicked = False

    end = (
        time.perf_counter() +
        timeout
    )

    while time.perf_counter() < end:

        if not win32gui.IsWindow(hwnd):
            return True

        if not confirm_clicked:

            dlg = find_confirm_dialog()

            if dlg:

                log(
                    "  BlueStacks close-confirmation "
                    "dialog appeared - clicking Close"
                )

                if click_dialog_button(
                    dlg,
                    "Close"
                ):
                    confirm_clicked = True

        time.sleep(0.5)

    return False


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

def restart_cycle(
    sct,
    region,
    hwnd,
    args
):
    log(
        "RESTART TEST: closing the emulator"
    )

    before = shot(
        sct,
        region,
        "restart_before"
    )

    log(f"  {before}")

    if not close_window(
        hwnd,
        args.close_timeout
    ):
        log(
            "  window did not close in time; "
            "not force-killing. Stopping."
        )

        return None

    log(
        "  emulator closed cleanly"
    )

    log(
        f"  waiting {args.restart_wait:.0f}s "
        f"before relaunch"
    )

    t0 = time.perf_counter()

    while (
        time.perf_counter() -
        t0
        <
        args.restart_wait
    ):

        if STOP:
            return None

        time.sleep(0.5)

    if args.restart_exe:

        import shlex

        extra = (
            shlex.split(
                args.restart_args,
                posix=False
            )
            if args.restart_args
            else []
        )

        cmd_list = (
            [args.restart_exe] +
            extra
        )

        log(
            f"  launching: {cmd_list}"
        )

        try:
            subprocess.Popen(
                cmd_list,
                shell=False
            )

        except Exception as e:
            log(
                f"  launch failed: {e}"
            )

            return None

    elif args.restart_cmd:

        log(
            f"  launching: "
            f"{args.restart_cmd}"
        )

        try:
            subprocess.Popen(
                args.restart_cmd,
                shell=True
            )

        except Exception as e:
            log(
                f"  launch failed: {e}"
            )

            return None

    else:

        log(
            "  no --restart-exe/--restart-cmd "
            "given, so nothing to launch."
        )

        return None

    end = (
        time.perf_counter() +
        args.relaunch_timeout
    )

    new_hwnd = None

    while (
        time.perf_counter() < end
    ):

        if STOP:
            return None

        h, title = find_window(
            args.title
        )

        if h:
            new_hwnd = h
            break

        time.sleep(1.0)

    if not new_hwnd:

        log(
            f"  window never reappeared "
            f"within {args.relaunch_timeout:.0f}s."
        )

        return None

    log(
        f"  window back after "
        f"{time.perf_counter() - t0 - args.restart_wait:.0f}s; "
        f"settling for {args.settle_after:.0f}s"
    )

    time.sleep(
        args.settle_after
    )

    return new_hwnd


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    line = (
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}  "
        f"{msg}"
    )

    print(line)

    try:
        with open(
            LOG_FILE,
            "a",
            encoding="utf-8"
        ) as f:
            f.write(line + "\n")

    except Exception:
        pass


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def calibrate(
    sct,
    region,
    hwnd
):
    print()
    print("CALIBRATE MODE")
    print(
        f"  Region: left={region['left']} "
        f"top={region['top']} "
        f"{region['width']}x{region['height']}"
    )

    print(
        f"  Templates captured now will be tagged as calibrated at "
        f"{region['width']}x{region['height']} (saved as ref_w/ref_h), "
        f"so they auto-scale correctly if BlueStacks later runs at a "
        f"different size."
    )

    print()
    print(
        "  Force a disconnect, wait for the "
        "dialog, then press F6."
    )

    print(
        "  Drag a box around the DIALOG PANEL, "
        "then press ENTER."
    )

    print(
        "  F9 to quit."
    )

    os.makedirs(
        TEMPLATE_DIR,
        exist_ok=True
    )

    while True:

        if STOP:
            print("Quit.")
            return

        if keyboard.is_pressed("f6"):

            focus_window(hwnd)

            frame = grab(
                sct,
                region
            )

            print()
            print(
                "Drag a box around the DIALOG PANEL, "
                "then press ENTER."
            )

            print(
                "Do NOT select the entire screen."
            )

            box = cv2.selectROI(
                "drag the dialog panel, then ENTER",
                frame,
                showCrosshair=False
            )

            cv2.destroyWindow(
                "drag the dialog panel, then ENTER"
            )

            x, y, w, h = [
                int(v)
                for v in box
            ]

            if w < 20 or h < 20:
                print(
                    "Discarded - no valid box."
                )

                time.sleep(0.3)
                continue

            crop = frame[
                y:y + h,
                x:x + w
            ]

            print()
            print(
                "Hover the mouse over the button "
                "to click and press F7."
            )

            print(
                "Press ESC to discard."
            )

            point = None

            while True:

                if keyboard.is_pressed(
                    "esc"
                ):
                    break

                if keyboard.is_pressed(
                    "f7"
                ):

                    mx, my = (
                        win32gui.GetCursorPos()
                    )

                    point = (
                        mx -
                        region["left"],
                        my -
                        region["top"]
                    )

                    break

                time.sleep(0.02)

            if point is None:
                print("Discarded.")
                time.sleep(0.3)
                continue

            if not (
                x <= point[0] <= x + w
                and
                y <= point[1] <= y + h
            ):
                print(
                    f"  WARNING: click point "
                    f"{point} is outside the "
                    f"template box."
                )

            name = input(
                "Template name "
                "(e.g. connection_lost): "
            ).strip()

            if not name:
                print(
                    "Empty name, discarded."
                )
                continue

            dismiss_only = input(
                "Is this a nuisance/ad popup "
                "close-X rather than a connection "
                "dialog? [y/N]: "
            ).strip().lower().startswith(
                "y"
            )

            cv2.imwrite(
                os.path.join(
                    TEMPLATE_DIR,
                    name + ".png"
                ),
                crop
            )

            with open(
                os.path.join(
                    TEMPLATE_DIR,
                    name + ".json"
                ),
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    {
                        "click_dx":
                            point[0] - x,

                        "click_dy":
                            point[1] - y,

                        "thresh":
                            0.85,

                        "dismiss_only":
                            dismiss_only,

                        # Resolution this template was captured at,
                        # so it can be auto-rescaled later if
                        # BlueStacks runs at a different size.
                        "ref_w":
                            region["width"],

                        "ref_h":
                            region["height"],
                    },
                    f,
                    indent=2
                )

            print()
            print(
                f"Saved {name}.png "
                f"({w}x{h}) calibrated at "
                f"{region['width']}x{region['height']}"
            )

            print(
                f"Saved {name}.json "
                f"with click offset "
                f"({point[0] - x},"
                f"{point[1] - y})"
            )

            time.sleep(0.3)

        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Main watchdog
# ---------------------------------------------------------------------------

def run(
    sct,
    region,
    hwnd,
    templates,
    args
):
    print()
    print(
        f"Watching with {len(templates)} "
        f"template(s): "
        f"{', '.join(t['name'] for t in templates)}"
    )

    print(
        f"Polling every {args.interval}s."
    )

    print(
        "F9 to quit."
    )

    last_fire = {}

    dismissed = 0
    ads_dismissed = 0
    failures = 0

    t_start = time.perf_counter()
    last_heartbeat = time.perf_counter()

    restarted = False

    state = "connected"

    log(
        f"session start; watching "
        f"{len(templates)} template(s) at "
        f"{region['width']}x{region['height']}"
    )

    ka_tmpl_base = None
    ka_tmpl = None
    next_ka = None

    if args.keepalive:

        if args.keepalive_template:

            ka_tmpl_base = cv2.imread(
                args.keepalive_template
            )

            if ka_tmpl_base is None:

                print(
                    f"  keep-alive template "
                    f"{args.keepalive_template} "
                    f"not found - falling back "
                    f"to --keepalive-x/y"
                )

            else:

                # The keep-alive image has no ref_w/ref_h metadata,
                # so it's assumed calibrated at the GAME_W/GAME_H
                # reference size and scaled the same way.
                sx = region["width"] / GAME_W
                sy = region["height"] / GAME_H

                ka_tmpl = scale_image(
                    ka_tmpl_base,
                    sx,
                    sy
                )

                print(
                    f"  keep-alive: matching "
                    f"{os.path.basename(args.keepalive_template)} "
                    f"({ka_tmpl.shape[1]}x"
                    f"{ka_tmpl.shape[0]} after scaling)"
                )

        next_ka = (
            time.perf_counter() +
            random.uniform(
                args.keepalive_min,
                args.keepalive_max
            )
        )

        print(
            f"  keep-alive every "
            f"{args.keepalive_min:.0f}-"
            f"{args.keepalive_max:.0f}s."
        )

    while True:

        if STOP:

            print(
                f"Quit. "
                f"{dismissed} dialog(s), "
                f"{ads_dismissed} ad(s) dismissed."
            )

            return

        # ---------------------------------------------------------------
        # Session timeout / restart
        # ---------------------------------------------------------------

        if (
            args.max_session > 0
            and
            time.perf_counter() -
            t_start
            >
            args.max_session * 3600
        ):

            log(
                f"--max-session "
                f"{args.max_session}h reached."
            )

            do_restart = (
                args.restart_loop
                or
                (
                    args.restart_test
                    and
                    not restarted
                )
            )

            if not do_restart:

                log("stopping.")
                return

            restarted = True

            new_hwnd = restart_cycle(
                sct,
                region,
                hwnd,
                args
            )

            if new_hwnd is None:
                return

            hwnd = new_hwnd

            # IMPORTANT:
            # Recalculate region after restart - BlueStacks may come
            # back at a different window size/resolution.
            region = game_region(
                hwnd,
                args
            )

            # Re-scale every template (and the keep-alive template,
            # if any) to match the new region size.
            prepare_templates(
                templates,
                region
            )

            if ka_tmpl_base is not None:

                sx = region["width"] / GAME_W
                sy = region["height"] / GAME_H

                ka_tmpl = scale_image(
                    ka_tmpl_base,
                    sx,
                    sy
                )

            after = shot(
                sct,
                region,
                "restart_after"
            )

            log(
                f"  after relaunch: {after}"
            )

            f2 = grab(
                sct,
                region
            )

            b2 = best_match(
                f2,
                templates
            )

            if (
                b2
                and
                b2[0].get("fatal")
            ):

                log(
                    f"  RESULT: "
                    f"'{b2[0]['name']}' "
                    f"is STILL PRESENT after "
                    f"restart."
                )

                cv2.imwrite(
                    "restart_result_fatal.png",
                    f2
                )

                return

            if b2:

                log(
                    f"  after relaunch the "
                    f"screen shows "
                    f"'{b2[0]['name']}'"
                )

            else:

                log(
                    "  no dialog after relaunch."
                )

            t_start = (
                time.perf_counter()
            )

            log(
                "  monitoring resumes."
            )

        # ---------------------------------------------------------------
        # Capture current screen
        # ---------------------------------------------------------------

        frame = grab(
            sct,
            region
        )

        best = best_match(
            frame,
            templates
        )

        # ---------------------------------------------------------------
        # No dialog found
        # ---------------------------------------------------------------

        if best is None:

            if state != "connected":

                log(
                    f"state: {state} -> connected "
                    f"(t+"
                    f"{(time.perf_counter() - t_start) / 3600:.2f}"
                    f"h)"
                )

                state = "connected"

            if (
                args.verbose
                and
                time.perf_counter() - last_heartbeat
                >=
                args.heartbeat
            ):

                log(
                    "  no reconnect/disconnect dialog detected - "
                    "still connected, watching..."
                )

                last_heartbeat = time.perf_counter()

            # Keepalive only when no dialog is present.
            if (
                next_ka is not None
                and
                time.perf_counter()
                >=
                next_ka
            ):

                ok, where = (
                    keepalive_click(
                        sct,
                        region,
                        hwnd,
                        ka_tmpl,
                        args
                    )
                )

                gap = random.uniform(
                    args.keepalive_min,
                    args.keepalive_max
                )

                next_ka = (
                    time.perf_counter() +
                    gap
                )

                log(
                    "keep-alive "
                    +
                    (
                        "clicked " + where
                        if ok
                        else
                        "SKIPPED: " + where
                    )
                    +
                    f"; next in {gap:.0f}s"
                )

                if (
                    not ok
                    and
                    args.debug_misses
                ):

                    miss_shot = (
                        "keepalive_miss_"
                        f"{time.strftime('%H%M%S')}.png"
                    )

                    cv2.imwrite(
                        miss_shot,
                        grab(sct, region)
                    )

                    log(
                        f"  wrote {miss_shot}"
                    )

            naptime(
                args.interval
            )

            continue

        # ---------------------------------------------------------------
        # Match found
        # ---------------------------------------------------------------

        t, (
            x,
            y,
            score
        ) = best

        # ---------------------------------------------------------------
        # Advertisement / nuisance popup
        # ---------------------------------------------------------------

        if t.get("dismiss_only"):

            now = (
                time.perf_counter()
            )

            if (
                now -
                last_fire.get(
                    t["name"],
                    0
                )
                <=
                args.ad_cooldown
            ):

                naptime(
                    args.interval
                )

                continue

            cx = (
                x +
                t["click_dx"]
            )

            cy = (
                y +
                t["click_dy"]
            )

            log(
                f"DETECTED ad/popup '{t['name']}' "
                f"score={score:.3f} "
                f"at ({x},{y}) "
                f"-> dismissing at ({cx},{cy})"
            )

            last_fire[
                t["name"]
            ] = now

            if args.dry_run:

                naptime(
                    args.interval
                )

                continue

            focus_window(hwnd)

            naptime(
                args.pre_click
            )

            click_at(
                region,
                cx,
                cy,
                hold=0.06
            )

            wait_settled(
                sct,
                region,
                quiet=args.quiet,
                timeout=args.settle_timeout
            )

            if verify_gone(
                sct,
                region,
                t["img"],
                t["thresh"],
                args.verify_secs
            ):

                ads_dismissed += 1

                log(
                    f"  CONFIRMED: ad '{t['name']}' closed "
                    f"({ads_dismissed} total)"
                )

            else:

                log(
                    f"  NOT CONFIRMED: '{t['name']}' still "
                    f"showing after click."
                )

            naptime(0.15)

            continue

        # ---------------------------------------------------------------
        # Connection dialog
        # ---------------------------------------------------------------

        if state != t["name"]:

            log(
                f"state: {state} -> "
                f"{t['name']} "
                f"(t+"
                f"{(time.perf_counter() - t_start) / 3600:.2f}"
                f"h)"
            )

            state = t["name"]

        now = (
            time.perf_counter()
        )

        if (
            now -
            last_fire.get(
                t["name"],
                0
            )
            <=
            args.cooldown
        ):

            naptime(
                args.interval
            )

            continue

        if t.get("fatal"):

            log(
                f"FATAL '{t['name']}' "
                f"score={score:.3f}"
            )

            cv2.imwrite(
                "reconnect_fatal.png",
                frame
            )

            return

        cx = (
            x +
            t["click_dx"]
        )

        cy = (
            y +
            t["click_dy"]
        )

        log(
            f"DETECTED disconnect dialog '{t['name']}' "
            f"(confidence {score:.3f}) at ({x},{y}) "
            f"-> clicking reconnect at ({cx},{cy})"
        )

        last_fire[
            t["name"]
        ] = now

        if args.dry_run:

            log(
                "  --dry-run: not clicking."
            )

            naptime(
                args.interval
            )

            continue

        ok = False

        for attempt in range(
            1,
            args.max_tries + 1
        ):

            if STOP:
                break

            focus_window(
                hwnd
            )

            naptime(
                args.pre_click
            )

            click_at(
                region,
                cx,
                cy,
                hold=0.06
            )

            wait_settled(
                sct,
                region,
                quiet=args.quiet,
                timeout=args.settle_timeout
            )

            if verify_gone(
                sct,
                region,
                t["img"],
                t["thresh"],
                args.verify_secs
            ):

                ok = True
                break

            log(
                f"  reconnect click NOT confirmed - "
                f"'{t['name']}' still present after attempt "
                f"{attempt}/"
                f"{args.max_tries}"
            )

        if ok:

            dismissed += 1

            failures = 0

            log(
                f"  CONFIRMED: reconnect click worked, "
                f"'{t['name']}' dismissed "
                f"({dismissed} total)"
            )

        else:

            failures += 1

            stuck_shot = (
                f"reconnect_stuck_"
                f"{failures}.png"
            )

            cv2.imwrite(
                stuck_shot,
                grab(sct, region)
            )

            log(
                f"  FAILED to dismiss "
                f"'{t['name']}' after "
                f"{args.max_tries} attempts. "
                f"Saved {stuck_shot} for review."
            )

            if (
                failures >=
                args.max_failures
            ):

                log(
                    f"  giving up after "
                    f"{failures} unresolved "
                    f"dialog(s)."
                )

                return

        naptime(
            args.cooldown
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=
        argparse.RawDescriptionHelpFormatter
    )

    ap.add_argument(
        "--title",
        default=WINDOW_TITLE,
        help=(
            f"window title substring "
            f"(default '{WINDOW_TITLE}')"
        )
    )

    ap.add_argument(
        "--region-x",
        type=int,
        default=None,
        dest="region_x",
        help=(
            "pixels to shift capture right. "
            "Default is 0. "
            "Use 0 when there is no ad panel."
        )
    )

    ap.add_argument(
        "--region-y",
        type=int,
        default=0,
        dest="region_y"
    )

    ap.add_argument(
        "--templates",
        default=TEMPLATE_DIR,
        help=(
            f"template folder "
            f"(default '{TEMPLATE_DIR}')"
        )
    )

    ap.add_argument(
        "--interval",
        type=float,
        default=2.0
    )

    ap.add_argument(
        "--cooldown",
        type=float,
        default=6.0
    )

    ap.add_argument(
        "--ad-cooldown",
        type=float,
        default=1.5,
        dest="ad_cooldown"
    )

    ap.add_argument(
        "--quiet",
        type=float,
        default=0.4
    )

    ap.add_argument(
        "--settle-timeout",
        type=float,
        default=5.0,
        dest="settle_timeout"
    )

    ap.add_argument(
        "--verify-secs",
        type=float,
        default=4.0,
        dest="verify_secs"
    )

    ap.add_argument(
        "--max-tries",
        type=int,
        default=3,
        dest="max_tries"
    )

    ap.add_argument(
        "--max-failures",
        type=int,
        default=2,
        dest="max_failures"
    )

    ap.add_argument(
        "--pre-click",
        type=float,
        default=1.0,
        dest="pre_click"
    )

    ap.add_argument(
        "--keepalive",
        action="store_true",
        help=(
            "click a harmless target periodically"
        )
    )

    ap.add_argument(
        "--keepalive-template",
        default=os.path.join(
            TEMPLATE_DIR,
            "_mine.png"
        ),
        dest="keepalive_template"
    )

    ap.add_argument(
        "--keepalive-x",
        type=int,
        default=None,
        dest="keepalive_x"
    )

    ap.add_argument(
        "--keepalive-y",
        type=int,
        default=None,
        dest="keepalive_y"
    )

    ap.add_argument(
        "--keepalive-min",
        type=float,
        default=30.0,
        dest="keepalive_min"
    )

    ap.add_argument(
        "--keepalive-max",
        type=float,
        default=120.0,
        dest="keepalive_max"
    )

    ap.add_argument(
        "--keepalive-thresh",
        type=float,
        default=0.80,
        dest="keepalive_thresh"
    )

    ap.add_argument(
        "--max-session",
        type=float,
        default=0.0,
        dest="max_session"
    )

    ap.add_argument(
        "--restart-test",
        action="store_true",
        dest="restart_test"
    )

    ap.add_argument(
        "--restart-loop",
        action="store_true",
        dest="restart_loop"
    )

    ap.add_argument(
        "--restart-cmd",
        default="",
        dest="restart_cmd"
    )

    ap.add_argument(
        "--restart-exe",
        default="",
        dest="restart_exe"
    )

    ap.add_argument(
        "--restart-args",
        default="",
        dest="restart_args"
    )

    ap.add_argument(
        "--restart-wait",
        type=float,
        default=90.0,
        dest="restart_wait"
    )

    ap.add_argument(
        "--close-timeout",
        type=float,
        default=30.0,
        dest="close_timeout"
    )

    ap.add_argument(
        "--relaunch-timeout",
        type=float,
        default=180.0,
        dest="relaunch_timeout"
    )

    ap.add_argument(
        "--settle-after",
        type=float,
        default=45.0,
        dest="settle_after"
    )

    ap.add_argument(
        "--calibrate",
        action="store_true"
    )

    ap.add_argument(
        "--dry-run",
        action="store_true"
    )

    ap.add_argument(
        "--debug-misses",
        action="store_true",
        dest="debug_misses"
    )

    ap.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "print a periodic heartbeat even when no "
            "dialog is detected, so you can see the "
            "watchdog is alive"
        )
    )

    ap.add_argument(
        "--heartbeat",
        type=float,
        default=30.0,
        dest="heartbeat",
        help=(
            "seconds between --verbose heartbeat "
            "messages (default 30)"
        )
    )

    args = ap.parse_args()

    # ------------------------------------------------------------------
    # NEW DEFAULT:
    # No old 228px BlueStacks advertisement panel.
    # ------------------------------------------------------------------

    if args.region_x is None:
        args.region_x = 0

        print(
            "  --region-x not given; "
            "defaulting to 0."
        )

    fix_dpi()

    try:
        keyboard.add_hotkey(
            "f9",
            request_stop
        )

    except Exception as e:

        print(
            f"  could not register F9 "
            f"hotkey ({e}); use Ctrl+C."
        )

    hwnd, title = find_window(
        args.title
    )

    if not hwnd:

        print(
            f"No window matching "
            f"'{args.title}'. "
            f"Is the emulator open?"
        )

        return

    print(
        f"Found: {title}"
    )

    # ------------------------------------------------------------------
    # Build capture region - works at ANY BlueStacks window size.
    # ------------------------------------------------------------------

    region = game_region(
        hwnd,
        args
    )

    print(
        f"  Final capture: "
        f"{region['width']}x"
        f"{region['height']} "
        f"at screen "
        f"({region['left']},"
        f"{region['top']})"
    )

    print()

    with mss.MSS() as sct:

        # --------------------------------------------------------------
        # Calibration
        # --------------------------------------------------------------

        if args.calibrate:

            calibrate(
                sct,
                region,
                hwnd
            )

            return

        # --------------------------------------------------------------
        # Load templates and scale them to the current resolution
        # --------------------------------------------------------------

        templates = load_templates(
            args.templates
        )

        if not templates:

            print(
                f"No templates in "
                f"'{args.templates}'."
            )

            print(
                "Run:"
            )

            print(
                "  python kot_reconnect.py "
                "--title BlueStacks --calibrate"
            )

            return

        prepare_templates(
            templates,
            region
        )

        # --------------------------------------------------------------
        # Start watchdog
        # --------------------------------------------------------------

        run(
            sct,
            region,
            hwnd,
            templates,
            args
        )


if __name__ == "__main__":
    main()