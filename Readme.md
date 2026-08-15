# kot_bot

Computer-vision tooling for **King of Thieves** running in an Android
emulator at 1280x720.

Reads the screen, tracks the thief by physics, replays recorded taps, and
handles the game's disconnect dialogs. Nothing here touches the game's
memory, files or network — it looks at pixels and sends mouse clicks.

> Automating this game is against its terms of service and accounts do get
> banned for it. Detection is behavioural and server-side: identical tap
> timings and sessions that never sleep are visible regardless of how the
> clicks are produced. Treat this as a computer-vision project.

---

## Install

```
pip install mss numpy opencv-python keyboard pywin32
```

Windows only (uses `SendInput` and `win32gui`). Set your emulator to
**1280x720**.

---

## Two flags you will need every time

Every tool takes these, and getting them wrong makes everything look broken:

| flag | when |
|---|---|
| `--title BlueStacks` | your emulator's window title (default is `LDPlayer`) |
| `--region-x 228` | **only** if your emulator shows an advert panel on the left |

To find out whether you need the offset:

```powershell
python -c "import win32gui;h=[];win32gui.EnumWindows(lambda w,_: h.append(w) if win32gui.IsWindowVisible(w) and 'bluestacks' in win32gui.GetWindowText(w).lower() else None,None);l,t,r,b=win32gui.GetClientRect(h[0]);print('client',r-l,'x',b-t)"
```

Client ~1515 wide → the panel is there, use `--region-x 228`.
Client ~1291 wide → no panel, leave it at 0.

A wrong region breaks every template at once. **If several things stop
working at the same time, check this first.**

---

## `kot_rec.py` — record and replay your taps ✅

The most reliable tool here. Records your taps with millisecond timestamps
and replays them exactly.

```
python kot_rec.py --title BlueStacks --file taps\lvl130.json
```

| key | does |
|---|---|
| **F6** | wipe and record — starts the level itself, then captures your taps |
| **F5** | replay |
| **F9** | quit (ESC aborts a replay) |

Sit on the pre-run screen, press the key, then take your hand off the
mouse. Measured drift on replay: **+0.0ms on every tap**.

**Why it lines up:** record and replay both send the level-start tap
themselves and time everything from it, so `t=0` is the same event in both
runs. Anchoring to a keypress or a fixed delay does not work — that is what
sank the earlier `kot_tapper`.

Tuning: `--offset 0.02` shifts every tap; edit the JSON to move one. Keep
`hold` shorter than the gap to the next tap, or the button is still down
when the next one fires.

---

## `kot_skipper.py` — find worthwhile raid targets ✅

Reads the gold and the three gems on a raid target and skips until one is
worth attacking.

```
python kot_skipper.py --title BlueStacks --gold 200000 --min-rich 2 --max-skips 15
```

| flag | does |
|---|---|
| `--gold N` | stop at this much gold |
| `--want purple,blue --min-gems 2` | stop on gem colours |
| `--min-rich 2` | stop on N **gold-rimmed** (high value) gems |
| `--show-gems --dry-run` | watch without acting; you skip by hand |
| `--log gems.csv` | append every base to a CSV |
| `--on-fail skip` | skip unreadable bases instead of stopping |

Verified on six real chests across six dungeon themes, all read exactly.

**How it reads.** The gold digits are a fixed bitmap font, so it
template-matches each digit rather than running OCR. The anchor is the coin
icon, not a fixed crop, because the defender chooses where the chest sits.
The three gem slots sit at a fixed offset from that coin, so one anchor
finds both.

**Gold rims** mark the valuable gems. Colour is sampled from the slot
centre and the rim from the annulus around it, because an orange *gem* and
a gold *rim* are the same hue and only position separates them. The
decider is how much saturated gold is in the rim: 245–298px on a real gold
rim against 43–116 on an ordinary one.

Skipping costs 300 gold, so `--max-skips` is a spend limit as much as a
safety limit. It reads your balance from the HUD and stops before you run
dry.

---

## `kot_reconnect.py` — dialog watchdog ✅

Watches for the game's disconnect dialogs and clicks through them, so an
unattended session doesn't stall in front of a modal.

```
python kot_reconnect.py --title BlueStacks
python kot_reconnect.py --title BlueStacks --dry-run     # detect only
python kot_reconnect.py --title BlueStacks --calibrate   # teach a new dialog
```

Three kinds of template, set in each `<name>.json`:

| kind | behaviour |
|---|---|
| normal | click, then **verify the dialog actually went away** |
| `"dismiss_only": true` | ad/offer popups — short cooldown, rapid re-check |
| `"fatal": true` | recognised but unrecoverable; log, screenshot, stop |

**It verifies rather than assumes.** A click that never reached the app
looks identical to one that worked, so it re-matches the template
afterwards and only counts success once the dialog is gone. After three
failed attempts it saves a screenshot and stops rather than looping.

`--max-session H` stops after H hours. `--restart-test` closes the
emulator at that point, waits, relaunches and records whether the
restriction survived — which distinguishes a server-side limit from a
client-side one.

### Templates

`reconnect_templates/<name>.png` + `<name>.json` holding the click point
relative to the match.

**Crop the panel, not the screen.** A full-frame template is mostly
background, which changes constantly:

```
full frame:  own dialog 0.997, other dialog 0.813   (margin 0.18)
panel only:  own dialog 1.000, other dialog 0.655   (margin 0.35)
```

**Include context around close-X icons.** KoT reuses the same red X on ad
panels and on dialogs. Cropped tight, an ad's X matches the reconnect
dialog at 0.950 and clicks the wrong button. With ~16px of surrounding
panel that drops to 0.642.

**Templates are per-emulator.** A dialog cut from LDPlayer scores **0.705**
on BlueStacks — under threshold, so it is never detected and the session
sits dead behind an undismissed dialog. Re-cut them if you switch.

---

## `kot_track_thief.py` — find the thief in a recording ✅

Locates the thief by **physics**, not appearance.

```
python kot_track.py --own                                  # record a run
python kot_track_thief.py runs\own_XXXX.json --tracks      # who did it pick?
python kot_track_thief.py runs\own_XXXX.json --debug       # PNGs to check
```

Colour alone locks onto gems and spider webs. Background subtraction
removes static decor but keeps every *moving* decoy — an earlier version
followed a patrolling minion for a whole run while reporting "thief located
in 97% of frames".

This version tracks every candidate at once and picks the one whose
vertical motion fits gravity, because only the thief falls. On campaign
level 28 it chose the thief **236 ballistic windows to the minion's 0**, at
92% coverage.

`--tracks` prints the candidate table — the most useful diagnostic here. It
shows every object with its extent, fitted gravity and ballistic score, so
a wrong pick is visible instead of silent.

---

## `kot_launch.py` — find the taps in a recording ✅

Detects taps as **contact-to-flight transitions**, so wall-jumps count and
slides don't.

```
python kot_launch.py runs\own_XXXX.json --still 40 --entry 200
```

**11 of 13** ground-truth taps recovered, mean error **+11ms**, sd 20ms.

Scoring is honest about the game: KoT ignores taps made mid-air, so it
reports the thief's state at each tap and separates *correct* misses — you
tapped mid-flight and the game ignored it too — from real detector
failures.

`--gravity-scan` and `--timeline` are the diagnostics.

---

## `kot_ghost.py` — extract taps from a ghost replay ⚠️

Buy a dungeon's solution, record the ghost, extract its taps.

```
python kot_track.py --ghost
python kot_ghost.py extract runs\ghost_XXXX.json --out ghosts\base.json
python kot_ghost.py replay ghosts\base.json --dry-run
python kot_ghost.py scan --mode white --seconds 12         # what can it see?
```

Recovered **8 launches from a ghost run, matching the 8 taps counted by
eye**. The earlier conclusion that video can never recover taps was wrong:
taps the game ignored changed nothing, so the ones that matter are exactly
the visible ones.

Partial because live replay must anchor on the thief appearing, and that
depends on the costume masking well. Use `scan` first — a green costume
scores **1%** on the white mask, so it would never be seen.

---

## `kot_waypoints.py` + `kot_agent.py` — position-triggered replay ❌

Fires taps when the thief reaches a recorded position. Reaches **waypoint 8
of 15**, then diverges.

Worth recording why. A waypoint says "tap when the thief is here moving
like this", but position and velocity don't pin down the game state — which
surface it's touching and which way it faces decide where a tap sends it.
Every tap lands within tolerance and slightly wrong, and the errors
compound. Tightening the velocity gate rejects genuine matches; loosening
it admits bad ones. **No setting does both.**

`kot_rec.py` replays *inputs* rather than *states*, which is exactly
reproducible. That is why it works and this doesn't.

---

## Things learned the hard way

- **"Located in 97% of frames" only says a blob was found.** It does not
  say the blob was the thief. Gravity does.
- **Fit windows, don't differentiate twice.** Gravity over a 7-frame window
  bends the path 5.7px while 1px of centroid jitter costs ~590 px/s². At 13
  frames it's 19.8px against ~171. Same data: p5/p95 went from
  −3491/+2664 to −330/+962.
- **Never read a half-drawn screen.** Reading mid-transition produced "103"
  and then "4" for six-figure bases, and skipped both.
- **A misread is worse than a failed read.** An unseen digit beat the
  runner-up by 0.012 and turned 167100 into 7100. Real digits win by 0.105
  at worst — so require a *margin*, not just a threshold.
- **Verify clicks landed.** Windows silently eats the first click on an
  unfocused window, and that looks exactly like success.
- **Recordings must start before the level does**, or the launch that
  begins the run isn't in the data and a replay waits forever.
- **Templates don't transfer** between emulators, resolutions or costumes.

---

## Layout

```
assets/                coin + digit templates for kot_skipper
reconnect_templates/   dialog templates for kot_reconnect
taps/                  recorded tap files (gitignored)
ghosts/                extracted ghost tap plans
waypoints/             extracted waypoints (abandoned approach)
runs/                  raw captures — gitignored, ~560MB each
```

---

## Credits

`JartanFTW/kot-skipper` and `restart-archive/kot-solver` for prior art.
All rights to the game and its assets belong to ZeptoLab.