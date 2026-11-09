# DroidDeck Smooth

> 🤖 **Made entirely and automatically by AI.** Claude by Anthropic did the investigation, the measurements, the code and the docs
> ([how](#made-entirely-by-ai)).

**Even frame pacing for capped games in [DroidDeck](https://github.com/Droid-Deck/DroidDeck).**
This fork changes one thing. When a frame limit is set, the game's frames reach the screen at a steady
33.3 ms (or 16.7 ms…) instead of jumping between 25, 41 and 50 ms.

> **Status: experimental.** The change builds in this repository's CI and has been modelled on frames
> recorded from a real game. Verification on the device is in progress, and results will be added below. Use at your own risk.
> It installs as a separate app (`com.droiddeck.launcher.smooth`, "DroidDeck Smooth"), so an existing
> DroidDeck or DroidDeck-MaliBridge install stays untouched.

Original DroidDeck readme: [README.upstream.md](README.upstream.md). All credit for DroidDeck goes to its
authors. This is a personal fork, not affiliated with or endorsed by them.

---

## The problem

World of Warcraft (Windows build) on a Xiaomi Pad Mini: MediaTek Dimensity, Mali-G925 GPU, 120 Hz screen without VRR.
It runs through Wine + DXVK + gamescope + [MaliBridge](https://github.com/ASEbrahim/MaliBridge). With DroidDeck's
**Frame limit 30** the counter reads a solid 30 fps, but the picture judders:

| Measured with SurfaceFlinger (120 Hz, frame limit 30) | |
|---|---|
| Intervals that are exactly 4 refreshes (33.3 ms) | **≈ 36 %** |
| Typical intervals | 25 → 41.7 → 25 → 50 ms … |
| Frames > 45 ms in 30 s | 68 – 180 |

No setting fixed it. More than 40 configurations were measured: DXVK, WoW and gamescope limiters, refresh rates,
graphics presets, frame generation, GPU priority, `latencySleep` and others. See [docs/smooth/INVESTIGATION.md](docs/smooth/INVESTIGATION.md).

## Why it happens

MaliBridge's per-frame log (`VKBRIDGE_LATLOG=1`) shows that the game hands over frames **in bursts**:
25, 25, 50 ms… It averages exactly 30 fps, but the intervals are never even. That comes from the translation
layers, bridge round trips and a GPU running close to its budget.

DroidDeck's compositor paces output on the display's vsync grid. When a slot comes up, it shows **the last
frame committed**:
- A slot that gets no new frame repeats the old one, so the picture stays up for 41.7 ms.
- A slot that gets two frames drops one.

The bursts turn straight into judder.

## The fix

[`compositor.c`](app/src/main/cpp/waylandcomp/src/compositor.c) gets a **FIFO for the game surface** while a frame
limit is set:

- Every frame the game commits waits in a short queue instead of replacing the previous one.
- Exactly one queued frame is shown per paced slot, oldest first, still on the vsync grid.
- If frames pile up beyond the depth, the oldest waiting one is dropped, so latency stays bounded.
- The replaced buffer goes back to the game at once. The queue itself throttles the game.

Replaying the recorded frame times through this queue ([`fifo_sim.py`](docs/smooth/tools/fifo_sim.py)) gives
**0.1–0.2 % repeated slots** instead of ~64 % uneven intervals. The cost is up to *depth* frames of extra latency
(default 2, so ≤ 67 ms at 30 fps).

Nothing changes without a frame limit, with frame generation or while streaming.
Details: [docs/smooth/DESIGN.md](docs/smooth/DESIGN.md).

## Using it

1. Download the APK from this repository's [Actions](../../actions) (workflow "Build APK", artifact). Install it next to your DroidDeck.
2. Set **Frame limit** in the session drawer (30, 40 or 60 on a 120 Hz screen).
3. Optionally tune the queue live over adb:
   ```sh
   adb shell setprop debug.droiddeck.fifo 2   # queue depth (default 2)
   adb shell setprop debug.droiddeck.fifo 0   # off: original DroidDeck behaviour
   ```
4. Check it works. Every 10 s the session's `wayland.log` gets a line
   `pacing  FIFO last 10 s: N game frames queued, N shown on their slot, N dropped, N ticks a slot waited for a late frame …`

## Measuring frame pacing yourself

Small scripts in [docs/smooth/tools](docs/smooth/tools) (Python 3 + adb, no dependencies). Set `ADB=/path/to/adb` and `ANDROID_SERIAL=<device>`.

| Script | What it does |
|---|---|
| `sfmeter.py` | fps, frame-time percentiles, % of frames on target, hitches, from `dumpsys SurfaceFlinger --latency` |
| `vsync_hist.py` | intervals as a histogram of display refreshes (an even 30 fps at 120 Hz = `4v:100%`) |
| `latlog_summary.py` | summary of MaliBridge's per-frame `vkbridge-latency.log` (submit, GPU done, commit, shown) |
| `fifo_sim.py` | replays a recorded log through the FIFO model |

## Made entirely by AI

**This project was made fully automatically by an AI: Claude, Anthropic's AI assistant, running in Claude Code.**
The repository owner only stated the goal ("get an even 30 fps in WoW on my tablet") and okayed the steps. They
wrote no code and ran no tests. Everything else was done by the AI on its own:

- connected to the tablet over adb and measured frame pacing with SurfaceFlinger, MaliBridge and DroidDeck logs;
- ran the 40+ configurations in the [investigation](docs/smooth/INVESTIGATION.md), restarting the game between them;
- read the DroidDeck, MaliBridge and gamescope sources to find where the judder comes from;
- built the replay model;
- wrote the compositor patch, the tools and these docs;
- created this fork and set up the CI build.

Every number here comes from a measurement on the device or from a recorded log. Upstream DroidDeck does not accept
AI-written contributions, so this lives in a personal fork and its authorship check is disabled here.

## License

GPL-3.0, like DroidDeck ([LICENSE](LICENSE)). Changes against upstream: `compositor.c` (FIFO presentation),
`app/build.gradle` (separate package name, debuggable release), the authorship check skipped outside upstream,
one test stub, and `docs/smooth/`.

---

### Кратко по-русски

Форк DroidDeck, который делает кадры **ровными** при ограничении FPS.

- **Проблема.** В WoW на планшете с Mali при Frame limit 30 счётчик показывает 30, но картинка дёргается. Только ~36 % кадров идут ровно через 33,3 мс, остальные скачут: 25/41/50 мс.
- **Причина.** Игра через Wine/DXVK/мост отдаёт кадры пачками, а композитор DroidDeck показывает «последний пришедший» кадр.
- **Решение.** Очередь кадров: строго один кадр на каждый такт, по порядку. На записанных кадрах это даёт 0,1–0,2 % повторов. Цена — до двух кадров дополнительной задержки.
- **Настройка.** Глубина очереди меняется без перезапуска: `adb shell setprop debug.droiddeck.fifo N` (0 — выключить).
- **Установка.** Ставится отдельным приложением «DroidDeck Smooth», рядом с обычным DroidDeck.
- **Статус.** Экспериментальный, проверка на устройстве идёт.
- **Как сделано.** Проект полностью и автоматически сделан ИИ: Claude (Anthropic) в Claude Code. Владелец репозитория только поставил задачу и подтверждал шаги, а замеры на планшете через adb, разбор исходников, модель, код, документацию, форк и сборку ИИ выполнил сам.
