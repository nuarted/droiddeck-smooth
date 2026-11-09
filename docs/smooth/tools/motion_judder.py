"""Is the MOTION even, not just the frame times? Records the screen while the camera turns at a constant rate and
measures how far the picture moved between consecutive new frames.

Even frame times are not enough. If the game renders frames at uneven moments (22 / 25 / 50 ms apart) and the
compositor shows them on an even 33.3 ms grid, each frame still carries the game time it was rendered at: the turn
advances by a small step, then a big one. The eye sees that as judder even though the frame times look perfect.

usage:  python motion_judder.py [seconds=10] [label]
env:    ADB, ANDROID_SERIAL, and how to turn the camera at a constant rate:
        TURN_STICK="x y dx" (on-screen stick) or TURN_KEY=21 (held key) - see screen_fps.py
prints: frames shown per second, then the step (horizontal picture shift, px) per new frame:
        median, and the share of steps within +-15 % / +-30 % of the median. A perfectly smooth turn gives 100 %.
        Also the frame interval (ms) next to each step, so a big step after a long interval (a repeat) is told
        apart from a big step after a normal interval (content judder).
"""
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import screen_fps  # noqa: E402

W, H = 480, 160                      # analysis size of the strip
STRIP = (0.27, 0.20, 0.80, 0.46)     # x0, y0, x1, y1: a band above the character, clear of the HUD and minimap


def frames(path):
    pts = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split()
    pts = [float(p) for p in pts if p.strip()]
    x0, y0, x1, y1 = STRIP
    vf = f"crop=iw*{x1 - x0}:ih*{y1 - y0}:iw*{x0}:ih*{y0},scale={W}:{H},format=gray"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-fps_mode", "passthrough",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    n = len(raw) // (W * H)
    img = np.frombuffer(raw[:n * W * H], dtype=np.uint8).reshape(n, H, W).astype(np.float32)
    return pts[:n], img


def shift(a, b):
    """Horizontal shift of b against a, sub-pixel, by phase correlation of column profiles plus 2-D check."""
    win = np.hanning(W)[None, :] * np.hanning(H)[:, None]
    fa = np.fft.fft2((a - a.mean()) * win)
    fb = np.fft.fft2((b - b.mean()) * win)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-6
    c = np.fft.ifft2(r).real
    iy, ix = np.unravel_index(np.argmax(c), c.shape)
    # parabolic sub-pixel along x
    l, m, rr = c[iy, (ix - 1) % W], c[iy, ix], c[iy, (ix + 1) % W]
    den = l - 2 * m + rr
    dx = ix + (0.5 * (l - rr) / den if den != 0 else 0)
    if dx > W / 2:
        dx -= W
    return float(dx), float(c.max())


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    seconds = int(args[0]) if args else 10
    label = args[1] if len(args) > 1 else ""
    screen_fps.record(seconds, swipe=True)
    tmp = os.path.join(tempfile.gettempdir(), "motion_judder.mp4")
    subprocess.run([screen_fps.ADB, "pull", screen_fps.REMOTE, tmp], capture_output=True)
    pts, img = frames(tmp)
    diffs = np.abs(np.diff(img, axis=0)).mean(axis=(1, 2))
    thr = float(os.environ.get("DIFF_THR", "0.5"))
    keep = [0] + [i + 1 for i, d in enumerate(diffs) if d > thr]
    keep = [i for i in keep if pts[i] >= pts[0] + 1.2]
    steps, ivs = [], []
    for a, b in zip(keep, keep[1:]):
        dx, conf = shift(img[a], img[b])
        steps.append(dx)
        ivs.append((pts[b] - pts[a]) * 1000)
    if len(steps) < 10:
        print(json.dumps({"label": label, "error": "too few moving frames", "frames": len(keep)}))
        return
    s = np.abs(np.array(steps))
    iv = np.array(ivs)
    med = float(np.median(s))
    ok = iv < 45                                   # steps after a normal interval (not after a repeated frame)
    rel = s[ok] / med if med > 0 else s[ok]
    rate = s / np.maximum(iv, 1)                   # px per ms: the content speed implied by each frame
    res = {
        "label": label,
        "frames_per_s": round(len(keep) / (pts[keep[-1]] - pts[keep[0]]), 1),
        "step_px_median": round(med, 2),
        "steps_within_15pct": round(100 * float(np.mean(np.abs(rel - 1) <= 0.15)), 1),
        "steps_within_30pct": round(100 * float(np.mean(np.abs(rel - 1) <= 0.30)), 1),
        "step_cv_pct": round(100 * float(np.std(s[ok]) / med), 1) if med else None,
        "intervals_even_pct": round(100 * float(np.mean(np.abs(iv - np.median(iv)) <= 4)), 1),
        "speed_px_per_ms_cv_pct": round(100 * float(np.std(rate) / np.mean(rate)), 1),
    }
    print(json.dumps(res, ensure_ascii=False))
    print("step px / interval ms, first 40:", " ".join(f"{a:.1f}/{b:.0f}" for a, b in zip(s[:40], iv[:40])))


if __name__ == "__main__":
    main()
