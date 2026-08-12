# kot-bot

Screen-capture automation tooling for King of Thieves (ZeptoLab / Nazara),
running in LDPlayer on Windows.

Recovers tap timings from gameplay video and replays them. Built to imitate
the in-game "dungeon solution" ghost replay, so a dungeon can be cleared
without playing it manually.

**Educational project.** Automating the game violates its terms of service.
Use a throwaway account.

## Status

| Component | State |
|---|---|
| Screen capture (83fps) | working |
| Click injection | working — cleared a campaign level on blind replay |
| Thief tracking | working on one dungeon, does not generalise |
| Tap recovery from video | 5/6 taps on a ground-truth run |
| Ghost replay end-to-end | not working |

## Install

```
pip install mss numpy opencv-python pillow keyboard pywin32
```

LDPlayer must be set to **1280x720**.

## Pipeline

```
kot_probe.py     capture + pixel inspection
kot_tapper.py    record / replay tap sequences   (F6 record, F5 replay)
kot_track.py     record a run to disk            (--own / --ghost)
kot_analyse.py   inspect frames and motion
kot_green.py     track the thief                 (--mode white|hue)
kot_launch.py    detect taps as launch events
kot_convert.py   launches -> replayable tap file
```

Typical flow:

```
python kot_track.py --own
python kot_convert.py runs/own_XXXX.json --measure
python kot_track.py --ghost
python kot_convert.py runs/ghost_XXXX.json --lag -51 --start-tap --out taps/zz_run.json
python kot_tapper.py
```

## Tuned constants

```
--white-v 220  --white-s 30      # white-mask thresholds (panda costume)
--still 40     --entry 200       # contact / launch speed thresholds, px/s
--gtol 0.4     --maxjump 40
--lag -51                        # ms, input latency
```

Measured gravity: ~850 px/s² at 640x360.

## Known limitation

Colour alone cannot identify the thief across arbitrary dungeons. Green
costumes collide with green gems and spinner traps; white costumes collide
with spider webs and pale platforms. The fix is to use **motion** as the
primary discriminator — the thief is the only thing making large sustained
translations. Not yet implemented.

See `KOT_BOT_HANDOFF.md` for full technical detail.