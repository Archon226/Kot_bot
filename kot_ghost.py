"""
kot_ghost.py - learn a dungeon from its ghost, then replay it.

WHAT THIS IS FOR

King of Thieves will show you the solution to a dungeon (the "ghost") for
gems. That makes raids automatable in a way campaign levels never were:

    buy the solution -> record the ghost -> extract the taps -> replay

No demonstration of your own is needed, so this works on dungeons you have
never solved. That is the whole point - a campaign replayer can only redo
levels you already beat, which is worth nothing.

WHY READING TAPS OFF A VIDEO WORKS AFTER ALL

The session-2 notes concluded it could not:

    "KoT ignores taps made mid-air. So a recorded tap log and what the game
     actually did are different things. Video can only show what the game
     DID, never what you PRESSED. This is a ceiling, not a tuning problem."

That reasoning has a hole. A tap the game ignored changed nothing about
the trajectory, so leaving it out of the replay produces an identical run.
The taps that matter are exactly the ones that altered the motion - and
those are precisely the ones visible on screen.

Measured on ghost_234213: 8 launches recovered from a run nobody played,
against 8 taps counted by eye. Nothing missing.

THE ANCHOR

Detected times are measured from when recording started, which is not an
event a replay can reproduce. The anchor used here is THE FRAME THE THIEF
FIRST APPEARS, because that moment exists identically in the ghost and in
your attempt.

The appearance is verified three ways before the clock starts: the blob
must be near where the ghost's thief appeared, roughly the same size, and
present for several consecutive frames. Anchoring on the wrong object
would shift every tap in the run.

WHAT THIS DOES NOT SOLVE

Timing precision. Frame capture jitter was 16.3-24.2ms on this machine and
a jump window is around 30ms, so tight dungeons may not replay. That is
measurable per dungeon: replay it, watch, and if it dies at the same place
every time the run is too tight rather than mistimed.

Usage:
    python kot_ghost.py extract runs/ghost_XXXX.json --out ghosts/base1.json
    python kot_ghost.py replay ghosts/base1.json --dry-run
    python kot_ghost.py replay ghosts/base1.json

Keys (replay):
    F7  learn background (pre-level screen, thief NOT placed)
    F8  arm and go
    F9  quit    ESC aborts mid-run
"""

import argparse
import ctypes
import json
import os
import time
from ctypes import wintypes

import cv2
import numpy as np

import kot_track_thief as kg
import kot_launch as kl

WINDOW_TITLE = "LDPlayer"
GAME_W, GAME_H = 1280, 720
CROP_TOP, CROP_BOTTOM = 0.06, 0.90


# ============================================================== EXTRACT ===

def extract(args):
    meta, frames = kg.load(args.run)
    print(f"{meta['mode']} run: {meta['frames']} frames, {meta['duration']}s")
    if meta["mode"] != "ghost":
        print("  note: this is not a --ghost recording. That is fine, but "
              "the taps will be whatever the tracker sees, not what you "
              "pressed.")

    path = kg.track(meta, frames, args)
    found = int((~np.isnan(path[:, 1])).sum())
    print(f"thief located in {found}/{meta['frames']} frames")

    t, xs, ys, vx, vy, ay, speed = kl.kinematics(path)
    _, x_raw, y_raw, observed = kl.prepare(path, quiet=True)
    cover = observed.sum() / len(observed)
    print(f"coverage of the tracked span: {100 * cover:.0f}%")
    if cover < 0.7:
        raise SystemExit("Tracking too poor. Check kot_track_thief --tracks.")

    a_fit, res = kl.local_accel(t, y_raw, args.fit_win)
    prior = kg.expected_gravity(meta, args)
    g, n_used, how = kl.fit_gravity(a_fit, res, prior, args.fit_res)
    g_full = g / meta["scale"]
    print(f"gravity fitted {g:+.0f} ({g_full:+.0f} full-res), prior "
          f"{prior:+.0f}  [{how}]")

    expected = args.expect_gravity
    if expected > 0 and not (0.75 * expected < g_full < 1.35 * expected):
        print(f"\n  *** TRACKING LIKELY WRONG: gravity {g_full:+.0f} vs "
              f"expected {expected:+.0f}. Whatever was tracked does not "
              f"fall like the thief. ***")
        if not args.force:
            raise SystemExit("  Refusing to write. --force to override.")

    launches, _ = kl.find_launches(t, vx, vy, ay, speed, args,
                                   a_fit=a_fit, res=res, g=g)
    print(f"\n{len(launches)} raw launches")

    k = 1.0 / meta["scale"]

    # kl.prepare trims to the observed span, so t[0] IS the frame the thief
    # first appears - which is the anchor.
    t0 = float(t[0])
    ax, ay_pos = float(xs[0]) * k, float(ys[0]) * k

    kept, dropped = [], []
    for lt, s0, s1, lvx, lvy in launches:
        # The ghost materialising at the door reads as a launch: on
        # ghost_234213 it showed 0 -> 1296 px/s moving hard left. Real
        # jumps on that run entered flight at 204-293 px/s.
        if s1 > args.max_entry:
            dropped.append((lt, f"entry {s1:.0f} px/s - spawn or animation"))
            continue
        if lvy > args.max_vy:
            dropped.append((lt, f"vy {lvy:+.0f} - a landing, not a launch"))
            continue
        if lt - t0 < args.skip_first:
            dropped.append((lt, "within --skip-first of the appearance"))
            continue
        kept.append(round(lt - t0, 4))

    for lt, why in dropped:
        print(f"  dropped {lt:6.3f}s: {why}")
    if not kept:
        raise SystemExit("Nothing survived filtering.")

    print(f"\n{len(kept)} taps, timed from the thief appearing at "
          f"{t0:.3f}s:")
    gaps = np.diff([0.0] + kept)
    for i, (tt, gp) in enumerate(zip(kept, gaps), 1):
        print(f"  {i:2d}  t={tt:6.3f}s   (+{gp * 1000:5.0f}ms)")
    if gaps.min() < args.refractory:
        print(f"  NOTE: closest pair is {gaps.min() * 1000:.0f}ms apart. "
              f"Real KoT wall-jumps can be that close, but so is one arc "
              f"counted twice - check it against the ghost.")

    # Anchor signature, so replay can confirm it is watching the thief and
    # not a torch: where it appeared, and roughly how big it is.
    out = {
        "source": os.path.basename(args.run),
        "game_w": GAME_W, "game_h": GAME_H,
        "gravity": round(float(g_full), 1),
        "anchor": {"x": round(ax, 1), "y": round(ay_pos, 1),
                   "area": args.anchor_area},
        "taps": kept,
    }
    dest = args.out or "ghosts/ghost.json"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w") as f:
        json.dump(out, f, indent=1)
    print(f"\nSaved {dest}")
    print(f"anchor: thief appears at ({ax:.0f},{ay_pos:.0f}) full-res")
    print("Replay with:  python kot_ghost.py replay "
          f"{dest} --dry-run")


# =============================================================== REPLAY ===

def fix_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


def find_window(sub):
    import win32gui
    out = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if sub.lower() in t.lower():
                out.append((hwnd, t))

    win32gui.EnumWindows(cb, None)
    return out[0] if out else (None, None)


def game_region(hwnd, args=None):
    """The 1280x720 game surface inside the emulator window.

    The old version assumed all the chrome was on the right and grabbed
    the leftmost 1280 columns. BlueStacks breaks that assumption: it puts
    an ADVERT PANEL down the left side, roughly 228px wide, so the capture
    started inside the adverts and clipped the game. The anchor duly locked
    onto a 2708px blob of jewellery.

    --region-x / --region-y shift the capture. Check ghost_bg.png: it
    should be the game and nothing else.
    """
    import win32gui
    l, t, r, b = win32gui.GetClientRect(hwnd)
    l, t = win32gui.ClientToScreen(hwnd, (l, t))
    r, b = win32gui.ClientToScreen(hwnd, (r, b))
    cw, ch = r - l, b - t
    ox = getattr(args, "region_x", 0) or 0
    oy = getattr(args, "region_y", 0) or 0
    if cw - ox < GAME_W or ch - oy < GAME_H:
        raise SystemExit(f"Window {cw}x{ch} with offset ({ox},{oy}) cannot "
                         f"contain {GAME_W}x{GAME_H}.")
    print(f"Client {cw}x{ch}; capturing {GAME_W}x{GAME_H} at offset "
          f"({ox},{oy}). Verify ghost_bg.png shows ONLY the game.")
    return {"left": l + ox, "top": t + oy,
            "width": GAME_W, "height": GAME_H}


def focus_window(hwnd):
    """Windows eats the first click on an inactive window as an activation
    click, so an unfocused emulator ignores the tap that starts the level."""
    import win32gui
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"  could not focus ({e}); click LDPlayer once yourself.")
    time.sleep(0.25)


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
    import win32api, win32con
    sx, sy = region["left"] + gx, region["top"] + gy
    vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
          int((sx - vx) * 65535 / (vw - 1)), int((sy - vy) * 65535 / (vh - 1)))


def precise_sleep(seconds):
    """time.sleep has ~15ms granularity on Windows - useless when a jump
    window is 30ms. Sleep most of the way, busy-spin the last 2ms."""
    if seconds <= 0:
        return
    end = time.perf_counter() + seconds
    coarse = seconds - 0.002
    if coarse > 0:
        time.sleep(coarse)
    while time.perf_counter() < end:
        pass


def tap(hold=0.05):
    _send(MOUSEEVENTF_LEFTDOWN)
    precise_sleep(hold)
    _send(MOUSEEVENTF_LEFTUP)


def grab(sct, box):
    raw = sct.grab(box)
    arr = np.frombuffer(raw.bgra, dtype=np.uint8)
    return arr.reshape(raw.height, raw.width, 4)[:, :, :3]


def thumb(frame):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (80, 45), interpolation=cv2.INTER_AREA)


def motion(a, b):
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def mask_of(patch, bg_patch, args):
    """Colour mask for the thief.

    THE COSTUME IS NOT ALWAYS WHITE. This only ever built a white mask,
    which is right for the panda skin and useless for anything else.
    Measured against a green-costumed thief in a real video: 1.0% of its
    pixels passed the white mask, while 35% sat in the green hue band. It
    was never going to be detected, and no amount of anchor tuning would
    have helped.
    """
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    mode = getattr(args, "mode", "white")
    if mode == "hue":
        m = cv2.inRange(hsv,
                        np.array([args.hue_lo, args.sat, args.val], np.uint8),
                        np.array([args.hue_hi, 255, 255], np.uint8))
    elif mode == "any":
        m = np.full(hsv.shape[:2], 255, np.uint8)
    else:
        m = cv2.inRange(hsv, np.array([0, 0, args.white_v], np.uint8),
                        np.array([179, args.white_s, 255], np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8),
                         iterations=2)
    if bg_patch is not None:
        d = cv2.absdiff(patch, bg_patch).max(axis=2)
        m = cv2.bitwise_and(m, (d > args.bg_thresh).astype(np.uint8) * 255)
    return m


def blobs(mask, args):
    nl, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    return [(float(cents[j][0]), float(cents[j][1]),
             int(stats[j, cv2.CC_STAT_AREA])) for j in range(1, nl)
            if args.minarea <= stats[j, cv2.CC_STAT_AREA] <= args.maxarea]


def learn_background(sct, region, args):
    stack = [grab(sct, region) for _ in range(5)]
    bg = np.median(np.stack(stack), axis=0).astype(np.uint8)
    cv2.imwrite("ghost_bg.png", bg)
    return bg


def wait_for_appearance(sct, region, bg, anchor, args):
    """Block until the thief shows up, and return that instant.

    This is the anchor for the whole run, so a wrong lock shifts every tap.
    Three conditions must hold together: the blob is near where the ghost's
    thief appeared, it is roughly the right size, and it persists for
    several frames. Any one alone would fire on a torch.
    """
    y0, y1 = int(GAME_H * CROP_TOP), int(GAME_H * CROP_BOTTOM)
    # A plan extracted from someone else's video has no idea where the
    # thief spawns in YOUR emulator - the recording is a different
    # resolution and aspect, so its coordinates do not transfer. In that
    # case anchor on the first thief-sized blob instead. Weaker: any
    # white object of about the right size will do, so check the reported
    # position against where the thief actually appears before trusting a
    # live run.
    any_mode = (anchor.get("mode") == "any" or anchor.get("x") is None)
    ax = anchor.get("x") or 0.0
    ay = anchor.get("y") or 0.0
    want_area = float(anchor.get("area") or 1100)
    if any_mode:
        print(f"  anchor mode: any blob of {want_area / args.area_tol:.0f}-"
              f"{want_area * args.area_tol:.0f}px (no spawn position in "
              f"the plan). Verify the reported position looks right.")
    hits = 0
    t_end = time.perf_counter() + args.appear_timeout
    best_seen = None

    while time.perf_counter() < t_end:
        import keyboard
        if keyboard.is_pressed("f9") or keyboard.is_pressed("esc"):
            return None
        frame = grab(sct, region)
        found = blobs(mask_of(frame[y0:y1], bg[y0:y1], args), args)
        if any_mode:
            # Without a spawn position, SIZE is the only check left, so it
            # has to be a real one. --minarea is 40, which accepts almost
            # any speck; a run anchored on a 578px fragment of scenery
            # times every tap from the wrong instant. The plan records the
            # thief's expected area - use it.
            lo_a = want_area / args.area_tol
            hi_a = want_area * args.area_tol
            near = [b for b in found if lo_a <= b[2] <= hi_a]
        else:
            near = [b for b in found
                    if np.hypot(b[0] - ax, b[1] + y0 - ay) <= args.anchor_tol]
        if near:
            b = max(near, key=lambda c: c[2])
            best_seen = b
            hits += 1
            if hits >= args.appear_frames:
                print(f"  anchored on a {b[2]}px blob at "
                      f"({b[0]:.0f},{b[1] + y0:.0f})")
                return time.perf_counter()
        else:
            hits = 0

    if any_mode:
        print(f"  no blob of {want_area / args.area_tol:.0f}-"
              f"{want_area * args.area_tol:.0f}px appeared within "
              f"{args.appear_timeout:.0f}s.")
    else:
        print(f"  the thief never appeared within "
              f"{args.appear_timeout:.0f}s at ({ax:.0f},{ay:.0f}) "
              f"+-{args.anchor_tol:.0f}px.")
    if best_seen:
        print(f"  closest thing seen: {best_seen[2]}px blob at "
              f"({best_seen[0]:.0f},{best_seen[1] + y0:.0f})")
    print("  Is this the same dungeon the ghost came from?")
    return None


def scan(args):
    """Print every blob the current mask finds, live.

    Guessing at the spawn position and the thief's size wasted several
    runs. This just shows what is actually detectable on the screen in
    front of you, so the anchor can be set from measurement.
    """
    import keyboard
    import mss

    fix_dpi()
    hwnd, title = find_window(args.title or WINDOW_TITLE)
    if not hwnd:
        print(f"No window matching '{args.title or WINDOW_TITLE}'.")
        return
    region = game_region(hwnd, args)
    print(f"Found: {title}\nmode={args.mode}   F8 to sample, F9 to quit\n")
    y0, y1 = int(GAME_H * CROP_TOP), int(GAME_H * CROP_BOTTOM)
    bg = None
    with mss.MSS() as sct:
        while True:
            if keyboard.is_pressed("f9"):
                return
            if keyboard.is_pressed("f7"):
                bg = learn_background(sct, region, args)
                print("background learned")
                time.sleep(0.4)
            if keyboard.is_pressed("f8"):
                # Sample continuously. A single snapshot has to be taken at
                # exactly the right instant of a run that lasts a couple of
                # seconds, which is not practical - press F8, then start
                # the run, and let this watch the whole thing.
                print(f"  sampling for {args.seconds:.0f}s - start the run "
                      f"NOW (Test it / TAP TO BREAK IN)")
                while keyboard.is_pressed("f8"):
                    time.sleep(0.01)
                seen = []
                best = None
                t_end = time.perf_counter() + args.seconds
                while time.perf_counter() < t_end:
                    if keyboard.is_pressed("f9"):
                        break
                    frame = grab(sct, region)
                    m = mask_of(frame[y0:y1],
                                None if bg is None else bg[y0:y1], args)
                    found = blobs(m, args)
                    for x, y, a in found:
                        seen.append((x, y + y0, a))
                    if found:
                        big = max(found, key=lambda b: b[2])
                        # Keep the frame with the most blobs - most likely
                        # to be mid-run rather than on a menu.
                        if best is None or len(found) > best[0]:
                            best = (len(found), frame.copy(), m.copy())
                    time.sleep(0.02)

                if not seen:
                    print("  NOTHING detected in the whole window. The mask "
                          "cannot see the thief at these settings.")
                else:
                    arr = np.array(seen)
                    print(f"  {len(seen)} blob sightings over "
                          f"{args.seconds:.0f}s")
                    print(f"    x range {arr[:,0].min():.0f}-"
                          f"{arr[:,0].max():.0f}   y range "
                          f"{arr[:,1].min():.0f}-{arr[:,1].max():.0f}")
                    print(f"    area p10 {np.percentile(arr[:,2],10):.0f}  "
                          f"median {np.median(arr[:,2]):.0f}  "
                          f"p90 {np.percentile(arr[:,2],90):.0f}")
                    print("    a blob whose x AND y both range widely is "
                          "the thief; static decor barely moves.")
                if best:
                    cv2.imwrite("scan_frame.png", best[1])
                    cv2.imwrite("scan_mask.png", best[2])
                    print("  wrote scan_frame.png / scan_mask.png "
                          f"({best[0]} blobs in that frame)")
                time.sleep(0.4)
            time.sleep(0.01)


def replay(args):
    import keyboard
    import mss

    with open(args.plan) as f:
        plan = json.load(f)
    taps = plan["taps"]
    anchor = plan["anchor"]
    print(f"{len(taps)} taps from {plan.get('source', '?')}")
    if anchor.get("x") is None or anchor.get("mode") == "any":
        print("anchor: first thief-sized blob (no spawn position in plan)")
    else:
        print(f"anchor: thief appears at "
              f"({anchor['x']:.0f},{anchor['y']:.0f})")
    if plan.get("note"):
        print(f"note: {plan['note']}")

    fix_dpi()
    hwnd, title = find_window(args.title or WINDOW_TITLE)
    if not hwnd:
        print(f"No window matching '{WINDOW_TITLE}'.")
        return
    region = game_region(hwnd, args)
    print(f"Found: {title}")

    bg = None
    print("\nF7 = learn background (pre-level screen)   F8 = go   F9 = quit\n")

    with mss.MSS() as sct:
        while True:
            if keyboard.is_pressed("f9"):
                print("Bye.")
                return
            if keyboard.is_pressed("f7"):
                bg = learn_background(sct, region, args)
                print("Background learned -> ghost_bg.png")
                time.sleep(0.4)
            if keyboard.is_pressed("f8"):
                if bg is None:
                    print("Learn a background first (F7).")
                    time.sleep(0.4)
                    continue
                run_once(sct, region, hwnd, bg, taps, anchor, args)
                time.sleep(0.6)
            time.sleep(0.005)


def run_once(sct, region, hwnd, bg, taps, anchor, args):
    import keyboard
    print(f"\n{'DRY RUN - no taps' if args.dry_run else 'LIVE'}   "
          f"{len(taps)} taps   F9/ESC aborts")
    while keyboard.is_pressed("f8"):
        time.sleep(0.01)

    focus_window(hwnd)
    park_cursor(region, args.x, args.y)
    if not args.dry_run:
        print("  tapping to start...")
        tap(args.hold)
    else:
        print("  (dry run: start the level yourself)")

    print("  waiting for the thief to appear...")
    t0 = wait_for_appearance(sct, region, bg, anchor, args)
    if t0 is None:
        return
    print(f"  ANCHORED. firing {len(taps)} taps.\n")

    for i, tt in enumerate(taps, 1):
        target = t0 + tt + args.offset
        while True:
            left = target - time.perf_counter()
            if left <= 0.002:
                break
            if left > 0.01:
                if keyboard.is_pressed("f9") or keyboard.is_pressed("esc"):
                    print("ABORTED.")
                    return
                time.sleep(0.004)
        while time.perf_counter() < target:
            pass

        if not args.dry_run:
            tap(args.hold)
        actual = time.perf_counter() - t0
        print(f"  tap {i:2d}/{len(taps)}  t={tt:6.3f}s  "
              f"drift {(actual - tt - args.offset) * 1000:+5.1f}ms")

    print("\nAll taps sent. Timing is anchored to the thief appearing, so "
          "drift cannot accumulate - if the run failed, the cause is the "
          "game state or capture jitter, not the clock.")


# ================================================================== MAIN ===

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="ghost recording -> tap plan")
    e.add_argument("run")
    e.add_argument("--out")
    e.add_argument("--still", type=float, default=40)
    e.add_argument("--entry", type=float, default=200)
    e.add_argument("--gain", type=float, default=80)
    e.add_argument("--gtol", type=float, default=0.4)
    e.add_argument("--window", type=int, default=8)
    e.add_argument("--refractory", type=float, default=0.18,
                   help="minimum gap between launches. 0.18 removed two "
                        "double-counted arcs on ghost_234213; note that "
                        "real KoT wall-jumps can be ~150ms apart, so this "
                        "can also suppress a genuine pair")
    e.add_argument("--fit-win", type=int, default=13, dest="fit_win")
    e.add_argument("--fit-res", type=float, default=2.0, dest="fit_res")
    e.add_argument("--flight-fill", type=int, default=2, dest="flight_fill")
    e.add_argument("--contact-min", type=int, default=3, dest="contact_min")
    e.add_argument("--max-vy", type=float, default=300, dest="max_vy")
    e.add_argument("--max-entry", type=float, default=600, dest="max_entry",
                   help="drop launches faster than this. The ghost "
                        "materialising read as 1296 px/s; real jumps were "
                        "204-293")
    e.add_argument("--skip-first", type=float, default=0.15,
                   dest="skip_first",
                   help="ignore detections this soon after the thief "
                        "appears - that is the spawn, not a tap")
    e.add_argument("--expect-gravity", type=float, default=1700,
                   dest="expect_gravity")
    e.add_argument("--anchor-area", type=int, default=1100,
                   dest="anchor_area",
                   help="expected full-res blob area of the thief")
    e.add_argument("--force", action="store_true")
    kg.add_detector_args(e)

    r = sub.add_parser("replay", help="tap plan -> live run")
    r.add_argument("plan")
    r.add_argument("--dry-run", action="store_true", dest="dry_run")
    r.add_argument("--offset", type=float, default=0.0,
                   help="shift every tap by this many seconds. If the run "
                        "dies consistently early or late, try +-0.03")
    r.add_argument("--title", default=None,
                   help="emulator window title substring, e.g. BlueStacks. "
                        "Overrides the built-in default so an updated copy "
                        "of this file does not undo your edit")
    r.add_argument("--region-x", type=int, default=0, dest="region_x",
                   help="px to shift the capture right. BlueStacks puts an "
                        "advert panel about 228px wide on the LEFT")
    r.add_argument("--region-y", type=int, default=0, dest="region_y",
                   help="px to shift the capture down")
    r.add_argument("--area-tol", type=float, default=1.8, dest="area_tol",
                   help="how far the anchor blob's area may differ from the "
                        "plan's expected area, as a factor either way")
    r.add_argument("--anchor-tol", type=float, default=90,
                   dest="anchor_tol",
                   help="px within which a blob counts as the thief "
                        "appearing")
    r.add_argument("--appear-frames", type=int, default=3,
                   dest="appear_frames",
                   help="consecutive frames before the anchor is accepted")
    r.add_argument("--appear-timeout", type=float, default=8.0,
                   dest="appear_timeout")
    r.add_argument("--mode", choices=["white", "hue", "any"],
                   default="white",
                   help="white = pale costume (panda); hue = coloured "
                        "costume, green by default; any = motion only. A "
                        "green thief scores 1%% on the white mask")
    r.add_argument("--hue-lo", type=int, default=35, dest="hue_lo")
    r.add_argument("--hue-hi", type=int, default=85, dest="hue_hi")
    r.add_argument("--sat", type=int, default=90)
    r.add_argument("--val", type=int, default=60)
    r.add_argument("--white-v", type=int, default=220, dest="white_v")
    r.add_argument("--white-s", type=int, default=30, dest="white_s")
    r.add_argument("--bg-thresh", type=int, default=28, dest="bg_thresh")
    r.add_argument("--minarea", type=int, default=40)
    r.add_argument("--maxarea", type=int, default=3000)
    r.add_argument("--hold", type=float, default=0.05)
    r.add_argument("--x", type=int, default=640)
    r.add_argument("--y", type=int, default=360)

    sc = sub.add_parser("scan", help="show what the mask detects, live")
    sc.add_argument("--title", default=None)
    sc.add_argument("--region-x", type=int, default=0, dest="region_x")
    sc.add_argument("--region-y", type=int, default=0, dest="region_y")
    sc.add_argument("--mode", choices=["white", "hue", "any"],
                    default="white")
    sc.add_argument("--hue-lo", type=int, default=35, dest="hue_lo")
    sc.add_argument("--hue-hi", type=int, default=85, dest="hue_hi")
    sc.add_argument("--sat", type=int, default=90)
    sc.add_argument("--val", type=int, default=60)
    sc.add_argument("--white-v", type=int, default=220, dest="white_v")
    sc.add_argument("--white-s", type=int, default=30, dest="white_s")
    sc.add_argument("--bg-thresh", type=int, default=28, dest="bg_thresh")
    sc.add_argument("--minarea", type=int, default=40)
    sc.add_argument("--maxarea", type=int, default=6000)
    sc.add_argument("--seconds", type=float, default=12.0,
                    help="how long to sample after F8, so you can start "
                         "the run and let it watch")

    args = ap.parse_args()
    if args.cmd == "extract":
        extract(args)
    elif args.cmd == "scan":
        scan(args)
    else:
        replay(args)


if __name__ == "__main__":
    main()