"""Summarise MaliBridge's per-frame latency log (vkbridge-latency.log).

Enable it for a game with the launch option `VKBRIDGE_LATLOG=1 %command%`. The log lands in the
session's log folder (DroidDeck: files/logs/<session>/vkbridge-latency.log, read it with run-as).
Columns per frame: queued (the game's vkQueuePresent), server, gpu_done, committed (handed to the
Wayland compositor), shown.

usage: python latlog_summary.py vkbridge-latency.log [last_n_frames]
"""
import sys


def main():
    rows = [l.split() for l in open(sys.argv[1]) if l.startswith("F ")]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 900
    rows = rows[-n:]
    q = [int(r[4]) for r in rows]
    g = [int(r[6]) for r in rows]
    c = [int(r[7]) for r in rows]
    s = [int(r[8]) for r in rows]
    ms = lambda x: x / 1e6
    d = lambda a: [ms(y - x) for x, y in zip(a, a[1:])]

    def summ(name, v):
        v = sorted(v)
        k = len(v)
        print(f"{name:28} p10 {v[k // 10]:6.1f} p50 {v[k // 2]:6.1f} p90 {v[k * 9 // 10]:6.1f} "
              f"p99 {v[min(k - 1, k * 99 // 100)]:6.1f} max {v[-1]:6.1f}")

    summ("queue interval", d(q))
    summ("commit interval", d(c))
    summ("shown interval", d([x for x in s if x > 0]))
    summ("queue->gpu_done", [ms(b - a) for a, b in zip(q, g)])
    summ("commit->shown", [ms(b - a) for a, b in zip(c, s) if b > 0])
    print("queue intervals, last 40:", [round(x, 1) for x in d(q)[-40:]])


if __name__ == "__main__":
    main()
