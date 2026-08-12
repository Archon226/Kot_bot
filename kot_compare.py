"""
kot_compare.py - did the detector recover the taps it could not see?

Takes an --own run (which has ground-truth taps) and a converted taps file
made from the same run's video, rebases both to their first event, and
lines them up.

This is the experiment. If detected and real taps agree, the perception
pipeline works and replay is just plumbing. If they do not, replaying will
fail and no amount of timing tweaks will save it.

Usage:
    python kot_compare.py runs/own_010008.json taps/mine.json
"""

import argparse
import json

import numpy as np


def rebase(times):
    if not times:
        return []
    t0 = times[0]
    return [t - t0 for t in times]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", help="the --own run JSON (has real taps)")
    ap.add_argument("converted", help="taps JSON made by kot_convert.py")
    ap.add_argument("--tol", type=float, default=60,
                    help="ms within which a pair counts as a match")
    args = ap.parse_args()

    with open(args.run) as f:
        run = json.load(f)
    with open(args.converted) as f:
        conv = json.load(f)

    real_abs = [t["t"] for t in run["taps"]]
    det_abs = [t["t"] for t in conv]

    print(f"real taps:     {len(real_abs)}")
    print(f"detected taps: {len(det_abs)}\n")

    print("absolute times (s)")
    print("  real:     " + "  ".join(f"{t:.3f}" for t in real_abs))
    print("  detected: " + "  ".join(f"{t:.3f}" for t in det_abs))

    real = rebase(real_abs)
    det = rebase(det_abs)

    print("\nrebased to first event (s)")
    print("  real:     " + "  ".join(f"{t:.3f}" for t in real))
    print("  detected: " + "  ".join(f"{t:.3f}" for t in det))

    # Match greedily, nearest first, one-to-one.
    used = set()
    pairs = []
    for i, r in enumerate(real):
        best, bd = None, 1e9
        for j, d in enumerate(det):
            if j in used:
                continue
            if abs(d - r) < bd:
                best, bd = j, abs(d - r)
        if best is not None and bd * 1000 <= args.tol:
            used.add(best)
            pairs.append((i, r, det[best], (det[best] - r) * 1000))

    print(f"\n{'#':>3} {'real':>8} {'detected':>9} {'diff(ms)':>9}")
    for i, r, d, e in pairs:
        print(f"{i + 1:3d} {r:8.3f} {d:9.3f} {e:+9.1f}")

    unmatched_real = [i + 1 for i in range(len(real))
                      if i not in [p[0] for p in pairs]]
    extra = len(det) - len(pairs)

    print(f"\nmatched {len(pairs)}/{len(real)} real taps")
    if unmatched_real:
        print(f"missed real taps: {unmatched_real}")
        print("  Taps that cause no jump (level start, mid-air taps the")
        print("  game ignores) SHOULD be missed - that is correct, not a bug.")
    if extra:
        print(f"extra detections: {extra}  (jumps with no matching tap - "
              f"likely false positives)")

    if pairs:
        errs = np.array([p[3] for p in pairs])
        print(f"\nerror: mean {errs.mean():+.1f}ms  sd {errs.std():.1f}ms  "
              f"worst {np.abs(errs).max():.1f}ms")
        if np.abs(errs).max() < 40:
            print("\n  -> good. Timings recovered from video alone are close")
            print("     enough that replay has a real chance.")
        else:
            print("\n  -> too loose. Some taps are off by more than a jump")
            print("     window; replay will likely fail on those.")


if __name__ == "__main__":
    main()