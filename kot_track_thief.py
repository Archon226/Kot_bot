"""
kot_track_thief.py - locate the thief. Replaces kot_green.py.

WHY THE OLD APPROACH FAILED

Colour alone cannot identify the thief across arbitrary dungeons:

  green costume  -> locked onto a hanging green GEM in a volcano dungeon
                    (also green spinner traps, green potion bottles)
  panda / white  -> locked onto white SPIDER WEBS in a crypt dungeon
                    (also pale platform edges, the "GHOST" caption)
                    median mask 1912px where the face is ~150px

Every dungeon has different decor and each one breaks a different colour
assumption. Enumerating decoys (as we did for the level badge) does not
scale - there is always another one.

THE FIX

All of those decoys have one thing in common: they are STATIC. Same
pixels, every frame. The thief is the only thing making large sustained
translations.

So build a median background from the run itself, and require candidates
to differ from it. The thief is never in the background because it never
sits in one place long enough to survive a median. Gems, webs, badges,
text and platforms all are.

This is dungeon-agnostic and costume-agnostic. Colour becomes a secondary
filter rather than the primary signal.

KNOWN LIMITATION: if the thief genuinely rests in one spot for most of a
run, it joins the background and becomes invisible. Keep recordings tight
around actual movement. --no-bg falls back to pure colour if needed.

Usage:
    python kot_track_thief.py runs/own_XXXX.json --calibrate
    python kot_track_thief.py runs/own_XXXX.json --debug
    python kot_track_thief.py runs/own_XXXX.json --no-bg --mode hue
"""

import argparse
import json
import os

import cv2
import numpy as np

# Green costume (hue mode)
HUE_LO, HUE_HI = 35, 85
SAT_MIN = 90
VAL_MIN = 60

CROP_TOP = 0.06     # HUD strip
CROP_BOTTOM = 0.90  # button bar


def add_detector_args(ap):
    """Shared by every script that tracks, so they cannot disagree."""
    ap.add_argument("--mode", choices=["white", "hue", "any"], default="white",
                    help="white = panda face; hue = green costume; "
                         "any = motion only, no colour filter")
    ap.add_argument("--white-v", type=int, default=185, dest="white_v")
    ap.add_argument("--white-s", type=int, default=70, dest="white_s")
    ap.add_argument("--hue-lo", type=int, default=HUE_LO, dest="hue_lo")
    ap.add_argument("--hue-hi", type=int, default=HUE_HI, dest="hue_hi")
    ap.add_argument("--sat", type=int, default=SAT_MIN)
    ap.add_argument("--val", type=int, default=VAL_MIN)
    ap.add_argument("--minarea", type=int, default=12)
    ap.add_argument("--maxarea", type=int, default=900)
    ap.add_argument("--maxjump", type=float, default=40,
                    help="max px/frame; beyond this the frame is LOST and "
                         "the tracker coasts - it never re-picks a distant "
                         "blob, which used to teleport onto scenery")
    ap.add_argument("--lost-limit", type=int, default=15, dest="lost_limit")
    ap.add_argument("--no-bg", action="store_true", dest="no_bg",
                    help="disable background subtraction (colour only)")
    ap.add_argument("--bg-thresh", type=int, default=28, dest="bg_thresh",
                    help="per-pixel difference from background to count "
                         "as moving")
    ap.add_argument("--bg-samples", type=int, default=80, dest="bg_samples",
                    help="frames sampled to build the median background")


def load(meta_path):
    with open(meta_path) as f:
        meta = json.load(f)
    raw = os.path.join(os.path.dirname(meta_path), meta["raw"])
    w, h, n = meta["width"], meta["height"], meta["frames"]
    if meta.get("channels", 1) == 1:
        raise SystemExit("This run is greyscale. Re-record in colour.")
    frames = np.memmap(raw, dtype=np.uint8, mode="r", shape=(n, h, w, 3))
    return meta, frames


def frame_times(meta):
    """Real capture time per frame.

    Spacing is NOT even (median 16.6ms, p95 20.6ms, stalls past 30ms).
    Assuming uniform spacing accumulates hundreds of ms across a run and
    silently corrupts every latency figure derived from it.
    """
    t = meta.get("times")
    if t and len(t) == meta["frames"]:
        return np.array(t, dtype=float)
    print("  WARNING: no per-frame timestamps; assuming even spacing. "
          "Timing results will be unreliable.")
    return np.linspace(0, meta["duration"], meta["frames"])


def build_background(frames, meta, samples):
    """Per-pixel median over sampled frames = everything that doesn't move.

    Median rather than mean: a mean is dragged by the thief passing
    through, a median ignores it as long as the thief is in any given
    pixel for a minority of the sampled frames.
    """
    n = meta["frames"]
    idx = np.linspace(0, n - 1, min(samples, n)).astype(int)
    stack = np.stack([np.array(frames[i]) for i in idx])
    return np.median(stack, axis=0).astype(np.uint8)


def colour_mask(bgr, args):
    if args.mode == "any":
        return np.full(bgr.shape[:2], 255, np.uint8)

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    if args.mode == "hue":
        lo = np.array([args.hue_lo, args.sat, args.val], np.uint8)
        hi = np.array([args.hue_hi, 255, 255], np.uint8)
        return cv2.inRange(hsv, lo, hi)

    lo = np.array([0, 0, args.white_v], np.uint8)
    hi = np.array([179, args.white_s, 255], np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    # Close first: the panda's black eyes punch holes through the white
    # face and would otherwise split it into separate components.
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8),
                            iterations=2)


def thief_mask(roi, bg_roi, args):
    m = colour_mask(roi, args)

    if bg_roi is not None:
        diff = cv2.absdiff(roi, bg_roi).max(axis=2)
        moving = (diff > args.bg_thresh).astype(np.uint8) * 255
        m = cv2.bitwise_and(m, moving)

    return cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))


def calibrate(meta, frames, args):
    h = meta["height"]
    y0, y1 = int(h * CROP_TOP), int(h * CROP_BOTTOM)

    bg = None if args.no_bg else build_background(frames, meta,
                                                  args.bg_samples)
    bg_roi = None if bg is None else bg[y0:y1]

    n = meta["frames"]
    step = max(1, n // 200)
    colour_only, combined = [], []
    for i in range(0, n, step):
        roi = np.array(frames[i][y0:y1])
        colour_only.append(int(colour_mask(roi, args).sum() // 255))
        combined.append(int(thief_mask(roi, bg_roi, args).sum() // 255))

    c = np.array(colour_only)
    k = np.array(combined)
    print(f"mode: {args.mode}   background: {'off' if args.no_bg else 'on'}")
    print(f"colour mask alone : median {np.median(c):7.0f}  "
          f"p90 {np.percentile(c, 90):7.0f}")
    print(f"after background  : median {np.median(k):7.0f}  "
          f"p90 {np.percentile(k, 90):7.0f}")
    if np.median(c) > 0:
        print(f"reduction: {100 * (1 - np.median(k) / max(np.median(c), 1)):.0f}%")
    print(f"frames with any mask: {(k > 0).sum()}/{len(k)}")

    if np.median(k) < 8:
        print("\n  -> almost nothing survives. Either the thief barely "
              "moves in this run, or --bg-thresh is too high.")
    elif np.median(k) > 2000:
        print("\n  -> still too much. Raise --bg-thresh, or tighten the "
              "colour thresholds.")
    else:
        print("\n  -> plausible. Confirm with --debug; a pixel count "
              "cannot tell you WHICH object was found.")


def track(meta, frames, args):
    h = meta["height"]
    y0, y1 = int(h * CROP_TOP), int(h * CROP_BOTTOM)
    times = frame_times(meta)

    bg = None if args.no_bg else build_background(frames, meta,
                                                  args.bg_samples)
    bg_roi = None if bg is None else bg[y0:y1]

    path = []
    last = None
    lost = 0

    for i in range(meta["frames"]):
        roi = np.array(frames[i][y0:y1])
        mask = thief_mask(roi, bg_roi, args)
        nl, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)

        cands = [(cents[j][0], cents[j][1], stats[j, cv2.CC_STAT_AREA])
                 for j in range(1, nl)
                 if args.minarea <= stats[j, cv2.CC_STAT_AREA] <= args.maxarea]

        pick = None
        if cands:
            if last is None:
                pick = max(cands, key=lambda c: c[2])
            else:
                lx, ly = last
                near = min(cands,
                           key=lambda c: (c[0] - lx) ** 2 + (c[1] - ly) ** 2)
                if np.hypot(near[0] - lx, near[1] - ly) <= args.maxjump:
                    pick = near
                    lost = 0
                else:
                    pick = None
                    lost += 1
                    if lost > args.lost_limit:
                        last = None
                        lost = 0

        if pick is None:
            path.append((times[i], np.nan, np.nan))
        else:
            last = (pick[0], pick[1])
            path.append((times[i], pick[0], pick[1] + y0))

    return np.array(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meta")
    add_detector_args(ap)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--debug-every", type=int, default=20, dest="debug_every")
    ap.add_argument("--save-bg", action="store_true", dest="save_bg",
                    help="write the median background as a PNG to inspect")
    args = ap.parse_args()

    meta, frames = load(args.meta)
    print(f"{meta['mode']} run: {meta['frames']} frames, {meta['duration']}s, "
          f"{len(meta['taps'])} taps")

    if args.save_bg:
        bg = build_background(frames, meta, args.bg_samples)
        p = os.path.splitext(args.meta)[0] + "_bg.png"
        cv2.imwrite(p, bg)
        print(f"background written to {p}")
        print("Everything static should be visible; the thief should NOT be.")
        return

    if args.calibrate:
        calibrate(meta, frames, args)
        return

    path = track(meta, frames, args)
    found = int((~np.isnan(path[:, 1])).sum())
    print(f"thief located in {found}/{meta['frames']} frames "
          f"({100 * found / meta['frames']:.0f}%)")

    xs = path[~np.isnan(path[:, 1]), 1]
    ys = path[~np.isnan(path[:, 2]), 2]
    if len(xs) > 2:
        print(f"x range {xs.min():.0f}-{xs.max():.0f}  "
              f"y range {ys.min():.0f}-{ys.max():.0f}")
        print("  A tracked object that barely moves is decor, not the thief.")

    if args.debug:
        out = os.path.splitext(args.meta)[0] + "_debug"
        os.makedirs(out, exist_ok=True)
        for i in range(0, meta["frames"], args.debug_every):
            img = np.array(frames[i]).copy()
            px, py = path[i][1], path[i][2]
            if not np.isnan(px):
                cv2.circle(img, (int(px), int(py)), 12, (0, 0, 255), 2)
            cv2.imwrite(os.path.join(out, f"d{i:05d}.png"), img)
        print(f"\nDebug PNGs in {out}/")


if __name__ == "__main__":
    main()