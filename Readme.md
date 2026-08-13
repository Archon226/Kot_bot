# kot_bot

Computer-vision tooling for King of Thieves running in LDPlayer at
1280x720. Two working tools and one unfinished experiment.

Educational project. Automating the game is against its terms of service.

## What actually works

**`kot_skipper.py`** — reads the gold on a raid target and skips until one
meets your threshold. Reads the number by template-matching a fixed bitmap
font, anchored on the coin icon rather than a fixed crop, because the
defender chooses where the chest sits. Verified against six real bases
across five dungeon themes: five read exactly, and the sixth correctly
*refuses* to read because the TAP TO BREAK IN overlay covers its leading
digits.

**`kot_track_thief.py`** — locates the thief in a recording by physics
rather than appearance. Colour alone locks onto gems and spider webs;
background subtraction removes static decor but keeps every moving decoy,
and an earlier version followed a patrolling minion for an entire run while
reporting "thief located in 97% of frames". This version tracks every
candidate simultaneously and picks the one whose vertical motion fits
gravity. On campaign level 28 it chose the thief 236 ballistic windows to
the minion's 0, at 92% frame coverage.

**`kot_launch.py`** — finds the taps in a recording as contact-to-flight
transitions, so wall-jumps count and slides do not. 11 of 13 ground-truth
taps recovered with +11ms mean error, sd 20ms.

## What does not work

**`kot_agent.py`** — closed-loop replay by position. Fires taps when the
thief reaches a recorded waypoint. Reaches waypoint 8 of 15 and then
diverges.

The reason is worth recording. A waypoint says "tap when the thief is here
moving like this", but position and velocity do not pin down the game
state: which surface it is touching and which way it is facing decide where
a tap sends it. Every tap lands within tolerance and slightly wrong, and
the errors compound. Tightening the velocity gate rejects genuine matches;
loosening it admits bad ones. There is no setting that does both.

**`kot_tapper.py`** — replays your own recorded taps instead, anchored to a
start tap the tool sends itself so t=0 means the same event in both runs.
This replays *inputs* rather than *states*, which is exactly reproducible.
Written but not yet tested end to end. This is the approach
`restart-archive/kot-solver` uses successfully with hardcoded intervals.

## Pipeline

    kot_track.py --own          record a run (F6 to start/stop)
    kot_track_thief.py --tracks confirm the right object was tracked
    kot_launch.py               score tap detection against ground truth
    kot_waypoints.py            extract waypoints
    kot_agent.py --dry-run      replay them

Every script shares `add_detector_args` in `kot_track_thief.py`, so the
mask settings cannot drift apart. They did once: the offline tools
defaulted to 185/70 while the live agent used 220/30, and waypoints were
silently extracted with a different detector than the one hunting for them.

## Things learned the hard way

- **"Located in 97% of frames" only says a blob was found.** It does not
  say the blob was the thief. Gravity does.
- **Fit windows, do not differentiate twice.** Gravity over a 7-frame
  window bends the path 5.7px while 1px of centroid jitter costs ~590
  px/s^2. At 13 frames it is 19.8px against ~171. The same data gave
  p5/p95 of -3491/+2664 at 7 frames and -330/+962 at 13.
- **Never trust a half-drawn screen.** Reading during a transition
  produced "103" and then "4" for real six-figure bases, and skipped both.
- **A misread is worse than a failed read.** An unseen digit beat the
  runner-up by 0.012 and turned 167100 into 7100; genuine digits win by
  0.105 at worst. Requiring a margin separates them; a threshold cannot.
- **Recordings must start before the level does.** A run recorded after
  the thief was already moving has no waypoint for the launch that starts
  it, so a replaying agent waits forever for a thief that never moves.

## Requirements

    pip install mss numpy opencv-python keyboard pywin32

LDPlayer at 1280x720. `runs/` is gitignored - a 13-second capture is
~560MB.

## Credits

`JartanFTW/kot-skipper` and `restart-archive/kot-solver` for prior art.
All rights to the game and its assets belong to ZeptoLab.