"""
kot_analyse.py - offline analysis of runs recorded by kot_track.py.

Subcommands:
    frames  export sample PNGs so you can see what the detector sees
    motion  per-frame motion summary - how much is moving, and where

Run `frames` first. Building a tracker before looking at the footage is
guesswork; the whole reason frames go to disk is so you can inspect them.

Usage:
    python kot_analyse.py frames runs/ghost_001234.json --every 40
    python kot_analyse.py frames runs/ghost_001234.json --range 100 120
    python kot_analyse.py motion runs/ghost_001234.json
"""

import argparse
import json
import os

import cv2
import numpy as np


def load(meta_path):
    with open(meta_path) as f:
        meta = json.load(f)
    raw_path = os.path.join(os.path.dirname(meta_path), meta["raw"])
    w, h, n = meta["width"], meta["height"], meta["frames"]
    ch = meta.get("channels", 1)

    # Colour runs are (n,h,w,3). Reading one as (n,h,w) walks three bytes
    # per pixel and shifts every row - the output looks like a diagonal
    # dither pattern and every derived number is meaningless.
    shape = (n, h, w, 3) if ch == 3 else (n, h, w)
    frames = np.memmap(raw_path, dtype=np.uint8, mode="r", shape=shape)
    return meta, frames, ch


def cmd_frames(args):
    meta, frames, ch = load(args.meta)
    out = os.path.splitext(args.meta)[0] + "_png"
    os.makedirs(out, exist_ok=True)

    if args.range:
        idx = list(range(args.range[0], min(args.range[1], meta["frames"])))
    else:
        idx = list(range(0, meta["frames"], args.every))

    for i in idx:
        cv2.imwrite(os.path.join(out, f"f{i:05d}.png"), np.array(frames[i]))

    print(f"Wrote {len(idx)} PNGs to {out}/")
    print("Open a few. Look for: is the ghost blob clearly visible? "
          "Is it distinguishable from traps and the real thief?")


def cmd_motion(args):
    meta, frames, ch = load(args.meta)
    n = meta["frames"]

    print(f"{n} frames, {meta['width']}x{meta['height']}, "
          f"{meta['duration']}s @ {meta['fps']}fps\n")
    print("frame    t(s)   moving_px   centroid      spread")

    def gray(i):
        f = np.array(frames[i])
        if ch == 3:
            f = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        return f.astype(np.int16)

    prev = gray(0)
    rows = []

    for i in range(1, n):
        cur = gray(i)
        diff = np.abs(cur - prev)
        mask = diff > args.thresh

        count = int(mask.sum())
        if count > 0:
            ys, xs = np.nonzero(mask)
            cx, cy = xs.mean(), ys.mean()
            spread = float(np.hypot(xs.std(), ys.std()))
        else:
            cx = cy = spread = float("nan")

        rows.append((i, count, cx, cy, spread))
        prev = cur

    t = meta["duration"] / n
    step = max(1, n // args.lines)
    for i, count, cx, cy, spread in rows[::step]:
        print(f"{i:5d}  {i * t:6.2f}   {count:8d}   "
              f"({cx:6.1f},{cy:6.1f})  {spread:6.1f}")

    counts = np.array([r[1] for r in rows])
    spreads = np.array([r[4] for r in rows])
    print(f"\nmoving px: median {np.median(counts):.0f}  "
          f"p95 {np.percentile(counts, 95):.0f}  max {counts.max()}")
    print(f"spread:    median {np.nanmedian(spreads):.1f}")
    print("\nIf spread is small and steady, one thing is moving and simple")
    print("centroid tracking will work. If it is large or jumpy, several")
    print("things move at once and the tracker needs to isolate the blob.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("frames", help="export sample PNGs")
    f.add_argument("meta")
    f.add_argument("--every", type=int, default=40)
    f.add_argument("--range", type=int, nargs=2, metavar=("START", "END"))
    f.set_defaults(func=cmd_frames)

    m = sub.add_parser("motion", help="per-frame motion summary")
    m.add_argument("meta")
    m.add_argument("--thresh", type=int, default=18,
                   help="per-pixel difference to count as movement")
    m.add_argument("--lines", type=int, default=60,
                   help="how many rows to print")
    m.set_defaults(func=cmd_motion)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
