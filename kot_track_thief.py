"""
kot_track_thief.py v3.1 - locate the thief by PHYSICS, not by appearance.

THE HISTORY OF THIS FILE

v1 (kot_green.py): colour only.
    green costume -> locked onto a hanging green GEM in a volcano dungeon
    panda / white -> locked onto white SPIDER WEBS in a crypt
    Every dungeon has different decor and each breaks a different colour
    assumption. Enumerating decoys does not scale.

v2: colour AND median-background subtraction.
    Better - it removed every STATIC decoy. But it kept every MOVING one,
    by construction, then locked onto a patrolling MINION and reported
    "thief located in 97% of frames" while doing it. Fitted gravity came
    out +333 px/s^2 and the waypoints were a 60px huddle of nonsense.

    The proposed v2 fix - "pick the candidate with the largest cumulative
    displacement" - would NOT have worked. A patrolling minion translates
    as much as the thief, and more than the thief while the thief stands
    still on a platform. Motion magnitude cannot separate them.

v3: select on BALLISTICS. Track every candidate simultaneously, then pick
    the track whose vertical motion contains the most clean parabolic
    segments matching gravity.

        patrolling minion   -> constant velocity, a ~ 0
        bone on a chain     -> pendulum, a oscillates and flips sign
        spinner trap        -> rotates in place, never translates
        fading UI text      -> changes brightness, never translates
        THE THIEF           -> a = +g on every airborne segment

    Confirmed on runs/own_040008.json: a swinging bone scored 10/134
    windows, end-of-level UI and a stationary blob scored 0/117 and 0/134,
    and the thief won. The discriminator works.

v3.1 (this file): MERGE CO-LOCATED TRACKS.
    v3 selected correctly but covered only 62% of frames, because the
    white mask splits the panda sprite into several components - face plus
    smaller white patches - and each became its own hypothesis. On that
    run, tracks 3, 1, 2 and 22 all had the same x/y range and the same
    fitted gravity (+783, +889, +808, +806), and their frame spans
    OVERLAPPED. They were not sequential fragments of a lost-and-reacquired
    thief; they were different parts of it, tracked in parallel.
    --track-gap does nothing for that.

    So after selection, any track sitting within --track-merge px of the
    winner across their shared frames is absorbed, and the winner's
    missing frames are filled from it.

    The offset correction matters. Two components of one sprite are a
    fixed distance apart, so filling gaps naively would inject a ~20px
    step into the path every time the source switched - a step in position
    is a spike in velocity, and a velocity spike is exactly what the launch
    detector is hunting for. The median (dx, dy) over the overlap is
    applied before filling.

DIAGNOSTIC

--tracks prints the full candidate table: frames, extent, ballistic score,
fitted gravity, position range. When the pick is wrong, that table shows
which track it should have been. Do not debug this tracker without it.

Usage:
    python kot_track_thief.py runs/own_XXXX.json --tracks
    python kot_track_thief.py runs/own_XXXX.json --debug
    python kot_track_thief.py runs/own_XXXX.json --save-bg
    python kot_track_thief.py runs/own_XXXX.json --single    # old behaviour
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

# Full-resolution gravity. Recordings are downscaled, so the value the
# tracker actually expects is this times meta["scale"]. Deriving it means
# nobody has to remember whether 850 or 1700 applies to a given run - that
# confusion has already cost time once.
GRAVITY_FULL_RES = 1700.0


def add_detector_args(ap):
    """Shared by every script that tracks, so they cannot disagree.

    Nothing here may collide with the argument names kot_launch.py,
    kot_convert.py or kot_waypoints.py define themselves (--still, --entry,
    --gain, --gtol, --window, --refractory, --max-vy, --max-entry,
    --expect-gravity, --force, --min-sep). Duplicate argparse arguments
    from patching have broken this project three times; every new flag here
    is prefixed --track-* precisely to keep that from happening again.

    --white-v / --white-s MUST match the defaults in kot_agent.py and
    kot_live.py. They are 220/30 in all four files. They used to be 185/70
    here, and running kot_waypoints.py without passing the flags silently
    extracted waypoints using a LOOSER mask than the one the live agent
    hunts with - so the agent was sent to look for positions found by a
    different detector. Nothing warned; the numbers just quietly disagreed.
    """
    ap.add_argument("--mode", choices=["white", "hue", "any"], default="white",
                    help="white = panda face; hue = green costume; "
                         "any = motion only, no colour filter")
    ap.add_argument("--white-v", type=int, default=220, dest="white_v")
    ap.add_argument("--white-s", type=int, default=30, dest="white_s")
    ap.add_argument("--hue-lo", type=int, default=HUE_LO, dest="hue_lo")
    ap.add_argument("--hue-hi", type=int, default=HUE_HI, dest="hue_hi")
    ap.add_argument("--sat", type=int, default=SAT_MIN)
    ap.add_argument("--val", type=int, default=VAL_MIN)
    ap.add_argument("--minarea", type=int, default=12)
    ap.add_argument("--maxarea", type=int, default=900)
    ap.add_argument("--maxjump", type=float, default=40,
                    help="max px/frame for association; the gate widens by "
                         "this much per missed frame, up to --track-gate")
    ap.add_argument("--lost-limit", type=int, default=15, dest="lost_limit",
                    help="only used by --single (legacy greedy tracker)")
    ap.add_argument("--no-bg", action="store_true", dest="no_bg",
                    help="disable background subtraction (colour only)")
    ap.add_argument("--bg-thresh", type=int, default=28, dest="bg_thresh",
                    help="per-pixel difference from background to count "
                         "as moving")
    ap.add_argument("--bg-samples", type=int, default=80, dest="bg_samples",
                    help="frames sampled to build the median background")

    # ---- v3: multi-hypothesis tracking and ballistic selection ----
    ap.add_argument("--single", action="store_true",
                    help="legacy greedy single-target tracker. This is the "
                         "code that locked onto a minion; kept only for "
                         "before/after comparison")
    ap.add_argument("--max-tracks", type=int, default=25, dest="max_tracks",
                    help="max simultaneously live hypotheses")
    ap.add_argument("--track-gap", type=int, default=12, dest="track_gap",
                    help="frames a track may go unseen before it is closed. "
                         "This only helps SEQUENTIAL fragmentation; "
                         "concurrent fragments need --track-merge")
    ap.add_argument("--track-gate", type=float, default=200,
                    dest="track_gate",
                    help="hard ceiling on the association gate in px, so a "
                         "long-missing track cannot re-acquire across the "
                         "whole screen")
    ap.add_argument("--track-min", type=int, default=25, dest="track_min",
                    help="a track shorter than this many frames is not "
                         "considered - too little evidence to judge")
    ap.add_argument("--track-gravity", type=float, default=0,
                    dest="track_gravity",
                    help="expected gravity px/s^2 AT RECORDING RESOLUTION. "
                         "0 = derive from meta['scale'] (recommended)")
    ap.add_argument("--track-gtol", type=float, default=0.35,
                    dest="track_gtol",
                    help="fraction of g a fitted segment may deviate and "
                         "still count as ballistic")
    ap.add_argument("--track-win", type=int, default=13, dest="track_win",
                    help="frames per parabola fit window. 13, not 7, for a "
                         "hard SNR reason: gravity bends the path by g*T^2/2 "
                         "while the quadratic coefficient amplifies centroid "
                         "noise by 8/T^2. At 7 frames (116ms) gravity gives "
                         "5.7px of curvature and 1px of jitter costs ~590 "
                         "px/s^2 - the signal sits at the noise floor. At 13 "
                         "frames (216ms) curvature is 19.8px and the same "
                         "jitter costs ~171. Measured on own_040008: p5/p95 "
                         "went from -3491/+2664 to -330/+962")
    ap.add_argument("--track-res", type=float, default=2.0, dest="track_res",
                    help="max RMS residual (px) for a window to count as a "
                         "clean parabola at all")
    ap.add_argument("--pick", choices=["ballistic", "frames", "extent"],
                    default="ballistic",
                    help="how to choose the winning track. ballistic is the "
                         "whole point; the others are for diagnosis")

    # ---- v3.1: merging concurrent fragments of one sprite ----
    ap.add_argument("--track-merge", type=float, default=25,
                    dest="track_merge",
                    help="absorb tracks whose median distance from the "
                         "winner over shared frames is under this many px. "
                         "0 disables merging. Roughly the size of the "
                         "sprite at recording resolution")
    ap.add_argument("--track-merge-min", type=int, default=10,
                    dest="track_merge_min",
                    help="shared frames required before a merge is even "
                         "considered - a brief coincidence is not evidence "
                         "that two blobs are the same object")

    ap.add_argument("--track-bridge", type=int, default=0,
                    dest="track_bridge",
                    help="stitch a track that begins within this many "
                         "frames of where another ended. OFF BY DEFAULT, "
                         "and it should stay off unless you have looked at "
                         "the debug PNGs first. It joins tracks on purely "
                         "circumstantial evidence - adjacent in time, close "
                         "endpoints, similar blob area - with no shared "
                         "frame to verify against. On own_040008 the thief "
                         "and two coin-burst blobs had areas 296, 292 and "
                         "294, so this rule was one timing coincidence away "
                         "from splicing a coin's trajectory onto the "
                         "thief's and producing waypoints that looked "
                         "perfectly reasonable and were fiction")
    ap.add_argument("--from-t", type=float, default=0.0, dest="from_t",
                    help="ignore everything before this time (s). Recordings "
                         "usually start on the TAP TO BREAK IN / TAP TO "
                         "START screens, where there is NO THIEF - only "
                         "decorations for the tracker to follow, and menu "
                         "taps that no launch can ever correspond to. On "
                         "own_040008 that was the first 3.6s, and it "
                         "contaminated the track scoring, the gravity fit "
                         "and the speed percentiles alike")
    ap.add_argument("--to-t", type=float, default=0.0, dest="to_t",
                    help="ignore everything after this time (s). 0 = to the "
                         "end. The level-completion animation moves fast "
                         "enough to be mistaken for gameplay")


# ----------------------------------------------------------------- loading

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
    silently corrupts every timing figure derived from it.
    """
    t = meta.get("times")
    if t and len(t) == meta["frames"]:
        return np.array(t, dtype=float)
    print("  WARNING: no per-frame timestamps; assuming even spacing. "
          "Timing results will be unreliable.")
    return np.linspace(0, meta["duration"], meta["frames"])


def expected_gravity(meta, args):
    if args.track_gravity > 0:
        return float(args.track_gravity)
    return GRAVITY_FULL_RES * float(meta.get("scale", 1.0))


def frame_range(meta, args):
    """Frames to analyse, honouring --from-t / --to-t.

    Everything - background, tracking, scoring - is restricted to this
    span. A recording that opens on a menu is not merely useless at the
    front; it is actively harmful, because the tracker will happily follow
    a decoration for three seconds and that stretch then counts toward the
    ballistic score of whatever it later becomes.
    """
    t = frame_times(meta)
    n = meta["frames"]
    f = float(getattr(args, "from_t", 0.0) or 0.0)
    e = float(getattr(args, "to_t", 0.0) or 0.0)
    lo = int(np.searchsorted(t, f)) if f > 0 else 0
    hi = int(np.searchsorted(t, e)) if e > 0 else n
    lo = max(0, min(lo, n - 1))
    hi = max(lo + 1, min(hi, n))
    return lo, hi


# -------------------------------------------------------------- perception

def build_background(frames, meta, samples, lo=None, hi=None):
    """Per-pixel median over sampled frames = everything that doesn't move.

    Sampled from [lo, hi) so a recording that opens on a menu screen does
    not put the menu into the background of the gameplay it precedes.

    Median rather than mean: a mean is dragged by the thief passing
    through, a median ignores it as long as the thief occupies any given
    pixel for a minority of the sampled frames.

    LIMITATION, now understood to be fundamental: this removes STATIC
    decor only. A patrolling minion, a bone swinging on a chain and a
    fading "TAP TO BREAK IN" caption all survive it, because all of them
    change. That is what ballistic scoring below is for.
    """
    lo = 0 if lo is None else lo
    hi = meta["frames"] if hi is None else hi
    span = max(1, hi - lo)
    idx = (lo + np.linspace(0, span - 1, min(samples, span))).astype(int)
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


def candidates(mask, args):
    """Every blob in the size window. NOT a decision - just the shortlist."""
    nl, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for j in range(1, nl):
        a = int(stats[j, cv2.CC_STAT_AREA])
        if args.minarea <= a <= args.maxarea:
            out.append((float(cents[j][0]), float(cents[j][1]), a))
    return out


# --------------------------------------------------------------- hypotheses

class Track:
    """One hypothesis about what the thief might be.

    Deliberately dumb: it stores observations and predicts forward at
    constant velocity. All the intelligence is in the scoring, which
    happens once, at the end, when the whole run is available.
    """

    __slots__ = ("id", "i", "t", "x", "y", "a", "missed")

    def __init__(self, tid, i, t, x, y, a):
        self.id = tid
        self.i = [i]
        self.t = [t]
        self.x = [x]
        self.y = [y]
        self.a = [a]
        self.missed = 0

    def add(self, i, t, x, y, a):
        self.i.append(i)
        self.t.append(t)
        self.x.append(x)
        self.y.append(y)
        self.a.append(a)
        self.missed = 0

    def predict(self, t):
        """Where this track should be at time t.

        Predicting rather than using the last seen position is what lets a
        track survive a dropout. The v2 tracker compared against the last
        SEEN position, so a thief that vanished for four frames mid-jump
        reappeared 150px away, failed the gate, and was abandoned - then
        re-acquired as a brand new track.
        """
        if len(self.t) < 2:
            return self.x[-1], self.y[-1]
        dt = self.t[-1] - self.t[-2]
        if dt <= 1e-6:
            return self.x[-1], self.y[-1]
        vx = float(np.clip((self.x[-1] - self.x[-2]) / dt, -2000, 2000))
        vy = float(np.clip((self.y[-1] - self.y[-2]) / dt, -2000, 2000))
        h = t - self.t[-1]
        return self.x[-1] + vx * h, self.y[-1] + vy * h


def track_multi(meta, frames, args, bg=None):
    """Run every hypothesis to the end of the run. No selection here."""
    h = meta["height"]
    y0, y1 = int(h * CROP_TOP), int(h * CROP_BOTTOM)
    times = frame_times(meta)

    lo, hi = frame_range(meta, args)
    if bg is None and not args.no_bg:
        bg = build_background(frames, meta, args.bg_samples, lo, hi)
    bg_roi = None if bg is None else bg[y0:y1]

    live, done = [], []
    next_id = 0

    for i in range(lo, hi):
        t = times[i]
        roi = np.array(frames[i][y0:y1])
        cands = candidates(thief_mask(roi, bg_roi, args), args)
        # ROI coords -> game coords. Constant offset, so it does not touch
        # the acceleration fit, but it must be right for the waypoints.
        cands = [(c[0], c[1] + y0, c[2]) for c in cands]

        # Global greedy association by distance. Not Hungarian - with <25
        # tracks and a handful of blobs the difference never shows, and
        # this stays readable.
        pairs = []
        for ti, tr in enumerate(live):
            px, py = tr.predict(t)
            gate = min(args.maxjump * (tr.missed + 1), args.track_gate)
            for ci, c in enumerate(cands):
                d = float(np.hypot(c[0] - px, c[1] - py))
                if d <= gate:
                    pairs.append((d, ti, ci))
        pairs.sort()

        used_t, used_c = set(), set()
        for d, ti, ci in pairs:
            if ti in used_t or ci in used_c:
                continue
            c = cands[ci]
            live[ti].add(i, t, c[0], c[1], c[2])
            used_t.add(ti)
            used_c.add(ci)

        for ti, tr in enumerate(live):
            if ti not in used_t:
                tr.missed += 1

        for ci, c in enumerate(cands):
            if ci not in used_c and len(live) < args.max_tracks:
                live.append(Track(next_id, i, t, c[0], c[1], c[2]))
                next_id += 1

        still_live = []
        for tr in live:
            if tr.missed > args.track_gap:
                done.append(tr)
            else:
                still_live.append(tr)
        live = still_live

    done.extend(live)
    return done


# ----------------------------------------------------------------- scoring

def _parabola(t, y):
    """Fit y = a/2 t^2 + b t + c. Returns (a, rms residual).

    a is the vertical acceleration over the window. For the thief in
    flight it is gravity. For anything on a rail or a track it is zero.
    For a pendulum it swings through zero and changes sign.
    """
    c = np.polyfit(t, y, 2)
    pred = np.polyval(c, t)
    return float(2.0 * c[0]), float(np.sqrt(np.mean((y - pred) ** 2)))


def ballistic_score(tr, g, args):
    """How much of this track looks like free fall under gravity g.

    Sliding a short window along the track and fitting a parabola to each
    is far more robust than differentiating twice and taking a histogram
    mode. Two numerical derivatives on a jittery centroid produce noise
    with the same magnitude as the signal; a least-squares fit over seven
    frames does not.

    Windows that do not fit a parabola cleanly at all are discarded rather
    than counted as failures - a window straddling a bounce is genuinely
    not a single ballistic segment and should not be evidence either way.

    A real thief scores 12-15%: most of a run is spent running along
    surfaces, not falling. A high fraction is not the goal; being the
    HIGHEST is.
    """
    t = np.asarray(tr.t)
    y = np.asarray(tr.y)
    w = args.track_win
    if len(t) < w:
        return 0, 0, []

    good, total, accs = 0, 0, []
    for i in range(len(t) - w + 1):
        tt = t[i:i + w]
        yy = y[i:i + w]
        # Frames must be contiguous in time. A window spanning a dropout
        # covers an unobserved bounce and would fit a meaningless curve.
        if tt[-1] - tt[0] > 0.05 * w:
            continue
        a, res = _parabola(tt - tt[0], yy)
        if res > args.track_res:
            continue
        total += 1
        if abs(a - g) <= args.track_gtol * g:
            good += 1
            accs.append(a)
    return good, total, accs


def score_tracks(tracks, meta, args):
    g = expected_gravity(meta, args)
    rows = []
    for tr in tracks:
        n = len(tr.i)
        if n < args.track_min:
            continue
        x = np.asarray(tr.x)
        y = np.asarray(tr.y)
        good, total, accs = ballistic_score(tr, g, args)
        rows.append({
            "id": tr.id,
            "track": tr,
            "frames": n,
            "first": tr.i[0],
            "last": tr.i[-1],
            "extent": float(np.hypot(x.max() - x.min(), y.max() - y.min())),
            "xr": (float(x.min()), float(x.max())),
            "yr": (float(y.min()), float(y.max())),
            "good": good,
            "total": total,
            "frac": (good / total) if total else 0.0,
            "g_fit": float(np.median(accs)) if accs else 0.0,
            "area": float(np.median(tr.a)),
        })

    key = {"ballistic": lambda r: (r["good"], r["extent"]),
           "frames": lambda r: r["frames"],
           "extent": lambda r: r["extent"]}[args.pick]
    rows.sort(key=key, reverse=True)
    return rows, g


def print_tracks(rows, g, limit=12):
    print(f"\ncandidate tracks (expected gravity {g:+.0f} px/s^2 at this "
          f"resolution)")
    print(f"{'id':>4} {'frames':>7} {'first':>6} {'last':>6} {'extent':>7} "
          f"{'ballistic':>11} {'g_fit':>7} {'area':>6}  "
          f"{'x range':>13} {'y range':>13}")
    for r in rows[:limit]:
        print(f"{r['id']:4d} {r['frames']:7d} {r['first']:6d} {r['last']:6d} "
              f"{r['extent']:7.0f} {r['good']:5d}/{r['total']:<5d} "
              f"{r['g_fit']:+7.0f} {r['area']:6.0f}  "
              f"{r['xr'][0]:5.0f}-{r['xr'][1]:<7.0f} "
              f"{r['yr'][0]:5.0f}-{r['yr'][1]:<7.0f}")
    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more")
    print("\n  ballistic = windows matching gravity / windows that fit a "
          "parabola at all.")
    print("  A patrolling minion fits parabolas perfectly with g_fit ~ 0.")
    print("  A pendulum (bone on a chain) scores a few percent by accident.")
    print("  Only the thief falls.")


# ------------------------------------------------------------------ merging

def _frames_of(tr):
    return {i: (x, y) for i, x, y in zip(tr.i, tr.x, tr.y)}


def _alignment(base, other):
    """Median offset, distance and offset SPREAD between two frame->pos maps.

    Returns (n_common, median_distance, (dx, dy), spread).

    The offset is what makes merging safe: two components of one sprite sit
    a fixed distance apart, so filling gaps without correcting for it would
    inject a step into the path every time the source changed - and a step
    in position is a spike in velocity, which is precisely what the launch
    detector looks for.

    The SPREAD is what makes merging correct. Two parts of one sprite keep
    a near-constant offset, so the spread is small. A static blob that the
    thief happens to pass has an offset that sweeps through every value,
    so the spread is huge - even though the median distance over those few
    frames may look small. Spread separates "same object" from "briefly in
    the same place" far better than counting frames does.
    """
    common = set(base) & set(other)
    if not common:
        return 0, None, (0.0, 0.0), None
    dx = np.array([base[i][0] - other[i][0] for i in common])
    dy = np.array([base[i][1] - other[i][1] for i in common])
    spread = float(np.std(dx) + np.std(dy))
    return (len(common), float(np.median(np.hypot(dx, dy))),
            (float(np.median(dx)), float(np.median(dy))), spread)


def _bridge(merged, tr, args, area_ref, area_other):
    """Can `tr` be stitched onto the end (or front) of `merged` in time?

    Overlap-based merging cannot join tracks that never coexist, and the
    thief fragments that way constantly: one component dies on frame 825,
    its replacement is born on 826. No shared frame means no computable
    offset, so those two can never be related by position alone.

    A bridge is allowed only when all three of these hold, because any one
    of them alone would stitch the wrong things together:
      - the tracks are consecutive within --track-bridge frames
      - the spatial jump between the endpoints is one plausible frame of
        motion, not a teleport across the dungeon
      - the blobs are a similar size, so a 300px sprite is not spliced
        onto a 20px spark
    """
    if getattr(args, "track_bridge", 0) <= 0 or not merged:
        return None
    if area_ref <= 0 or not (0.5 <= area_other / area_ref <= 2.0):
        return None

    mi, ma = min(merged), max(merged)
    fi, fa = tr.i[0], tr.i[-1]
    if fi > ma:
        gap, p0, p1 = fi - ma, merged[ma], (tr.x[0], tr.y[0])
    elif fa < mi:
        gap, p0, p1 = mi - fa, merged[mi], (tr.x[-1], tr.y[-1])
    else:
        return None
    if gap > args.track_bridge:
        return None

    d = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
    if d > min(args.maxjump * max(gap, 1), args.track_gate):
        return None
    return gap, d


def merge_into(best_row, rows, args):
    """Absorb tracks that are really the same object as the winner.

    Concurrent fragmentation is the normal case, not the exception: the
    white mask splits the panda into face plus smaller patches, so one
    sprite becomes three or four hypotheses with identical trajectories.
    They overlap in TIME, so no gap setting merges them - they have to be
    recognised by being in the same PLACE.
    """
    merged = _frames_of(best_row["track"])
    absorbed = []
    if args.track_merge <= 0:
        return merged, absorbed

    pool = [r for r in rows if r["id"] != best_row["id"]]
    area_ref = max(best_row["area"], 1.0)
    # Repeat passes: a fragment may not overlap or adjoin the winner
    # directly, but may reach something already absorbed. The thief on
    # own_040008 needed two hops - 0 -> 5 -> 6 - to span the whole level.
    for _ in range(4):
        gained = False
        for r in list(pool):
            other = _frames_of(r["track"])
            n, dist, (dx, dy), spread = _alignment(merged, other)

            how = None
            if dist is not None and n >= args.track_merge_min:
                # Same object seen twice at once: close together AND
                # keeping a steady offset while both move.
                if dist <= args.track_merge and spread <= args.track_merge:
                    how = f"overlap n={n} d={dist:.1f} spread={spread:.1f}"
            if how is None:
                b = _bridge(merged, r["track"], args, area_ref, r["area"])
                if b is not None:
                    dx = dy = 0.0
                    how = f"bridge gap={b[0]}f jump={b[1]:.0f}px"

            if how is None:
                continue

            added = 0
            for i, (x, y) in other.items():
                if i not in merged:
                    merged[i] = (x + dx, y + dy)
                    added += 1
            absorbed.append((r["id"], n, dist, dx, dy, added, how))
            pool.remove(r)
            gained = True
        if not gained:
            break

    return merged, absorbed


def explain_rejections(merged, best_row, rows, absorbed_ids, args):
    """Say WHY every track that was not absorbed was not absorbed.

    A merge that silently declines looks identical to a merge that was
    never needed - coverage is simply lower than it should be and nothing
    says so. This project has lost time to that class of failure more than
    once, so the refusal gets printed with its numbers.
    """
    skip = absorbed_ids | {best_row["id"]}
    others = [r for r in rows if r["id"] not in skip]
    if not others:
        return
    print(f"\nnot merged (winner spans frames "
          f"{min(merged)}-{max(merged)}):")
    print(f"{'id':>4} {'span':>12} {'shared':>7} {'dist':>7} {'spread':>7} "
          f"{'area':>6}  reason")
    for r in others:
        tr = r["track"]
        n, dist, _, spread = _alignment(merged, _frames_of(tr))
        b = _bridge(merged, tr, args, max(best_row["area"], 1.0), r["area"])
        span = f"{tr.i[0]}-{tr.i[-1]}"
        if n == 0:
            why = ("bridged" if b else
                   f"no shared frames; bridge rejected "
                   f"(gap or jump too large, or area mismatch)")
            ds, ss = f"{'-':>7}", f"{'-':>7}"
        else:
            ds, ss = f"{dist:7.1f}", f"{spread:7.1f}"
            if dist > args.track_merge:
                why = (f"coexists but {dist:.0f}px apart - a DIFFERENT "
                       f"object, not a fragment of this one")
            elif spread > args.track_merge:
                why = (f"close but the offset wanders (spread "
                       f"{spread:.0f}px) - passing by, not attached")
            elif n < args.track_merge_min:
                why = f"only {n} shared frames, need {args.track_merge_min}"
            else:
                why = "should have merged - report this"
        print(f"{r['id']:4d} {span:>12} {n:7d} {ds} {ss} {r['area']:6.0f}  "
              f"{why}")

    print("\n  A track that COEXISTS with the winner in a different place "
          "is a different object. If it also scores well ballistically and "
          "carries on after the winner dies, the winner may have been lost "
          "and this may be the thief - check the debug PNGs around the "
          "handover before merging anything.")


def path_from_frames(fmap, meta):
    """frame->pos map -> the (t, x, y) array the rest of the project expects.

    Frames with no observation stay NaN. kot_launch.kinematics interpolates
    across them, which is correct: an unobserved frame is missing data, not
    a position of zero.
    """
    times = frame_times(meta)
    path = np.full((meta["frames"], 3), np.nan)
    path[:, 0] = times
    for i, (x, y) in fmap.items():
        if 0 <= i < meta["frames"]:
            path[i, 1] = x
            path[i, 2] = y
    return path


# ------------------------------------------------------- legacy greedy path

def track_greedy(meta, frames, args, bg=None):
    """The v2 tracker, verbatim. This is the code that followed a minion
    for a whole run while reporting 97% located. Kept so the improvement
    can be measured rather than asserted."""
    h = meta["height"]
    y0, y1 = int(h * CROP_TOP), int(h * CROP_BOTTOM)
    times = frame_times(meta)

    lo, hi = frame_range(meta, args)
    if bg is None and not args.no_bg:
        bg = build_background(frames, meta, args.bg_samples, lo, hi)
    bg_roi = None if bg is None else bg[y0:y1]

    path, last, lost = [], None, 0

    for i in range(meta["frames"]):
        if not (lo <= i < hi):
            path.append((times[i], np.nan, np.nan))
            continue
        roi = np.array(frames[i][y0:y1])
        cands = candidates(thief_mask(roi, bg_roi, args), args)

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
                    lost += 1
                    if lost > args.lost_limit:
                        last, lost = None, 0

        if pick is None:
            path.append((times[i], np.nan, np.nan))
        else:
            last = (pick[0], pick[1])
            path.append((times[i], pick[0], pick[1] + y0))

    return np.array(path)


# -------------------------------------------------------------- public API

def track(meta, frames, args, bg=None, verbose=True):
    """Locate the thief. Signature and return shape unchanged from v2, so
    kot_launch, kot_convert and kot_waypoints need no edits."""
    if getattr(args, "single", False):
        return track_greedy(meta, frames, args, bg)

    tracks = track_multi(meta, frames, args, bg)
    rows, g = score_tracks(tracks, meta, args)

    if not rows:
        if verbose:
            print(f"  no track survived --track-min {args.track_min} frames. "
                  f"Either nothing was detected, or detection is so "
                  f"intermittent that no hypothesis lived long enough.")
        return path_from_frames({}, meta)

    best = rows[0]
    merged, absorbed = merge_into(best, rows, args)

    if verbose:
        own = len(best["track"].i)
        print(f"  picked track {best['id']}: {own} frames, "
              f"{best['good']}/{best['total']} ballistic windows, "
              f"g_fit {best['g_fit']:+.0f} (expected {g:+.0f})")
        if absorbed:
            ids = ", ".join(str(a[0]) for a in absorbed)
            print(f"  merged co-located tracks {ids} -> "
                  f"{len(merged)}/{meta['frames']} frames "
                  f"({100 * len(merged) / meta['frames']:.0f}%)")
        if best["good"] < 3:
            print("  *** WARNING: the winning track barely falls. No object "
                  "in this run behaves like the thief. Treat the output as "
                  "unreliable and inspect --tracks. ***")

    return path_from_frames(merged, meta)


# ------------------------------------------------------------------- extras

def calibrate(meta, frames, args):
    h = meta["height"]
    y0, y1 = int(h * CROP_TOP), int(h * CROP_BOTTOM)

    bg = None if args.no_bg else build_background(frames, meta,
                                                  args.bg_samples)
    bg_roi = None if bg is None else bg[y0:y1]

    n = meta["frames"]
    step = max(1, n // 200)
    colour_only, combined, blobs = [], [], []
    for i in range(0, n, step):
        roi = np.array(frames[i][y0:y1])
        colour_only.append(int(colour_mask(roi, args).sum() // 255))
        m = thief_mask(roi, bg_roi, args)
        combined.append(int(m.sum() // 255))
        blobs.append(len(candidates(m, args)))

    c = np.array(colour_only)
    k = np.array(combined)
    b = np.array(blobs)
    print(f"mode: {args.mode}   background: {'off' if args.no_bg else 'on'}")
    print(f"colour mask alone : median {np.median(c):7.0f}  "
          f"p90 {np.percentile(c, 90):7.0f}")
    print(f"after background  : median {np.median(k):7.0f}  "
          f"p90 {np.percentile(k, 90):7.0f}")
    print(f"candidate blobs   : median {np.median(b):7.1f}  "
          f"max {b.max():4d}")
    print(f"frames with any mask: {(k > 0).sum()}/{len(k)}")

    if np.median(k) < 8:
        print("\n  -> almost nothing survives. Either the thief barely "
              "moves in this run, or --bg-thresh is too high.")
    elif np.median(b) > 6:
        print("\n  -> many moving candidates per frame. That is survivable "
              "now (they become tracks and get scored), but it costs time. "
              "Tighten the colour thresholds if it is slow.")
    else:
        print("\n  -> plausible. A pixel count still cannot tell you WHICH "
              "object was found. Confirm with --tracks.")


def write_debug(meta, frames, rows, best_id, absorbed_ids, args):
    out = os.path.splitext(args.meta)[0] + "_debug"
    os.makedirs(out, exist_ok=True)

    # Per-frame lookup for every scored track, so the PNGs show the
    # competition, not just the winner. Seeing the rejected tracks is how
    # you tell "picked the wrong one" from "never saw the right one".
    per_frame = {}
    for r in rows:
        tr = r["track"]
        for i, x, y in zip(tr.i, tr.x, tr.y):
            per_frame.setdefault(i, []).append((r["id"], x, y))

    for i in range(0, meta["frames"], args.debug_every):
        img = np.array(frames[i]).copy()
        for tid, x, y in per_frame.get(i, []):
            if tid == best_id:
                col, rad = (0, 0, 255), 12          # red   - picked
            elif tid in absorbed_ids:
                col, rad = (0, 255, 0), 9           # green - merged in
            else:
                col, rad = (0, 200, 255), 7         # amber - rejected
            cv2.circle(img, (int(x), int(y)), rad, col, 2)
            cv2.putText(img, str(tid), (int(x) + 12, int(y) - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1)
        cv2.imwrite(os.path.join(out, f"d{i:05d}.png"), img)
    print(f"\nDebug PNGs in {out}/")
    print("  red = picked   green = merged into it   amber = rejected")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meta")
    add_detector_args(ap)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--tracks", action="store_true",
                    help="print the candidate track table - the single most "
                         "useful diagnostic in this project")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--debug-every", type=int, default=20, dest="debug_every")
    ap.add_argument("--save-bg", action="store_true", dest="save_bg",
                    help="write the median background as a PNG to inspect")
    args = ap.parse_args()

    meta, frames = load(args.meta)
    print(f"{meta['mode']} run: {meta['frames']} frames, {meta['duration']}s, "
          f"{len(meta['taps'])} taps, scale {meta.get('scale', 1)}")

    if args.save_bg:
        bg = build_background(frames, meta, args.bg_samples)
        p = os.path.splitext(args.meta)[0] + "_bg.png"
        cv2.imwrite(p, bg)
        print(f"background written to {p}")
        print("Static decor should be visible; the thief should NOT be. "
              "Note that moving decor - patrolling minions, swinging bones, "
              "fading captions - will be smeared or absent, and those are "
              "exactly the objects that survive to compete with the thief.")
        return

    if args.calibrate:
        calibrate(meta, frames, args)
        return

    if args.single:
        path = track_greedy(meta, frames, args)
        found = int((~np.isnan(path[:, 1])).sum())
        print(f"[legacy greedy] located in {found}/{meta['frames']} frames "
              f"({100 * found / meta['frames']:.0f}%)")
        print("  Reminder: this number says a blob was found. It does not "
              "say the blob was the thief.")
        return

    lo, hi = frame_range(meta, args)
    if (lo, hi) != (0, meta["frames"]):
        tt = frame_times(meta)
        print(f"analysing frames {lo}-{hi} "
              f"({tt[lo]:.2f}s to {tt[min(hi, meta['frames'] - 1)]:.2f}s) "
              f"of {meta['frames']}")

    bg = None if args.no_bg else build_background(frames, meta,
                                                  args.bg_samples, lo, hi)
    tracks = track_multi(meta, frames, args, bg)
    rows, g = score_tracks(tracks, meta, args)
    print(f"{len(tracks)} hypotheses, {len(rows)} long enough to judge "
          f"(>= {args.track_min} frames)")

    if not rows:
        print("Nothing to score. Check --calibrate.")
        return

    if args.tracks or args.debug:
        print_tracks(rows, g)

    best = rows[0]
    merged, absorbed = merge_into(best, rows, args)
    own = len(best["track"].i)

    print(f"\npicked track {best['id']}: {own}/{meta['frames']} frames on "
          f"its own ({100 * own / meta['frames']:.0f}%), "
          f"g_fit {best['g_fit']:+.0f} vs expected {g:+.0f}")

    if absorbed:
        print(f"\nmerged {len(absorbed)} track(s) into the winner - same "
              f"object, split by the mask or handed off in time:")
        print(f"{'id':>4} {'dx':>7} {'dy':>7} {'added':>6}  how")
        for tid, n, dist, dx, dy, added, how in absorbed:
            print(f"{tid:4d} {dx:+7.1f} {dy:+7.1f} {added:6d}  {how}")

    print(f"\ncoverage after merge: {len(merged)}/{meta['frames']} frames "
          f"({100 * len(merged) / meta['frames']:.0f}%)")

    if args.tracks or args.debug:
        explain_rejections(merged, best, rows, {a[0] for a in absorbed}, args)

    if best["good"] < 3:
        print("\n  *** WARNING: nothing in this run falls like the thief. ***")
        print("  Most likely the thief was never detected at all - check the "
              "colour thresholds, or the background may have the thief "
              "baked into it.")

    rest = [r for r in rows[1:] if r["id"] not in {a[0] for a in absorbed}]
    if rest and rest[0]["good"] >= best["good"] * 0.8:
        print(f"\n  NOTE: track {rest[0]['id']} scored nearly as well "
              f"({rest[0]['good']} vs {best['good']}) and was NOT merged. "
              f"Check the debug PNGs - if it is on the thief, raise "
              f"--track-merge; if it is elsewhere, something else in this "
              f"dungeon falls.")

    if args.debug:
        write_debug(meta, frames, rows, best["id"],
                    {a[0] for a in absorbed}, args)


# NOTE FOR kot_agent / kot_live
#
# None of this transfers directly to the live agent: ballistic scoring
# needs the whole run, and the agent has to commit at frame 0. The live
# equivalent is an ACQUISITION PHASE - track all candidates for the first
# ~0.7s after the run starts, then lock the one whose y(t) fits a parabola
# near g, and only then begin matching waypoints. Cheap, because it runs
# once per run rather than per frame. Not built yet.
#
# The live agent also needs the merge idea in a simpler form: if the mask
# splits the sprite, biggest_blob() will follow whichever component happens
# to be largest that frame, jittering position by ~20px between frames.
# Taking the centroid of all candidate blobs within one sprite-width of the
# last position is probably enough, and costs nothing.

if __name__ == "__main__":
    main()