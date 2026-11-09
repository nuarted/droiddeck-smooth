"""What the eye sees: how often the picture on screen really changes, and how evenly.

fps counters (DXVK/MangoHud/DroidDeck HUD, even SurfaceFlinger) count frames handed over, including frames that
repeat the previous picture. This records the screen with `screenrecord` while turning the camera with a slow
swipe, then compares consecutive recorded frames (centre of the screen, away from HUD overlays) and keeps only
the ones whose picture actually changed.

usage:   python screen_fps.py [seconds=6] [label] [--no-swipe] [--keep]
needs:   adb (ADB=..., ANDROID_SERIAL=...), ffmpeg + ffprobe on PATH, numpy
prints:  real (changed) frames per second, their intervals (ms and 120 Hz refreshes), % within +-4 ms of the median,
         and the longest gaps.
The swipe drags across the right half of the screen: in a game that maps touch drags to mouse-look it turns
the camera. Use --no-swipe if you move the camera yourself.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

ADB = os.environ.get("ADB", "adb")
REMOTE = "/sdcard/Download/screen_fps.mp4"
W, H = 160, 100           # analysis size
CROP = (0.25, 0.25, 0.75, 0.70)  # x0, y0, x1, y1 as fractions: centre, away from HUDs and on-screen buttons


def adb(*args, **kw):
    return subprocess.run([ADB, *args], capture_output=True, text=True, **kw)


def record(seconds, swipe):
    adb("shell", "rm -f " + REMOTE)
    rec = subprocess.Popen([ADB, "shell", f"screenrecord --size 1280x800 --bit-rate 20M --time-limit {seconds} {REMOTE}"])
    time.sleep(0.8)
    if swipe:
        # display is 3008x1880 here; a slow horizontal drag on the right half turns the camera
        size = adb("shell", "wm size").stdout.strip().split()[-1]
        sw, sh = (int(v) for v in size.split("x"))
        if sw < sh:
            sw, sh = sh, sw
        y = int(sh * 0.35)  # above on-screen controls
        x0, x1 = int(sw * 0.55), int(sw * 0.9)
        dur = int((seconds - 1.5) * 1000)
        if os.environ.get("TURN_KEY"):
            # hold a key instead (e.g. TURN_KEY=21 = left arrow, turns the character in many games)
            adb("shell", f"input keyevent --duration {dur} {os.environ['TURN_KEY']}")
        else:
            adb("shell", f"input swipe {x0} {y} {x1} {y} {dur}")
    rec.wait(timeout=seconds + 20)
    time.sleep(1.0)


def frames(path):
    pts = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split()
    pts = [float(p) for p in pts if p.strip()]
    x0, y0, x1, y1 = CROP
    vf = f"crop=iw*{x1 - x0}:ih*{y1 - y0}:iw*{x0}:ih*{y0},scale={W}:{H},format=gray"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-fps_mode", "passthrough",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    n = len(raw) // (W * H)
    img = np.frombuffer(raw[:n * W * H], dtype=np.uint8).reshape(n, H, W).astype(np.int16)
    return pts[:n], img


def analyse(pts, img, label):
    diffs = np.abs(np.diff(img, axis=0)).mean(axis=(1, 2))
    thr = float(os.environ.get("DIFF_THR", "0.5"))  # mean gray-level change: codec noise on a still picture ~0.03-0.3, a camera move > 1
    changed = [pts[0]] + [pts[i + 1] for i, d in enumerate(diffs) if d > thr]
    changed = [t for t in changed if t >= pts[0] + 1.2]  # skip the start, before the camera moves
    iv = np.diff(changed) * 1000.0
    if len(iv) < 5:
        print(json.dumps({"label": label, "error": "too few changed frames", "recorded": len(pts)}))
        return
    span = changed[-1] - changed[0]
    med = float(np.median(iv))
    hist = {}
    for v in iv:
        k = int(round(v / 8.333))
        hist[k] = hist.get(k, 0) + 1
    res = {
        "label": label,
        "recorded_frames": len(pts),
        "real_fps": round((len(changed) - 1) / span, 1),
        "median_ms": round(med, 1),
        "p90_ms": round(float(np.percentile(iv, 90)), 1),
        "max_ms": round(float(iv.max()), 1),
        "even_pct": round(100.0 * float(np.mean(np.abs(iv - med) <= 4)), 1),
        "gaps_over_100ms": int((iv > 100).sum()),
        "refreshes_120hz": {f"{k}v": f"{100 * c // len(iv)}%" for k, c in sorted(hist.items())},
        "threshold": round(thr, 2),
    }
    print(json.dumps(res, ensure_ascii=False))
    print("intervals (refreshes), first 60:", " ".join(str(int(round(v / 8.333))) for v in iv[:60]))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    seconds = int(args[0]) if args else 6
    label = args[1] if len(args) > 1 else ""
    swipe = "--no-swipe" not in sys.argv
    record(seconds, swipe)
    tmp = os.path.join(tempfile.gettempdir(), "screen_fps.mp4")
    adb("pull", REMOTE, tmp)
    pts, img = frames(tmp)
    analyse(pts, img, label)
    if "--keep" in sys.argv:
        print("kept", tmp)


if __name__ == "__main__":
    main()
