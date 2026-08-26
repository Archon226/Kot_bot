"""
kot_skipper.py - read the gold on a raid target, skip until it is worth it.

WHY THIS IS A DIFFERENT KIND OF PROBLEM TO kot_agent.py

The waypoint agent had to act inside a ~30ms window on a moving target
whose exact pre-tap state it could not reproduce. This does not:

    timing precision   none - the screen is static
    state to reproduce none
    failure cost       tap skip again
    feedback           the number is on screen

So it is worth building carefully once rather than tuning run by run.

HOW IT READS THE NUMBER

Not OCR. The game uses a fixed bitmap font, so the same digit is the same
pixels every time, and template matching is both exact and fast. Tesseract
on a 9x22px glyph would be neither.

Anchoring matters more than the reading. The gold plate is drawn on the
defender's treasure chest, and the defender chooses where that chest sits,
so a fixed crop cannot work. Instead the coin icon on the plate is matched
anywhere in the play area and the digits are read from immediately right
of it. The HUD strip and the button bar are excluded because they contain
coin icons too - your own gold at the top, the skip cost at the bottom.

RESOLUTION INDEPENDENCE

assets/coin.png, assets/skip.png, and every fixed-pixel measurement below
(the HUD gold box, the three gem-slot offsets, the digit-strip search
width, glyph-area thresholds...) were measured against real 1280x720
screenshots. Older versions of this script required the BlueStacks/
LDPlayer client area to be at least that size and refused to run
otherwise.

This version captures whatever the emulator window's client area
actually is - any size, any aspect ratio - and rescales the coin/skip
templates and every one of those pixel measurements to match, based on
the ratio between the real capture size and the 1280x720 reference. So
the same assets/ and digits/ folders work whether BlueStacks is a small
window or a maximised 1920x1080 screen.

BOOTSTRAPPING THE DIGITS

digits/ ships with 0,1,3,5,7 extracted from a real screenshot. The other
five appear as you run. When an unknown glyph turns up it is saved to
digits/unlabeled/ and the read is reported as failed rather than guessed -
a misread number is worse than no number, because it makes the tool skip
a base worth keeping. Rename each file to its digit and it is learnt.

    digits/unlabeled/g0003.png  ->  digits/8.png

Usage:
    python kot_skipper.py --calibrate          # check what it sees, no taps
    python kot_skipper.py --gold 50000 --dry-run
    python kot_skipper.py --gold 50000 --max-skips 40

Keys:
    F8  start / stop
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

WINDOW_TITLE = "LDPlayer"

# Reference resolution. coin.png/skip.png and every fixed-pixel constant
# below (HUD_GOLD, GEM_SLOTS, and the --strip/--gem-radius/--glyph-area
# style CLI defaults) were measured against screenshots at this size.
# main() computes how far the REAL capture differs from this and scales
# everything accordingly - this is not an enforced minimum any more.
GAME_W, GAME_H = 1280, 720

# Play area. Excludes the HUD strip (your own gold, gems, keys - all with
# coin icons) and the button bar (the skip button's own cost icon).
# Expressed as fractions of the frame height, so these need no scaling.
PLAY_TOP, PLAY_BOTTOM = 0.10, 0.88

DIGIT_SIZE = (12, 24)


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


def game_region(hwnd, args=None):
    """The emulator's game surface, captured at whatever resolution the
    window ACTUALLY is right now.

    Older versions required the client area to be at least 1280x720 and
    raised SystemExit otherwise. This version captures whatever is
    there - any size - and main() rescales the coin/skip templates and
    every fixed-pixel measurement (HUD box, gem slot offsets, digit-strip
    sizes, etc) from the 1280x720 reference they were measured at, to
    match. So the same assets/ and digits/ work at any window size.

    --region-x / --region-y still exist because some emulator layouts put
    an ADVERT PANEL down the side. Use 0 if there is none.
    """
    l, t, r, b = win32gui.GetClientRect(hwnd)
    l, t = win32gui.ClientToScreen(hwnd, (l, t))
    r, b = win32gui.ClientToScreen(hwnd, (r, b))
    cw, ch = r - l, b - t
    ox = getattr(args, "region_x", 0) or 0
    oy = getattr(args, "region_y", 0) or 0

    if ox < 0 or oy < 0:
        raise SystemExit(f"Invalid region offset ({ox},{oy}). Offsets "
                         f"cannot be negative.")

    cap_w, cap_h = cw - ox, ch - oy

    if cap_w <= 0 or cap_h <= 0:
        raise SystemExit(f"Region offset ({ox},{oy}) is outside the "
                         f"{cw}x{ch} client area.")

    print(f"Client {cw}x{ch}; capturing {cap_w}x{cap_h} at ({ox},{oy})")

    if (cap_w, cap_h) != (GAME_W, GAME_H):
        print(f"  NOTE: differs from the {GAME_W}x{GAME_H} reference the "
              f"assets were measured at - templates/geometry will be "
              f"auto-scaled by ({cap_w / GAME_W:.3f}x, "
              f"{cap_h / GAME_H:.3f}x).")

    return {"left": l + ox, "top": t + oy,
            "width": cap_w, "height": cap_h}


def focus_window(hwnd):
    """Windows eats the first click on an inactive window as an activation
    click, so an unfocused emulator silently ignores the first tap."""
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


def wait_settled(sct, region, quiet, timeout):
    """Wait until the screen stops changing.

    Reading mid-transition gives a half-drawn number, and a half-drawn
    number that happens to parse is the worst possible outcome - it would
    skip a base worth keeping and you would never know.
    """
    prev = thumb(grab(sct, region))
    still_since = None
    t_end = time.perf_counter() + timeout
    while time.perf_counter() < t_end:
        time.sleep(0.03)
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


# ------------------------------------------------------- resolution scaling

def scale_image(img, sx, sy):
    """Resize an image by independent x/y scale factors. Returns it
    unchanged if the factors are effectively 1.0."""
    if img is None:
        return None
    h, w = img.shape[:2]
    new_w = max(1, int(round(w * sx)))
    new_h = max(1, int(round(h * sy)))
    if new_w == w and new_h == h:
        return img
    interp = cv2.INTER_AREA if (new_w < w or new_h < h) else cv2.INTER_LINEAR
    return cv2.resize(img, (new_w, new_h), interpolation=interp)


def raw_score(frame, tmpl, y0=None, y1=None):
    """Best match score for a template against the frame, IGNORING any
    threshold - used purely for --calibrate diagnostics so you can tell a
    near-miss (score close to threshold) from a total miss (score near
    zero). Returns None if the template is too big for the search area."""
    if tmpl is None:
        return None
    sub = frame if y0 is None else frame[y0:y1]
    if sub.size == 0:
        return None
    if tmpl.shape[0] > sub.shape[0] or tmpl.shape[1] > sub.shape[1]:
        return None
    res = cv2.matchTemplate(sub, tmpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, _ = cv2.minMaxLoc(res)
    return float(score)


def load_templates(folder):
    coin = cv2.imread(os.path.join(folder, "coin.png"))
    skip = cv2.imread(os.path.join(folder, "skip.png"))
    digits = {}
    for d in "0123456789":
        p = os.path.join(folder, f"{d}.png")
        if os.path.isfile(p):
            digits[d] = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    return coin, skip, digits


def find_anchor(frame, tmpl, thresh, y0=None, y1=None):
    """Locate a template, optionally restricted to a horizontal band."""
    if tmpl is None:
        return None
    sub = frame if y0 is None else frame[y0:y1]
    res = cv2.matchTemplate(sub, tmpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < thresh:
        return None
    return loc[0], loc[1] + (y0 or 0), score


def glyphs_right_of(frame, x, y, h, width, args, sat=None):
    """White glyphs immediately right of the coin, on one baseline.

    The strip starts --strip-back px BEFORE the coin template's right edge.
    Chests differ, and on one of them the first digit began inside that
    edge: segmentation returned 5 glyphs for a 6-digit number, silently
    losing the leading 1. The coin itself is saturated yellow and cannot
    pass the white mask, so reaching back over it costs nothing.

    The baseline rule is not decoration. Segmenting the real screenshot
    returned six components for "51037" - the sixth was chest scenery
    13px further right and 6px lower. Keeping only the contiguous run that
    shares a baseline with the first glyph discards it.

    Note: args.strip, args.strip_back and args.glyph_area/glyph_gap are
    all in CURRENT-resolution pixels by the time this runs - main()
    rescales them from their 1280x720 reference values before the loop
    starts, so this function itself needs no resolution awareness.
    """
    strip = frame[max(0, y - 4):y + h + 4, max(0, x - args.strip_back):
                  x - args.strip_back + width]
    if strip.size == 0:
        return [], None
    hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, np.array([0, 0, args.glyph_v], np.uint8),
                    np.array([179, args.glyph_s if sat is None else sat,
                              255], np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    cand = sorted([(stats[i, 0], stats[i, 1], stats[i, 2], stats[i, 3])
                   for i in range(1, n)
                   if stats[i, 4] >= args.glyph_area and stats[i, 3] >= 10])
    if not cand:
        return [], m
    base_y, base_h = cand[0][1], cand[0][3]
    keep, prev_end = [], None
    for gx, gy, gw, gh in cand:
        if abs(gy - base_y) > 3 or abs(gh - base_h) > 4:
            continue
        if prev_end is not None and gx - prev_end > args.glyph_gap:
            break                      # a gap this wide ends the number
        keep.append((gx, gy, gw, gh))
        prev_end = gx + gw
    # Adjacent digits can touch and merge into one component - on a real
    # base "32440" segmented as widths 8, 7, 18, 8, the two 4s welded
    # together and the pair was then unrecognisable. A glyph much wider
    # than its neighbours is not a glyph; split it into equal parts.
    if keep and args.split_wide > 0:
        med = float(np.median([w for _, _, w, _ in keep]))
        out = []
        for gx, gy, gw, gh in keep:
            parts = int(round(gw / med)) if med > 0 else 1
            if parts >= 2 and gw >= med * args.split_wide:
                step = gw / parts
                for k in range(parts):
                    out.append((int(gx + k * step), gy, int(step), gh))
            else:
                out.append((gx, gy, gw, gh))
        keep = out

    return [(m[gy:gy + gh, gx:gx + gw], (gx, gy, gw, gh)) for gx, gy, gw, gh
            in keep], m


def classify(glyph, digits, thresh, margin):
    """Best template match, but only if it wins CLEARLY.

    The threshold alone is not enough. A digit with no template still
    correlates with something: on a real base the '6' we had never seen
    scored 0.576 for '0' and 0.564 for '5'. Both cleared a 0.55 threshold,
    '0' won by 0.012, and 167100 was read as 7100 - which would have
    skipped a 167k base as though it were worth 7k.

    Genuine digits win by a mile. In that same read the five known glyphs
    won by 0.33 to 0.59. So requiring a margin over the runner-up
    separates "I recognise this" from "this is the least bad of five
    wrong answers", which a threshold cannot do.

    This step is already resolution-independent: the captured glyph is
    resized to the fixed DIGIT_SIZE before comparison, regardless of what
    resolution it was captured at, so digit recognition needs no scaling.
    """
    g = cv2.resize(glyph, DIGIT_SIZE, interpolation=cv2.INTER_AREA)
    scores = sorted(((float(cv2.matchTemplate(g, t,
                                              cv2.TM_CCOEFF_NORMED).max()), d)
                     for d, t in digits.items()), reverse=True)
    if not scores:
        return None, 0.0
    best, second = scores[0], (scores[1] if len(scores) > 1 else (0.0, None))
    if best[0] < thresh or (best[0] - second[0]) < margin:
        return None, best[0]
    return best[1], best[0]


# Your own gold, top-left, measured at the 1280x720 reference resolution.
# Same bitmap font as the chest plate, verified against real frames: 1186
# and 253 both read exactly. main() rescales this into args.hud_gold to
# match the actual capture resolution before use.
HUD_GOLD = (150, 0, 330, 50)      # x0, y0, x1, y1 in reference-res pixels


def read_balance(frame, digits, args):
    """Your gold. Returns None if it cannot be read - never a guess."""
    x0, y0, x1, y1 = getattr(args, "hud_gold", HUD_GOLD)
    hud = frame[y0:y1, x0:x1]
    if hud.size == 0:
        return None
    hsv = cv2.cvtColor(hud, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, np.array([0, 0, args.glyph_v], np.uint8),
                    np.array([179, args.glyph_s, 255], np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(m, 8)
    gl = sorted([(st[i, 0], st[i, 1], st[i, 2], st[i, 3]) for i in range(1, n)
                 if st[i, 4] >= args.glyph_area and st[i, 3] >= 10])
    if not gl:
        return None
    out = []
    for x, y, w, h in gl:
        d, _ = classify(m[y:y + h, x:x + w], digits, args.digit_thresh,
                        args.digit_margin)
        if d is None:
            return None
        out.append(d)
    try:
        return int("".join(out))
    except ValueError:
        return None


# The three gem slots sit on the chest lid at a FIXED offset from the coin
# icon (measured at the 1280x720 reference resolution), so the anchor
# that finds the gold also finds the gems - no second template needed.
# Measured across four real chests: dx -6/+27/+59, dy -46, about 18px
# each. main() rescales these into args.gem_slots before use.
#
# Slot CENTRES relative to the coin. The MIDDLE slot sits about 8px
# higher than the outer two - the chest lid has three lobes and the
# centre one is raised. Measured across four chests.
GEM_SLOTS = [(4, -36), (37, -44), (69, -36)]

# OpenCV hue is 0-179. Red wraps, hence two bands.
GEM_BANDS = [("red", 0, 10), ("red", 168, 179), ("orange", 11, 22),
             ("yellow", 23, 33), ("green", 34, 85), ("blue", 86, 125),
             ("purple", 126, 167)]


def read_gems(frame, cx, cy, args):
    """Each slot as (colour, rich) where rich means a GOLD RIM.

    Two samples per slot, and the split matters: the gem sits in the
    centre, the rim is an annulus around it. Sampled together, an ORANGE
    GEM and a GOLD RIM are the same hue and indistinguishable.

    What actually separates a gold rim from an orange gem is how MUCH
    saturated gold there is in the annulus. Measured on real chests:

        gold rim   rim 245-298px, sat 195-222, hue 16-17
        ordinary   rim  43-116px, sat  97-144

    The orange-gem chest that defeats a pure hue test scores 113 rim
    pixels against 245+ for a genuine gold rim, so the count carries the
    decision and hue and saturation act as guards.

    args.gem_slots / args.gem_radius / args.rim_min / args.core_min are
    all already scaled to the current capture resolution by main().
    """
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    H, W = frame.shape[:2]
    out = []
    r = args.gem_radius
    slots = getattr(args, "gem_slots", GEM_SLOTS)
    for dx, dy in slots:
        gx, gy = cx + dx, cy + dy
        if not (r < gx < W - r and r < gy < H - r):
            out.append(("?", False))
            continue
        y0, y1 = int(gy - r * 1.2), int(gy + r * 1.2)
        x0, x1 = int(gx - r * 1.2), int(gx + r * 1.2)
        sub = hsv[y0:y1, x0:x1]
        yy, xx = np.mgrid[y0:y1, x0:x1]
        d = np.hypot(xx - gx, yy - gy)

        def sample(mask):
            px = sub[mask]
            px = px[(px[:, 1] > args.gem_sat) & (px[:, 2] > args.gem_val)]
            if len(px) < 8:
                return None, None, 0
            return int(np.median(px[:, 0])), int(np.median(px[:, 1])), len(px)

        rh, rs, rn = sample((d > r * 0.72) & (d <= r * 1.15))
        rich = (rh is not None and args.rim_lo <= rh <= args.rim_hi
                and rs >= args.rim_sat and rn >= args.rim_min)

        # On the highest tiers the gold setting spreads INTO the middle of
        # the slot, so a plain colour sample returns gold and the gem gets
        # called yellow. Measured on a chest of three purple gems: the
        # middle one sampled hue 28 (gold) with everything included and
        # hue 149 (purple) with gold excluded, while its neighbours read
        # 146 either way.
        #
        # Only exclude gold when the rim says this is a gold setting -
        # otherwise a genuinely orange or yellow GEM would be thrown away.
        ch, _, cn = sample(d <= r * 0.55)
        if rich:
            core = (d <= r * 0.55)
            px = sub[core]
            px = px[(px[:, 1] > args.gem_sat) & (px[:, 2] > args.gem_val)]
            ng = px[~((px[:, 0] >= args.rim_lo) & (px[:, 0] <= args.rim_hi))]
            if len(ng) >= args.core_min:
                ch, cn = int(np.median(ng[:, 0])), len(ng)

        if ch is None or cn < args.gem_fill * (r * r):
            out.append(("empty", False))
            continue
        colour = next((nm for nm, a, b in GEM_BANDS if a <= ch <= b), "?")
        out.append((colour, rich))
    return out


def read_gold(frame, coin, digits, args, save_unknown=None):
    """Returns (value, note). value is None when the read is not trusted."""
    h = frame.shape[0]
    y0, y1 = int(h * PLAY_TOP), int(h * PLAY_BOTTOM)
    hit = find_anchor(frame, coin, args.coin_thresh, y0, y1)
    if hit is None:
        return None, "no coin icon found in the play area"
    cx, cy, score = hit
    ch, cw = coin.shape[:2]

    # Decoration crosses the plate on some dungeons. A winter base had
    # spider web over the number: the web is white enough to pass the
    # mask, and it welded all five digits into one 70x34 blob. The web is
    # translucent, so it takes colour from the plate behind it and a
    # tighter saturation ceiling rejects it - but that same ceiling loses
    # digits on other chests.
    #
    # So try the normal setting first and only tighten if the read FAILED.
    # A retry can turn a failure into a success; it can never turn one
    # successful read into a different one.
    sats = [args.glyph_s] + [int(v) for v in args.glyph_s_alt.split(",")
                             if v.strip()]
    last = "no digits beside the coin"
    first_note = None
    for attempt, sat in enumerate(sats):
        # Save unknown glyphs from the FIRST attempt only. The later
        # attempts use a tighter saturation ceiling to cut through
        # decoration, which can also shave pixels off a real digit -
        # labelling one of those would poison the template set
        # permanently.
        val, note = _read_with(frame, cx, cy, cw, ch, digits, args, sat,
                               save_unknown if attempt == 0 else None)
        if val is not None:
            return val, note + ("" if attempt == 0
                                else f" [needed glyph-s {sat}]")
        if attempt == 0:
            first_note = note
        last = note
    return None, first_note or last


def _read_with(frame, cx, cy, cw, ch, digits, args, sat, save_unknown):
    gl, _ = glyphs_right_of(frame, cx + cw, cy, ch, args.strip, args, sat)
    if not gl:
        return None, f"coin found at ({cx},{cy}) but no digits beside it"

    out, unknown = [], []
    for i, (g, box) in enumerate(gl):
        d, s = classify(g, digits, args.digit_thresh, args.digit_margin)
        if d is None:
            unknown.append((i, g))
            out.append("?")
        else:
            out.append(d)
    text = "".join(out)

    if unknown:
        saved = []
        if save_unknown:
            os.makedirs(save_unknown, exist_ok=True)
            for i, g in unknown:
                # Name it after the pattern and the position, so a number
                # with two unknowns is still unambiguous to label: with
                # '1?71?0' you can see at a glance that pos1 and pos4 are
                # the two files, and read the real digits off the screen.
                p = os.path.join(save_unknown, f"{text}_pos{i}.png")
                cv2.imwrite(p, cv2.resize(g, DIGIT_SIZE,
                                          interpolation=cv2.INTER_AREA))
                saved.append(os.path.basename(p))
        return None, (f"unknown digit(s) in '{text}' -> {save_unknown}/"
                      f"{' '.join(saved)} ; read the real number off the "
                      f"screen and rename each to its digit, e.g. "
                      f"{args.digits}/8.png")
    try:
        return int(text), f"read '{text}'"
    except ValueError:
        return None, f"could not parse '{text}'"


# -------------------------------------------------------------------- run

def calibrate(sct, region, coin, skip, digits, args):
    frame = grab(sct, region)
    cv2.imwrite("skipper_frame.png", frame)
    print(f"digit templates loaded: "
          f"{''.join(sorted(digits)) if digits else 'NONE'}")
    missing = [d for d in "0123456789" if d not in digits]
    if missing:
        print(f"  missing: {''.join(missing)} - these will read as '?' "
              f"until they appear and you label them")

    val, note = read_gold(frame, coin, digits, args, "digits/unlabeled")
    print(f"gold: {val}   ({note})")

    h = frame.shape[0]
    y0, y1 = int(h * PLAY_TOP), int(h * PLAY_BOTTOM)
    hit = find_anchor(frame, coin, args.coin_thresh, y0, y1)
    vis = frame.copy()
    cv2.rectangle(vis, (0, y0), (frame.shape[1], y1), (60, 60, 60), 1)

    # Report the coin match either way: FOUND with its score, or NOT
    # FOUND with the best score it came up with anyway. A near-miss
    # (score just under threshold) means "lower --coin-thresh a touch or
    # recalibrate the asset"; a score near zero means "wrong region,
    # wrong screen, or the wrong emulator window is focused".
    best_coin = raw_score(frame, coin, y0, y1)
    if hit:
        cx, cy, score = hit
        ch, cw = coin.shape[:2]
        cv2.rectangle(vis, (cx, cy), (cx + cw, cy + ch), (0, 255, 0), 2)
        cv2.rectangle(vis, (cx + cw, cy - 4),
                      (cx + cw + args.strip, cy + ch + 4), (0, 200, 255), 1)
        print(f"coin icon: FOUND at ({cx},{cy})  score {score:.3f} "
              f"(need {args.coin_thresh})")
    else:
        print(f"coin icon: NOT found - best score "
              f"{best_coin:.3f} " if best_coin is not None else "N/A "
              f"(need {args.coin_thresh}) - is a raid/base screen showing?")

    sh = find_anchor(frame, skip, args.skip_thresh)
    best_skip = raw_score(frame, skip)
    if sh:
        sx, sy, ss = sh
        th, tw = skip.shape[:2]
        cv2.rectangle(vis, (sx, sy), (sx + tw, sy + th), (255, 0, 255), 2)
        print(f"skip button: FOUND at ({sx + tw // 2},{sy + th // 2}) "
              f"score {ss:.3f} (need {args.skip_thresh})")
    else:
        print(f"skip button: NOT found - best score "
              f"{best_skip:.3f} " if best_skip is not None else "N/A "
              f"(need {args.skip_thresh}) - is the raid screen showing?")

    cv2.imwrite("skipper_calib.png", vis)
    print("\nwrote skipper_frame.png and skipper_calib.png")
    print("  green = coin anchor   amber = digit strip   magenta = skip")
    if (args.scale_x, args.scale_y) != (1.0, 1.0):
        print(f"  (assets/geometry auto-scaled by "
              f"{args.scale_x:.3f}x / {args.scale_y:.3f}y for this "
              f"{region['width']}x{region['height']} capture)")


def run(sct, region, hwnd, coin, skip, digits, args):
    focus_window(hwnd)
    print(f"\n{'DRY RUN - no taps' if args.dry_run else 'LIVE'}   "
          f"target gold >= {args.gold}   max {args.max_skips} skips   "
          f"F9 aborts\n")
    while keyboard.is_pressed("f8"):
        time.sleep(0.01)

    skips = 0
    fails = 0
    seen = []
    last_gems = None
    while skips < args.max_skips:
        if keyboard.is_pressed("f9") or keyboard.is_pressed("esc"):
            print("Stopped by key.")
            break

        if not wait_settled(sct, region, args.quiet, args.settle_timeout):
            # Do NOT read anyway. An unsettled screen is a loading or
            # animating one, and a half-drawn number that happens to parse
            # is the worst outcome available: on a slow connection this
            # read '103' and then '4', and skipped on both. A misread that
            # is too HIGH merely stops and you notice. One that is too LOW
            # spends 300 gold and destroys a base you might have wanted,
            # and nothing tells you it happened.
            fails += 1
            print(f"  screen never settled - not reading it "
                  f"({fails}/{args.max_fails})")
            if fails >= args.max_fails:
                print("  giving up rather than skipping blind.")
                break
            time.sleep(args.delay)
            continue
        # The "TAP TO BREAK IN" lock and caption are drawn near the centre
        # of the screen and PULSE. When a defender puts the chest there,
        # the overlay covers the coin anchor and often the leading digits
        # too - one base showed only '0471' of a longer number. Reading
        # that would be worse than failing, so we retry across the pulse
        # instead and let it fail if the plate stays covered.
        val = None
        for attempt in range(max(1, args.retry_reads)):
            frame = grab(sct, region)
            val, note = read_gold(frame, coin, digits, args,
                                  "digits/unlabeled")
            if val is not None:
                break
            if attempt + 1 < max(1, args.retry_reads):
                time.sleep(args.retry_wait)

        if val is not None and val < args.min_gold:
            # Same reasoning: a plausible-looking tiny number is far more
            # likely to be a partial render than a real base.
            note = (f"read {val}, below --min-gold {args.min_gold} - "
                    f"treating as a failed read, not a poor base")
            val = None

        if val is None:
            fails += 1
            print(f"  READ FAILED: {note}")
            cv2.imwrite(f"skipper_fail_{fails}.png", frame)
            if fails >= args.max_fails:
                print(f"  {fails} consecutive failures - stopping. That is "
                      f"a systematic problem, not one awkward base. Check "
                      f"skipper_fail_*.png.")
                break
            if args.on_fail == "stop":
                print("  stopping on an unreadable base (--on-fail skip to "
                      "skip past these instead; costs a skip each time).")
                break
            print("  --on-fail skip: skipping it unread.")
            sh = find_anchor(frame, skip, args.skip_thresh)
            if sh is None:
                best = raw_score(frame, skip)
                print(f"  skip button not found "
                      f"(best score {best:.3f})" if best is not None
                      else "  skip button not found." + " stopping.")
                break
            if not args.dry_run:
                sx, sy, _ = sh
                th, tw = skip.shape[:2]
                click_at(region, sx + tw // 2, sy + th // 2, args.hold)
            skips += 1
            time.sleep(args.delay)
            continue
        fails = 0

        gems = []
        if args.want or args.show_gems or args.min_rich:
            hit = find_anchor(frame, coin, args.coin_thresh,
                              int(frame.shape[0] * PLAY_TOP),
                              int(frame.shape[0] * PLAY_BOTTOM))
            if hit:
                gems = read_gems(frame, hit[0], hit[1], args)

        # The same base read twice is not two bases. In a dry run the
        # screen-change wait can be tripped by an idle animation before
        # you have actually skipped, and the log then shows one base five
        # times as though five were checked.
        if seen and val == seen[-1] and last_gems == gems:
            time.sleep(args.delay)
            continue
        seen.append(val)
        last_gems = gems

        # read_gems returns (colour, rich) per slot, where rich means the
        # gem sits in a GOLD RIM - the high-value ones.
        want = [c for c, _ in gems if c in args.want.split(",")] \
            if args.want else []
        rich = [c for c, rr in gems if rr]
        gem_ok = bool(args.want) and len(want) >= args.min_gems
        rich_ok = args.min_rich > 0 and len(rich) >= args.min_rich
        gold_ok = val >= args.gold

        if args.log:
            new = not os.path.isfile(args.log)
            with open(args.log, "a") as f:
                if new:
                    f.write("time,gold,gem1,gem2,gem3,rich\n")
                g = [c for c, _ in gems] + ["", "", ""]
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{val},"
                        f"{g[0]},{g[1]},{g[2]},{len(rich)}\n")

        if gems:
            shown = ", ".join(f"{c}{'*' if rr else ''}" for c, rr in gems)
            print(f"    gems: {shown}"
                  + (f"   ({len(rich)} gold-rimmed)" if rich else "")
                  + (f"   ({len(want)} wanted)" if want else ""))

        if gold_ok or gem_ok or rich_ok:
            why = []
            if gold_ok:
                why.append(f"{val:,} gold")
            if gem_ok:
                why.append(f"{len(want)}x {'/'.join(sorted(set(want)))}")
            if rich_ok:
                why.append(f"{len(rich)} GOLD-RIMMED "
                           f"({'/'.join(sorted(set(rich)))})")
            print(f"\n  FOUND {' and '.join(why)} after {skips} skips. "
                  f"Stopping.")
            print(f"  (gold seen: min {min(seen):,} median "
                  f"{int(np.median(seen)):,} max {max(seen):,})")
            return
        print(f"  {val:,} < {args.gold:,} - skip {skips + 1}/"
              f"{args.max_skips}")

        bal = read_balance(frame, digits, args)
        if bal is not None and bal < args.skip_cost:
            print(f"\n  BALANCE {bal:,} is below the {args.skip_cost} skip "
                  f"cost - stopping before the game refuses.")
            break

        sh = find_anchor(frame, skip, args.skip_thresh)
        if sh is None:
            best = raw_score(frame, skip)
            print("  skip button not found - stopping."
                  + (f" (best score {best:.3f} vs need "
                     f"{args.skip_thresh})" if best is not None else ""))
            if bal is not None and bal < args.skip_cost * 2:
                print(f"  your gold is {bal:,} and a skip costs "
                      f"{args.skip_cost} - almost certainly out of gold.")
            else:
                print("  is the raid screen still showing?")
            break
        sx, sy, _ = sh
        th, tw = skip.shape[:2]
        if args.dry_run:
            # A dry run never taps, so the screen never changes - and the
            # loop would otherwise report the same base over and over, as
            # though it had skipped seven times. Wait for you to skip by
            # hand instead, so each printed line is a different base.
            print("    (dry run - skip it yourself; waiting for the "
                  "screen to change)")
            prev = thumb(frame)
            t_end = time.perf_counter() + args.change_timeout
            while time.perf_counter() < t_end:
                if keyboard.is_pressed("f9") or keyboard.is_pressed("esc"):
                    return
                time.sleep(0.05)
                if scene_motion(prev, thumb(grab(sct, region))) > 1.0:
                    break
            else:
                print("  nothing changed - stopping.")
                return
        else:
            click_at(region, sx + tw // 2, sy + th // 2, args.hold)
        skips += 1
        time.sleep(args.delay)

    if seen:
        print(f"\nstopped after {skips} skips. gold seen: min {min(seen):,} "
              f"median {int(np.median(seen)):,} max {max(seen):,}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default=WINDOW_TITLE,
                    help="emulator window title substring, e.g. BlueStacks")
    ap.add_argument("--region-x", type=int, default=0, dest="region_x",
                    help="px to shift the capture right; some emulator "
                         "layouts have an advert panel on one side")
    ap.add_argument("--region-y", type=int, default=0, dest="region_y")
    ap.add_argument("--gold", type=int, default=50000,
                    help="stop when a base has at least this much gold")
    ap.add_argument("--want", default="",
                    help="comma-separated gem colours to stop on, e.g. "
                         "'purple,blue'. Choices: red orange yellow green "
                         "blue purple. Combined with --gold as OR: it "
                         "stops on either")
    ap.add_argument("--min-gems", type=int, default=1, dest="min_gems",
                    help="how many of the wanted colours must be present")
    ap.add_argument("--show-gems", action="store_true", dest="show_gems",
                    help="print the gems on every base without acting on "
                         "them - use this first to see what is out there")
    ap.add_argument("--min-rich", type=int, default=0, dest="min_rich",
                    help="stop when this many gems have a GOLD RIM - the "
                         "high-value ones. 0 disables")
    ap.add_argument("--log", default="",
                    help="append every base to this CSV: time, gold, the "
                         "three gems, and how many had gold rims. Run it "
                         "for a while and the real distribution answers "
                         "what a skip is worth far better than a guess")
    ap.add_argument("--gem-radius", type=int, default=12, dest="gem_radius",
                    help="pixels, measured at the 1280x720 reference "
                         "resolution - auto-scaled to your actual capture "
                         "size")
    ap.add_argument("--rim-min", type=int, default=180, dest="rim_min",
                    help="saturated gold pixels needed in the rim, at the "
                         "1280x720 reference resolution (auto-scaled). "
                         "Real gold rims measured 245-298; an orange GEM "
                         "in a plain slot only reached 113")
    ap.add_argument("--core-min", type=int, default=25, dest="core_min",
                    help="non-gold pixels (reference resolution, "
                         "auto-scaled) needed in the centre before the "
                         "gem colour is taken from them rather than from "
                         "the whole slot")
    ap.add_argument("--rim-sat", type=int, default=170, dest="rim_sat")
    ap.add_argument("--rim-lo", type=int, default=8, dest="rim_lo")
    ap.add_argument("--rim-hi", type=int, default=32, dest="rim_hi")
    ap.add_argument("--gem-sat", type=int, default=110, dest="gem_sat")
    ap.add_argument("--gem-val", type=int, default=110, dest="gem_val")
    ap.add_argument("--gem-fill", type=float, default=0.10, dest="gem_fill",
                    help="fraction of the slot that must be saturated "
                         "before it counts as holding a gem")
    ap.add_argument("--max-skips", type=int, default=30, dest="max_skips",
                    help="hard cap. Skipping is not free, so this is a "
                         "spend limit as much as a safety limit")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="read and report, never tap")
    ap.add_argument("--calibrate", action="store_true",
                    help="show what it sees on the current screen, no taps")
    ap.add_argument("--assets", default="assets",
                    help="folder with coin.png and skip.png, captured at "
                         "the 1280x720 reference resolution (auto-scaled "
                         "to your actual capture size)")
    ap.add_argument("--digits", default="digits",
                    help="folder with 0.png .. 9.png")
    ap.add_argument("--delay", type=float, default=0.6,
                    help="seconds after a skip before reading again")
    ap.add_argument("--quiet", type=float, default=0.25,
                    help="seconds of a still screen before it is read")
    ap.add_argument("--settle-timeout", type=float, default=5.0,
                    dest="settle_timeout")
    ap.add_argument("--skip-cost", type=int, default=300, dest="skip_cost",
                    help="gold a skip costs. The run stops when your "
                         "balance drops below it, rather than skipping into "
                         "a refusal and reporting a missing button")
    ap.add_argument("--retry-reads", type=int, default=3,
                    dest="retry_reads",
                    help="attempts per base before calling it unreadable. "
                         "The TAP TO BREAK IN overlay pulses, so a chest it "
                         "covers may be readable a moment later")
    ap.add_argument("--retry-wait", type=float, default=0.45,
                    dest="retry_wait",
                    help="seconds between those attempts")
    ap.add_argument("--on-fail", choices=["stop", "skip"], default="stop",
                    dest="on_fail",
                    help="what to do with a base that cannot be read. "
                         "stop is the safe default; skip keeps the run "
                         "going unattended but spends a skip on a base "
                         "whose value you never learned")
    ap.add_argument("--min-gold", type=int, default=500, dest="min_gold",
                    help="a read below this is treated as a FAILED read "
                         "rather than a poor base. Partial renders during "
                         "loading parse as tiny numbers - this run saw "
                         "'103' and '4' and skipped on both, at 300 gold "
                         "each")
    ap.add_argument("--max-fails", type=int, default=5, dest="max_fails",
                    help="consecutive read failures before stopping. Never "
                         "skip on a failed read - a base you wanted is gone "
                         "for good, and you will not know it happened")
    ap.add_argument("--coin-thresh", type=float, default=0.60,
                    dest="coin_thresh",
                    help="match score needed to accept the coin anchor. "
                         "Real bases scored 0.74 to 1.00, so 0.72 left "
                         "almost no headroom. A wrong match here is not "
                         "dangerous - no digits follow it, so the read "
                         "fails rather than lying")
    ap.add_argument("--skip-thresh", type=float, default=0.75,
                    dest="skip_thresh")
    ap.add_argument("--digit-thresh", type=float, default=0.55,
                    dest="digit_thresh",
                    help="minimum correlation to accept a digit. Below "
                         "this the glyph is saved as unknown rather than "
                         "guessed")
    ap.add_argument("--digit-margin", type=float, default=0.06,
                    dest="digit_margin",
                    help="how far the best template must beat the runner-up. "
                         "Measured on real bases: correct digits win by "
                         "0.105 at worst (0 vs 8, which genuinely look "
                         "alike), while a digit with no template won by "
                         "0.012 and turned 167100 into 7100. 0.06 sits in "
                         "that gap")
    ap.add_argument("--strip-back", type=int, default=8, dest="strip_back",
                    help="px (reference resolution, auto-scaled) to reach "
                         "back over the coin when looking for the first "
                         "digit; chests vary in spacing")
    ap.add_argument("--glyph-v", type=int, default=190, dest="glyph_v")
    ap.add_argument("--glyph-s", type=int, default=70, dest="glyph_s")
    ap.add_argument("--glyph-s-alt", default="30,20", dest="glyph_s_alt",
                    help="stricter saturation ceilings tried IN ORDER when "
                         "a read fails, for chests with translucent "
                         "decoration (spider web) crossing the number")
    ap.add_argument("--glyph-area", type=int, default=25, dest="glyph_area",
                    help="min pixel AREA for a glyph blob, at the "
                         "1280x720 reference resolution - auto-scaled by "
                         "the capture's area ratio")
    ap.add_argument("--split-wide", type=float, default=1.5,
                    dest="split_wide",
                    help="split a glyph this many times wider than the "
                         "median into equal parts; touching digits merge "
                         "into one blob otherwise. 0 disables")
    ap.add_argument("--glyph-gap", type=int, default=8, dest="glyph_gap",
                    help="px (reference resolution, auto-scaled) gap that "
                         "ends the number")
    ap.add_argument("--strip", type=int, default=120,
                    help="px (reference resolution, auto-scaled) to the "
                         "right of the coin searched for digits")
    ap.add_argument("--change-timeout", type=float, default=30.0,
                    dest="change_timeout",
                    help="dry run only: how long to wait for you to skip "
                         "by hand before giving up")
    ap.add_argument("--hold", type=float, default=0.05)
    args = ap.parse_args()

    coin, skip, digits = load_templates(args.assets)
    for d in "0123456789":
        p = os.path.join(args.digits, f"{d}.png")
        if os.path.isfile(p):
            digits[d] = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    if coin is None:
        raise SystemExit(f"{args.assets}/coin.png not found.")
    if skip is None:
        print(f"  warning: {args.assets}/skip.png not found - cannot tap "
              f"skip.")

    fix_dpi()
    hwnd, title = find_window(args.title)
    if not hwnd:
        print(f"No window matching '{args.title}'.")
        return
    region = game_region(hwnd, args)
    print(f"Found: {title}")

    # ----------------------------------------------------------------
    # Resolution independence: scale the coin/skip templates and every
    # fixed-pixel measurement from the 1280x720 reference they were
    # captured/measured at, to whatever the ACTUAL capture size is.
    # Everything downstream already receives `args`, so attaching the
    # scaled geometry there means read_balance/read_gems/glyphs_right_of
    # etc need no signature changes.
    # ----------------------------------------------------------------
    sx = region["width"] / GAME_W
    sy = region["height"] / GAME_H

    coin = scale_image(coin, sx, sy)
    skip = scale_image(skip, sx, sy)

    args.scale_x, args.scale_y = sx, sy
    args.hud_gold = (
        int(round(HUD_GOLD[0] * sx)), int(round(HUD_GOLD[1] * sy)),
        int(round(HUD_GOLD[2] * sx)), int(round(HUD_GOLD[3] * sy)),
    )
    args.gem_slots = [
        (int(round(dx * sx)), int(round(dy * sy))) for dx, dy in GEM_SLOTS
    ]
    args.gem_radius = max(1, int(round(args.gem_radius * (sx + sy) / 2)))
    args.strip = max(1, int(round(args.strip * sx)))
    args.strip_back = max(1, int(round(args.strip_back * sx)))
    args.glyph_gap = max(1, int(round(args.glyph_gap * sx)))
    args.glyph_area = max(1, int(round(args.glyph_area * sx * sy)))
    args.rim_min = max(1, int(round(args.rim_min * sx * sy)))
    args.core_min = max(1, int(round(args.core_min * sx * sy)))

    if (round(sx, 3), round(sy, 3)) != (1.0, 1.0):
        print(f"  scaled coin/skip templates and on-screen geometry by "
              f"({sx:.3f}x, {sy:.3f}x) for a {region['width']}x"
              f"{region['height']} capture")

    with mss.MSS() as sct:
        if args.calibrate:
            calibrate(sct, region, coin, skip, digits, args)
            return
        print("\nF8 = start/stop   F9 = quit\n")
        while True:
            if keyboard.is_pressed("f9"):
                print("Bye.")
                return
            if keyboard.is_pressed("f8"):
                run(sct, region, hwnd, coin, skip, digits, args)
                time.sleep(0.6)
            time.sleep(0.005)


if __name__ == "__main__":
    main()