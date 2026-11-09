# How to test frame pacing so the bottleneck is clear

A single number is not enough: the fps counter (DXVK/HUD), the compositor's counter and SurfaceFlinger can each look fine
while the picture judders, as happened during this investigation. Every claim below has to be backed by measuring
**every stage of the frame path in the same window**, under the **same conditions**.

## The frame path and what measures each stage

| # | Stage | Measured by | What it tells |
|---|---|---|---|
| 1 | Game submits frames (CPU) | MaliBridge per-frame log, `VKBRIDGE_LATLOG=1`: `queued` | game + Wine/DXVK CPU side: rate and evenness of submissions |
| 2 | GPU finishes the frame | same log: `gpu_done - queued` | GPU time per frame (heat, resolution, settings) |
| 3 | Driver bridge overhead | MaliBridge `vkbridge-client.log`, "waits over 10s" lines | ms per second each game thread is blocked in the bridge (main thread = driver/bridge cost, not game logic) |
| 4 | Bridge hands the frame on | same per-frame log: `committed` | frames leaving the game process |
| 5 | Compositor receives / queues / shows | DroidDeck `app.log` (tag `DroidDeckWayland`): `[stats] ... GPU frames from games`, `FIFO last 10 s: queued, shown, dropped, ticks waited` | frames reaching DroidDeck; whether the FIFO ran dry or overflowed |
| 6 | SurfaceFlinger presents | `dumpsys SurfaceFlinger --latency` on the game layer (`sfmeter.py`) | when buffers reached the display |
| 7 | **The screen** | `screenrecord` + picture-change analysis (`screen_fps.py`) | what the eye sees: only frames whose picture changed |

`tools/pipeline_check.py <target_fps>` runs stages 1–7 at once for 10 s, prints one table and names the first stage that
falls short of the target.

## Rules of a fair test

1. **Same scene and the same motion.**
   - Stand on the same spot and close every menu, chat box and popup. A screenshot before each run proves it.
   - Move the camera with a held key (`TURN_KEY=21` or `22`) or an on-screen stick (`TURN_STICK="x y dx"`), not by hand.
     A still picture cannot show judder.
   - Check the recording really moved. A chat box, the game menu or lost key focus silently stops the turn, and the
     screen stage then reports "too few changed frames".
2. **Thermal state is part of the result.**
   - Log GPU and battery temperature before and after every run; the tool does it.
   - Do not compare a cool run with a hot one. Here GPU time was 19 ms cold against 28–31 ms at 79 °C, and charging adds heat.
   - Keep a band, for example GPU < 65 °C, or test hot on purpose and say so.
3. **One change at a time, toggled live.**
   - Prefer settings that switch without a restart: `debug.droiddeck.fifo`, `debug.droiddeck.fifo_lat`, the drawer frame limit.
   - Anything that needs a restart (launch options, dxvk.conf, driver) gets its own baseline afterwards.
4. **Repeat and interleave.**
   - At least 3 runs per setting, in A/B/A/B order, so slow drift (heat, scene streaming) hits both sides equally.
   - Compare medians. Runs vary by ±15 percentage points.
5. **Check the target is reachable first.**
   - If stage 1 delivers fewer frames than the limit, no pacing change can make that limit even.
   - Then lower the limit to an even divisor of the refresh: 20 at 120 Hz (6 refreshes), or 25 at 50 Hz (2 refreshes).
6. **Keep the measurement off the critical path.**
   - `screenrecord` costs a little GPU/encoder time. Compare settings with it always on, never once on and once off.
7. **Write it down.**
   - Label, settings, temperatures, the table and the verdict for every run.

## Reading the verdict

| What the table shows | Bottleneck | What to do |
|---|---|---|
| stage 1 fps < target, GPU p90 near or over the frame budget | **GPU** (heat, resolution, settings) | cool down, lower render scale or settings, lower the limit |
| stage 1 fps < target, GPU time low, main thread blocked in the bridge for a large share of each second | **driver bridge** (synchronous round trips) | bridge-side fix: cache queries, fewer round trips |
| stage 1 fps < target, GPU time low, small bridge waits | **game CPU** (game logic, Wine/DXVK CPU) | CPU settings, core pinning, view distance |
| stage 1 is fine, compositor receives fewer frames | **gamescope / commit path** | gamescope settings, sync mode |
| compositor receives enough, FIFO "ticks waited" high | frames arrive too irregularly for the queue | deeper queue (`debug.droiddeck.fifo 3`) |
| FIFO shows every slot, SurfaceFlinger uneven | **compositor → display** (copy misses the latch) | `debug.droiddeck.fifo_lat`, GPU load |
| presents even, screen uneven | game repeats pictures, or a measurement artifact | check the recording, the threshold (`DIFF_THR`) |
| every stage keeps up, screen even | none at this target | try a higher target |
| no thread near 100 %, lower settings barely help, clocks sit far below their maximum | **clock caps** (power governor, e.g. MediaTek FPSGO sizing clocks for the compositor thread, not the game) | performance mode / game mode, ADPF hints for the game threads |

## Setup for a full run

- In DroidDeck → game → launch options, prepend `VKBRIDGE_LATLOG=1`, then restart the game. Without it, stages 1–4 are
  blank, and the tool says only "the game side delivers too few frames".
  - MaliBridge v0.5.4.2 (DroidDeck 0.7.0) has no per-frame log. There `pipeline_check.py` falls back to per-thread CPU
    load and the highest CPU clock each cluster reached.
- **Who the game waits for:** `adb shell atrace -t 5 -b 98304 sched freq sync mtk_debug -o /data/local/tmp/at.txt`, pull
  it, then run `python wait_trace.py at.txt Wow-arm64`.
  - It prints, for each game thread, its share of time running, waiting for a CPU and sleeping, and which thread woke it.
  - On MediaTek, `mtk_debug` adds FPSGO's view: the tracked render thread, `max_gpu_cap`, target fps.
- adb over the network: wireless debugging drops when the tablet sleeps. After connecting once, run `adb tcpip 5555`
  and connect to port 5555 (valid until reboot); `svc power stayon true` keeps the screen on.
- Run, for example:
  `ADB=... ANDROID_SERIAL=host:5555 TURN_KEY=21 python pipeline_check.py 30 10 "limit 30, fifo 2, lat 3"`.
