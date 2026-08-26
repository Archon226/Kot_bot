"""
kot_adb.py - capture and tap through ADB instead of the desktop.

WHY

mss grabs raw SCREEN pixels and SendInput clicks wherever the cursor is,
so both need the emulator visible, unobscured and focused. That is why
the tools steal focus before every click, why a browser covering the game
area breaks capture, and why --region-x exists at all.

ADB talks to the Android instance directly. The emulator can be minimised,
covered, or on another desktop. There is no advert panel to skip because
the framebuffer contains only Android - so --region-x becomes unnecessary
and the coordinates are always 0..1279 x 0..719.

WHAT IT IS NOT GOOD FOR

Timing. Every tap is a separate shell round-trip, tens of milliseconds
with real variance. kot_rec.py replays taps at +0.0ms drift through
SendInput and a KoT jump window is about 30ms, so moving THAT to ADB
would throw away the thing that makes it work.

Use ADB for the tools that poll slowly and must survive being in the
background - the reconnect watchdog, the skipper. Keep SendInput for tap
replay. Run the benchmark below and decide with numbers rather than by
assumption.

SETUP

  1. BlueStacks: Settings -> Advanced -> enable Android Debug Bridge.
     It shows a port, usually 127.0.0.1:5555 (per-instance ports differ).
  2. adb comes with BlueStacks; the platform-tools download also works.
  3. python kot_adb.py bench --serial 127.0.0.1:5555

Usage:
    python kot_adb.py devices
    python kot_adb.py bench                       # how fast is it here?
    python kot_adb.py shot --out screen.png
    python kot_adb.py tap 640 360
"""

import argparse
import subprocess
import time

import cv2
import numpy as np


class Adb:
    def __init__(self, serial=None, exe="adb"):
        self.exe = exe
        self.serial = serial

    def _base(self):
        return [self.exe] + (["-s", self.serial] if self.serial else [])

    def run(self, *args, binary=False, timeout=20):
        p = subprocess.run(self._base() + list(args),
                           capture_output=True, timeout=timeout)
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"adb {' '.join(args)} failed: {err}")
        return p.stdout if binary else p.stdout.decode(errors="replace")

    def connect(self, addr):
        out = self.run("connect", addr)
        return "connected" in out.lower(), out.strip()

    def devices(self):
        out = self.run("devices")
        rows = []
        for line in out.splitlines()[1:]:
            if line.strip():
                parts = line.split()
                rows.append((parts[0], parts[-1]))
        return rows

    def screencap(self):
        """Framebuffer as a BGR array.

        `exec-out` rather than `shell` because shell mangles binary on
        some transports - a stray CR turns the PNG into garbage. The
        image is the ANDROID screen, so there is no emulator chrome and
        no advert panel to offset past.
        """
        raw = self.run("exec-out", "screencap", "-p", binary=True)
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("screencap returned something that is not a "
                               "PNG. Try a different adb, or check the "
                               "device is really connected.")
        return img

    def tap(self, x, y):
        self.run("shell", "input", "tap", str(int(x)), str(int(y)))

    def swipe(self, x1, y1, x2, y2, ms=100):
        self.run("shell", "input", "swipe", str(int(x1)), str(int(y1)),
                 str(int(x2)), str(int(y2)), str(int(ms)))

    def size(self):
        out = self.run("shell", "wm", "size")
        for tok in out.split():
            if "x" in tok and tok[0].isdigit():
                w, h = tok.split("x")
                return int(w), int(h)
        return None


def cmd_devices(a, args):
    for serial, state in a.devices():
        print(f"  {serial:24} {state}")
    if not a.devices():
        print("  none. Enable ADB in the emulator, then:")
        print("    adb connect 127.0.0.1:5555")


def cmd_bench(a, args):
    """Measure, don't assume.

    Capture rate decides whether a tool can use ADB at all: the reconnect
    watchdog polls every 2s and does not care, while anything tracking a
    moving thief needs frames far faster than screencap can deliver.
    """
    print(f"device size: {a.size()}")
    t = []
    for i in range(args.n):
        s = time.perf_counter()
        img = a.screencap()
        t.append((time.perf_counter() - s) * 1000)
    t = np.array(t)
    print(f"\nscreencap x{args.n}: median {np.median(t):.0f}ms  "
          f"p90 {np.percentile(t, 90):.0f}ms  max {t.max():.0f}ms")
    print(f"  frame {img.shape[1]}x{img.shape[0]}")
    print(f"  -> about {1000 / np.median(t):.1f} fps")

    if np.median(t) < 120:
        print("  fine for the watchdog (2s poll) and the skipper.")
    else:
        print("  slow. Still fine for the watchdog; the skipper will feel "
              "sluggish between bases.")
    print("  NOT usable for tracking a moving thief either way - that "
          "needs ~6ms frames, which is 20-100x faster than this.")

    if args.taps:
        tt = []
        for i in range(args.taps):
            s = time.perf_counter()
            a.tap(args.tap_x, args.tap_y)
            tt.append((time.perf_counter() - s) * 1000)
        tt = np.array(tt)
        print(f"\ntap x{args.taps}: median {np.median(tt):.0f}ms  "
              f"sd {tt.std():.0f}ms  max {tt.max():.0f}ms")
        print(f"  sd is what matters for replay. A KoT jump window is "
              f"~30ms; kot_rec via SendInput measures +-0ms.")
        if tt.std() > 15:
            print("  -> too jittery for tap replay. Keep kot_rec on "
                  "SendInput.")


def cmd_shot(a, args):
    img = a.screencap()
    cv2.imwrite(args.out, img)
    print(f"wrote {args.out} ({img.shape[1]}x{img.shape[0]})")
    print("This is the Android screen only - no emulator chrome, no "
          "advert panel. Coordinates here need no --region-x.")


def cmd_tap(a, args):
    a.tap(args.x, args.y)
    print(f"tapped ({args.x},{args.y})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=None,
                    help="device, e.g. 127.0.0.1:5555. Omit if only one")
    ap.add_argument("--adb", default="adb", help="path to adb.exe")
    ap.add_argument("--connect", default=None,
                    help="connect to this address first, e.g. "
                         "127.0.0.1:5555")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices")

    b = sub.add_parser("bench")
    b.add_argument("-n", type=int, default=20)
    b.add_argument("--taps", type=int, default=0,
                   help="also time this many taps (they DO tap the screen)")
    b.add_argument("--tap-x", type=int, default=640, dest="tap_x")
    b.add_argument("--tap-y", type=int, default=360, dest="tap_y")

    s = sub.add_parser("shot")
    s.add_argument("--out", default="adb_screen.png")

    t = sub.add_parser("tap")
    t.add_argument("x", type=int)
    t.add_argument("y", type=int)

    args = ap.parse_args()
    a = Adb(args.serial, args.adb)

    if args.connect:
        ok, msg = a.connect(args.connect)
        print(f"connect: {msg}")

    try:
        {"devices": cmd_devices, "bench": cmd_bench,
         "shot": cmd_shot, "tap": cmd_tap}[args.cmd](a, args)
    except FileNotFoundError:
        raise SystemExit(f"adb not found at '{args.adb}'. BlueStacks ships "
                         f"one - try --adb \"C:\\Program Files\\BlueStacks_nxt"
                         f"\\HD-Adb.exe\"")
    except RuntimeError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()