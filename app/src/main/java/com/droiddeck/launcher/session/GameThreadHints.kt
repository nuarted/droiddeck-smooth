package com.droiddeck.launcher.session

import android.content.Context
import android.os.Build
import android.os.PerformanceHintManager
import android.os.PowerManager
import android.os.Process
import android.system.Os
import android.system.OsConstants
import android.util.Log
import com.droiddeck.launcher.wayland.WaylandCompositor
import java.io.File
import kotlin.math.abs

object GameThreadHints {
    private const val TAG = "GameThreadHints"
    private const val TARGET_NS = 10_000_000L
    private const val REPORT_MS = 50L
    private const val SCAN_EVERY = 20
    private const val ENTER = 0.6
    private const val LEAVE = 0.15
    private const val MAX_THREADS = 2
    private const val TOP_PROCESSES = 3
    private const val PACE_WINDOW_S = 3
    private const val PACE_LEAVE_S = 10
    private const val PACE_TOLERANCE = 0.08

    private val NOT_GAMES = setOf("steam", "steamwebhelper")

    /** How threads are chosen and kept. */
    internal class Profile(val enter: Double, val leave: Double, val maxThreads: Int, val leaveWhenPaced: Boolean)

    private val ODIN = Profile(ENTER, LEAVE, MAX_THREADS, leaveWhenPaced = true)

    /** MediaTek: the FPSGO governor sizes CPU and GPU clocks for the one thread it sees queuing frames to Android,
     *  the compositor's light copy, and caps them at about half while the game renders out of its sight (through the
     *  bridge and gamescope). The game's threads are mostly waiting in a serial CPU -> bridge -> GPU chain, so none is
     *  busy (main thread ~20 %): take the busiest few of the game's and the bridge's threads, and keep the hint while
     *  the game holds its pace - dropping it lets the clocks fall and the pace with them. */
    private val MEDIATEK = Profile(enter = 0.08, leave = 0.04, maxThreads = 5, leaveWhenPaced = false)

    private var worker: Thread? = null

    @Synchronized
    fun start(context: Context): String {
        if (!SessionPrefs.gameThreadHints(context)) return "game thread hints: off"
        if (Build.VERSION.SDK_INT < 31) return "game thread hints: needs Android 12"
        val profile = when {
            Build.SOC_MODEL == "CQ8725S" -> ODIN
            Build.SOC_MANUFACTURER.equals("Mediatek", ignoreCase = true) -> MEDIATEK
            else -> return "game thread hints: Odin 3 and MediaTek only"
        }
        val hm = context.getSystemService(PerformanceHintManager::class.java)
            ?: return "game thread hints: no PerformanceHintManager"
        val pm = context.getSystemService(PowerManager::class.java)
        worker?.interrupt()
        worker = Thread({ run(hm, pm, profile) }, "game-thread-hints").apply { isDaemon = true; start() }
        return "game thread hints: watching" + if (profile === MEDIATEK) " (MediaTek: game and bridge threads, kept while paced)" else ""
    }

    @Synchronized
    fun stop() {
        worker?.interrupt()
        worker = null
    }

    private fun run(hm: PerformanceHintManager, pm: PowerManager, profile: Profile) {
        val scanner = Scanner()
        var session: PerformanceHintManager.Session? = null
        var boosted = emptySet<Int>()
        val frames = ArrayDeque<Long>()
        var pacedFor = 0
        var tick = 0
        try {
            while (!Thread.currentThread().isInterrupted) {
                if (tick++ % SCAN_EVERY == 0) {
                    val busy = scanner.sample()
                    frames.addLast(WaylandCompositor.gameFrameCount())
                    if (frames.size > PACE_WINDOW_S + 1) frames.removeFirst()
                    val fps = (frames.last() - frames.first()).toDouble() / (frames.size - 1).coerceAtLeast(1)
                    val paced = frames.size > PACE_WINDOW_S && holdsPace(fps, SessionState.refreshHz.toDouble())
                    pacedFor = if (paced) pacedFor + 1 else 0
                    val spare = pm.isPowerSaveMode || pm.currentThermalStatus >= PowerManager.THERMAL_STATUS_CRITICAL
                    val next = when {
                        spare || (profile.leaveWhenPaced && pacedFor >= PACE_LEAVE_S) -> emptySet()
                        // Odin 3: once paced, only keep what is boosted. MediaTek: keep choosing among all the game's
                        // threads, or the ones picked while loading (asset, network) stay and the render chain never joins.
                        paced && profile.leaveWhenPaced -> pick(boosted, busy.filterKeys { it in boosted }, profile)
                        else -> pick(boosted, busy, profile)
                    }
                    if (next != boosted) {
                        session = update(hm, session, next)
                        boosted = if (session == null) emptySet() else next
                        Log.i(TAG, (if (session == null && next.isNotEmpty()) "no session for " else "boosting ") +
                            next.joinToString { "${readName("/proc/$it/comm")}/$it ${((busy[it] ?: 0.0) * 100).toInt()}%" }.ifEmpty { "nothing" } +
                            " (game ${fps.toInt()} fps${if (paced) ", holding pace" else ""})")
                    }
                }
                session?.reportActualWorkDuration(TARGET_NS * 3 / 2)
                Thread.sleep(REPORT_MS)
            }
        } catch (_: InterruptedException) {
        } catch (t: Throwable) {
            Log.w(TAG, "stopped", t)
        } finally {
            session?.close()
        }
    }

    private fun update(hm: PerformanceHintManager, session: PerformanceHintManager.Session?, tids: Set<Int>): PerformanceHintManager.Session? {
        session?.close()
        if (tids.isEmpty()) return null
        return try {
            hm.createHintSession(tids.toIntArray(), TARGET_NS)
        } catch (t: RuntimeException) {
            Log.w(TAG, "createHintSession", t)
            null
        }
    }

    internal fun holdsPace(fps: Double, refreshHz: Double): Boolean =
        fps >= 10 && refreshHz > 0 && (1..4).any { k -> abs(fps - refreshHz / k) < PACE_TOLERANCE * refreshHz / k }

    internal fun pick(boosted: Set<Int>, busy: Map<Int, Double>, profile: Profile = ODIN): Set<Int> =
        busy.entries
            .filter { (tid, share) -> share >= if (tid in boosted) profile.leave else profile.enter }
            .sortedWith(compareByDescending<Map.Entry<Int, Double>> { it.key in boosted }.thenByDescending { it.value })
            .take(profile.maxThreads)
            .map { it.key }
            .toSet()

    private fun readName(path: String) = try { File(path).readText().trim() } catch (_: Exception) { "?" }

    private class Scanner {
        private val self = Process.myPid()
        private val uid = Process.myUid()
        private val hz = Os.sysconf(OsConstants._SC_CLK_TCK).toDouble()
        private var lastNs = 0L
        private var lastProcess = HashMap<Int, Long>()
        private var lastThread = HashMap<Int, Long>()
        private var lastBusy = HashMap<Int, Double>()

        fun sample(): Map<Int, Double> {
            val now = System.nanoTime()
            val seconds = (now - lastNs) / 1e9
            lastNs = now
            val process = HashMap<Int, Long>()
            File("/proc").list()?.forEach { entry ->
                val pid = entry.toIntOrNull() ?: return@forEach
                cpuTicks("/proc/$pid/stat")?.let { process[pid] = it }
            }
            val top = process.entries
                .map { (pid, ticks) -> pid to ticks - (lastProcess[pid] ?: ticks) }
                .filter { (pid, delta) -> delta > 0 && pid != self && ownedByUs(pid) }
                .sortedByDescending { it.second }
                .asSequence()
                .filter { (pid, _) -> readName("/proc/$pid/comm") !in NOT_GAMES }
                .take(TOP_PROCESSES)
                .toList()
            lastProcess = process
            val thread = HashMap<Int, Long>()
            val busy = HashMap<Int, Double>()
            for ((pid, _) in top) {
                File("/proc/$pid/task").list()?.forEach { entry ->
                    val tid = entry.toIntOrNull() ?: return@forEach
                    val ticks = cpuTicks("/proc/$pid/task/$tid/stat") ?: return@forEach
                    thread[tid] = ticks
                    lastThread[tid]?.let { before -> busy[tid] = ((ticks - before) / hz / seconds + (lastBusy[tid] ?: 0.0)) / 2 }
                }
            }
            lastThread = thread
            lastBusy = busy
            return busy
        }

        /** A hint session only takes this app's threads (the game and the bridge run as its children). */
        private fun ownedByUs(pid: Int): Boolean = try { Os.stat("/proc/$pid").st_uid == uid } catch (_: Exception) { false }

        private fun cpuTicks(path: String): Long? = try {
            val line = File(path).readText()
            val fields = line.substring(line.lastIndexOf(')') + 2).split(' ')
            fields[11].toLong() + fields[12].toLong()
        } catch (_: Exception) {
            null
        }
    }
}
