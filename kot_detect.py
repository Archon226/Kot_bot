"""
kot_detect.py - find the thief, find the jumps, score against ground truth.

The thief is a small dark compact blob. The scene is full of animated
distractors - glow rings, torches, spinners, swinging cocoons - so motion
differencing lights up everything at once (that's the 110-175px spread we
measured). Appearance is the stronger signal: threshold for dark compact
regions, then use continuity of position to pick the right one.

A tap causes a jump. A jump shows up as a sharp upward velocity change:
vy goes from >= 0 (falling or resting) to strongly negative within a
frame or two. We detect those onsets, then compare their times to the
real tap times stored by kot_track.py --own.

The gap between them IS your input latency. Once measured on own-runs,
you can subtract it from timings inferred on runs where you don't have
ground truth.

Usage:
    python kot_detect.py runs/own_001938.json
    python kot_detect.py runs/own_001938.json --debug
    python kot_detect.py runs/own_001938.json --dark 90 --maxarea 400
"""

import argparse
import json
import os

import cv2
import numpy as np


# The HUD strip along the top and the button bar at the bottom both
# contain dark shapes and animate. Ignoring them removes a whole class
# of false positives for free.
CROP_TOP = 0.06
CROP_BOTTOM = 0.90


def load(meta_path):
    with open(meta_path) as f:
        meta = json.load(f)
    raw = os.path.join(os.path.dirname(meta_path), meta["raw"])
    w, h, n = meta["width"], meta["height"], meta["frames"]
    frames = np.memmap(raw, dtype=np.uint8, mode="r", shape=(n, h, w))
    return meta, frames


def find_blobs(frame, dark, min_area, max_area):
    """Dark compact regions that could be the thief."""
    _, mask = cv2.threshold(frame, dark, 255, cv2.THRESH_BINARY_INV)

    # Close small holes (the white eyes punch through the silhouette)
    # then open to drop thin dark edges like chains and platform outlines.
    k = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)

    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)

    out = []
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if not (min_area <= area <= max_area):
            continue
        bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
        if bw == 0 or bh == 0:
            continue
        aspect = bw / bh
        if not (0.4 <= aspect <= 2.5):        # thief is roughly square
            continue
        fill = area / (bw * bh)
        if fill < 0.45:                        # and roughly solid
            continue
        out.append((cents[i][0], cents[i][1], area))
    return out, mask


def track(meta, frames, args):
    h, w = meta["height"], meta["width"]
    y0, y1 = int(h * CROP_TOP), int(h * CROP_BOTTOM)

    times = np.linspace(0, meta["duration"], meta["frames"])
    path = []
    last = None
    lost = 0

    for i in range(meta["frames"]):
        roi = np.array(frames[i][y0:y1], dtype=np.uint8)
        blobs, _ = find_blobs(roi, args.dark, args.minarea, args.maxarea)

        pick = None
        if blobs:
            if last is None:
                # Cold start: take the largest candidate.
                pick = max(blobs, key=lambda b: b[2])
            else:
                # Continuity: nearest to where we last saw it. A thief
                # cannot teleport, so distance is a strong filter.
                lx, ly = last
                pick = min(blobs, key=lambda b: (b[0] - lx) ** 2 + (b[1] - ly) ** 2)
                if np.hypot(pick[0] - lx, pick[1] - ly) > args.maxjump:
                    pick = None

        if pick is None:
            lost += 1
            path.append((times[i], np.nan, np.nan))
        else:
            last = (pick[0], pick[1])
            path.append((times[i], pick[0], pick[1] + y0))

    return np.array(path), lost


def find_jumps(path, args):
    """Onsets where vertical velocity swings sharply upward.

    Screen y grows downward, so 'up' is negative vy. A jump is the frame
    where vy crosses from >= -small into strongly negative.
    """
    t, x, y = path[:, 0], path[:, 1], path[:, 2]

    # Interpolate short gaps so a couple of lost frames don't split a run.
    ok = ~np.isnan(y)
    if ok.sum() < 10:
        return [], None
    y = np.interp(t, t[ok], y[ok])

    # Light smoothing - raw centroid jitters by a pixel or two.
    kern = np.ones(3) / 3
    ys = np.convolve(y, kern, mode="same")

    vy = np.gradient(ys, t)

    jumps = []
    for i in range(2, len(vy) - 2):
        if vy[i] < -args.vthresh and vy[i - 1] >= -args.vthresh * 0.3:
            if jumps and t[i] - jumps[-1] < args.refractory:
                continue
            jumps.append(t[i])

    return jumps, vy


def score(jumps, taps, tol=0.25):
    """Match detected jumps to real taps, greedily, nearest-first."""
    if not taps:
        print("\nNo ground truth in this run (ghost mode). "
              "Detections are unvalidated.")
        return

    used = set()
    pairs = []
    for tap in taps:
        best, bd = None, 1e9
        for j, jt in enumerate(jumps):
            if j in used:
                continue
            d = abs(jt - tap)
            if d < bd:
                best, bd = j, d
        if best is not None and bd <= tol:
            used.add(best)
            pairs.append((tap, jumps[best], jumps[best] - tap))

    print(f"\n{'tap(s)':>9} {'detected':>9} {'lag(ms)':>9}")
    for tap, jt, d in pairs:
        print(f"{tap:9.3f} {jt:9.3f} {d * 1000:+9.1f}")

    missed = len(taps) - len(pairs)
    extra = len(jumps) - len(pairs)
    print(f"\nmatched {len(pairs)}/{len(taps)}   missed {missed}   "
          f"false positives {extra}")

    if pairs:
        lags = np.array([p[2] for p in pairs]) * 1000
        print(f"lag: mean {lags.mean():+.1f}ms  sd {lags.std():.1f}ms  "
              f"range {lags.min():+.1f} to {lags.max():+.1f}ms")
        if lags.std() < 25:
            print("  -> lag is consistent. Subtract the mean to convert "
                  "detected jump times into tap times.")
        else:
            print("  -> lag is too variable to subtract reliably. Tune the "
                  "detector before trusting inferred timings.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meta")
    ap.add_argument("--dark", type=int, default=80,
                    help="pixels darker than this are blob candidates")
    ap.add_argument("--minarea", type=int, default=40)
    ap.add_argument("--maxarea", type=int, default=600)
    ap.add_argument("--maxjump", type=float, default=60,
                    help="max px the thief can move between frames")
    ap.add_argument("--vthresh", type=float, default=120,
                    help="px/s upward velocity that counts as a jump")
    ap.add_argument("--refractory", type=float, default=0.12,
                    help="min seconds between two detected jumps")
    ap.add_argument("--debug", action="store_true",
                    help="write an annotated PNG every 20 frames")
    args = ap.parse_args()

    meta, frames = load(args.meta)
    print(f"{meta['mode']} run: {meta['frames']} frames, "
          f"{meta['duration']}s, {len(meta['taps'])} taps")

    path, lost = track(meta, frames, args)
    found = meta["frames"] - lost
    print(f"thief located in {found}/{meta['frames']} frames "
          f"({100 * found / meta['frames']:.0f}%)")

    if found < meta["frames"] * 0.5:
        print("\nTracking is failing more than half the time. Try adjusting")
        print("--dark (higher = accepts lighter pixels) or --maxarea.")
        print("Run with --debug to see what is being picked.")

    jumps, vy = find_jumps(path, args)
    print(f"detected {len(jumps)} jump onsets")

    taps = [t["t"] for t in meta["taps"]]
    score(jumps, taps)

    if args.debug:
        out = os.path.splitext(args.meta)[0] + "_debug"
        os.makedirs(out, exist_ok=True)
        for i in range(0, meta["frames"], 20):
            img = cv2.cvtColor(np.array(frames[i]), cv2.COLOR_GRAY2BGR)
            _, px, py = path[i]
            if not np.isnan(px):
                cv2.circle(img, (int(px), int(py)), 12, (0, 0, 255), 2)
            cv2.imwrite(os.path.join(out, f"d{i:05d}.png"), img)
        print(f"\nDebug PNGs in {out}/ - red circle is the tracked blob.")


if __name__ == "__main__":
    main()