# FIFO presentation: design

File: [`app/src/main/cpp/waylandcomp/src/compositor.c`](../../app/src/main/cpp/waylandcomp/src/compositor.c), the
Android-side Wayland compositor that gamescope (and other clients) present to.

## Before

With a frame limit (`nativeSetFpsLimit`, the session drawer's *Frame limit* for non-Steam sessions):

- `surface_commit` applies every commit at once, so the newest buffer replaces the current one ("latest wins").
- `on_vsync` renders only when `scene_held_for_limit` says the paced slot is due. Slots are `g_scene_due_ns`, advanced by `1e9 / limit` and kept in phase with the vsync ticks.
- `release_buffer` returns replaced buffers on a timer, one interval apart. This is how the client gets throttled.

A client that commits at an even 30 fps maps onto the slots perfectly. A client that commits in bursts does not:

- A slot with no new commit shows the old frame again, so it stays up 41.7 ms at 120 Hz.
- A slot that got two commits shows only the second; the first frame is "replaced before drawing".

## After

While `fifo_active()` holds, commits of the game surface (`g_hud_surface`, the surface whose dma-buf frames DroidDeck already counts as the game's) go into a per-surface queue instead of being applied. `fifo_active()` requires all of:

- a frame limit is set;
- release pacing is on;
- no frame generation;
- no streaming;
- depth > 0.

| Step | Code |
|---|---|
| Commit arrives | `surface_commit`: `fifo_capture` moves the surface's whole pending state (buffer, damage, viewport src/dst, opaque region, frame callbacks, presentation feedback) into a `fifo_entry`. If more than `depth` entries wait, the oldest is discarded: buffer released, callbacks fired, feedback discarded. |
| Every vsync | `fifo_tick` runs first in `on_vsync`. On a due slot it applies exactly one entry, oldest first, through the original commit code (`surface_commit_apply`). The client's in-progress pending state is stashed and restored around it. The normal render on that slot then shows it. |
| Slot with an empty queue | Nothing new is applied; the scene keeps its previous picture. The next frame is applied on the first tick after it arrives, and the slot grid keeps its phase. Counted as "ticks a slot waited for a late frame". |
| Replaced buffer | Released at once for a FIFO surface. The queue throttles the client; pacing releases on top would starve it of buffers. |
| FIFO stops applying (limit off, frame generation on, depth 0) | Queued entries are applied in order immediately, so nothing is lost or reordered. |
| Surface destroyed | Queued entries are freed. Their callbacks are destroyed and their feedback discarded. |

Frame callbacks and `wp_presentation` feedback of a queued frame are sent when it is actually shown. Clients that pace themselves on them, such as gamescope's nested backend, see the real display time.

## Controls and diagnostics

- **Depth.** System property `debug.droiddeck.fifo`, read about once a second:
  - unset: 2;
  - `0`: off, the original behaviour;
  - maximum 7.
  - Set it with `adb shell setprop debug.droiddeck.fifo N`; no root needed for `debug.*`.
- **Stats.** Every 10 s while frames go through the queue, `wayland.log` gets:
  ```
  pacing  FIFO last 10 s: Q game frames queued, S shown on their slot, D dropped (queue over depth N),
          W ticks a slot waited for a late frame | queued avg/max ms before shown | deepest queue M
  ```
  - On an even result, S ≈ 10 × limit and W ≈ 0.
  - D > 0 means the game produces more than the limit; a tighter in-game cap lowers latency.

## Trade-offs

- **Latency.** Up to `depth` frames (2 × 33.3 ms at 30 fps) more than "latest wins". Depth 1 halves that, but absorbs less jitter.
- **The average still matters.** If the game produces fewer frames than the limit on average, no queue can fill every slot; repeats come back. Cooling, lower settings or a lower limit help there.
- **Only the game surface is queued.** Overlays, cursor and other windows still update on their own commits.

## Not changed

- Release pacing for other surfaces.
- Render-on-commit without a limit.
- Zero-copy and HDR paths.
- Frame generation: the queue is bypassed while it runs.

There is one test stub for `fifo_tick` in [tools/tests/test_render_on_commit.py](../../tools/tests/test_render_on_commit.py), because that test compiles `on_vsync` on its own.
