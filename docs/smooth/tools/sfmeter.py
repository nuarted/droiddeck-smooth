"""Frame pacing meter over adb.

Samples `dumpsys SurfaceFlinger --latency <layer>` for a while and prints one JSON line with
avg fps, frame-time percentiles, the share of frames within +-4 ms of the target interval and
the number of hitches (> 45 ms, > 70 ms).

usage:   python sfmeter.py <layer-substring> <seconds> [target_ms] [label]
example: python sfmeter.py "SurfaceView[com.droiddeck" 30 33.33 "frame limit 30"

adb:     ADB=/path/to/adb (default: adb on PATH); pick the device with ANDROID_SERIAL.
Results are also appended to sf_results.jsonl next to this script.
"""
import json
import os
import re
import statistics
import subprocess
import sys
import time

ADB = os.environ.get("ADB", "adb")
NONE = ("0", "9223372036854775807")


def sh(cmd, timeout=20):
    try:
        return subprocess.run([ADB, "shell", cmd], capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace").stdout
    except Exception:
        return ""


def latency(layer):
    return sh("dumpsys SurfaceFlinger --latency '%s'" % layer.replace("'", "'\\''"))


def find_layer(sub):
    """The matching layer that actually receives frames (BLAST layers have several names)."""
    cands = []
    for line in sh("dumpsys SurfaceFlinger --list").splitlines():
        if sub.lower() not in line.lower():
            continue
        m = re.match(r"\s*RequestedLayerState\{(.*?#\d+)", line)
        cands.append(m.group(1) if m else line.strip())
    best, bestn = None, -1
    for c in cands:
        n = sum(1 for l in latency(c).splitlines()[1:] if l.split() and l.split()[1] not in NONE)
        if n > bestn:
            best, bestn = c, n
    return best, cands


def sample(layer, seconds):
    """Present timestamps (ns) seen during `seconds` (the dump keeps only the last ~128 frames)."""
    seen, t0 = set(), time.time()
    while time.time() - t0 < seconds:
        for l in latency(layer).splitlines()[1:]:
            p = l.split()
            if len(p) == 3 and p[1] not in NONE:
                seen.add(int(p[1]))
        time.sleep(0.8)
    return sorted(seen)


def main():
    sub, dur = sys.argv[1], float(sys.argv[2])
    tgt = float(sys.argv[3]) if len(sys.argv) > 3 else 33.33
    label = sys.argv[4] if len(sys.argv) > 4 else ""
    layer, cands = find_layer(sub)
    if not layer:
        print("no layer matching", sub)
        print("\n".join(cands))
        return
    ts = sample(layer, dur)
    d = [(b - a) / 1e6 for a, b in zip(ts, ts[1:]) if 0 < (b - a) / 1e6 < 1000]
    if len(d) < 10:
        print("too few frames", len(d), layer)
        return
    span = (ts[-1] - ts[0]) / 1e9
    ds = sorted(d)
    pct = lambda p: ds[min(len(ds) - 1, int(len(ds) * p))]
    res = {
        "label": label, "layer": layer[:80], "frames": len(d), "secs": round(span, 1),
        "fps": round(len(d) / span, 1), "median_ms": round(statistics.median(d), 1),
        "p95_ms": round(pct(0.95), 1), "p99_ms": round(pct(0.99), 1),
        "std_ms": round(statistics.pstdev(d), 1),
        "on_target_pct": round(100 * sum(1 for x in d if abs(x - tgt) <= 4) / len(d), 1),
        "hitch_gt45": sum(1 for x in d if x > 45), "hitch_gt70": sum(1 for x in d if x > 70),
    }
    print(json.dumps(res, ensure_ascii=False))
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sf_results.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
