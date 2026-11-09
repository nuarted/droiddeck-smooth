# DroidDeck Smooth

Personal fork of [DroidDeck](https://github.com/Droid-Deck/DroidDeck) (GPL-3.0) with one change: **even frame pacing under a frame limit**. It installs next to DroidDeck and DroidDeck-MaliBridge as `com.droiddeck.launcher.smooth` ("DroidDeck Smooth").

## Problem

Measured on a Xiaomi Pad Mini (Dimensity, Mali-G925, 120 Hz) with World of Warcraft (Wine + DXVK + gamescope + MaliBridge), drawer Frame limit 30:

- The game commits its frames in bursts: 22, 25, 50 ms between frames, averaging 33.3 ms (from MaliBridge's `VKBRIDGE_LATLOG`).
- When a paced slot comes up, the compositor shows the **last** buffer committed. A slot either gets no new frame, so the old one is held for 41.7 ms, or gets two, so one is dropped. On screen this alternates 25/41/50 ms.
- Only 36% of intervals are 33.3 ms.

## Fix (`app/src/main/cpp/waylandcomp/src/compositor.c`)

While a frame limit is set:

- Every commit of the game surface waits in a short FIFO queue.
- Exactly one queued commit is applied per paced slot, oldest first. Slots stay on the display's vsync grid.
- If more frames are queued than the depth allows, the oldest waiting frame is dropped, so latency stays bounded.
- The replaced buffer is released at once: the queue itself holds the client back.

Replaying the recorded commit times through this queue repeats 0.1–0.2% of slots instead of 64% uneven intervals. The cost is up to `depth` frames of latency.

Controls:

- Set the queue depth live with `adb shell setprop debug.droiddeck.fifo N`. The default is 2; `0` turns it off and restores the original behaviour.
- Every 10 s, `wayland.log` gets a line `pacing  FIFO last 10 s: ... queued, ... shown on their slot, ... dropped, ... ticks a slot waited for a late frame`.

Nothing changes without a frame limit, with frame generation, or while streaming.
