"""Histogram of presented frame intervals in display refreshes (vsyncs).

At 120 Hz an even 30 fps is "4v:100%"; judder shows up as a spread (3v/5v, 2v/6v...).

usage:   python vsync_hist.py <layer-substring> <seconds> [label] [refresh_hz]
example: python vsync_hist.py "SurfaceView[com.droiddeck" 10 "frame limit 30" 120
adb as in sfmeter.py (ADB, ANDROID_SERIAL).
"""
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sfmeter  # noqa: E402


def main():
    sub, dur = sys.argv[1], float(sys.argv[2])
    label = sys.argv[3] if len(sys.argv) > 3 else ""
    hz = float(sys.argv[4]) if len(sys.argv) > 4 else 120.0
    period = 1000.0 / hz
    layer, _ = sfmeter.find_layer(sub)
    ts = sfmeter.sample(layer, dur)
    d = [(b - a) / 1e6 for a, b in zip(ts, ts[1:]) if 0 < (b - a) / 1e6 < 500]
    if not d:
        print("no frames")
        return
    h = collections.Counter(round(x / period) for x in d)
    print(label, "n=%d" % len(d), " ".join("%dv:%d%%" % (k, 100 * v // len(d)) for k, v in sorted(h.items())))
    print("last 80:", " ".join(str(round(x / period)) for x in d[-80:]))


if __name__ == "__main__":
    main()
