# King of Thieves Bot — Handoff

**Supersedes the session 1 and session 2 handoffs.** Several conclusions in
those documents turned out to be wrong; the corrections are in section 3
and are the most useful part of this file.

Last updated: 15 Aug 2026.

---

## 1. Where things stand

| Tool | Status | Result |
|---|---|---|
| `kot_rec.py` | **works** | replays own taps at +0.0ms drift |
| `kot_skipper.py` | **works** | gold + gems read on 6/6 real chests |
| `kot_reconnect.py` | **works** | dismisses dialogs, verifies, logs |
| `kot_track_thief.py` | **works** | picks thief 236 ballistic windows to minion's 0 |
| `kot_launch.py` | **works** | 11/13 taps, +11ms mean, sd 20ms |
| `kot_ghost.py` | partial | extracts 8/8 ghost taps; live anchor untested |
| `kot_agent.py` | abandoned | 8/15 waypoints, then diverges — structural |
| `kot_tapper.py` | superseded | use `kot_rec.py` |
| `kot_convert.py` | superseded | time-replay path, replaced by `kot_rec` |

---

## 2. Environment

- **OS**: Windows, PowerShell. Python 3.14.
- **Working dir**: `C:\Users\divyp\Downloads\Kot`
- **Emulator**: was LDPlayer, **now BlueStacks** at 1280x720.
- **Packages**: `mss numpy opencv-python keyboard pywin32`
- **Costume**: gangster skin (white hat/face on black suit). Was panda,
  before that green. **The costume changes what masks work** — see §3.6.

### Emulator differences that bit us

- Every script defaults to `WINDOW_TITLE = "LDPlayer"`. On BlueStacks pass
  `--title BlueStacks`.
- **BlueStacks shows an advert panel ~228px wide on the LEFT.** The capture
  takes the leftmost 1280 columns, so without `--region-x 228` it captures
  adverts and clips the game. This produced anchors locking onto jewellery.
  The panel is not always present — check the client width before assuming.
- **BlueStacks intercepts `WM_CLOSE`** with its own confirmation popup, a
  separate top-level window. Closing it programmatically requires finding
  and clicking that popup.
- **Templates do not transfer between emulators.** A dialog template cut
  from LDPlayer scores **0.705** on BlueStacks, under the 0.85 threshold,
  so it is never detected. This cost an 8-hour session that sat dead behind
  an undismissed dialog while the log looked healthy.

---

## 3. Corrections to earlier handoffs

### 3.1 "Use motion as the primary discriminator" — WRONG

Session 1 §8 proposed: track candidates, pick the one with the largest
cumulative displacement. **This would not have worked.** A patrolling
minion translates as much as the thief, and more than the thief while the
thief stands still on a platform.

**What works is BALLISTICS.** Only the thief follows a free-fall arc.
Track every candidate, fit a parabola to each one's `y(t)`, and pick the
one whose vertical acceleration matches gravity. A minion on a rail gives
`a ≈ 0`; a bone swinging on a chain gives a sign-flipping `a` that hits
gravity a few percent of the time by accident; fading UI text never
translates. Measured on level 28: thief **236**, minion **0**.

### 3.2 The gravity estimator was wrong by 40%

The histogram-mode estimator returned **+514** where the true value is
~850, because it differentiates twice through two moving averages and
interpolation across tracking gaps contributes exact zeros.

That is not cosmetic: flight was defined as `|ay - g| < gtol*g`, so g=514
with gtol=0.4 accepted 308–720 — a window that **excludes real gravity**.
The detector was hunting for launches into a state defined so as to
exclude the actual airborne state.

**Fix**: sliding parabola fit, anchored to the known constant as a prior.
Returns +852 against an +850 prior on the same data.

### 3.3 Fit window length matters enormously

Gravity bends the path by `g·T²/2` while the quadratic coefficient
amplifies centroid noise by `8/T²`. At 7 frames (116ms) that is 5.7px of
signal against ~590 px/s² of noise per pixel of jitter — the signal sits at
the noise floor. At 13 frames (216ms): 19.8px against ~171.

Same data, same code: p5/p95 of fitted acceleration went from
**−3491/+2664 to −330/+962**.

### 3.4 "Video can never recover the taps" — WRONG

Session 2 concluded that because KoT ignores taps made mid-air, video can
only show what the game did and never what you pressed, and called this a
ceiling.

**The reasoning has a hole.** A tap the game ignored changed nothing about
the trajectory, so omitting it from a replay produces an identical run. The
taps that matter are exactly the ones that altered the motion — and those
are the visible ones.

Measured on `ghost_234213`: **8 launches recovered, matching the 8 taps
counted by eye.** Nothing missing.

### 3.5 Launches were credited to the wrong frame

`find_launches` credited a launch to the frame where its forward scan
*started*, not where the thief actually left the surface. Every detected
launch came in early by up to `--window` frames — a systematic **−67.5ms**
bias, which looked like scatter but was a constant offset. Crediting the
last contact frame gives **+11ms, sd 20ms**.

It matters most for waypoints, which sample position at the launch frame:
4 frames of travel is ~20px at full resolution.

### 3.6 The costume is not always white

`--white-v 220 --white-s 30` is right for the panda. Measured against a
**green** costume: **1.0%** of its pixels pass that mask, against 35% in
the green hue band. It was never going to be detected, and no amount of
anchor tuning would have helped.

Use `kot_ghost.py scan` to find out what your thief actually masks as
before tuning anything downstream.

### 3.7 Being online DOES prevent raids

Mid-session I claimed raids run server-side against your stored layout so
staying connected doesn't shield it. **That is wrong.** The official help
centre and the wiki both state a dungeon becomes attackable only after you
go offline (about a minute).

Recorded here because it changes the reasoning around session length — and
because the KoT forums describe permanent-online play as bot abuse, with a
moderator confirming there is a system in place to deal with it. The
9-hour/3-hour lockout is plausibly part of that system.

---

## 4. Tuned constants

```
--white-v 220  --white-s 30      # white mask (panda / gangster hat)
--white-v 200  --white-s 40      # looser, for the gangster skin's small white area
--mode hue --hue-lo 35 --hue-hi 85   # green costume
--still 40     --entry 200       # contact / launch thresholds px/s
--gtol 0.4     --maxjump 40
--max-vy 300                     # NOT -40: wall-jumps can move DOWNWARD
--max-entry 600                  # rejects spawn (1296 px/s) and end animation
--fit-win 13   --track-win 13    # see §3.3
--refractory 0.18                # 0.10 double-counted one arc as two launches
--track-merge 25                 # merge sprite fragments
--track-bridge 0                 # OFF: stitches on circumstantial evidence
CROP_TOP = 0.06   CROP_BOTTOM = 0.90
```

Gravity: **~850 px/s² at 640x360**, **~1700 at 1280x720**. Confirmed across
three independent runs (+852, +844, +873, +882).

---

## 5. Game facts

- Base-skip costs **300 gold**. `kot_skipper` reads your balance and stops
  before you run dry.
- Raids cost **lockpicks**, which regenerate slowly. This makes RL
  impractical — ~10 attempts, then wait.
- **9 hours of continuous play → 3-hour lockout** ("You have been playing
  for too long"). RECONNECT does not clear it; it is account state.
- Idle disconnect fires on a **184-second cycle** ("disconnected due to
  inactivity").
- The **gem display case moves** between bases — it is part of the
  defender's layout. Fixed-slot approaches cannot work.
- The **three gem slots sit at a fixed offset from the chest's coin icon**
  (dx −6/+27/+59, dy −46/−54/−46 as centres). The middle slot is ~8px
  higher; the chest lid has three lobes and the centre is raised.
- **Gold rims mark high-value gems.** Rim gold: 245–298px on a real gold
  rim, 43–116 on an ordinary one. On the highest tier the gold spreads into
  the centre and the gem reads as yellow unless gold is excluded from the
  colour sample.
- KoT is **one-tap-anywhere**; (640,360) is a safe click point.
- The ghost replay renders the thief **with your own equipped costume**.
- The **SKIP button is pixel-identical** across bases — reliable anchor.
- Chest art varies enough that **one coin template is not enough**; a
  bright-gold chest scored 0.579 against a 0.60 threshold.

---

## 6. Why the waypoint agent failed (structural, not tunable)

A waypoint says "tap when the thief is here moving like this". Position and
velocity **do not pin down the game state** — which surface it is touching
and which way it faces decide where a tap sends it.

Every tap lands within tolerance and slightly wrong, and errors compound.
Measured: `vd` (velocity mismatch) 450–570 px/s on every fire. At
`--vtol 400` it rejects a genuine 13px match; at 600 it accepts garbage and
diverges by waypoint 7. **No setting does both.**

`kot_rec.py` replays **inputs**, not states. 550ms is 550ms. That is why it
works and this doesn't.

---

## 7. Open problems

1. **`kot_ghost.py` live replay is untested.** Extraction works; the
   replay must anchor on the thief appearing, and that anchor depends on
   the costume masking well.
2. **Video-derived taps are marginal.** 30fps quantises every tap to 33ms
   against a ~30ms jump window. A base extracted from a YouTube video
   produced 10 plausible launches that did not clear it.
3. **Magnet bases break ballistic tracking.** A "double spider magnet" base
   is not following free-fall for much of the run, so the physics
   discriminator is weak there; speed-gain detection was substituted.
4. **A physics simulator is the only route to unseen dungeons.** Gravity is
   measured to within 4% and launch velocities are in the waypoint files.
   The cheap first test: feed a recorded run's tap times to a forward
   simulator and compare against the tracked path. Geometry extraction is
   the hard part.

---

## 8. Debugging habits that paid off

- **`--tracks` before anything else.** "Located in 97% of frames" only says
  a blob was found; the candidate table says which one and why.
- **Look at the debug PNGs.** Every wrong lock in this project was obvious
  in one frame and invisible in the numbers.
- **Check `--help` after taking a new file.** Several sessions were lost to
  a script silently missing the flag being passed to it.
- **Repeated identical log lines are a symptom.** 200 consecutive
  `keep-alive SKIPPED: target not on screen` meant a dialog had covered the
  screen 8 hours earlier.
- **A misread is worse than a failed read.** Always prefer refusing to
  produce a number over producing a plausible wrong one.

---

## 9. User preferences

- **Full file contents** when code changes, not diffs or snippets.
- Brief, direct, concrete. No softening.
- Pushes back when assumptions are wrong — corrections mid-session
  (minion visibility, tap counts, which dungeon is which) have all been
  right and should be taken seriously rather than argued with.