"""
kot_waypoints.py - turn a recorded run into POSITION-triggered waypoints.

WHY THIS EXISTS

Time-triggered replay failed. Every failure traced back to the clock:

  - the replay's t=0 and the ghost's t=0 are different moments, and
    nothing aligned them (--start-gap was a guess that never worked)
  - detected launch times carried ~+-100ms error, wider than a jump window
  - a single missed or spurious tap shifted everything after it

But a demonstration contains more than timings. It contains WHERE the
thief was when each launch happened. Position is not subject to any of
those problems:

  - no clock, so no alignment problem and no drift
  - detection timing error stops mattering; you match on position
  - divergence becomes DETECTABLE - if the thief is nowhere near the next
    waypoint, the run has already gone wrong and the agent can abort
    instead of firing taps into a dead run

So this writes waypoints, not timestamps:

    "tap when the thief is near (566, 196) moving left at ~600 px/s"

Coordinates are stored at FULL resolution (1280x720) because kot_live.py
tracks at full resolution, even though runs are recorded at 640x360.

Usage:
    python kot_waypoints.py runs/own_XXXX.json --out waypoints/mine.json
    python kot_waypoints.py runs/own_XXXX.json --out wp.json --max-vy 300
"""

import argparse
import json
import os

import numpy as np

import kot_track_thief as kg
import kot_launch as kl


def extract(meta_path, args):
    meta, frames = kg.load(meta_path)
    print(f"{meta['mode']} run: {meta['frames']} frames, {meta['duration']}s")

    path = kg.track(meta, frames, args)
    found = int((~np.isnan(path[:, 1])).sum())
    print(f"thief located in {found}/{meta['frames']} frames "
          f"({100 * found / meta['frames']:.0f}%)")
    if found < meta["frames"] * 0.7:
        raise SystemExit("Tracking too poor to extract waypoints.")

    t, xs, ys, vx, vy, ay, speed = kl.kinematics(path)
    launches, g = kl.find_launches(t, vx, vy, ay, speed, args)
    g_full = g / meta["scale"]
    print(f"fitted gravity {g:+.0f} px/s^2 "
          f"({g_full:+.0f} at full res), {len(launches)} launches")

    # Gravity is a free CORRECTNESS check, not just a curiosity. "Located
    # in 97% of frames" only says a blob was found - it does not say the
    # blob was the thief. But whatever we tracked must obey the game's
    # physics, and KoT's gravity is ~1700 px/s^2 at full resolution on
    # every good run. A wildly different value means we tracked something
    # else: a minion, a coin, a flickering torch.
    expected = args.expect_gravity
    if expected > 0 and not (0.55 * expected < g_full < 1.8 * expected):
        print(f"\n  *** TRACKING LIKELY WRONG ***")
        print(f"  Gravity came out {g_full:+.0f}, expected around "
              f"{expected:+.0f} px/s^2.")
        print(f"  Whatever was tracked is not falling like the thief.")
        print(f"  Check with:  python kot_track_thief.py {meta_path} "
              f"--white-v {args.white_v} --white-s {args.white_s} --debug")
        if not args.force:
            raise SystemExit("  Refusing to write waypoints. --force to "
                             "override.")

    # Scale from the recording resolution up to the live tracker's.
    k = 1.0 / meta["scale"]

    wps = []
    for lt, s0, s1, lvx, lvy in launches:
        if lvy > args.max_vy or s1 > args.max_entry:
            continue
        # Position at the launch frame - that is where the thief was when
        # the tap took effect, and therefore where to fire next time.
        i = int(np.argmin(np.abs(t - lt)))
        if np.isnan(xs[i]) or np.isnan(ys[i]):
            continue
        wps.append({
            "x": round(float(xs[i]) * k, 1),
            "y": round(float(ys[i]) * k, 1),
            "vx": round(float(vx[i]) * k, 1),
            "vy": round(float(vy[i]) * k, 1),
            "speed": round(float(speed[i]) * k, 1),
            "t_ref": round(float(lt), 3),   # for reference only, not used
        })

    if not wps:
        raise SystemExit("No waypoints survived filtering.")

    print(f"\n{len(wps)} waypoints (full-res coords):")
    print(f"{'#':>3} {'x':>7} {'y':>7} {'vx':>8} {'vy':>8} {'t_ref':>7}")
    for i, w in enumerate(wps, 1):
        print(f"{i:3d} {w['x']:7.0f} {w['y']:7.0f} {w['vx']:+8.0f} "
              f"{w['vy']:+8.0f} {w['t_ref']:7.2f}")

    # Waypoints too close together cannot be told apart at replay time -
    # the agent would fire both on the first match. Flag them rather than
    # silently producing an unreplayable file.
    warn = 0
    for i in range(len(wps) - 1):
        d = np.hypot(wps[i + 1]["x"] - wps[i]["x"],
                     wps[i + 1]["y"] - wps[i]["y"])
        if d < args.min_sep:
            print(f"  WARNING: waypoints {i + 1} and {i + 2} are only "
                  f"{d:.0f}px apart (min useful separation ~{args.min_sep})")
            warn += 1
    if warn:
        print("  Close pairs may fire together. Raise --min-sep tolerance "
              "at replay time, or accept the risk.")

    out = args.out or "waypoints.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        json.dump({
            "source": os.path.basename(meta_path),
            "game_w": 1280,
            "game_h": 720,
            "gravity": round(float(g) * k, 1),
            "waypoints": wps,
        }, f, indent=1)
    print(f"\nSaved {out}")
    print("Replay with kot_agent.py - it fires on POSITION, not time.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meta")
    ap.add_argument("--out", help="output waypoints JSON")
    ap.add_argument("--still", type=float, default=40)
    ap.add_argument("--entry", type=float, default=200)
    ap.add_argument("--gain", type=float, default=80)
    ap.add_argument("--gtol", type=float, default=0.4)
    ap.add_argument("--window", type=int, default=8)
    ap.add_argument("--refractory", type=float, default=0.10)
    ap.add_argument("--max-vy", type=float, default=300, dest="max_vy",
                    help="vy at flight entry above this is a landing, not a "
                         "tap. NOTE: wall-jumps can move DOWNWARD and still "
                         "be real taps, so this must stay loose - a tight "
                         "value silently deleted half the real launches")
    ap.add_argument("--max-entry", type=float, default=600, dest="max_entry",
                    help="reject launches faster than this (end-of-level "
                         "animation reaches 1000+ px/s)")
    ap.add_argument("--expect-gravity", type=float, default=1700,
                    dest="expect_gravity",
                    help="expected full-res gravity px/s^2; 0 disables the "
                         "check")
    ap.add_argument("--force", action="store_true",
                    help="write waypoints even if the gravity check fails")
    ap.add_argument("--min-sep", type=float, default=60, dest="min_sep",
                    help="warn when consecutive waypoints are closer than "
                         "this many px")
    kg.add_detector_args(ap)
    args = ap.parse_args()
    extract(args.meta, args)


if __name__ == "__main__":
    main()