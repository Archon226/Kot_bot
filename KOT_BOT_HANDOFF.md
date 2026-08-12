# King of Thieves Bot — Handoff Document

Session date: 12 Aug 2026 (approx. 22:00 – 04:30).
Everything below is the state at end of session.

---

## 1. Goal

Build an automatic King of Thieves (ZeptoLab, now published by Nazara)
player. The user's explicit target is **PvP raids** — either solving
dungeons live, or **imitating the in-game "dungeon solution" ghost
replay** and playing back its taps. The user explicitly rejected plain
campaign-level macro recording as "just simple macro recording."

Two distinct sub-projects were identified:

| Project | Difficulty | Status |
|---|---|---|
| Campaign macro replay | Easy | **WORKS** (cleared level 4 blind) |
| Ghost imitation (raids) | Hard | Perception works on own dungeon; fails to generalise |
| Live puzzle-solving agent | Very hard | Not started |

---

## 2. Environment

- **OS**: Windows, PowerShell
- **Python**: 3.14 at `C:\Users\divyp\AppData\Local\Python\pythoncore-3.14-64\`
- **Working dir**: `C:\Users\divyp\Downloads\Kot`
- **Emulator**: LDPlayer, resolution 1280x720
- **Window geometry**: client area 1318x754; chrome is **34px top, 38px right**, nothing left/bottom. Derived at runtime, not hardcoded.
- **Packages**: `mss numpy opencv-python pillow keyboard pywin32`
- **Costume**: currently **panda** (white face). Previously green.

Notes:
- `pywin32_postinstall` is not importable as a module; run by full path from `Scripts\`. Not actually needed — `import win32gui` worked without it.
- The `keyboard` library may need an admin terminal for global hotkeys (worked without in practice).

---

## 3. Files (all in the working dir)

### `kot_probe.py` — capture layer (DONE, stable)
Finds the LDPlayer window, strips chrome, screenshots the game area.
- `F7` print pixel colour+coords, `F8` save PNG, `F9` quit.
- `fix_dpi()` via `SetProcessDpiAwareness(2)` — **critical**, without it coordinates are virtualised on scaled displays.
- Chrome offsets **derived** as `client_h - 720` and `client_w - 1280`.
- Capture perf: 22.8ms → **12.1ms/frame (~83fps)** after downscaling and switching from PIL to `np.frombuffer`.

### `kot_tapper.py` — input layer (DONE, stable)
Records and replays tap sequences.
- `F6` record, `F5` replay, `F4` list, `F9` quit, `ESC` aborts replay.
- Uses `SendInput` (not deprecated `mouse_event`), absolute coords scaled to 0..65535 across the virtual desktop.
- `precise_sleep()`: coarse `time.sleep` then 2ms busy-spin. Windows sleep granularity is ~15ms, far too coarse for jump windows.
- Timing anchored to a single `t0`, never accumulated per-tap.
- **Proven**: a 17-tap recording cleared campaign level 4 blind.
- **KNOWN BUG**: `load_latest()` picks the alphabetically-last file in `taps/`. Repeatedly loaded the wrong file. Workaround: name files `zz_*.json`.
- Tap JSON format: `[{"t": float, "x": int, "y": int, "hold": float}, ...]`

### `kot_track.py` — run recorder (DONE)
Records colour frames + timestamps + ground-truth taps to disk.
- `--own` (records your taps), `--ghost` (no taps), `--list`
- Output: `runs/<mode>_<HHMMSS>.raw` + `.json`
- **Colour BGR**, downscaled 0.5 → 640x360, 691KB/frame, ~43MB/s
- `INTER_AREA` interpolation (prevents sprite flicker that differencing reads as motion)
- Frames stream to disk, **not** RAM — long runs exceed memory
- Nothing is analysed during capture (dropped frame = unrecoverable jump)
- meta JSON: `mode, width, height, scale, frames, duration, fps, frame_bytes, channels, raw, taps, times`

### `kot_analyse.py` — inspection (DONE)
- `frames <meta> --every N | --range A B` → export PNGs
- `motion <meta>` → per-frame moving-pixel count, centroid, spread

### `kot_green.py` — tracker (WORKS on own dungeon, FAILS to generalise)
Despite the name, now supports two modes via `--mode`:
- `white` (**default**) — panda face: `V > white_v`, `S < white_s`, MORPH_CLOSE first (black eyes split the face otherwise)
- `hue` — original green costume mask
Plus: `--calibrate`, `--debug` (red circle overlay), `--dump`
Contains `add_detector_args(ap)` shared by the other two scripts.

### `kot_launch.py` — tap detection (5/6 on own dungeon)
Detects taps as **launches** (contact → flight transitions), not vertical velocity.
- `--gravity-scan` prints ay percentiles + histogram
- Prints each launch with `vx`, `vy`, and a `vertical` / `wall/horiz` label

### `kot_convert.py` — jumps → replayable taps
- `--measure` reports lag constant from an `--own` run
- `--lag <ms>`, `--skip-before <s>`, `--start-tap`, `--start-gap`, `--out`
- Filters: `--max-vy -40` (taps go up), `--max-entry 600` (rejects end-of-level animation)
- Imports both `kot_green` and `kot_launch`

### `kot_compare.py` — **BROKEN, DO NOT USE**
Double-rebases (convert already rebases), so comparison is structurally invalid. Use `kot_green.py --dump` or `kot_launch.py` instead.

---

## 4. Tuned constants (took the whole night to find)

```
--white-v 220     # min brightness for white mask
--white-s 30      # max saturation for white mask
--still 40        # px/s below this = in contact
--entry 200       # px/s required to count as launched
--gtol 0.4        # fraction of g that ay may deviate in free flight
--maxjump 40      # max px/frame; beyond this the frame is LOST, never re-picked
--lag -51         # ms (measured on own-run with panda + white mode)
CROP_TOP = 0.06
CROP_BOTTOM = 0.90
BADGE_W, BADGE_H = 60, 40
```

Measured physics: **gravity ≈ +850 px/s²** at 640x360 (own dungeon).
Own-run speed profile: median 127, p10 0, p90 365, max ~1740 px/s.

---

## 5. Results achieved

- Capture: 12.1ms/frame, 83fps ceiling
- Campaign level 4 cleared by blind replay of a 17-tap recording
- Panda tracking: **100% frame coverage** on own dungeon
- Tap recovery from video alone: **5/6 taps**, 2 explainable false positives, lag mean −51ms (sd 43ms)
- Ghost recording: 97% tracking on a 48s capture

---

## 6. Bugs found and fixed (in order)

1. **PIL in hot loop** → replaced with `np.frombuffer` (no copy). 22.8 → 12.1ms.
2. **LDPlayer chrome in frame** → derived 34/38px crop.
3. **Per-frame timestamps discarded** — `kot_track` computed them for a printout and never saved them; analysis assumed uniform spacing. Frame gaps are median 16.6 / p95 20.6 / max 36ms, so error accumulated to hundreds of ms. **Fixed**: saved as `times` in meta.
4. **Drift measured after mouse-up** — included the hold duration, so a 100ms hold looked like 100ms lateness. **Fixed**: measure at the mouse-DOWN edge.
5. **`keyboard.is_pressed` inside the tight spin loop** added real jitter. **Fixed**: ESC only polled during coarse phase.
6. **Teleport fallback in `track()`** — when nothing was near, it re-picked the *largest* blob anywhere, producing 7300 px/s spikes that destroyed derivatives. **Fixed**: coast instead (return NaN), plus a 2000 px/s velocity clamp.
7. **`kot_analyse` read colour runs as greyscale** — reshaped `(n,h,w)` instead of `(n,h,w,3)`, producing diagonal moiré garbage and bogus 150,000-moving-pixel readings. This caused me to wrongly declare a good ghost recording "broken". **Fixed**: `channels`-aware.
8. **Gravity estimator returned +13 instead of +850** — took the median of `ay` over fast frames, but the thief mostly *runs along surfaces* (fast, zero acceleration), so contact frames dominated the median. With g=13 and gtol=0.4 the flight window was ±5 px/s², so almost nothing qualified as flight. **Fixed**: positive-side histogram mode, skipping the first 3 bins.
9. **Player-level badge** — a green rosette top-left, larger than the thief and stationary. Cold start grabbed it and continuity held it forever (98% "located", 2 launches). Fixed by excluding a 60x40 corner box. **Note**: an earlier attempt raised `CROP_TOP` to 0.13, which regressed own-run from 5/6 to 3/6 because the thief climbs to the totem in that band. Reverted.
10. **Duplicate argparse args** after regex patching (`--lost-limit` conflict) — deleted the leftover line in `kot_convert.py`.

---

## 7. The key insight (why `vy` detection failed)

The original detector looked for `vy < -threshold`. It scored **2/6**.

**KoT's thief wall-jumps, and a wall-jump is mostly horizontal** — no
vertical velocity spike, so no threshold could ever catch it. Meanwhile
bounces and landings *do* spike `vy` upward with no tap behind them.

Replacement: a tap is what **launches** the thief from contact into free
flight. That definition catches wall-jumps and vertical jumps equally and
ignores sliding. Score went 2/6 → **5/6**.

The 1 remaining miss is the level-start tap, which produces no launch and
*should* be missed.

---

## 8. The unsolved problem

**Colour alone cannot identify the thief across arbitrary dungeons.**

- **Green costume** failed in a volcano dungeon full of green gems, green spinner traps and green potion bottles. Tracker locked onto a hanging gem → 1 launch from a full run.
- **Panda / white** failed in a crypt dungeon: white spider webs, pale platform edges, and the "GHOST" caption all pass a `V>220, S<30` mask. Median mask was 1912px where the panda's face is ~150px. Tracker locked onto a green gem.

Every dungeon has different decor and each breaks a different colour assumption.

### Proposed fix (designed, NOT implemented)

Use **motion** as the primary discriminator, colour only as a candidate
filter. The thief is the only thing making large sustained translations;
webs, gems, text and traps never translate (spinners rotate in place).

Sketch: find colour candidates → track each across N frames → pick the one
with the largest cumulative displacement. ~30 lines in `track()`.

---

## 9. Game facts learned

- Base-skip in the raid selector costs **300 gold per skip** (observed 26636 → 25136 over 6 skips).
- Raids cost **lockpicks**, which regenerate slowly. This makes RL impractical — you get ~10 attempts then wait.
- The gem display case (stained-glass window) **moves** between bases — it is part of the player's dungeon layout. Any fixed-slot approach (as in the `kot-skipper` repo) cannot work. Template-matched positions observed: (1012,248), (148,536), (796,536), (1012,104), (364,176).
- Match the case on **Canny edges, not colour** — the game tints it per dungeon theme (colour match dropped to 0.574 on a grey dungeon; edges held at 0.762).
- The **SKIP button is pixel-identical** across every base (mean deviation 0.30 over 7 frames) — a reliable state detector and click target.
- The "dungeon solution" replay is triggered by a **CONFIRMATION dialog** ("Do you want to see the dungeon solution?"), shows a **"GHOST"** caption at bottom-centre, and **renders the thief with your own equipped costume** (confirmed visually).
- KoT is **one-tap-anywhere** control — x/y only need to avoid UI buttons. (640, 360) is safe.
- The wiki claims the ghost may show the dungeon's *second save*, i.e. possibly a stale layout. **Unverified.**

---

## 10. Repos evaluated (all rejected)

- **JartanFTW/kot-skipper** — best of the three. Skips bases hunting for gems/gold; does NOT play levels. Has a trained CNN (`identify_gem.h5`). Requires BlueStacks at 1600x900. 10 stars, 6 open issues, hardcoded pixel regions likely rotted.
- **kauefraga/auto-player** — irrelevant. Autoclickers for Clicker Heroes / Idle Slayer, no screen capture at all. Only transferable idea: hotkey-toggle pattern.
- **tqnghia1998/king-of-thieves-autoclick** — C#/WPF macro recorder using `FindWindow("King of Thieves")`, which only matches a native Windows build that no longer exists. Uses deprecated `mouse_event`. Useful idea: `GetPixel` for cheap state checks.

---

## 11. Next steps

1. **Implement motion-based candidate selection** in `kot_green.track()` (section 8). This is the blocker for generalising to arbitrary dungeons.
2. **Fix `kot_tapper.load_latest()`** to take an explicit path argument instead of alphabetical sort.
3. **Delete `kot_compare.py`** or rewrite it without double-rebasing.
4. **Record ghost runs tightly** — F6 when the ghost *starts moving*, F6 when it finishes. A 48s recording containing menus produced `speed: median 2 px/s` and scattered noise.
5. **Re-measure the lag constant** across 2–3 own-runs; the current −51ms rests on 5 matches from one run, sd 43ms.
6. **Unsolved design question**: even with perfect tap times, the replay must *start* in the same state the ghost's run did. Nothing currently aligns those. `--start-gap` is a guess.

---

## 12. User preferences observed

- Wants **full file contents** when code changes, not diffs/snippets.
- Brief, direct, concrete. No softening.
- Works late; asked repeatedly to keep going past 4am.
