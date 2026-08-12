"""
kot_launch.py - detect taps as LAUNCHES, not as upward velocity.

Why the previous detector failed:

    vy < -threshold  finds vertical jumps only.

But KoT's thief wall-jumps, and a wall-jump is mostly HORIZONTAL - no
vertical velocity spike, so no threshold could ever catch it. Meanwhile
bounces and landings DO spike vy upward with no tap behind them. On a
real run that gave 2/6 matches and 3 false positives.

The better signal is contact vs free flight. The thief is either:
    - in contact (running a surface, clinging to a wall) - speed low,
      acceleration not gravity
    - airborne - following a ballistic arc, ay ~ constant gravity

A tap is what LAUNCHES it from contact into flight. That definition
catches wall-jumps and vertical jumps equally, and ignores sliding.

Bounces also start ballistic segments, so they are separated by their
incoming speed: a tap-launch begins from rest or near-rest, a bounce
arrives fast and leaves fast.

Usage:
    python kot_launch.py runs/own_XXXX.json
    python kot_launch.py runs/own_XXXX.json --gravity-scan
    python kot_launch.py runs/own_XXXX.json --still 40 --entry 120
"""

import argparse
import json
import os

import numpy as np

import kot_track_thief as kg


def smooth(a, n=5):
    """Moving average. Centroid jitter of a pixel or two becomes huge
    acceleration noise after two derivatives, so smoothing is not optional."""
    k = np.ones(n) / n
    return np.convolve(a, k, mode="same")


def kinematics(path):
    """Position -> velocity -> acceleration, on real timestamps."""
    t, x, y = path[:, 0], path[:, 1], path[:, 2]

    ok = ~np.isnan(x)
    if ok.sum() < 20:
        raise SystemExit("Tracking too sparse to analyse.")

    # Duplicate or non-increasing timestamps make np.gradient divide by
    # zero and poison every derivative downstream. Drop them rather than
    # producing plausible-looking garbage.
    keep = np.concatenate(([True], np.diff(t) > 1e-6))
    if not keep.all():
        print(f"  dropped {int((~keep).sum()) } duplicate timestamps")
        t, x, y = t[keep], x[keep], y[keep]
        ok = ok[keep]
    x = np.interp(t, t[ok], x[ok])
    y = np.interp(t, t[ok], y[ok])

    xs, ys = smooth(x), smooth(y)
    vx = np.gradient(xs, t)
    vy = np.gradient(ys, t)

    # Belt and braces: clamp physically impossible speeds. The thief cannot
    # cross the frame in one tick, so anything above this is a tracking
    # artefact, and one spike ruins the gravity estimate for the whole run.
    cap = 2000.0
    bad = np.hypot(vx, vy) > cap
    if bad.any():
        print(f"  clamped {bad.sum()} impossible velocity samples "
              f"(>{cap:.0f} px/s) - tracking glitches")
        vx[bad] = 0.0
        vy[bad] = 0.0

    ay = np.gradient(smooth(vy), t)
    speed = np.hypot(vx, vy)
    return t, xs, ys, vx, vy, ay, speed


def estimate_gravity(ay, speed, still):
    """Gravity = the positive mode of the acceleration distribution.

    The obvious estimator - median of ay over fast frames - returns ~0 and
    is wrong. The thief spends most of its time RUNNING ALONG SURFACES:
    fast, but zero acceleration. Those frames dominate any median.

    The measured histogram of ay has two clusters: a tall spike at ~0
    (contact) and a separate hump at ~+900 px/s^2 (free fall). Gravity is
    the second one, so find it by taking the positive-side histogram peak
    while skipping the near-zero bin.
    """
    pos = ay[ay > 0]
    if len(pos) < 30:
        return float(np.median(ay))

    counts, edges = np.histogram(pos, bins=25,
                                 range=(0, np.percentile(pos, 98)))
    # Skip the first few bins - that is the tail of the contact cluster,
    # not flight.
    skip = 3
    if counts[skip:].sum() == 0:
        return float(np.median(pos))
    k = skip + int(np.argmax(counts[skip:]))
    return float((edges[k] + edges[k + 1]) / 2)


def find_launches(t, vx, vy, ay, speed, args):
    """A launch = transition from contact to flight.

    contact: slow, and not accelerating like a free body
    flight:  ay near gravity

    We look for frames where the thief was in contact and shortly after
    is moving fast in flight. The launch is credited to the last contact
    frame - that is when the tap took effect.
    """
    g = estimate_gravity(ay, speed, args.still)
    tol = abs(g) * args.gtol

    in_flight = np.abs(ay - g) < tol
    in_contact = (speed < args.still) | (~in_flight & (speed < args.entry))

    launches = []
    i = 2
    n = len(t)
    while i < n - 3:
        if in_contact[i]:
            # look ahead a few frames for a fast flight state
            j = i + 1
            while j < min(i + args.window, n) and in_contact[j]:
                j += 1
            if j < min(i + args.window, n):
                # speed gained across the transition
                gained = speed[j] - speed[i]
                if gained > args.gain and speed[j] > args.entry:
                    if not launches or t[i] - launches[-1][0] > args.refractory:
                        launches.append((float(t[i]),
                                         float(speed[i]),
                                         float(speed[j]),
                                         float(vx[j]),
                                         float(vy[j])))
                    i = j
        i += 1

    return launches, g


def score(launches, taps, tol=0.12):
    times = [l[0] for l in launches]
    if not taps:
        print("\nNo ground truth. Detected launch times (s):")
        print("  " + "  ".join(f"{x:.3f}" for x in times))
        return

    used, pairs = set(), []
    for tap in taps:
        best, bd = None, 1e9
        for j, lt in enumerate(times):
            if j in used:
                continue
            if abs(lt - tap) < bd:
                best, bd = j, abs(lt - tap)
        if best is not None and bd <= tol:
            used.add(best)
            pairs.append((tap, times[best], times[best] - tap))

    print(f"\n{'tap(s)':>9} {'launch':>9} {'lag(ms)':>9}")
    for tap, lt, d in pairs:
        print(f"{tap:9.3f} {lt:9.3f} {d * 1000:+9.1f}")

    unmatched = [x for j, x in enumerate(times) if j not in used]
    print(f"\nmatched {len(pairs)}/{len(taps)}   "
          f"missed {len(taps) - len(pairs)}   "
          f"false positives {len(unmatched)}")
    if unmatched:
        print("  unmatched launches: " +
              "  ".join(f"{x:.3f}" for x in unmatched))

    if pairs:
        lags = np.array([p[2] for p in pairs]) * 1000
        print(f"lag: mean {lags.mean():+.1f}ms  sd {lags.std():.1f}ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meta")
    ap.add_argument("--still", type=float, default=40,
                    help="px/s below this counts as in contact")
    ap.add_argument("--entry", type=float, default=120,
                    help="px/s the thief must reach to count as launched")
    ap.add_argument("--gain", type=float, default=80,
                    help="px/s speed increase required across a launch")
    ap.add_argument("--gtol", type=float, default=0.5,
                    help="fraction of g that ay may deviate and still "
                         "count as free flight")
    ap.add_argument("--window", type=int, default=8,
                    help="frames to look ahead for the flight state")
    ap.add_argument("--refractory", type=float, default=0.10)
    ap.add_argument("--gravity-scan", action="store_true",
                    help="report the fitted gravity and speed profile only")
    kg.add_detector_args(ap)
    args = ap.parse_args()

    meta, frames = kg.load(args.meta)
    print(f"{meta['mode']} run: {meta['frames']} frames, "
          f"{meta['duration']}s, {len(meta['taps'])} taps")

    path = kg.track(meta, frames, args)
    found = int((~np.isnan(path[:, 1])).sum())
    print(f"thief located in {found}/{meta['frames']} frames "
          f"({100 * found / meta['frames']:.0f}%)")

    t, xs, ys, vx, vy, ay, speed = kinematics(path)
    g = estimate_gravity(ay, speed, args.still)

    print(f"\nfitted gravity: {g:+.0f} px/s^2")
    print(f"speed: median {np.median(speed):.0f}  "
          f"p10 {np.percentile(speed, 10):.0f}  "
          f"p90 {np.percentile(speed, 90):.0f}  max {speed.max():.0f} px/s")

    if args.gravity_scan:
        # The median-over-fast-frames estimate keeps returning near zero.
        # Likely cause: the thief spends most of its time running along
        # surfaces (fast, zero acceleration), so contact frames dominate
        # and free-fall frames never move the median. Print the actual
        # distribution instead of assuming a shape for it.
        print("\nay percentiles (px/s^2):")
        for p in (5, 10, 25, 50, 75, 90, 95):
            print(f"  p{p:<3d} {np.percentile(ay, p):+9.0f}")

        print("\nay histogram (all frames):")
        lo, hi = np.percentile(ay, 2), np.percentile(ay, 98)
        counts, edges = np.histogram(ay, bins=18, range=(lo, hi))
        peak = counts.max() or 1
        for c, e0, e1 in zip(counts, edges[:-1], edges[1:]):
            bar = "#" * int(40 * c / peak)
            print(f"  {e0:+8.0f}..{e1:+8.0f} {c:5d} {bar}")

        print("\nIf there are TWO clusters - one near 0 and one at some")
        print("positive value - the positive one is gravity, and contact")
        print("vs flight is separable. If it is a single blob around 0,")
        print("acceleration is too noisy at half resolution and the")
        print("detector needs full-res capture or a different signal.")

        print("\nspeed percentiles (px/s):")
        for p in (5, 10, 25, 50, 75, 90, 95):
            print(f"  p{p:<3d} {np.percentile(speed, p):9.0f}")
        return

    launches, g = find_launches(t, vx, vy, ay, speed, args)
    print(f"\ndetected {len(launches)} launches")
    for lt, s0, s1, lvx, lvy in launches:
        kind = "wall/horiz" if abs(lvx) > abs(lvy) else "vertical"
        print(f"  {lt:6.3f}s  {s0:5.0f} -> {s1:5.0f} px/s  "
              f"vx {lvx:+6.0f} vy {lvy:+6.0f}  {kind}")

    score(launches, [x["t"] for x in meta["taps"]])


if __name__ == "__main__":
    main()