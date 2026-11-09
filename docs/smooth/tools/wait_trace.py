"""Who does the game wait for? Reads an atrace/ftrace text dump and, for the game's threads, splits time into
running / runnable (waiting for a CPU) / sleeping, and attributes every sleep to the thread that woke it up.

Capture (no root needed):
    adb shell atrace -t 5 -b 98304 sched freq sync -o /data/local/tmp/at.txt
    adb pull /data/local/tmp/at.txt
usage:  python wait_trace.py at.txt [process-name-substring=Wow-arm64]

How to read it: if the game's main thread sleeps most of the frame and is woken by
  * a GPU/driver interrupt or kernel worker (mali, irq/, kworker, fence)  -> it waits for the GPU;
  * a driver-bridge server thread (libvkbridge_server)                     -> it waits for a bridge round trip
    (which itself may be waiting for the GPU: look at what woke the bridge thread);
  * another game thread (dxvk-cs, dxvk-submit, ...)                         -> an internal pipeline wait;
  * a compositor (gamescope, Xwayland, wineserver)                          -> presentation / Wine server.
"""
import collections
import re
import sys

LINE = re.compile(r"^\s*(.+?)-(\d+)\s+\(\s*(\d+|-+)\)\s+\[(\d+)\]\s+\S+\s+([0-9.]+):\s+(\w+):\s?(.*)$")
LINE2 = re.compile(r"^\s*(.+?)-(\d+)\s+\[(\d+)\]\s+\S+\s+([0-9.]+):\s+(\w+):\s?(.*)$")
KV = re.compile(r"(\w+)=(\S+)")


def parse(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE.match(line)
            if m:
                comm, tid, tgid, cpu, ts, ev, rest = m.groups()
            else:
                m = LINE2.match(line)
                if not m:
                    continue
                comm, tid, cpu, ts, ev, rest = m.groups()
                tgid = "-"
            yield comm, int(tid), tgid, int(cpu), float(ts), ev, rest


def main():
    path = sys.argv[1]
    want = sys.argv[2] if len(sys.argv) > 2 else "Wow-arm64"
    names = {}          # tid -> comm (thread name)
    tgid_of = {}
    game_tgid = None
    # first pass: thread names and the game's tgid
    for comm, tid, tgid, cpu, ts, ev, rest in parse(path):
        names.setdefault(tid, comm)
        if tgid.isdigit():
            tgid_of[tid] = int(tgid)
        if ev == "sched_switch":
            kv = dict(KV.findall(rest.replace(" ==> ", " ")))
    # pick game threads: those whose tgid's main thread comm matches `want`
    for tid, tg in tgid_of.items():
        if tid == tg and want.lower() in names.get(tid, "").lower():
            game_tgid = tg
    if game_tgid is None:
        cands = [t for t, n in names.items() if want.lower() in n.lower()]
        game_tgid = tgid_of.get(cands[0], cands[0]) if cands else None
    game = {t for t, g in tgid_of.items() if g == game_tgid}
    print(f"game process {names.get(game_tgid)} pid {game_tgid}: {len(game)} threads seen")

    state = {}          # tid -> (state, since)
    acc = collections.defaultdict(lambda: collections.Counter())   # tid -> Counter(run/runnable/sleep_S/sleep_D)
    waker_of = {}       # tid -> (waker label, wake ts)
    sleep_start = {}    # tid -> ts when it went to sleep
    by_waker = collections.defaultdict(lambda: collections.Counter())      # tid -> waker -> sleep ms
    cnt_waker = collections.defaultdict(lambda: collections.Counter())
    freq = collections.defaultdict(list)
    gpu_freq = []
    other = collections.Counter()
    t0 = t1 = None
    for comm, tid, tgid, cpu, ts, ev, rest in parse(path):
        t0 = ts if t0 is None else t0
        t1 = ts
        if ev == "sched_switch":
            a, b = rest.split(" ==> ")
            pa = dict(KV.findall(a))
            pb = dict(KV.findall(b))
            prev = int(pa["prev_pid"])
            nxt = int(pb["next_pid"])
            if prev in game:
                st, since = state.get(prev, ("run", ts))
                acc[prev]["run"] += ts - since
                ps = pa.get("prev_state", "R")
                if ps.startswith("R"):
                    state[prev] = ("runnable", ts)
                else:
                    state[prev] = ("sleep_" + ps[0], ts)
                    sleep_start[prev] = ts
            if nxt in game:
                st, since = state.get(nxt, ("runnable", ts))
                if st.startswith("sleep"):
                    # woke without a recorded waking event
                    w = waker_of.pop(nxt, ("(unknown)", since))
                    acc[nxt][st] += w[1] - since
                    acc[nxt]["runnable"] += ts - w[1]
                    by_waker[nxt][w[0]] += (w[1] - since) * 1000
                    cnt_waker[nxt][w[0]] += 1
                else:
                    acc[nxt][st] += ts - since
                state[nxt] = ("run", ts)
        elif ev in ("sched_waking", "sched_wakeup"):
            kv = dict(KV.findall(rest))
            target = int(kv.get("pid", -1))
            if target in game and target not in waker_of and ev == "sched_waking":
                label = comm if tid != 0 else "<idle/irq>"
                if tid in game:
                    label = "game:" + comm
                waker_of[target] = (f"{label}", ts)
            elif target in game and target not in waker_of:
                waker_of[target] = (comm if tid else "<idle/irq>", ts)
        elif ev == "cpu_frequency":
            kv = dict(KV.findall(rest))
            freq[int(kv["cpu_id"])].append((ts, int(kv["state"]) // 1000))
        elif "gpu" in ev and "freq" in ev:
            gpu_freq.append((ts, rest))
        else:
            other[ev] += 1

    span = (t1 - t0) if t0 is not None else 0
    print(f"trace span {span:.2f} s")
    tops = sorted(game, key=lambda t: -acc[t]["run"])[:6]
    for t in tops:
        a = acc[t]
        tot = sum(a.values()) or 1
        print(f"\nthread {t} {names.get(t)}: running {100 * a['run'] / tot:.0f} %, waiting for a CPU {100 * a['runnable'] / tot:.0f} %, "
              f"sleeping {100 * (a['sleep_S'] + a['sleep_D']) / tot:.0f} % (D {100 * a['sleep_D'] / tot:.0f} %)")
        ws = by_waker[t]
        tw = sum(ws.values()) or 1
        for w, ms in ws.most_common(8):
            print(f"   woken by {w:28} {ms / span / 10:5.1f} % of time asleep-for-it  ({cnt_waker[t][w]} wakeups, "
                  f"{ms / max(1, cnt_waker[t][w]):.2f} ms each)")
    if freq:
        print("\nCPU frequency changes in the trace (MHz, min/max per cpu):")
        for c in sorted(freq):
            v = [x for _, x in freq[c]]
            print(f"   cpu{c}: {min(v)}-{max(v)}  ({len(v)} changes)")
    if gpu_freq:
        print("GPU frequency events:", gpu_freq[:5], "...", len(gpu_freq))
    print("\nother events:", ", ".join(f"{k} {v}" for k, v in other.most_common(15)))


if __name__ == "__main__":
    main()
