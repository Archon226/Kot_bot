"""
kot_launch.py v2.1 - detect taps as LAUNCHES, not as upward velocity.

Why the vy detector failed (v1):

    vy < -threshold  finds vertical jumps only.

But KoT's thief wall-jumps, and a wall-jump is mostly HORIZONTAL - no
vertical velocity spike, so no threshold could ever catch it. Meanwhile
bounces and landings DO spike vy upward with no tap behind them.

Replacement: a tap is what LAUNCHES the thief from contact into free
flight. That catches wall-jumps and vertical jumps equally.

--------------------------------------------------------------------------
v2 - GRAVITY IS FITTED, NOT BINNED

The old estimate_gravity() took np.gradient of a smoothed vy, itself
computed from a smoothed y, then took a histogram mode. Two moving
averages ahead of two numerical derivatives flattens the acceleration
plateau of a short flight segment, and interpolation across tracking gaps
contributes exact zeros.

On runs/own_040008.json it returned +514 where the real value is ~850.
That is not cosmetic: flight is |ay - g| < gtol*g, so g=514 with gtol=0.4
accepts 308..720 - a window that EXCLUDES real gravity. The detector was
hunting for launches into a state defined so as to exclude the actual
airborne state. A sliding parabola fit returns +800 on the same path.

--------------------------------------------------------------------------
v2.1 - ONE FALL IS ONE EVENT

Observed on the same run:

    12.969  vy +207   13.111  vy +207   13.322  vy +208   13.470  vy +200

Four "launches" in half a second with near-identical velocity, one tap
between them. That is a single descent counted four times.

The cause is in the state definition:

    in_contact = (speed < still) | (~in_flight & speed < entry)

While falling at ~150 px/s the thief is under --entry, so the moment
in_flight flickers false for ONE frame - a residual spike, a frame where
the fit is not finite - that frame reads as contact, and the flight frames
after it read as a fresh launch.

Two fixes, both about not believing single frames:

  1. Short holes in the flight mask are filled (--flight-fill). A one-frame
     interruption in the middle of a ballistic arc is a classification
     artefact, not a landing.
  2. A launch must come out of SUSTAINED contact (--contact-min frames).
     Real contact lasts: the thief lands, sits, clings, runs. A single
     contact frame between two flight frames is noise.

--------------------------------------------------------------------------
SCORING IS HONEST ABOUT MID-AIR TAPS

KoT IGNORES TAPS MADE MID-AIR, so the tap log and what the game did are
different things and raw recall was never a fair score - a human taps more
often than the game responds. Every tap is now reported with the thief's
state at that instant. A tap made airborne that produced no launch is a
CORRECT miss. A tap made in contact that produced no launch is a real
failure. Only the second is worth tuning against.

Usage:
    python kot_launch.py runs/own_XXXX.json
    python kot_launch.py runs/own_XXXX.json --gravity-scan
    python kot_launch.py runs/own_XXXX.json --timeline 0 --timeline-end 4
    python kot_launch.py runs/own_XXXX.json --still 40 --entry 200
"""

import argparse

import numpy as np

import kot_track_thief as kg


# ------------------------------------------------------------- kinematics

def smooth(a, n=5):
    """Moving average. Centroid jitter of a pixel or two becomes huge
    acceleration noise after two derivatives, so smoothing is not optional
    for the velocity path. The ACCELERATION path no longer goes through
    here at all - see local_accel()."""
    k = np.ones(n) / n
    return np.convolve(a, k, mode="same")


def prepare(path, quiet=False):
    """Clean a tracked path into usable arrays.

    Returns (t, x, y, observed) where x and y are interpolated across gaps
    and `observed` marks the frames actually seen. Anything scoring tap
    times needs to know which samples are real and which were filled in.
    """
    t, x, y = path[:, 0], path[:, 1], path[:, 2]
    ok = ~np.isnan(x)
    if ok.sum() < 20:
        raise SystemExit("Tracking too sparse to analyse.")

    # Duplicate or non-increasing timestamps make np.gradient divide by
    # zero and poison every derivative downstream.
    keep = np.concatenate(([True], np.diff(t) > 1e-6))
    if not keep.all():
        if not quiet:
            print(f"  dropped {int((~keep).sum())} duplicate timestamps")
        t, x, y, ok = t[keep], x[keep], y[keep], ok[keep]

    # Trim to the observed span. np.interp CLAMPS outside its range, so
    # leading and trailing gaps would otherwise become long stretches of a
    # perfectly constant position - speed 0, acceleration 0, indefinitely.
    # That is invented data, and it lands in every percentile and histogram
    # as though it were measurement. The 155-frame tail of own_040008 was
    # doing exactly this.
    first, last = int(np.argmax(ok)), int(len(ok) - 1 - np.argmax(ok[::-1]))
    if (first, last) != (0, len(ok) - 1) and not quiet:
        print(f"  trimmed to observed span: frames {first}-{last} "
              f"({t[first]:.2f}s to {t[last]:.2f}s)")
    t, x, y, ok = (t[first:last + 1], x[first:last + 1],
                   y[first:last + 1], ok[first:last + 1])

    xi = np.interp(t, t[ok], x[ok])
    yi = np.interp(t, t[ok], y[ok])
    return t, xi, yi, ok


def kinematics(path):
    """Position -> velocity -> acceleration, on real timestamps.

    Signature unchanged from v1 so kot_convert and kot_waypoints keep
    working. `ay` is the old double-gradient value, retained for
    --gravity-scan and backwards compatibility; launch detection no longer
    relies on it.
    """
    t, x, y, ok = prepare(path)

    xs, ys = smooth(x), smooth(y)
    vx = np.gradient(xs, t)
    vy = np.gradient(ys, t)

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


def local_accel(t, y, win=7, max_gap=0.05):
    """Vertical acceleration by sliding parabola fit.

    Fitting is far better conditioned than differentiating twice: it uses
    all `win` samples at once rather than compounding per-sample jitter.
    The residual is a bonus - it says when the window is NOT one clean
    ballistic arc, which no amount of smoothing can.

    Fitted on the RAW interpolated y. Smoothing first and fitting second
    attenuates the very quantity being measured.
    """
    n = len(t)
    a = np.full(n, np.nan)
    r = np.full(n, np.nan)
    h = win // 2
    for i in range(h, n - h):
        tt = t[i - h:i + h + 1]
        yy = y[i - h:i + h + 1]
        if tt[-1] - tt[0] > max_gap * win:
            continue          # frames not contiguous; the fit is meaningless
        tau = tt - tt[h]
        c = np.polyfit(tau, yy, 2)
        a[i] = 2.0 * c[0]
        r[i] = float(np.sqrt(np.mean((yy - np.polyval(c, tau)) ** 2)))
    return a, r


def fit_gravity(a_fit, res, prior, max_res=2.0, band=0.45):
    """Gravity from the clean fitted accelerations near a known prior.

    Using the game constant as a PRIOR rather than rediscovering it every
    run is deliberate. KoT's gravity does not vary; what varies is how much
    of a run is airborne. Re-deriving a physical constant from a noisy
    17-second sample and then defining flight in terms of that estimate is
    how v1 talked itself into 514.

    The tracker has already confirmed the tracked object obeys this
    constant - that is how it was selected - so anchoring here is
    consistent, not circular.
    """
    ok = np.isfinite(a_fit) & np.isfinite(res) & (res < max_res)
    cand = a_fit[ok]
    if len(cand) == 0:
        return float(prior), 0, "prior (no clean fits)"

    near = cand[np.abs(cand - prior) <= band * prior]
    if len(near) >= 20:
        return float(np.median(near)), len(near), "fitted"

    pos = cand[cand > 0]
    if len(pos) >= 20:
        return float(np.median(pos)), len(pos), "fallback (NOTHING near prior)"
    return float(prior), len(near), "prior (too few samples)"


# ------------------------------------------------------------------ states

def fill_holes(mask, n):
    """Fill runs of False shorter than or equal to n, between Trues.

    A ballistic arc does not stop being ballistic for one frame. Without
    this, a single bad fit inside a fall splits it into two flights with a
    fictitious 'contact' between them - and that fiction is then detected
    as a launch.
    """
    if n <= 0:
        return mask
    out = mask.copy()
    i = 0
    N = len(mask)
    while i < N:
        if not out[i]:
            j = i
            while j < N and not out[j]:
                j += 1
            if 0 < i and j < N and (j - i) <= n:
                out[i:j] = True
            i = j
        else:
            i += 1
    return out


def sustained(mask, k):
    """True where `mask` has been continuously true for k frames."""
    if k <= 1:
        return mask.copy()
    out = np.zeros_like(mask)
    run = 0
    for i, v in enumerate(mask):
        run = run + 1 if v else 0
        out[i] = run >= k
    return out


def states(a_fit, res, speed, g, args):
    """Return (in_flight, in_contact, contact_ok) as booleans per frame."""
    tol = abs(g) * args.gtol
    clean = np.isfinite(a_fit) & np.isfinite(res)
    clean &= res < getattr(args, "fit_res", 2.0)
    in_flight = clean & (np.abs(a_fit - g) < tol)
    in_flight = fill_holes(in_flight, getattr(args, "flight_fill", 2))

    in_contact = (speed < args.still) | (~in_flight & (speed < args.entry))
    contact_ok = sustained(in_contact, getattr(args, "contact_min", 3))
    return in_flight, in_contact, contact_ok


# ---------------------------------------------------------------- launches

def find_launches(t, vx, vy, ay, speed, args, a_fit=None, res=None, g=None):
    """A launch = transition from SUSTAINED contact into free flight.

    The launch is credited to the last contact frame - that is when the tap
    took effect.

    a_fit/res/g are optional so kot_convert and kot_waypoints, which still
    call this with the v1 signature, keep working. Pass them and detection
    uses the parabola fit; omit them and it falls back to the old ay.
    """
    if g is None:
        g = estimate_gravity(ay, speed, args.still)

    if a_fit is not None:
        in_flight, in_contact, contact_ok = states(a_fit, res, speed, g, args)
    else:
        tol = abs(g) * args.gtol
        in_flight = np.abs(ay - g) < tol
        in_contact = (speed < args.still) | (~in_flight & (speed < args.entry))
        contact_ok = in_contact

    launches = []
    i = 2
    n = len(t)
    while i < n - 3:
        if contact_ok[i]:
            j = i + 1
            while j < min(i + args.window, n) and in_contact[j]:
                j += 1
            if j < min(i + args.window, n):
                gained = speed[j] - speed[i]
                if gained > args.gain and speed[j] > args.entry:
                    # Credit the LAST contact frame, not the first.
                    #
                    # `i` is merely where the forward scan started, and the
                    # scan only succeeds once i has crept to within
                    # --window frames of the real departure. Crediting t[i]
                    # therefore reports the launch up to --window frames
                    # EARLY, and it showed: every matched launch on
                    # own_155730 came in ahead of its tap, mean -67.5ms
                    # with sd 29.7 - a systematic ~4-frame bias against an
                    # 8-frame window, not scatter.
                    #
                    # It matters most for waypoints, which sample position
                    # at the launch frame: 4 frames of travel is ~20px at
                    # full resolution, placing every waypoint behind where
                    # the thief actually was.
                    lc = j - 1
                    if (not launches
                            or t[lc] - launches[-1][0] > args.refractory):
                        launches.append((float(t[lc]),
                                         float(speed[lc]),
                                         float(speed[j]),
                                         float(vx[j]),
                                         float(vy[j])))
                    # Consume the whole flight arc. Otherwise a brief
                    # contact classification partway through one jump
                    # starts a second "launch" out of the same arc - which
                    # is how one descent came back as four events.
                    k = j
                    while k < n and in_flight[k]:
                        k += 1
                    i = max(j, k - 1)
                    continue
        i += 1

    return launches, g


def estimate_gravity(ay, speed, still):
    """v1 estimator, kept only so the old call path still runs.

    DO NOT trust this. It returned +514 on a run whose real gravity was
    ~850. Use fit_gravity() instead.
    """
    pos = ay[ay > 0]
    if len(pos) < 30:
        return float(np.median(ay))
    counts, edges = np.histogram(pos, bins=25,
                                 range=(0, np.percentile(pos, 98)))
    skip = 3
    if counts[skip:].sum() == 0:
        return float(np.median(pos))
    k = skip + int(np.argmax(counts[skip:]))
    return float((edges[k] + edges[k + 1]) / 2)


# ----------------------------------------------------------------- scoring

def score(launches, taps, t, in_flight, in_contact, observed, args):
    """Score detection against ground-truth taps, accounting for the fact
    that the game ignores some of them."""
    times = [l[0] for l in launches]
    if not taps:
        print("\nNo ground truth. Detected launch times (s):")
        print("  " + "  ".join(f"{x:.3f}" for x in times))
        return

    # Taps outside the analysed span cannot be scored and must not be
    # counted as failures. On own_040008 the recording opened on TAP TO
    # BREAK IN and TAP TO START, so the first four taps were MENU taps -
    # the game acted on every one of them, just not by launching a thief.
    # Scoring those as missed launches is scoring the detector for
    # something that never happened.
    lo_t, hi_t = float(t[0]), float(t[-1])
    outside = [x for x in taps if not (lo_t <= x <= hi_t)]
    taps = [x for x in taps if lo_t <= x <= hi_t]
    if outside:
        print(f"\n{len(outside)} tap(s) outside the analysed span "
              f"{lo_t:.2f}-{hi_t:.2f}s excluded from scoring: "
              + "  ".join(f"{x:.3f}" for x in outside))
        print("  These may be menu taps, taps after the level ended, or "
              "REAL GAMEPLAY TAPS in a stretch the tracker did not cover. "
              "Check the span against the level - if it ends early, the "
              "thief was lost, and those taps are unscored, not excused.")
    if not taps:
        print("  No taps left inside the span - nothing to score against.")
        return

    def state_at(tap):
        i = int(np.argmin(np.abs(t - tap)))
        if not observed[i]:
            return "lost"
        if in_flight[i]:
            return "airborne"
        if in_contact[i]:
            return "contact"
        return "?"

    used = set()
    rows = []
    for tap in taps:
        best, bd = None, 1e9
        for j, lt in enumerate(times):
            if j in used:
                continue
            if abs(lt - tap) < bd:
                best, bd = j, abs(lt - tap)
        st = state_at(tap)
        if best is not None and bd <= args.match_tol:
            used.add(best)
            rows.append((tap, times[best], times[best] - tap, st, True))
        else:
            nearest = min((abs(lt - tap) for lt in times), default=float("nan"))
            rows.append((tap, None, nearest, st, False))

    print(f"\n{'tap(s)':>9} {'launch':>9} {'delta(ms)':>10} {'state':>9}  hit")
    for tap, lt, d, st, hit in rows:
        if hit:
            print(f"{tap:9.3f} {lt:9.3f} {d * 1000:+10.1f} {st:>9}  yes")
        else:
            print(f"{tap:9.3f} {'-':>9} {d * 1000:10.0f} {st:>9}  no")

    hits = [r for r in rows if r[4]]
    misses = [r for r in rows if not r[4]]
    m_air = [r for r in misses if r[3] == "airborne"]
    m_lost = [r for r in misses if r[3] == "lost"]
    m_real = [r for r in misses if r[3] not in ("airborne", "lost")]
    unmatched = [x for j, x in enumerate(times) if j not in used]

    print(f"\ntaps {len(taps)}   matched {len(hits)}   missed {len(misses)}")
    print(f"  of the misses: {len(m_air)} were made MID-AIR (the game "
          f"ignored them too - correct misses)")
    if m_lost:
        print(f"                 {len(m_lost)} fell in a tracking gap "
              f"(unknowable, not a detector failure)")
    print(f"                 {len(m_real)} were made IN CONTACT "
          f"<- these are the real failures")

    denom = len(hits) + len(m_real)
    print(f"\nEFFECTIVE RECALL: {len(hits)}/{denom} of taps the game could "
          f"have acted on")
    print(f"false positives: {len(unmatched)}")
    if unmatched:
        print("  unmatched launches: " +
              "  ".join(f"{x:.3f}" for x in unmatched))
        print("  Clusters of these within a few hundred ms of each other, "
              "with near-identical vy, are ONE fall counted many times - "
              "raise --contact-min or --flight-fill.")

    if hits:
        lags = np.array([r[2] for r in hits]) * 1000
        print(f"\nlag: mean {lags.mean():+.1f}ms  sd {lags.std():.1f}ms  "
              f"range {lags.min():+.0f} to {lags.max():+.0f}ms")
        print("  A negative mean is not causal - the launch is credited to "
              "the last contact FRAME, which can precede the tap that ended "
              "it by up to one frame, and smoothing smears the transition "
              "further. Position-triggered replay does not care; only "
              "time-triggered replay did.")


def report_gaps(t, observed, speed):
    """Where the tracker lost the thief, and whether it lost it while fast.

    If dropouts cluster during fast motion, every gap is filled by a
    straight line through the most curved part of the path, and the launch
    beside it is being measured against fiction.
    """
    miss = ~observed
    if not miss.any():
        print("no tracking gaps")
        return
    edges = np.diff(miss.astype(int))
    starts = list(np.where(edges == 1)[0] + 1)
    ends = list(np.where(edges == -1)[0] + 1)
    if miss[0]:
        starts.insert(0, 0)
    if miss[-1]:
        ends.append(len(miss))
    lens = [e - s for s, e in zip(starts, ends)]

    print(f"tracking gaps: {len(lens)}   frames lost {int(miss.sum())}"
          f"   longest {max(lens)} frames")
    around = []
    for s, e in zip(starts, ends):
        if s - 1 >= 0 and observed[s - 1]:
            around.append(speed[s - 1])
        if e < len(speed) and observed[e]:
            around.append(speed[e])
    if around:
        print(f"speed at gap edges: median {np.median(around):.0f} px/s "
              f"vs {np.median(speed[observed]):.0f} px/s overall")
        if np.median(around) > 1.5 * np.median(speed[observed]):
            print("  -> the thief is lost mainly while moving FAST. Those "
                  "gaps are straight lines through exactly the airborne "
                  "arcs the detector needs.")


def timeline(t, xs, ys, speed, a_fit, res, in_flight, contact_ok, observed,
             taps, t0, t1, every):
    """Per-sample state over a time window. For working out WHY a stretch
    of the run produced no launches - a summary statistic cannot tell you
    that the thief was simply sitting still for three seconds."""
    print(f"\ntimeline {t0:.1f}s to {t1:.1f}s "
          f"(every {every * 1000:.0f}ms, * = tap within the row)")
    print(f"{'t':>7} {'x':>6} {'y':>6} {'speed':>7} {'a_fit':>8} "
          f"{'res':>6} {'state':>9} {'seen':>5}")
    last = -1e9
    for i in range(len(t)):
        if t[i] < t0 or t[i] > t1:
            continue
        if t[i] - last < every:
            continue
        last = t[i]
        st = ("airborne" if in_flight[i]
              else "contact" if contact_ok[i] else "-")
        af = f"{a_fit[i]:+8.0f}" if np.isfinite(a_fit[i]) else f"{'-':>8}"
        rs = f"{res[i]:6.1f}" if np.isfinite(res[i]) else f"{'-':>6}"
        star = "*" if any(abs(tp - t[i]) < every / 2 for tp in taps) else " "
        print(f"{t[i]:7.3f} {xs[i]:6.0f} {ys[i]:6.0f} {speed[i]:7.0f} "
              f"{af} {rs} {st:>9} {str(bool(observed[i])):>5} {star}")


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meta")
    ap.add_argument("--still", type=float, default=40,
                    help="px/s below this counts as in contact")
    ap.add_argument("--entry", type=float, default=120,
                    help="px/s the thief must reach to count as launched")
    ap.add_argument("--gain", type=float, default=80,
                    help="px/s speed increase required across a launch")
    ap.add_argument("--gtol", type=float, default=0.4,
                    help="fraction of g the fitted acceleration may deviate "
                         "and still count as free flight")
    ap.add_argument("--window", type=int, default=8,
                    help="frames to look ahead for the flight state")
    ap.add_argument("--refractory", type=float, default=0.10)
    ap.add_argument("--fit-win", type=int, default=13, dest="fit_win",
                    help="frames per parabola fit for acceleration. 13, not "
                         "7: gravity bends the path by g*T^2/2 while the "
                         "quadratic coefficient amplifies centroid noise by "
                         "8/T^2, so a short window measures mostly jitter. "
                         "Going 7 -> 13 took p5/p95 of the fitted "
                         "acceleration from -3491/+2664 to -330/+962")
    ap.add_argument("--fit-res", type=float, default=2.0, dest="fit_res",
                    help="max RMS residual (px) for a fit to be believed. "
                         "Too tight and real flight windows are discarded - "
                         "centroid jitter on a 20px sprite is easily 2px")
    ap.add_argument("--flight-fill", type=int, default=2, dest="flight_fill",
                    help="fill flight-mask holes this many frames long. An "
                         "arc does not stop being ballistic for one frame")
    ap.add_argument("--contact-min", type=int, default=3, dest="contact_min",
                    help="frames of continuous contact required before a "
                         "launch can be credited to it")
    ap.add_argument("--match-tol", type=float, default=0.12,
                    dest="match_tol",
                    help="seconds within which a launch matches a tap")
    ap.add_argument("--old-gravity", action="store_true", dest="old_gravity",
                    help="use the v1 histogram estimator, for comparison")
    ap.add_argument("--gravity-scan", action="store_true",
                    help="report both gravity estimators and the fitted "
                         "acceleration distribution, then stop")
    ap.add_argument("--timeline", type=float, default=None,
                    help="print per-sample state from this time (s)")
    ap.add_argument("--timeline-end", type=float, default=None,
                    dest="timeline_end")
    ap.add_argument("--timeline-every", type=float, default=0.05,
                    dest="timeline_every")
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
    _, _, y_raw, observed = prepare(path, quiet=True)

    a_fit, res = local_accel(t, y_raw, args.fit_win)
    prior = kg.expected_gravity(meta, args)
    g_fit, n_used, how = fit_gravity(a_fit, res, prior, args.fit_res)
    g_old = estimate_gravity(ay, speed, args.still)

    print(f"\ngravity prior (from meta scale): {prior:+.0f} px/s^2")
    print(f"  parabola fit : {g_fit:+.0f}  ({how}, {n_used} clean windows)")
    print(f"  v1 histogram : {g_old:+.0f}  (kept for comparison only)")
    if "NOTHING near prior" in how:
        print("\n  *** The tracked object does not fall like the thief. "
              "Stop here and check kot_track_thief.py --tracks. ***")

    g = g_old if args.old_gravity else g_fit
    print(f"  using {g:+.0f}; flight = fitted a within "
          f"{g * (1 - args.gtol):+.0f}..{g * (1 + args.gtol):+.0f} px/s^2")

    clean = np.isfinite(a_fit) & np.isfinite(res) & (res < args.fit_res)
    print(f"  clean fit windows: {int(clean.sum())}/{len(t)} "
          f"({100 * clean.sum() / len(t):.0f}%) at --fit-res {args.fit_res}")
    if clean.sum() < 0.5 * len(t):
        print("  -> most windows are being REJECTED as unfittable. Try "
              "--fit-res 4. A discarded window cannot be flight, so this "
              "caps how much airborne time can ever be seen.")

    print()
    report_gaps(t, observed, speed)
    print(f"\nspeed: median {np.median(speed):.0f}  "
          f"p10 {np.percentile(speed, 10):.0f}  "
          f"p90 {np.percentile(speed, 90):.0f}  max {speed.max():.0f} px/s")
    if np.percentile(speed, 90) < args.entry:
        print(f"  -> --entry {args.entry:.0f} is above the 90th percentile "
              f"of speed. A launch must reach it, so the detector can only "
              f"ever fire on the fastest few percent of frames.")

    in_flight, in_contact, contact_ok = states(a_fit, res, speed, g, args)

    if args.gravity_scan:
        print("\nfitted acceleration percentiles (clean windows only):")
        if clean.sum():
            for p in (5, 10, 25, 50, 75, 90, 95):
                print(f"  p{p:<3d} {np.percentile(a_fit[clean], p):+9.0f}")
            print(f"\nfitted acceleration histogram "
                  f"({int(clean.sum())} windows):")
            lo, hi = (np.percentile(a_fit[clean], 2),
                      np.percentile(a_fit[clean], 98))
            counts, edges = np.histogram(a_fit[clean], bins=18, range=(lo, hi))
            peak = counts.max() or 1
            for c, e0, e1 in zip(counts, edges[:-1], edges[1:]):
                print(f"  {e0:+8.0f}..{e1:+8.0f} {c:5d} "
                      f"{'#' * int(40 * c / peak)}")
            print("\nExpect TWO clusters: a tall one near 0 (running along "
                  "surfaces) and a shorter one near gravity (airborne). If "
                  "the second is missing, either the thief never got "
                  "airborne or the residual filter ate those windows.")
        else:
            print("  no clean fit windows at all - the path is too noisy.")
        return

    if args.timeline is not None:
        t1 = args.timeline_end if args.timeline_end is not None else t[-1]
        timeline(t, xs, ys, speed, a_fit, res, in_flight, contact_ok,
                 observed, [x["t"] for x in meta["taps"]],
                 args.timeline, t1, args.timeline_every)
        return

    launches, g = find_launches(t, vx, vy, ay, speed, args,
                                a_fit=a_fit, res=res, g=g)
    print(f"\ndetected {len(launches)} launches")
    for lt, s0, s1, lvx, lvy in launches:
        kind = "wall/horiz" if abs(lvx) > abs(lvy) else "vertical"
        print(f"  {lt:6.3f}s  {s0:5.0f} -> {s1:5.0f} px/s  "
              f"vx {lvx:+6.0f} vy {lvy:+6.0f}  {kind}")

    print(f"\nframes airborne: {int(in_flight.sum())}/{len(t)} "
          f"({100 * in_flight.sum() / len(t):.0f}%)")
    expect = len(launches) * 18
    if in_flight.sum() < 0.6 * expect:
        print(f"  -> {len(launches)} launches imply roughly {expect} "
              f"airborne frames. Seeing far fewer means flight is being "
              f"under-detected; --fit-res and --gtol are the knobs.")

    score(launches, [x["t"] for x in meta["taps"]],
          t, in_flight, in_contact, observed, args)


if __name__ == "__main__":
    main()