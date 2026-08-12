"""
kot_agent.py v2 - position-triggered replay. The closed-loop agent.

Tracks the thief live and fires a tap when it reaches the next waypoint,
instead of when a clock says so.

WHY POSITION AND NOT TIME

Time-triggered replay failed repeatedly:
  - the replay's t=0 and the demonstration's t=0 were different moments
  - detected launch times carried ~+-100ms error, wider than a jump window
  - one missed tap shifted every tap after it
  - KoT ignores taps made mid-air, so a tap log and what the game did are
    different things

Position has none of those failure modes, and adds one thing time cannot:
if the thief is nowhere near the next waypoint, the run has ALREADY gone
wrong, and the agent can stop rather than firing taps into a dead run.

--------------------------------------------------------------------------
WHAT CHANGED IN v2

Three problems, all seen on the first live attempt on campaign level 28.

1. IT ABORTED BEFORE THE LEVEL STARTED.

       ABORT: 504px from waypoint 1 (428,645) - run has diverged.

   Waypoint 1 is where the thief was 2.05s into the demonstration. At the
   moment F8 is pressed the thief is still in the doorway, legitimately
   ~500px away, and the divergence check ran on the very first frame. The
   check had no concept of "the run has not begun".

   v2 adds a start gate: the agent taps to start the level itself, then
   waits for real motion, and only then begins matching waypoints. The
   divergence check is suspended for --grace seconds after that.

2. IT PICKED ITS TARGET THE WAY THE OLD TRACKER DID.

   v1 called biggest_blob() on a full-frame scan and committed to whatever
   came back. That is precisely the greedy algorithm that followed a
   MINION through an entire run offline while reporting "thief located in
   97% of frames". Level 28 has a bird patrolling at y~153 for the whole
   level, and its blob was sitting in the background snapshot.

   v2 runs an ACQUISITION PHASE: track every candidate for --acquire
   seconds, fit a parabola to each one's y(t), and lock the one whose
   vertical acceleration matches gravity. The thief drops out of the door
   at level start, so the evidence arrives immediately and for free.

   Offline this test rejected a patrolling minion 220 to 0. It is the same
   test, on a shorter sample.

3. IT FOLLOWED WHICHEVER FRAGMENT WAS BIGGEST.

   The white mask splits the panda into several components, so
   biggest_blob() hops between face and body and jitters the position by
   ~20px frame to frame - which is noise straight into the velocity gate.
   v2 takes the centroid of all blobs within one sprite width of the last
   position, so a split sprite reads as one object.

WINDOW LENGTH IS IN SECONDS, NOT FRAMES

The offline tools fit over 13 frames because they run at ~60fps. This loop
runs at ~6ms, so 13 frames would span only 78ms - and the noise in a
fitted acceleration scales as 1/T^2. At 78ms gravity bends the path 5px
while 1px of centroid jitter costs ~1300 px/s^2: unusable. At 200ms it
bends the path 34px and the same jitter costs ~200. So the window is
specified in TIME and the frame count is derived.

SAFETY

  - F9 or ESC aborts instantly, mid-run
  - --dry-run tracks and reports what it WOULD do, without clicking
  - it aborts if the thief drifts far from the expected waypoint
  - it aborts if the run exceeds --timeout seconds

NOTE ON --dry-run: it never taps, so the thief never follows the
demonstrated path, so it MUST diverge after waypoint 1. A dry run can
validate background learning, live tracking and acquisition. It cannot
validate a waypoint sequence. Use campaign levels for live runs - free
retries, no lockpicks.

Usage:
    python kot_agent.py waypoints/lvl28.json --dry-run
    python kot_agent.py waypoints/lvl28.json --vtol 400
    python kot_agent.py waypoints/lvl28.json --no-start-tap

Keys:
    F7  learn background (pre-level screen, thief NOT placed)
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

# Full-resolution gravity, px/s^2. The same constant the offline tools
# validate against; the agent tracks at full res so no scaling is needed.
GRAVITY = 1700.0


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


def blobs_of(mask, args):
    """Every candidate blob, not just the biggest.

    v1 returned only the largest. That is a decision, made once per frame,
    with no evidence - and it is the same decision the offline tracker used
    to make when it followed a minion for a whole run.
    """
    nl, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for j in range(1, nl):
        a = int(stats[j, cv2.CC_STAT_AREA])
        if args.minarea <= a <= args.maxarea:
            out.append((float(cents[j][0]), float(cents[j][1]), a))
    return out


def biggest_blob(mask, args):
    b = blobs_of(mask, args)
    return max(b, key=lambda c: c[2]) if b else None


def sprite_centroid(blobs, near, radius):
    """Area-weighted centroid of the blobs belonging to one sprite.

    The white mask splits the panda into face plus smaller patches. Taking
    whichever fragment happens to be biggest makes the reported position
    hop between body parts by ~20px from frame to frame, and that noise
    goes straight into the velocity gate. Merging everything within one
    sprite width gives a stable point.
    """
    if not blobs:
        return None
    if near is None:
        b = max(blobs, key=lambda c: c[2])
        near = (b[0], b[1])
    sel = [c for c in blobs
           if (c[0] - near[0]) ** 2 + (c[1] - near[1]) ** 2 <= radius ** 2]
    if not sel:
        return None
    w = sum(c[2] for c in sel)
    return (sum(c[0] * c[2] for c in sel) / w,
            sum(c[1] * c[2] for c in sel) / w,
            w)


# ----------------------------------------------------------- background

def _bg_warn(bg, args):
    y0, y1 = int(GAME_H * CROP_TOP), int(GAME_H * CROP_BOTTOM)
    probe = biggest_blob(mask_of(bg[y0:y1], None, args), args)
    if probe:
        print(f"  note: {probe[2]}px white blob in background at "
              f"({probe[0]:.0f}, {probe[1] + y0:.0f}) - only a problem if "
              f"that is the thief. A patrolling minion parked here is "
              f"normal, and it will read as MOVING all run.")
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
    """
    span, n = args.bg_secs, args.bg_frames
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
    return _bg_warn(bg, args)


# --------------------------------------------------------- acquisition

class Cand:
    __slots__ = ("t", "x", "y", "a", "missed")

    def __init__(self, t, x, y, a):
        self.t, self.x, self.y, self.a = [t], [x], [y], [a]
        self.missed = 0

    def add(self, t, x, y, a):
        self.t.append(t)
        self.x.append(x)
        self.y.append(y)
        self.a.append(a)
        self.missed = 0


def _ballistic(cand, win_secs, tol_frac, max_res):
    """How many sliding windows of this candidate fall like the thief.

    Window length is in SECONDS. The agent loop runs at ~6ms, so a
    fixed frame count would give a window far too short to resolve
    gravity: the noise in a fitted acceleration scales as 1/T^2, and at
    78ms one pixel of centroid jitter is worth ~1300 px/s^2.
    """
    t = np.asarray(cand.t)
    y = np.asarray(cand.y)
    if len(t) < 8:
        return 0, 0
    dt = np.median(np.diff(t)) if len(t) > 1 else 0.016
    k = max(7, int(win_secs / max(dt, 1e-4)))
    if len(t) < k:
        k = len(t)
    good = total = 0
    for i in range(0, len(t) - k + 1):
        tt, yy = t[i:i + k], y[i:i + k]
        if tt[-1] - tt[0] > 2.5 * win_secs:
            continue                      # spans a dropout
        tau = tt - tt[0]
        c = np.polyfit(tau, yy, 2)
        res = float(np.sqrt(np.mean((yy - np.polyval(c, tau)) ** 2)))
        if res > max_res:
            continue
        total += 1
        if abs(2.0 * c[0] - GRAVITY) <= tol_frac * GRAVITY:
            good += 1
    return good, total


def acquire(sct, region, bg, args):
    """Watch every candidate, then lock the one that falls like the thief.

    This is the live version of the offline track-selection that rejected a
    patrolling minion 220 windows to 0. It runs once per run rather than
    per frame, so its cost does not matter.

    The thief drops out of the doorway the moment the level starts, so the
    ballistic evidence is available immediately.
    """
    y0, y1 = int(GAME_H * CROP_TOP), int(GAME_H * CROP_BOTTOM)
    cands = []
    t0 = time.perf_counter()
    frames = 0
    blob_counts = []
    snap = None

    while time.perf_counter() - t0 < args.acquire:
        if keyboard.is_pressed("f9") or keyboard.is_pressed("esc"):
            return None, []
        now = time.perf_counter()
        full = grab(sct, region)
        m = mask_of(full[y0:y1], bg[y0:y1], args)
        found = blobs_of(m, args)
        blob_counts.append(len(found))
        if snap is None and frames >= 3:
            snap = (full.copy(), m.copy())
        frames += 1
        used = set()
        for c in cands:
            best, bd = None, 1e9
            for k, b in enumerate(found):
                if k in used:
                    continue
                d = (b[0] - c.x[-1]) ** 2 + (b[1] - c.y[-1]) ** 2
                if d < bd:
                    best, bd = k, d
            gate = args.roi * (c.missed + 1)
            if best is not None and bd <= gate * gate:
                b = found[best]
                c.add(now, b[0], b[1] + y0, b[2])
                used.add(best)
            else:
                c.missed += 1
        for k, b in enumerate(found):
            if k not in used and len(cands) < args.max_cands:
                cands.append(Cand(now, b[0], b[1] + y0, b[2]))
        cands = [c for c in cands if c.missed <= args.lost_limit]

    rows = []
    for c in cands:
        if len(c.t) < args.acq_min:
            continue
        good, total = _ballistic(c, args.acq_win, args.gtol, args.fit_res)
        x, y = np.asarray(c.x), np.asarray(c.y)
        rows.append({
            "good": good, "total": total, "n": len(c.t),
            "extent": float(np.hypot(x.max() - x.min(), y.max() - y.min())),
            "pos": (c.x[-1], c.y[-1]), "start": (c.x[0], c.y[0]),
            "area": float(np.median(c.a)),
        })
    rows.sort(key=lambda r: (r["good"], r["extent"]), reverse=True)

    print(f"  acquisition: {frames} frames "
          f"({1000 * args.acquire / max(frames, 1):.0f}ms each), "
          f"{len(cands)} candidates, "
          f"{np.median(blob_counts) if blob_counts else 0:.0f} blobs/frame")
    for r in rows[:5]:
        print(f"    n={r['n']:4d} ballistic {r['good']:3d}/{r['total']:<4d} "
              f"extent {r['extent']:5.0f} area {r['area']:5.0f} "
              f"at ({r['pos'][0]:.0f},{r['pos'][1]:.0f})")

    weak = (not rows) or rows[0]["good"] < args.acq_good
    if snap is not None and (args.acq_debug or weak):
        img, m = snap
        for r in rows:
            cv2.circle(img, (int(r["pos"][0]), int(r["pos"][1])), 14,
                       (0, 200, 255), 2)
        cv2.imwrite("acq_frame.png", img)
        cv2.imwrite("acq_mask.png", m)
        print("  wrote acq_frame.png and acq_mask.png - if the thief is "
              "visible in the frame but absent from the mask, the problem "
              "is detection; if it is absent from BOTH, the level was not "
              "running yet.")

    if not rows:
        print("  ACQUISITION FAILED: nothing detected at all.")
        return None, rows
    if rows[0]["good"] < args.acq_good:
        print(f"  ACQUISITION FAILED: best candidate has only "
              f"{rows[0]['good']} ballistic windows (need {args.acq_good}). "
              f"Nothing on screen is falling like the thief.")
        return None, rows
    print(f"  locked on ({rows[0]['pos'][0]:.0f},{rows[0]['pos'][1]:.0f}) "
          f"- {rows[0]['good']}/{rows[0]['total']} ballistic")
    return rows[0]["pos"], rows


# ---------------------------------------------------------------- the run

def focus_window(hwnd):
    """Bring the emulator to the foreground before sending any input.

    Windows consumes the first click on an INACTIVE window as an
    activation click - the application never sees it. So an agent that
    politely sends one tap to start the level, from a console that
    currently has focus, starts nothing at all. That is exactly what
    happened on the first live attempt: the level never began, and
    acquisition found only the minion patrolling the pre-level screen.
    """
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"  could not focus the window ({e}). Click on LDPlayer once "
              f"yourself before pressing F8.")
    time.sleep(0.25)


def thumb(frame):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (80, 45), interpolation=cv2.INTER_AREA)


def scene_motion(a, b):
    """Mean abs difference on a tiny thumbnail. ~0.1ms, same metric
    kot_track.py uses to auto-start recording."""
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def wait_for_start(sct, region, args):
    """Block until the scene is actually moving.

    Fixed sleeps cannot tell "the level is running" from "the tap was
    swallowed". Motion can. This also adapts to however long the break-in
    transition takes, instead of guessing.
    """
    prev = thumb(grab(sct, region))
    t_end = time.perf_counter() + args.start_timeout
    hits = 0
    peak = 0.0
    while time.perf_counter() < t_end:
        if keyboard.is_pressed("f9") or keyboard.is_pressed("esc"):
            return False
        cur = thumb(grab(sct, region))
        m = scene_motion(prev, cur)
        peak = max(peak, m)
        prev = cur
        if m >= args.start_motion:
            hits += 1
            if hits >= 3:
                return True
        else:
            hits = 0
    print(f"  the scene never started moving (peak {peak:.2f}, need "
          f"{args.start_motion:.2f}).")
    print("  The level did not start. Usual cause: the start tap was "
          "swallowed as a window-activation click, or the game is not on "
          "the TAP TO START screen.")
    return False


def run(sct, region, bg, wps, args, hwnd):
    y0, y1 = int(GAME_H * CROP_TOP), int(GAME_H * CROP_BOTTOM)
    park_cursor(region, args.tap_x, args.tap_y)

    print(f"\n{'DRY RUN - no clicks' if args.dry_run else 'LIVE'}   "
          f"{len(wps)} waypoints   tol {args.tol}px   F9/ESC aborts")

    while keyboard.is_pressed("f8"):
        time.sleep(0.01)

    # ---- start gate -------------------------------------------------
    # v1 checked --abort-dist on its very first frame, while the thief was
    # still in the doorway and waypoint 1 was 2 seconds of gameplay away.
    # It aborted before the level began, every time.
    if args.start_tap and not args.dry_run:
        focus_window(hwnd)
        park_cursor(region, args.tap_x, args.tap_y)
        print("  tapping to start the level...")
        tap(args.hold)
    elif args.start_tap:
        print("  (dry run: NOT sending the start tap - start it yourself)")

    if not wait_for_start(sct, region, args):
        return []
    print("  level is running.")
    time.sleep(args.start_wait)

    tries = 0
    prev_still = []
    while True:
        print(f"  acquiring for {args.acquire:.1f}s...")
        pos, rows = acquire(sct, region, bg, args)
        if pos is not None:
            break

        # A blob that is white, large, ABSENT FROM THE BACKGROUND and
        # perfectly still is the thief. Nothing else can be all four: a
        # static decoration is in the median background and gets
        # subtracted, and the minion is never still. That is enough to
        # lock on, and waiting for ballistic proof costs the run.
        #
        # This matters more than it looks. The previous version kicked the
        # thief and then spent ANOTHER --acquire seconds watching before it
        # began matching waypoints - while the thief flew uncontrolled and
        # died. The demonstration fires waypoint 1 about 1.2s after the
        # launch tap, so an agent that spends 1.2s looking cannot ever
        # reach it. Lock, launch, and start matching in the same breath.
        still = [r for r in rows if r["extent"] < args.still_extent]
        if still and not args.dry_run:
            b = max(still, key=lambda r: r["area"])
            print(f"  LOCKED on the stationary {b['area']:.0f}px object at "
                  f"({b['pos'][0]:.0f},{b['pos'][1]:.0f}) - white, moving, "
                  f"not in the background, and waiting to be launched.")
            pos = b["pos"]

            # Does the waypoint file already describe this launch?
            #
            # If a demonstration was recorded from the TAP TO START screen,
            # its first waypoint IS the thief at rest in the doorway, and
            # the matcher will fire it. Kicking as well would launch the
            # thief with a tap of our own, leaving the matcher waiting for
            # a waypoint that has already been spent - the deadlock this
            # kick was invented to avoid, just one step later.
            #
            # If instead the demonstration began mid-flight, no waypoint
            # covers a resting thief, nothing will ever fire, and the kick
            # is the only way out.
            w0 = wps[0]
            d0 = float(np.hypot(pos[0] - w0["x"], pos[1] - w0["y"]))
            if d0 <= args.tol:
                print(f"  waypoint 1 is {d0:.0f}px away - the file already "
                      f"contains this launch, so NOT kicking. Matching now.")
            else:
                print(f"  waypoint 1 is {d0:.0f}px away, so no waypoint "
                      f"covers a resting thief. Launching it ourselves.")
                tap(args.hold)
            break

        # Nothing still either. Fall back to the causal test: whatever
        # moves right after a tap WE sent, from where it was sitting, is
        # the thief. The minion patrols whether we tap or not.
        responders = []
        for r in rows:
            if r["extent"] <= args.still_extent:
                continue
            for sp in prev_still:
                d = float(np.hypot(r["start"][0] - sp[0],
                                   r["start"][1] - sp[1]))
                if d <= args.kick_radius:
                    responders.append((r, d))
                    break
        if responders:
            r, d = max(responders, key=lambda z: z[0]["area"])
            print(f"  LOCKED by kick response: a {r['area']:.0f}px object "
                  f"that was stationary at ({r['start'][0]:.0f},"
                  f"{r['start'][1]:.0f}) moved {r['extent']:.0f}px right "
                  f"after our tap ({r['good']}/{r['total']} ballistic).")
            pos = r["pos"]
            break

        prev_still = [r["pos"] for r in still]
        tries += 1
        if args.dry_run:
            print("  (dry run: not sending a kick-off tap)")
            return []
        if tries > args.acq_retries:
            print("  Nothing to follow. Aborting before any taps are sent.")
            return []
        print(f"  kick-off tap, retry {tries}/{args.acq_retries}")
        tap(args.hold)
        time.sleep(args.kick_wait)

    last = pos
    last_t = time.perf_counter()
    hist = []
    lost = 0
    lost_since = None
    last_report = 0.0
    last_fire = None
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
            found = blobs_of(
                mask_of(grab(sct, box), bg[yy0:yy0 + 2 * args.roi,
                                           x0:x0 + 2 * args.roi], args), args)
            ox, oy = x0, yy0
            near = (cx - x0, cy - yy0)
        else:
            found = blobs_of(
                mask_of(grab(sct, region)[y0:y1], bg[y0:y1], args), args)
            ox, oy = 0, y0
            near = None

        hit = sprite_centroid(found, near, args.sprite)
        if hit is None:
            lost += 1
            if lost_since is None:
                lost_since = now
            # In SECONDS, not frames. The loop runs at ~6ms on the ROI path
            # and ~33ms on a full scan, so a frame count means anything
            # from half a second to three - and 90 frames aborted a run
            # after well under a second of a thief that was merely behind
            # scenery.
            if now - lost_since > args.lost_secs:
                print(f"ABORT: thief not seen for {now - lost_since:.1f}s "
                      f"({lost} frames). It may have died, or the level "
                      f"ended.")
                return fired
            continue
        lost_since = None

        x, y = hit[0] + ox, hit[1] + oy
        hist.append((now, x, y))
        if len(hist) > args.smooth:
            hist.pop(0)
        last, last_t, lost = (x, y), now, 0

        vx = vy = 0.0
        v_known = len(hist) >= max(2, args.smooth)
        if v_known:
            dt = hist[-1][0] - hist[0][0]
            if dt > 1e-4:
                vx = (hist[-1][1] - hist[0][1]) / dt
                vy = (hist[-1][2] - hist[0][2]) / dt
            else:
                v_known = False

        w = wps[idx]
        d = float(np.hypot(x - w["x"], y - w["y"]))

        # TIME AS A FLOOR, NOT A TRIGGER.
        #
        # Position-triggered replay deliberately ignores the clock, and
        # that was right: time-triggered replay drifted and never cleared a
        # level. But it throws away something the demonstration knows and
        # position cannot express - that the human WAITED.
        #
        # KoT hazards move on their own schedule. A patrolling minion or a
        # saw has a phase that has nothing to do with where the thief is,
        # so "the thief is at the right place" is not the same as "it is
        # safe to jump". On lvl28 the human waited for the patroller to
        # clear before tapping; the agent arrived at the same place sooner,
        # tapped immediately, and was hit.
        #
        # So the demonstrated gap becomes a MINIMUM. The agent still fires
        # on position - it just refuses to fire earlier than the human did
        # relative to the previous waypoint. Drift cannot accumulate,
        # because each floor is measured from the last actual fire, not
        # from a global clock.
        floor_ok = True
        if idx > 0 and args.gap_frac > 0 and last_fire is not None:
            demo_gap = w.get("t_ref", 0) - wps[idx - 1].get("t_ref", 0)
            need = max(0.0, demo_gap * args.gap_frac)
            floor_ok = (now - last_fire) >= need

        # Telemetry. Without this the agent is silent between waypoints, so
        # a run that takes 29 seconds to fire waypoint 1 looks identical to
        # one that fires it immediately - and there is no way to tell a
        # thief that died and respawned from one that sat still or wandered
        # off. One instrumented run answers that; guessing does not.
        if now - last_report > args.report:
            sp = float(np.hypot(vx, vy))
            vtxt = (f"v=({vx:+5.0f},{vy:+5.0f}) |v|={sp:4.0f}" if v_known
                    else f"v=  (measuring)  |v|=   ?")
            print(f"    t={now - t0:5.1f}s ({x:4.0f},{y:4.0f}) "
                  f"{vtxt}  "
                  f"wp{idx + 1} at ({w['x']:.0f},{w['y']:.0f}) "
                  f"d={d:4.0f} vd={float(np.hypot(vx - w['vx'], vy - w['vy'])):4.0f}"
                  f"{'' if floor_ok else '  [waiting out demo gap]'}")
            last_report = now

        # Velocity gate: two waypoints can share a position but differ in
        # direction (going up vs coming down through the same point).
        #
        # v_known matters as much as vtol. hist is cleared after every tap,
        # so for the next few frames the measured velocity is (0,0) - and
        # comparing (0,0) against a waypoint gives vd = the waypoint's own
        # speed, which for this file is 257-386 and slips under any
        # sensible tolerance. The gate passed every time on a velocity it
        # had not actually measured. Firing on an unknown velocity is
        # firing blind, so it now waits the few ms for --smooth samples.
        vd = float(np.hypot(vx - w["vx"], vy - w["vy"]))
        v_ok = v_known and (args.vtol <= 0 or vd <= args.vtol)

        if d <= args.tol and v_ok and floor_ok:
            if not args.dry_run:
                tap(args.hold)
            el = now - t0
            print(f"  wp {idx + 1}/{len(wps)} FIRED at ({x:.0f},{y:.0f}) "
                  f"d={d:.0f}px vd={vd:.0f} t={el:.2f}s")
            fired.append((idx, el, x, y))
            last_fire = now
            idx += 1
            # Refractory: without it the next frames still satisfy the same
            # waypoint and the agent double-taps.
            rt = time.perf_counter() + args.refractory
            while time.perf_counter() < rt:
                pass
            hist.clear()
            continue

        # Divergence check, suspended during --grace. Getting from the
        # doorway to waypoint 1 is legitimately a few hundred px of travel
        # that no waypoint describes, so measuring against waypoint 1 from
        # the first frame guarantees a false abort.
        if now - t0 > args.grace and d > args.abort_dist:
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
                    help="velocity match tolerance px/s; 0 disables. Worth "
                         "setting (~400): the first live attempt fired "
                         "waypoint 1 on a position match whose velocity was "
                         "off by 1025 px/s")
    ap.add_argument("--abort-dist", type=float, default=500,
                    dest="abort_dist",
                    help="give up if this far from the expected waypoint")
    ap.add_argument("--grace", type=float, default=4.0,
                    help="seconds after acquisition during which the "
                         "divergence check is suspended")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--refractory", type=float, default=0.15)
    ap.add_argument("--smooth", type=int, default=4,
                    help="frames over which velocity is averaged")
    ap.add_argument("--hold", type=float, default=0.05)
    ap.add_argument("--tap-x", type=int, default=640, dest="tap_x")
    ap.add_argument("--tap-y", type=int, default=360, dest="tap_y")
    # start gate
    ap.add_argument("--no-start-tap", action="store_false", dest="start_tap",
                    help="do NOT send the level-start tap; start it "
                         "yourself after pressing F8")
    ap.add_argument("--start-wait", type=float, default=0.4,
                    dest="start_wait",
                    help="seconds to wait after motion begins before "
                         "acquiring, so the break-in transition finishes")
    ap.add_argument("--start-motion", type=float, default=0.08,
                    dest="start_motion",
                    help="scene motion that counts as 'the level started'. "
                         "Same metric as kot_track.py --motion-scan")
    ap.add_argument("--start-timeout", type=float, default=6.0,
                    dest="start_timeout",
                    help="give up if the scene never starts moving")
    ap.add_argument("--acq-retries", type=int, default=2,
                    dest="acq_retries",
                    help="how many kick-off taps to try when nothing on "
                         "screen is falling. KoT levels start with the "
                         "thief stationary in the doorway, and the tap that "
                         "launches it produces no waypoint, so the agent "
                         "has to supply it")
    ap.add_argument("--kick-wait", type=float, default=0.25,
                    dest="kick_wait",
                    help="seconds after a kick-off tap before re-acquiring")
    ap.add_argument("--kick-radius", type=float, default=140,
                    dest="kick_radius",
                    help="px within which a newly-moving candidate must "
                         "have STARTED, relative to something that was "
                         "stationary last round, to count as having "
                         "responded to our kick-off tap")
    ap.add_argument("--still-extent", type=float, default=10,
                    dest="still_extent",
                    help="px of movement below which a candidate counts as "
                         "stationary, and therefore as something waiting to "
                         "be launched rather than decor")
    ap.add_argument("--acq-debug", action="store_true", dest="acq_debug",
                    help="always write acq_frame.png / acq_mask.png "
                         "(written automatically when acquisition is weak)")
    # acquisition
    ap.add_argument("--acquire", type=float, default=1.2,
                    help="seconds spent identifying the thief before any "
                         "waypoint matching begins")
    ap.add_argument("--acq-win", type=float, default=0.20, dest="acq_win",
                    help="parabola fit window in SECONDS. Not frames: this "
                         "loop runs at ~6ms and fitted-acceleration noise "
                         "scales as 1/T^2")
    ap.add_argument("--acq-min", type=int, default=15, dest="acq_min",
                    help="samples a candidate needs before it is judged")
    ap.add_argument("--acq-good", type=int, default=3, dest="acq_good",
                    help="ballistic windows required to lock on. If nothing "
                         "reaches this, the agent refuses to run rather "
                         "than following a minion")
    ap.add_argument("--gtol", type=float, default=0.35,
                    help="fraction of g a window may deviate and still "
                         "count as ballistic")
    ap.add_argument("--fit-res", type=float, default=4.0, dest="fit_res",
                    help="max RMS residual (px) for a fit to be believed")
    ap.add_argument("--max-cands", type=int, default=25, dest="max_cands")
    ap.add_argument("--sprite", type=float, default=45,
                    help="px radius over which blobs are merged into one "
                         "sprite, so a split mask does not jitter the "
                         "reported position between body parts")
    # detection
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
    ap.add_argument("--gap-frac", type=float, default=0.85, dest="gap_frac",
                    help="refuse to fire waypoint i until this fraction of "
                         "the DEMONSTRATED gap since waypoint i-1 has "
                         "elapsed. The human waits for moving hazards; "
                         "position alone cannot express that. 0 disables")
    ap.add_argument("--report", type=float, default=0.5,
                    help="seconds between telemetry lines during a run. "
                         "0.5 is enough to see whether the thief is moving, "
                         "stuck, or respawning, without flooding")
    ap.add_argument("--lost-secs", type=float, default=1.5, dest="lost_secs",
                    help="abort if the thief is not seen for this long. In "
                         "seconds because the loop runs at ~6ms on the ROI "
                         "path and ~33ms on a full scan, so a frame count "
                         "means wildly different amounts of real time")
    args = ap.parse_args()

    with open(args.waypoints) as f:
        data = json.load(f)
    wps = data["waypoints"]
    print(f"{len(wps)} waypoints from {data.get('source', '?')}")
    unseen = [i for i, w in enumerate(wps, 1) if w.get("observed") is False]
    if unseen:
        print(f"  WARNING: waypoint(s) {unseen} sit on interpolated "
              f"positions - the thief was never actually seen there.")

    fix_dpi()
    hwnd, title = find_window(WINDOW_TITLE)
    if not hwnd:
        print(f"No window matching '{WINDOW_TITLE}'.")
        return
    region = game_region(hwnd)
    print(f"Found: {title}")

    bg = None
    print("\nF7 = learn background (pre-level screen)   F8 = arm/run   "
          "F9 = quit\n")

    try:
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
                    run(sct, region, bg, wps, args, hwnd)
                    time.sleep(0.6)
                time.sleep(0.005)
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()