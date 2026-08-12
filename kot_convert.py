"""
kot_convert.py - turn a recorded run into a replayable tap file.

Closes the loop:
    kot_track.py --ghost   record the solution replay
    kot_convert.py         detect jumps -> subtract lag -> write taps JSON
    kot_tapper.py          F5 to replay it into the game

The lag constant is what makes this work. Detected jump times are when the
thief VISIBLY moved; the tap that caused it happened earlier. Measure that
offset on --own runs where both are known, then subtract it here.

    python kot_convert.py --measure runs/own_XXXX.json
    python kot_convert.py runs/ghost_XXXX.json --lag -9.9 --out taps/dungeon.json

Two things this deliberately does NOT do:

1. Guess the start tap. The tap that begins a level produces no jump, so
   detection cannot see it. Use --start-tap to prepend one.

2. Trust itself. Replay is open-loop - if the timing is off, the thief
   dies and nothing recovers. Check the printed intervals against the
   original run before spending a lockpick on it.
"""

import argparse
import json
import os

import numpy as np

import kot_track_thief as kg
import kot_launch as kl


def measure(meta_path, args):
    """Report the lag constant from an --own run with ground truth."""
    meta, frames = kg.load(meta_path)
    if not meta["taps"]:
        raise SystemExit("This run has no taps. Use an --own recording.")

    path = kg.track(meta, frames, args)
    t, xs, ys, vx, vy, ay, speed = kl.kinematics(path)
    launches, _ = kl.find_launches(t, vx, vy, ay, speed, args)
    jumps = [lt for lt, s0, s1, lvx, lvy in launches
             if lvy <= args.max_vy and s1 <= args.max_entry]
    taps = [t2["t"] for t2 in meta["taps"]]

    print(f"{meta['frames']} frames, {len(taps)} taps, "
          f"{len(jumps)} jumps detected")

    pairs = []
    used = set()
    for tap in taps:
        best, bd = None, 1e9
        for j, jt in enumerate(jumps):
            if j in used:
                continue
            if abs(jt - tap) < bd:
                best, bd = j, abs(jt - tap)
        if best is not None and bd <= 0.25:
            used.add(best)
            pairs.append(jumps[best] - tap)

    if not pairs:
        raise SystemExit("No matches. Tune --still/--entry with kot_launch.py.")

    lags = np.array(pairs) * 1000
    print(f"matched {len(pairs)}/{len(taps)}")
    print(f"lag: mean {lags.mean():+.1f}ms  sd {lags.std():.1f}ms")
    print(f"\nUse:  --lag {lags.mean():.1f}")
    if lags.std() > 25:
        print("WARNING: sd above 25ms. Conversions will be unreliable.")


def convert(meta_path, args):
    meta, frames = kg.load(meta_path)
    path = kg.track(meta, frames, args)
    found = int((~np.isnan(path[:, 1])).sum())
    print(f"thief located in {found}/{meta['frames']} frames "
          f"({100 * found / meta['frames']:.0f}%)")
    if found < meta["frames"] * 0.7:
        raise SystemExit("Tracking too poor to convert. Check --calibrate.")

    t, xs, ys, vx, vy, ay, speed = kl.kinematics(path)
    launches, _ = kl.find_launches(t, vx, vy, ay, speed, args)

    # Two filters that kill most false positives, both from physics:
    #   1. a tap always sends the thief UPWARD, so vy at flight entry must
    #      be negative (screen y grows downward). Landings and slides have
    #      positive vy and get dropped.
    #   2. end-of-level animations produce speeds far beyond real movement.
    kept = []
    for lt, s0, s1, lvx, lvy in launches:
        if lvy > args.max_vy:
            continue
        if s1 > args.max_entry:
            continue
        kept.append(lt)

    dropped = len(launches) - len(kept)
    print(f"detected {len(launches)} launches, kept {len(kept)} "
          f"(dropped {dropped} as landings/animation)")
    jumps = kept
    if not jumps:
        raise SystemExit("Nothing left after filtering. Loosen --max-vy.")

    lag_s = args.lag / 1000.0
    tap_times = [j - lag_s for j in jumps]

    # Trim anything before the replay actually starts. Menu animation and
    # transitions can produce spurious early detections.
    if args.skip_before > 0:
        before = len(tap_times)
        tap_times = [t for t in tap_times if t >= args.skip_before]
        if before != len(tap_times):
            print(f"dropped {before - len(tap_times)} detections before "
                  f"{args.skip_before}s")

    if args.skip_after:
        before = len(tap_times)
        tap_times = [t for t in tap_times if t <= args.skip_after]
        if before != len(tap_times):
            print(f"dropped {before - len(tap_times)} detections after "
                  f"{args.skip_after}s")

    if not tap_times:
        raise SystemExit("Nothing left after trimming.")

    # A long silence in the middle almost always means the recording
    # spans more than one attempt. Replaying across that boundary is
    # guaranteed to fail, so say so rather than writing a broken file.
    gaps_s = np.diff(tap_times)
    if len(gaps_s) and gaps_s.max() > 4.0:
        k = int(np.argmax(gaps_s))
        print(f"\n  WARNING: {gaps_s.max():.1f}s gap between "
              f"{tap_times[k]:.2f}s and {tap_times[k + 1]:.2f}s.")
        print(f"  That is probably a boundary between two attempts. Use "
              f"--skip-before {tap_times[k + 1] - 0.5:.1f} or "
              f"--skip-after {tap_times[k] + 0.5:.1f} to keep just one.")

    # Rebase so the first tap sits at the requested lead-in, and optionally
    # prepend the level-start tap that detection can never see.
    # KoT is one-tap-anywhere control, so x/y only need to land inside the
    # game area and away from UI buttons. Centre screen is safe.
    t0 = tap_times[0]
    taps = []
    if args.start_tap:
        taps.append({"t": 0.0, "x": args.x, "y": args.y, "hold": args.hold})
        offset = args.start_gap
    else:
        offset = 0.0

    for t in tap_times:
        taps.append({"t": round(t - t0 + offset, 4),
                     "x": args.x, "y": args.y, "hold": args.hold})

    gaps = np.diff([t["t"] for t in taps])
    print(f"\n{len(taps)} taps over {taps[-1]['t']:.2f}s")
    print(f"gaps: min {gaps.min() * 1000:.0f}ms  median "
          f"{np.median(gaps) * 1000:.0f}ms  max {gaps.max() * 1000:.0f}ms")
    if gaps.min() < 0.08:
        print("  NOTE: some gaps are under 80ms. Check these are real "
              "double-taps and not the detector firing twice on one jump.")

    print("\ntap times (s):")
    print("  " + "  ".join(f"{t['t']:.3f}" for t in taps))

    out = args.out or os.path.join("taps", "converted.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        json.dump(taps, f, indent=1)
    print(f"\nSaved {out}")
    print("Load it in kot_tapper.py and press F5 with the level ready.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meta")
    ap.add_argument("--measure", action="store_true",
                    help="report the lag constant from an --own run")
    ap.add_argument("--lag", type=float, default=0.0,
                    help="ms to subtract from detected jump times")
    ap.add_argument("--out", help="output taps JSON path")
    ap.add_argument("--hold", type=float, default=0.10,
                    help="tap hold duration in seconds")
    ap.add_argument("--x", type=int, default=640,
                    help="tap x in game coords (tap-anywhere; avoid buttons)")
    ap.add_argument("--y", type=int, default=360, help="tap y in game coords")
    ap.add_argument("--start-tap", action="store_true",
                    help="prepend a tap at t=0 to start the level")
    ap.add_argument("--start-gap", type=float, default=1.0,
                    help="seconds between start tap and first real tap")
    ap.add_argument("--skip-before", type=float, default=0.0,
                    help="ignore detections before this time")
    ap.add_argument("--skip-after", type=float, default=0.0,
                    dest="skip_after",
                    help="ignore detections after this time")
    # detector knobs, shared so every script agrees
    kg.add_detector_args(ap)
    ap.add_argument("--still", type=float, default=40,
                    help="px/s below this counts as in contact")
    ap.add_argument("--entry", type=float, default=200,
                    help="px/s the thief must reach to count as launched")
    ap.add_argument("--gain", type=float, default=80)
    ap.add_argument("--gtol", type=float, default=0.4)
    ap.add_argument("--window", type=int, default=8)
    ap.add_argument("--max-vy", type=float, default=-40, dest="max_vy",
                    help="vy at flight entry must be below this; taps send "
                         "the thief upward, landings do not")
    ap.add_argument("--max-entry", type=float, default=600, dest="max_entry",
                    help="reject launches faster than this (animations)")
    ap.add_argument("--refractory", type=float, default=0.10)
    args = ap.parse_args()

    if args.measure:
        measure(args.meta, args)
    else:
        convert(args.meta, args)


if __name__ == "__main__":
    main()