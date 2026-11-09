# Investigation: why WoW judders at a "solid" 30 fps

## Setup

| | |
|---|---|
| Device | Xiaomi Pad Mini (MediaTek Dimensity, Mali-G925 Immortalis MC12), 120 Hz LCD, no VRR, HyperOS tablet mode |
| App | DroidDeck-MaliBridge 0.5.5.3 (DroidDeck + MaliBridge Vulkan bridge to the Android Mali driver) |
| Game | World of Warcraft (Windows arm64 build) via Battle.net, DroidDeck-Proton 11, DXVK 3.1.1, gamescope 3.16 nested on DroidDeck's Wayland compositor |
| Scene | Character standing still in a forest zone, same camera |

## How we measured

| Metric | Source |
|---|---|
| "even" | % of presented intervals within ±4 ms of the target. At 120 Hz and 30 fps, an interval of exactly 4 refreshes. |
| "hitches" | Intervals > 45 ms in ~30 s |
| Present timestamps | `dumpsys SurfaceFlinger --latency` on the game's SurfaceView layer ([sfmeter.py](tools/sfmeter.py), [vsync_hist.py](tools/vsync_hist.py)) |
| Per-frame pipeline | MaliBridge `VKBRIDGE_LATLOG=1`: submit, GPU done, commit, shown ([latlog_summary.py](tools/latlog_summary.py)) |
| Compositor view | DroidDeck `wayland.log` perf/pacing lines every 10 s |

Numbers vary run to run by roughly ±30 hitches. The table only calls something better when it clearly is.

## The frame path

WoW (Wine/DXVK) → MaliBridge client → gamescope (nested, Wayland backend) → DroidDeck compositor (Android) → SurfaceFlinger.

The compositor:
- waits for the frame's GPU fence;
- copies the frame into its own swapchain;
- presents.

For non-Steam sessions (Battle.net), the drawer's **Frame limit** drives the DroidDeck compositor, not gamescope. In that mode gamescope's own limiter is off.

## What was tried

| Configuration | fps | even | hitches | Result |
|---|---|---|---|---|
| DXVK `dxgi.maxFrameRate = 30` | 30 | 20 % | 265 | bad |
| same, display at 60 Hz | 24 | 38 % | 532 | worse |
| render scale 0.75 / minimum view distance and detail | 25 | 22 % | 374–390 | no help |
| WoW `maxFPS 30` | 30 | 19 % | 197 | bad, WoW's limiter is uneven (20–47 ms) |
| WoW `maxFPS 24` | 24 | 20 % | 438 | bad |
| DXVK `syncInterval 2` | 30 | 26–29 % | 128–134 | bad |
| **DroidDeck Frame limit 30** (compositor) | 30 | 36 % | 68–78 | best "30" available |
| same, character-select screen (light scene) | 30 | 58 % | 17 | light scene almost even |
| Frame limit 30 + CAS sharpening | 30 | 28–33 % | 60–85 | an extra GPU pass makes it worse |
| Frame limit 30 + render scale 0.8 / 0.6 / 0.7 | 30 | 34–37 % | 85–156 | marginal |
| Frame limit 30 + Android fixed performance mode | 30 | 34–41 % | 96–149 | noise |
| Frame limit 30 + WoW `maxFPS` 29/30/31 | 29 | 30–34 % | 85–184 | no help |
| Frame limit 30 + `GxMaxFrameLatency 1` | 29 | 34 % | 186 | no help |
| Frame limit 30 + DXVK limiter 30 | 30 | 35 % | 137 | no help |
| + `dxvk.latencySleep = True` | 28 | 37 % | 171 | no help |
| + DroidDeck "gamescope: realtime GPU queue" | 30 | 28 % | 150 | no help |
| gamescope limiter (FIFO) via `gamescopectl debug_set_fps_limit 30` | 28.5 | 20 % | 176 | worse |
| DroidDeck frame generation DD-FG 2× (60 out) | 60 | — | — | very uneven: 1-4-1-3-1-2 refreshes |
| Frame limit 40 | 36 | 32–34 % | 54–68 | not better |
| Frame limit 60 (the game reaches ~48) | 48 | 46 % | 9 | fewest hitches, intervals alternate 16.7/25 ms |
| No limit | 44–50 | — | 12–19 | not 30 |

Ruled out by reading the sources:
- **Zero-copy presentation.** Only switched on together with HDR, and not available on this device.
- **`VKBRIDGE_PRESENT_HOLD_MS`.** Has no effect on the Wayland dma-buf path.
- **`VKBRIDGE_FRAME_QUEUE`.** Unused under DXVK.
- **Hiding `VK_EXT_memory_budget`.** DXVK still makes the round trip.

## What the per-frame log showed

1. **The game submits in bursts.**
   - Submit intervals run 22 → 25 → 50 ms (p10 21.5, p50 29.8, p90 49 ms) while averaging exactly 30 fps.
   - The screen reproduced the same rhythm.
   - No limiter tried (WoW, DXVK, compositor) made the submissions even.
2. **The compositor shows "the last frame committed" on each paced slot.** Bursts become 25/41 ms pairs. The log counted 15 frames "replaced before drawing" in 10 s, with the game delivering 30.1 fps.
3. **GPU time is close to the budget.**
   - Submit to GPU-done took 19 ms on a cool tablet and 28–31 ms after ~20 minutes while charging. The GPU was at 79 °C, and Android reported no thermal status.
   - Render scale 0.7 did not lower it, so this is not fill rate.
   - Cooler means more headroom.
4. **The game's main thread spends ~7 ms per frame** in two synchronous bridge round trips (`vkGetPhysicalDeviceMemoryProperties2`, the memory budget query behind `QueryVideoMemoryInfo`). It only shows up as a jitter source and is not addressed here.

## Model of the fix

[fifo_sim.py](tools/fifo_sim.py) replays the recorded commit times (900-frame windows at 30 fps) through a paced FIFO: one frame per 33.3 ms slot, oldest first, at most 3 queued.

| Start offset | Repeated slots |
|---|---|
| 0–33 ms | 0.0–0.4 % (typically 0.1–0.2 %) |

The same frames that look uneven today would land almost perfectly on the grid. That is the change in [DESIGN.md](DESIGN.md).

## Open points

- On-device results of the FIFO build: to be added.
- Burst submission upstream of the compositor: Wine/DXVK/bridge.
- The memory budget round trips in the bridge.
