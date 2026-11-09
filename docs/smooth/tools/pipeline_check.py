"""Where is the bottleneck? Measure every stage of the frame path in the same window and name the first one that fails.

    game renders -> bridge (GPU done, commit) -> gamescope -> DroidDeck compositor (FIFO) -> SurfaceFlinger -> screen

One run (default 10 s) collects, at the same time:
  * state before/after: GPU / battery temperature, charging, refresh rate, FIFO properties;
  * game stage: MaliBridge per-frame log (needs the launch option `VKBRIDGE_LATLOG=1 %command%`): submit rate,
    GPU time per frame (submit -> GPU done), commit rate;
  * compositor stage: DroidDeck's 10 s stats lines (frames received from the game, FIFO queued/shown/dropped/
    starved ticks);
  * SurfaceFlinger stage: present timestamps of the game's layer;
  * screen stage: `screenrecord` + picture-change analysis (what the eye sees), while turning the camera with a
    held key (TURN_KEY, default 21 = left arrow).
Then prints one table and a verdict: the first stage whose rate or evenness falls short of the target.

usage:   python pipeline_check.py <target_fps> [seconds=10] [label]
env:     ADB, ANDROID_SERIAL, PKG (default com.droiddeck.launcher.smooth), TURN_KEY (default 21, "" = don't turn)
needs:   ffmpeg/ffprobe, numpy; sfmeter.py and screen_fps.py next to this file.
Rules of a fair test (see ../TESTING.md): same spot and camera move, no menus open, GPU temperature logged,
3+ runs per setting in A/B/A/B order, compare medians.
"""
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sfmeter  # noqa: E402
import screen_fps  # noqa: E402

ADB = os.environ.get("ADB", "adb")
PKG = os.environ.get("PKG", "com.droiddeck.launcher.smooth")


def sh(cmd, timeout=30):
    return subprocess.run([ADB, "shell", cmd], capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace").stdout


def state():
    t = sh("dumpsys thermalservice | grep -E 'Temperature\\{' ; dumpsys battery | grep -E 'AC powered|USB powered|level';"
           " dumpsys display | grep -m1 -oE 'renderFrameRate [0-9.]+'; getprop debug.droiddeck.fifo; getprop debug.droiddeck.fifo_lat")
    gpu = re.findall(r"mValue=([0-9.]+), mType=1, mName=GPU", t)
    bat = re.findall(r"mValue=([0-9.]+), mType=2, mName=BATTERY", t)
    lines = [l.strip() for l in t.splitlines()]
    props = [l for l in lines if re.fullmatch(r"-?\d+", l)]
    return {
        "gpu_c": float(gpu[-1]) if gpu else None,
        "battery_c": float(bat[-1]) if bat else None,
        "charging": "AC powered: true" in t or "USB powered: true" in t,
        "refresh": (re.findall(r"renderFrameRate ([0-9.]+)", t) or [None])[0],
        "fifo": props[0] if props else "", "fifo_lat": props[1] if len(props) > 1 else "",
    }


def session_dir():
    return sh(f"run-as {PKG} sh -c 'ls -t files/logs | head -1'").strip()


def compositor_stats(d):
    out = sh(f"run-as {PKG} sh -c \"grep -E '\\[stats\\]|FIFO last' files/logs/{d}/app.log | tail -2\"")
    st = re.findall(r"(\d+) frames on screen .*?\| (\d+) GPU frames from games", out)
    ff = re.findall(r"(\d+) game frames queued, (\d+) shown on their slot, (\d+) dropped .*?, (\d+) ticks a slot waited", out)
    r = {}
    if st:
        r["comp_shown_fps"] = int(st[-1][0]) / 10
        r["comp_received_fps"] = int(st[-1][1]) / 10
    if ff:
        q, s, dr, w = (int(x) for x in ff[-1])
        r.update({"fifo_queued": q, "fifo_shown": s, "fifo_dropped": dr, "fifo_waited_ticks": w})
    return r


def game_stats(d, seconds):
    """Last `seconds` of the MaliBridge per-frame log of the game (the process with the most frames)."""
    raw = sh(f"run-as {PKG} sh -c 'tail -c 600000 files/logs/{d}/vkbridge-latency.log 2>/dev/null'", timeout=60)
    rows = [l.split() for l in raw.splitlines() if l.startswith("F ")]
    if not rows:
        return {"game": "no VKBRIDGE_LATLOG=1 in the launch options"}
    by = {}
    for r in rows:
        by.setdefault(r[2], []).append(r)
    rows = max(by.values(), key=len)
    q = [int(r[4]) for r in rows]
    end = q[-1]
    sel = [r for r in rows if int(r[4]) >= end - seconds * 1e9]
    q = [int(r[4]) for r in sel]
    g = [int(r[6]) for r in sel]
    c = [int(r[7]) for r in sel if int(r[7]) > 0]
    span = (q[-1] - q[0]) / 1e9 if len(q) > 1 else 0
    gpu = sorted((b - a) / 1e6 for a, b in zip(q, g) if b > a)
    iv = sorted((b - a) / 1e6 for a, b in zip(q, q[1:]))
    pct = lambda v, p: v[min(len(v) - 1, int(len(v) * p))] if v else None
    return {
        "game_fps": round((len(q) - 1) / span, 1) if span else None,
        "game_submit_ms_p50": round(pct(iv, .5), 1) if iv else None,
        "game_submit_ms_p90": round(pct(iv, .9), 1) if iv else None,
        "gpu_ms_p50": round(pct(gpu, .5), 1) if gpu else None,
        "gpu_ms_p90": round(pct(gpu, .9), 1) if gpu else None,
        "commit_fps": round((len(c) - 1) / ((c[-1] - c[0]) / 1e9), 1) if len(c) > 1 else None,
    }


def bridge_waits(d):
    """How long the game's threads were blocked in the driver bridge (MaliBridge 'waits over 10.0s' lines).
    Main thread time here is driver/bridge overhead, not game logic; dxvk-queue / vkb-present waits are GPU waits."""
    out = sh(f"run-as {PKG} sh -c \"grep 'waits over' files/logs/{d}/vkbridge-client.log 2>/dev/null | tail -40\"", timeout=60)
    last = {}
    for line in out.splitlines():
        m = re.search(r"\[(\S+) \d+\]: waits over ([0-9.]+)s, thread (\S+) \(\d+\): ([0-9.]+) ms in (\d+) round trips; (.*)", line)
        if m and not m.group(1).startswith(("gamescope", "Xwayland", "explorer")):
            proc, span, thread, ms, trips, what = m.groups()
            top = what.split(") ")[0] + ")" if ")" in what else what
            last[thread] = (float(ms) / float(span), int(trips) / float(span), top[:60])
    return last


def sf_window(seconds, out):
    layer, _ = sfmeter.find_layer("SurfaceView[")
    ts = sfmeter.sample(layer, seconds)
    d = [(b - a) / 1e6 for a, b in zip(ts, ts[1:]) if 0 < (b - a) / 1e6 < 1000]
    out["sf"] = d


def cpu_window(seconds, out):
    """CPU side without any game logging: per-thread load over the window (top -H) and the highest clock each
    CPU cluster reached. A game or bridge thread near 100 % of a core = CPU-bound on that thread; a cluster that
    never passes a clock well under its hardware maximum while loaded = a clock cap (heat or power policy)."""
    span = max(3, seconds - 2)
    p = subprocess.Popen([ADB, "shell", f"top -H -b -n 2 -d {span} -o TID,%CPU,CMD,NAME"], stdout=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    pol = sh("ls -d /sys/devices/system/cpu/cpufreq/policy*").split()
    hw = [int(x) // 1000 for x in sh("cat /sys/devices/system/cpu/cpufreq/policy*/cpuinfo_max_freq").split()]
    seen = [[] for _ in pol]
    t0 = time.time()
    while time.time() - t0 < span:
        for i, v in enumerate(sh("cat /sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq").split()[:len(pol)]):
            seen[i].append(int(v) // 1000)
        time.sleep(1)
    txt = p.communicate(timeout=60)[0]
    last = txt.split("Threads:")[-1]
    rows = []
    for line in last.splitlines():
        m = re.match(r"\s*(\d+)\s+([0-9.]+) (.{15}) (.*)$", line)
        if m and float(m.group(2)) > 0 and m.group(4).strip() != "top":
            rows.append((float(m.group(2)), m.group(3).strip(), m.group(4).strip()[-28:]))
    rows.sort(reverse=True)
    out["threads"] = rows[:8]
    out["clusters"] = [(os.path.basename(pp), max(sv) if sv else None, h) for pp, sv, h in zip(pol, seen, hw)]


def main():
    target = float(sys.argv[1])
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    label = sys.argv[3] if len(sys.argv) > 3 else ""
    tgt_ms = 1000.0 / target
    before = state()
    d = session_dir()

    sfres = {}
    th = threading.Thread(target=sf_window, args=(seconds, sfres))
    th.start()
    cpures = {}
    tc = threading.Thread(target=cpu_window, args=(seconds, cpures))
    tc.start()
    key = os.environ.get("TURN_KEY", "21")
    if key:
        os.environ["TURN_KEY"] = key
    screen_fps.record(seconds, swipe=bool(key))
    th.join()
    tc.join()
    tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "pipeline_check.mp4")
    subprocess.run([ADB, "pull", screen_fps.REMOTE, tmp], capture_output=True)
    pts, img = screen_fps.frames(tmp)
    time.sleep(1)
    comp = compositor_stats(d)
    game = game_stats(d, seconds)
    waits = bridge_waits(d)
    after = state()

    # screen stage: changed-picture intervals
    import numpy as np
    diffs = np.abs(np.diff(img, axis=0)).mean(axis=(1, 2)) if len(img) > 1 else np.array([])
    thr = float(os.environ.get("DIFF_THR", "0.5"))  # mean gray-level change: codec noise on a still picture ~0.03-0.3, a camera move > 1
    changed = [pts[0]] + [pts[i + 1] for i, x in enumerate(diffs) if x > thr] if pts else []
    changed = [t for t in changed if pts and t >= pts[0] + 1.2]  # skip the start, before the camera moves
    siv = [v for v in (np.diff(changed) * 1000.0).tolist() if v < 500]
    even = lambda v: round(100.0 * sum(1 for x in v if abs(x - tgt_ms) <= 4) / len(v), 1) if v else None
    sf = sfres.get("sf", [])

    stages = [
        ("game renders", game.get("game_fps"), f"submit p50/p90 {game.get('game_submit_ms_p50')}/{game.get('game_submit_ms_p90')} ms, "
                                               f"GPU p50/p90 {game.get('gpu_ms_p50')}/{game.get('gpu_ms_p90')} ms"),
        ("bridge commits", game.get("commit_fps"), ""),
        ("compositor receives", comp.get("comp_received_fps"),
         f"FIFO queued {comp.get('fifo_queued')}, shown {comp.get('fifo_shown')}, dropped {comp.get('fifo_dropped')}, "
         f"waited ticks {comp.get('fifo_waited_ticks')}"),
        ("SurfaceFlinger presents", round(len(sf) / (sum(sf) / 1000), 1) if sf else None, f"even {even(sf)} %"),
        ("screen changes", round(len(siv) / (sum(siv) / 1000), 1) if siv else None, f"even {even(siv)} %"),
    ]
    print(json.dumps({"label": label, "target_fps": target, "before": before, "after": after}, ensure_ascii=False))
    print(f"{'stage':24} {'fps':>6}  detail")
    for name, fps, det in stages:
        print(f"{name:24} {fps if fps is not None else '-':>6}  {det}")
    if waits:
        print("time blocked in the driver bridge, per second (last MaliBridge report):")
        for thread, (ms, trips, top) in sorted(waits.items(), key=lambda kv: -kv[1][0]):
            print(f"  {thread:24} {ms:6.0f} ms/s in {trips:5.0f} calls/s  e.g. {top}")

    if cpures.get("clusters"):
        print("CPU clock, highest reached / hardware max (MHz): " +
              ", ".join(f"{n} {m}/{h}" for n, m, h in cpures["clusters"]))
    if cpures.get("threads"):
        print("busiest threads (% of one core):")
        for pc, tname, proc in cpures["threads"]:
            print(f"  {pc:5.1f}  {tname:16} {proc}")

    # verdict
    v = []
    if before.get("charging") or (before.get("gpu_c") or 0) > 70:
        v.append(f"conditions: GPU {before.get('gpu_c')} C, charging {before.get('charging')} - hot tablet, results not comparable with cool runs")
    gf = game.get("game_fps")
    if gf is not None and gf < target * 1.05:
        gp90 = game.get("gpu_ms_p90") or 0
        if gp90 > tgt_ms * 0.9:
            v.append(f"BOTTLENECK = game GPU: {gf} fps < {target}, GPU time p90 {gp90} ms vs {tgt_ms:.1f} ms budget (heat, resolution, settings)")
        else:
            main = max((w for t, w in waits.items() if t.lower().startswith(("wow", "main")) or ".exe" in t.lower()),
                       key=lambda w: w[0], default=None)
            if main and main[0] > 0.25 * 1000:
                v.append(f"BOTTLENECK = driver bridge on the game's main thread: {main[0]:.0f} ms/s blocked ({main[2]}), {gf} fps < {target}")
            else:
                v.append(f"BOTTLENECK = game CPU: {gf} fps < {target} while GPU time p90 is only {gp90} ms and the bridge waits are small")
    elif gf is None and comp.get("comp_received_fps") is not None and comp["comp_received_fps"] < target * 0.95:
        hot = [t for t in cpures.get("threads", []) if t[0] > 85 and t[2] != "top"]
        capped = [f"{n} {m}/{h}" for n, m, h in cpures.get("clusters", []) if m and h and m < 0.7 * h]
        if hot:
            v.append(f"CPU-bound thread: {hot[0][1]} ({hot[0][2]}) at {hot[0][0]:.0f} % of a core" +
                     (f"; clocks capped: {', '.join(capped)}" if capped else ""))
        elif cpures.get("threads"):
            v.append("no CPU thread is saturated: the game waits on the GPU or on a synchronous handoff "
                     "(lower the graphics to tell the two apart)")
        v.append(f"BOTTLENECK = the game side delivers only {comp['comp_received_fps']} fps (< {target}); add VKBRIDGE_LATLOG=1 "
                 "to the launch options to tell the game, the driver bridge and gamescope apart")
    elif gf is not None and comp.get("comp_received_fps") is not None and comp["comp_received_fps"] < min(gf, target) * 0.95:
        v.append("BOTTLENECK = between the game and the compositor (gamescope / bridge commit path)")
    elif comp.get("fifo_waited_ticks", 0) > 2 * seconds and comp.get("fifo_queued", 0) < target * 10:
        v.append("BOTTLENECK = frames arrive too irregularly for the queue depth (raise debug.droiddeck.fifo)")
    elif sf and even(sf) is not None and even(sf) < 80:
        v.append("BOTTLENECK = compositor -> SurfaceFlinger (copy misses the latch: check debug.droiddeck.fifo_lat / GPU load)")
    elif siv and even(siv) is not None and even(siv) < 80:
        v.append("BOTTLENECK = screen shows repeated pictures although presents are even (game repeats frames, or measurement)")
    else:
        v.append("no bottleneck at this target: every stage keeps up and the screen is even")
    print("VERDICT:", " | ".join(v))


if __name__ == "__main__":
    main()
