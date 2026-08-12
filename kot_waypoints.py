"""
kot_waypoints.py v2 - turn a recorded run into POSITION-triggered waypoints.

WHY THIS EXISTS

Time-triggered replay failed, and every failure traced back to the clock:

  - the replay's t=0 and the demonstration's t=0 are different moments, and
    nothing aligned them (--start-gap was a guess that never worked)
  - detected launch times carried ~+-100ms error, wider than a jump window
  - a single missed or spurious tap shifted everything after it
  - KoT IGNORES TAPS MADE MID-AIR, so a tap log and what the game actually
    did are different things

But a demonstration contains more than timings. It contains WHERE the
thief was when each launch happened, and position has none of those
problems:

  - no clock, so no alignment problem and no drift
  - detection timing error stops mattering; you match on position
  - divergence becomes DETECTABLE - if the thief is nowhere near the next
    waypoint the run has already gone wrong, and the agent can abort
    instead of firing taps into a dead run

So this writes waypoints, not timestamps:

    "tap when the thief is near (566, 196) moving left at ~600 px/s"

Coordinates are stored at FULL resolution (1280x720) because the live
agent tracks at full resolution, even though runs are recorded at 640x360.

--------------------------------------------------------------------------
WHAT CHANGED IN v2

The gravity correctness check now uses the FITTED gravity, not the v1
histogram estimator.

That matters because this check is the project's main defence against a
wrong-object lock, and it was validating against a number that could not
be trusted. On runs/own_040008.json the v1 estimator returned +514 where
the true value was ~850 - and on a shorter slice of the same run it
returned +1038. A guard whose reference wanders by a factor of two can
both reject good runs and pass bad ones.

With a sliding parabola fit the same path returns +852 against an +850
prior, so the acceptance band can be tightened from 0.55x-1.8x to
0.75x-1.35x. A tighter band is the whole point: it is what makes the check
able to catch a minion, a coin burst or a swinging bone.

Launch detection also now uses the fitted acceleration and the
sustained-contact rule, so one fall is no longer counted three times as
three separate waypoints.

Usage:
    python kot_waypoints.py runs/own_XXXX.json --out waypoints/mine.json
    python kot_waypoints.py runs/own_XXXX.json --out wp.json --from-t 1.5
"""

import argparse
import json
import os

import numpy as np

import kot_track_thief as kg
import kot_launch as kl


def approach_velocity(t, x, y, i, k):
    """Velocity ARRIVING at frame i, from raw positions before it.

    THIS IS THE STATE THE AGENT CAN ACTUALLY OBSERVE.

    A waypoint says "tap when the thief is here, moving like this". The
    agent measures how the thief is moving as it ARRIVES. So the file must
    record the same thing - the approach - not the velocity the thief has
    after the tap has taken effect.

    The old code read vx[i] from kinematics(), which derives from a
    5-frame CENTRED moving average. Centred smoothing pulls the launch
    backwards about two frames, so the recorded "velocity at the last
    contact frame" already contained the jump that had not happened yet.

    The giveaway was waypoint 1 of lvl28: the thief is provably stationary
    there - the agent locks onto it precisely because it has not moved a
    pixel in 1.2s - and the file recorded (+84,-311), magnitude 322. The
    agent measured (0,0), scored vd=324, and was marked a poor match. It
    was a perfect match measured against the wrong target.

    That one error made the velocity gate meaningless: vd sat at 300-570
    on every fire, so --vtol 400 rejected a genuine 13px match while
    --vtol 600 admitted real garbage.

    Raw positions, not smoothed, and strictly before i.
    """
    j = max(0, i - k)
    dt = t[i] - t[j]
    if dt <= 1e-4:
        return 0.0, 0.0
    return (x[i] - x[j]) / dt, (y[i] - y[j]) / dt


def extract(meta_path, args):
    meta, frames = kg.load(meta_path)
    print(f"{meta['mode']} run: {meta['frames']} frames, {meta['duration']}s")

    path = kg.track(meta, frames, args)
    found = int((~np.isnan(path[:, 1])).sum())
    print(f"thief located in {found}/{meta['frames']} frames "
          f"({100 * found / meta['frames']:.0f}%)")

    t, xs, ys, vx, vy, ay, speed = kl.kinematics(path)
    _, x_raw, y_raw, observed = kl.prepare(path, quiet=True)

    # Coverage is measured over the ANALYSED span, not the whole file. A
    # recording with a long menu head or a level-end tail would otherwise
    # look like poor tracking when it is simply a longer file.
    covered = observed.sum() / len(observed)
    print(f"coverage of the analysed span: {100 * covered:.0f}%")
    if covered < 0.7:
        raise SystemExit("Tracking too poor to extract waypoints. Check "
                         "kot_track_thief.py --tracks and --debug first.")

    a_fit, res = kl.local_accel(t, y_raw, args.fit_win)
    prior = kg.expected_gravity(meta, args)
    g, n_used, how = kl.fit_gravity(a_fit, res, prior, args.fit_res)
    g_full = g / meta["scale"]

    print(f"\ngravity prior {prior:+.0f}  fitted {g:+.0f} "
          f"({how}, {n_used} clean windows)")
    print(f"  = {g_full:+.0f} px/s^2 at full resolution")

    # THE CORRECTNESS CHECK.
    #
    # "Located in 97% of frames" only says a blob was found - it does not
    # say the blob was the thief. The tracker has repeatedly reported high
    # coverage while following a minion, a coin burst or a swinging bone.
    #
    # But whatever was tracked must obey the game's physics, and KoT's
    # gravity is ~1700 px/s^2 at full resolution on every good run. This
    # turns a silent failure into a loud one, and it is the single most
    # useful diagnostic in the project.
    expected = args.expect_gravity
    if expected > 0 and not (0.75 * expected < g_full < 1.35 * expected):
        print(f"\n  *** TRACKING LIKELY WRONG ***")
        print(f"  Gravity came out {g_full:+.0f}, expected around "
              f"{expected:+.0f} px/s^2.")
        print(f"  Whatever was tracked is not falling like the thief.")
        print(f"  Check with:  python kot_track_thief.py {meta_path} "
              f"--white-v {args.white_v} --white-s {args.white_s} --tracks")
        if not args.force:
            raise SystemExit("  Refusing to write waypoints. --force to "
                             "override.")
    if "NOTHING near prior" in how:
        print("\n  *** No window in this run fits a ballistic arc near the "
              "expected gravity. Do not trust anything below. ***")
        if not args.force:
            raise SystemExit("  Refusing to write waypoints.")

    launches, _ = kl.find_launches(t, vx, vy, ay, speed, args,
                                   a_fit=a_fit, res=res, g=g)
    print(f"{len(launches)} launches detected")

    # GROUND TRUTH BEATS INFERENCE.
    #
    # An --own recording knows exactly when the human tapped. Any detected
    # "launch" with no tap beside it is a bounce, a wall-slide or a landing
    # that looked like one - and every spurious waypoint becomes an extra
    # tap the agent sends that the demonstration never contained.
    #
    # That is not a cosmetic error. On lvl28, 13 taps produced 16
    # waypoints, so the agent tapped three extra times; near a patrolling
    # hazard an unplanned tap kills the run outright.
    #
    # Detection still has to be good - this cannot rescue a run whose
    # launches are badly timed - but where truth is available, use it.
    taps = [x["t"] for x in meta.get("taps", [])]
    if args.taps_only and taps:
        keep, used = [], set()
        for L in launches:
            best, bd = None, 1e9
            for j, tp in enumerate(taps):
                if j in used:
                    continue
                if abs(L[0] - tp) < bd:
                    best, bd = j, abs(L[0] - tp)
            if best is not None and bd <= args.match_tol:
                used.add(best)
                keep.append(L)
        dropped = len(launches) - len(keep)
        print(f"  --taps-only: kept {len(keep)} launches matching one of "
              f"{len(taps)} recorded taps, dropped {dropped} unmatched")
        if dropped:
            print(f"  dropped at: " + "  ".join(
                f"{L[0]:.2f}s" for L in launches if L not in keep))
        missed = len(taps) - len(used)
        if missed:
            print(f"  NOTE: {missed} of your taps produced no detected "
                  f"launch. Those are actions the agent will never "
                  f"reproduce - it will be playing a shorter run than you "
                  f"did. Check kot_launch.py on this recording.")
        launches = keep
    elif args.taps_only:
        print("  --taps-only requested but this run has no recorded taps.")

    # Scale from the recording resolution up to the live tracker's.
    k = 1.0 / meta["scale"]

    wps = []
    for lt, s0, s1, lvx, lvy in launches:
        # NOTE: --max-vy must stay loose. A wall-jump can move DOWNWARD and
        # still be a real tap; a tight value silently deleted half the real
        # launches and is very likely why every early ghost conversion came
        # out short.
        if lvy > args.max_vy or s1 > args.max_entry:
            continue
        # Position at the launch frame - where the thief was when the tap
        # took effect, and therefore where to fire next time.
        i = int(np.argmin(np.abs(t - lt)))
        if np.isnan(xs[i]) or np.isnan(ys[i]):
            continue
        avx, avy = approach_velocity(t, x_raw, y_raw, i, args.approach)
        wps.append({
            "x": round(float(xs[i]) * k, 1),
            "y": round(float(ys[i]) * k, 1),
            "vx": round(float(avx) * k, 1),
            "vy": round(float(avy) * k, 1),
            "speed": round(float(np.hypot(avx, avy)) * k, 1),
            # The post-tap velocity, kept for reference and for debugging.
            # It is NOT what the agent matches against.
            "vx_launch": round(float(lvx) * k, 1),
            "vy_launch": round(float(lvy) * k, 1),
            "observed": bool(observed[i]),
            "t_ref": round(float(lt), 3),   # for reference only, not used
        })

    if not wps:
        raise SystemExit("No waypoints survived filtering.")

    print(f"\n{len(wps)} waypoints (full-res coords):")
    print("  vx/vy are the APPROACH velocity - what the agent sees "
          "arriving, not the post-tap velocity.")
    print(f"{'#':>3} {'x':>7} {'y':>7} {'vx':>8} {'vy':>8} "
          f"{'|v|':>7} {'t_ref':>7} seen")
    for i, w in enumerate(wps, 1):
        print(f"{i:3d} {w['x']:7.0f} {w['y']:7.0f} {w['vx']:+8.0f} "
              f"{w['vy']:+8.0f} {w['speed']:7.0f} {w['t_ref']:7.2f} "
              f"{'yes' if w['observed'] else 'NO'}")

    unseen = [i for i, w in enumerate(wps, 1) if not w["observed"]]
    if unseen:
        print(f"  WARNING: waypoint(s) {unseen} sit on INTERPOLATED frames "
              f"- the thief was not actually seen there, the position was "
              f"filled in across a tracking gap. The agent will be waiting "
              f"for the thief to reach a place it may never have been.")

    # Waypoints too close together cannot be told apart at replay time -
    # the agent would fire both on the first match.
    warn = 0
    for i in range(len(wps) - 1):
        d = np.hypot(wps[i + 1]["x"] - wps[i]["x"],
                     wps[i + 1]["y"] - wps[i]["y"])
        if d < args.min_sep:
            print(f"  WARNING: waypoints {i + 1} and {i + 2} are only "
                  f"{d:.0f}px apart (min useful separation ~{args.min_sep})")
            warn += 1
    if warn:
        print("  Close pairs may fire together. Use --vtol at replay time "
              "if they differ in direction, or accept the risk.")

    out = args.out or "waypoints.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        json.dump({
            "source": os.path.basename(meta_path),
            "game_w": 1280,
            "game_h": 720,
            "gravity": round(float(g_full), 1),
            "waypoints": wps,
        }, f, indent=1)
    print(f"\nSaved {out}")
    print("Replay with kot_agent.py --dry-run FIRST - it fires on POSITION, "
          "not time.")


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
    # Kept identical to kot_launch.py so a run that scores well there
    # produces the same launches here. Divergent defaults between the two
    # would mean tuning against one and shipping the other.
    ap.add_argument("--fit-win", type=int, default=13, dest="fit_win")
    ap.add_argument("--fit-res", type=float, default=2.0, dest="fit_res")
    ap.add_argument("--flight-fill", type=int, default=2, dest="flight_fill")
    ap.add_argument("--contact-min", type=int, default=3, dest="contact_min")
    ap.add_argument("--max-vy", type=float, default=300, dest="max_vy",
                    help="vy at flight entry above this is a landing, not a "
                         "tap. NOTE: wall-jumps can move DOWNWARD and still "
                         "be real taps, so this must stay loose")
    ap.add_argument("--max-entry", type=float, default=600, dest="max_entry",
                    help="reject launches faster than this (end-of-level "
                         "animation reaches 1000+ px/s)")
    ap.add_argument("--approach", type=int, default=5,
                    help="frames before the launch over which the APPROACH "
                         "velocity is measured. This is the state the agent "
                         "can observe as it arrives; the post-tap velocity "
                         "is not")
    ap.add_argument("--taps-only", action="store_true", dest="taps_only",
                    help="keep only launches that match a recorded tap. "
                         "Strongly recommended for --own runs: a spurious "
                         "waypoint is an extra tap the agent will send that "
                         "you never sent")
    ap.add_argument("--match-tol", type=float, default=0.15,
                    dest="match_tol",
                    help="seconds within which a launch matches a tap")
    ap.add_argument("--expect-gravity", type=float, default=1700,
                    dest="expect_gravity",
                    help="expected FULL-RES gravity px/s^2; 0 disables the "
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