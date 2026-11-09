"""Replay recorded commit times through a paced FIFO and count repeated slots.

Feeds the `committed` timestamps of a MaliBridge vkbridge-latency.log through the model of the
droiddeck-smooth compositor change: one queued frame per paced slot (33.3 ms), oldest first,
the queue bounded at 3 frames. A slot with nothing queued repeats the previous picture.

usage: python fifo_sim.py vkbridge-latency.log [frames_per_window]
"""
import sys

PATH_MS = 4  # gamescope + compositor path between the bridge's commit and the queue


def simulate(arr, period=33_333_333, start_ms=(0, 8, 17, 25, 33)):
    out = []
    for lat in start_ms:
        t, q, i, shown, repeats = arr[0] + lat * 1_000_000, 0, 0, 0, 0
        while t < arr[-1]:
            while i < len(arr) and arr[i] + PATH_MS * 1_000_000 <= t:
                q += 1
                i += 1
            if q > 0:
                q = min(q - 1, 3)
                shown += 1
            else:
                repeats += 1
            t += period
        out.append((lat, shown, repeats))
    return out


def main():
    rows = [l.split() for l in open(sys.argv[1]) if l.startswith("F ")]
    win = int(sys.argv[2]) if len(sys.argv) > 2 else 900
    c = [int(r[7]) for r in rows if int(r[7]) > 0]
    for b in range(len(c), win, -win):
        arr = c[b - win:b]
        fps = (len(arr) - 1) / ((arr[-1] - arr[0]) / 1e9)
        res = simulate(arr)
        print(f"frames {b - win}-{b} at {fps:5.1f} fps: " +
              "  ".join(f"start+{lat}ms repeats {r / (s + r) * 100:4.1f}%" for lat, s, r in res))


if __name__ == "__main__":
    main()
