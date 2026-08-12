"""
kot_green.py - track the thief, find jumps, score against taps.

Two detection modes:

  white (default) - for the PANDA costume. The face is bright white and
    low-saturation, and KoT's palette is saturated mid-tones almost
    everywhere else, so this is close to unique on screen.

  hue - the original green-costume mask. Kept because it works in plain
    dungeons, but it FAILS badly in dungeons containing green gems,
    spinners or potion bottles: on a volcano dungeon it locked onto a
    hanging gem and recovered 1 launch from a full run.

Also excludes the player-level badge, a green rosette pinned to the
top-left that is bigger than the thief and never moves - a cold start
grabs it and continuity then holds it forever.

Usage:
    python kot_green.py runs/own_XXXX.json --calibrate
    python kot_green.py runs/own_XXXX.json --debug
    python kot_green.py runs/own_XXXX.json --mode hue
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

# Level badge box, in cropped-ROI coordinates.
BADGE_W, BADGE_H = 60, 40


def add_detector_args(ap):
    """Shared by kot_green, kot_launch and kot_convert so all three agree."""
    ap.add_argument("--mode", choices=["white", "hue"], default="white",
                    help="white = panda face (robust); hue = green costume")
    ap.add_argument("--white-v", type=int, default=185, dest="white_v",
                    help="minimum brightness to count as white")
    ap.add_argument("--white-s", type=int, default=60, dest="white_s",
                    help="maximum saturation to count as white")
    ap.add_argument("--hue-lo", type=int, default=HUE_LO, dest="hue_lo")
    ap.add_argument("--hue-hi", type=int, default=HUE_HI, dest="hue_hi")
    ap.add_argument("--sat", type=int, default=SAT_MIN)
    ap.add_argument("--val", type=int, default=VAL_MIN)
    ap.add_argument("--minarea", type=int, default=12)
    ap.add_argument("--maxarea", type=int, default=600)
    ap.add_argument("--maxjump", type=float, default=40)
    ap.add_argument("--lost-limit", type=int, default=15, dest="lost_limit")


def load(meta_path):
    with open(meta_path) as f:
        meta = json.load(f)
    raw = os.path.join(os.path.dirname(meta_path), meta["raw"])
    w, h, n = meta["width"], meta["height"], meta["frames"]
    if meta.get("channels", 1) == 1:
        raise SystemExit(
            "This run is greyscale. Colour tracking needs a run recorded "
            "with the updated kot_track.py."
        )
    frames = np.memmap(raw, dtype=np.uint8, mode="r", shape=(n, h, w, 3))
    return meta, frames


def frame_times(meta):
    """Real capture time of each frame.

    Frame spacing is NOT even (median 16.6ms, p95 20.6ms, stalls past
    30ms), so assuming uniform spacing accumulates hundreds of ms of error
    across a run and silently corrupts every latency figure.
    """
    t = meta.get("times")
    if t and len(t) == meta["frames"]:
        return np.array(t, dtype=float)
    print("  WARNING: no per-frame timestamps. Assuming even spacing - "
          "timing results will be unreliable.")
    return np.linspace(0, meta["duration"], meta["frames"])


def thief_mask(bgr, args):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)

    if args.mode == "hue":
        lo = np.array([args.hue_lo, args.sat, args.val], np.uint8)
        hi = np.array([args.hue_hi, 255, 255], np.uint8)
        mask = cv2.inRange(hsv, lo, hi)
    else:
        lo = np.array([0, 0, args.white_v], np.uint8)
        hi = np.array([179, args.white_s, 255], np.uint8)
        mask = cv2.inRange(hsv, lo, hi)
        # Close first: the panda's black eyes punch holes through the face
        # and would otherwise split it into separate components.
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                np.ones((3, 3), np.uint8), iterations=2)

    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))


def calibrate(meta, frames, args):
    n = meta["frames"]
    counts = []
    for i in range(0, n, max(1, n // 200)):
        m = thief_mask(np.array(frames[i]), args)
        counts.append(int(m.sum() // 255))
    c = np.array(counts)
    print(f"mode: {args.mode}")
    print(f"mask px per frame: median {np.median(c):.0f}  "
          f"p10 {np.percentile(c, 10):.0f}  p90 {np.percentile(c, 90):.0f}  "
          f"max {c.max()}")
    print(f"frames with any mask: {(c > 0).sum()}/{len(c)}")
    if np.median(c) < 10:
        print("\n  -> too little. Lower --white-v or raise --white-s.")
    elif np.median(c) > 4000:
        print("\n  -> too much; scenery is leaking in. Raise --white-v or "
              "lower --white-s.")
    else:
        print("\n  -> plausible. Confirm with --debug; the mask count alone "
              "cannot tell you WHICH object it found.")


def track(meta, frames, args):
    h = meta["height"]
    y0, y1 = int(h * CROP_TOP), int(h * CROP_BOTTOM)
    times = frame_times(meta)

    path = []
    last = None
    lost = 0

    for i in range(meta["frames"]):
        roi = np.array(frames[i][y0:y1])
        mask = thief_mask(roi, args)
        nl, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)

        cands = [(cents[j][0], cents[j][1], stats[j, cv2.CC_STAT_AREA])
                 for j in range(1, nl)
                 if args.minarea <= stats[j, cv2.CC_STAT_AREA] <= args.maxarea]

        # Drop the level badge without cropping away play area the thief
        # actually uses (it climbs to the totem near the top).
        cands = [c for c in cands
                 if not (c[0] < BADGE_W and c[1] < BADGE_H)]

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
                    # Never re-pick a distant blob: that teleports the
                    # centroid onto scenery and produces impossible
                    # velocities that wreck the derivatives. Coast instead.
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


def find_jumps(path, args):
    """Legacy vy-based detector. Superseded by kot_launch, which also
    catches wall-jumps - those are horizontal and invisible to this."""
    t, y = path[:, 0], path[:, 2]
    ok = ~np.isnan(y)
    if ok.sum() < 10:
        return [], None
    y = np.interp(t, t[ok], y[ok])
    ys = np.convolve(y, np.ones(3) / 3, mode="same")
    vy = np.gradient(ys, t)

    jumps = []
    for i in range(2, len(vy) - 2):
        if vy[i] < -args.vthresh and vy[i - 1] >= -args.vthresh * 0.3:
            if jumps and t[i] - jumps[-1] < args.refractory:
                continue
            jumps.append(float(t[i]))
    return jumps, vy


def score(jumps, taps, tol=0.25):
    if not taps:
        print("\nNo ground truth here. Detected times (s):")
        print("  " + "  ".join(f"{j:.3f}" for j in jumps))
        return

    used, pairs = set(), []
    for tap in taps:
        best, bd = None, 1e9
        for j, jt in enumerate(jumps):
            if j in used:
                continue
            if abs(jt - tap) < bd:
                best, bd = j, abs(jt - tap)
        if best is not None and bd <= tol:
            used.add(best)
            pairs.append((tap, jumps[best], jumps[best] - tap))

    print(f"\n{'tap(s)':>9} {'detected':>9} {'lag(ms)':>9}")
    for tap, jt, d in pairs:
        print(f"{tap:9.3f} {jt:9.3f} {d * 1000:+9.1f}")
    print(f"\nmatched {len(pairs)}/{len(taps)}   "
          f"missed {len(taps) - len(pairs)}   "
          f"false positives {len(jumps) - len(pairs)}")

    if pairs:
        lags = np.array([p[2] for p in pairs]) * 1000
        print(f"lag: mean {lags.mean():+.1f}ms  sd {lags.std():.1f}ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meta")
    add_detector_args(ap)
    ap.add_argument("--vthresh", type=float, default=120)
    ap.add_argument("--refractory", type=float, default=0.12)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()

    meta, frames = load(args.meta)
    print(f"{meta['mode']} run: {meta['frames']} frames, {meta['duration']}s, "
          f"{len(meta['taps'])} taps")

    if args.calibrate:
        calibrate(meta, frames, args)
        return

    path = track(meta, frames, args)
    found = int((~np.isnan(path[:, 1])).sum())
    print(f"thief located in {found}/{meta['frames']} frames "
          f"({100 * found / meta['frames']:.0f}%)")

    jumps, _ = find_jumps(path, args)
    print(f"detected {len(jumps)} jump onsets")

    taps = [t["t"] for t in meta["taps"]]
    if args.dump:
        print("\n  taps:     " + "  ".join(f"{t:.3f}" for t in taps))
        print("  detected: " + "  ".join(f"{j:.3f}" for j in jumps))

    score(jumps, taps)

    if args.debug:
        out = os.path.splitext(args.meta)[0] + "_debug"
        os.makedirs(out, exist_ok=True)
        for i in range(0, meta["frames"], 20):
            img = np.array(frames[i]).copy()
            px, py = path[i][1], path[i][2]
            if not np.isnan(px):
                cv2.circle(img, (int(px), int(py)), 12, (0, 0, 255), 2)
            cv2.imwrite(os.path.join(out, f"d{i:05d}.png"), img)
        print(f"\nDebug PNGs in {out}/ - red circle is the tracked thief.")


if __name__ == "__main__":
    main()