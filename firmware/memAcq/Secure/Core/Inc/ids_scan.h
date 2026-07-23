/*
 * ids_scan.h -- the IDS runtime tick (Secure): a secure TIM2 interrupt
 * preempts the NonSecure workload every IDS_SCAN_PERIOD_S seconds and runs
 * the Part-2 NV scan (cache invalidate -> read the live NV region ->
 * features -> Mahalanobis score -> verdict). The trigger is a Secure-owned
 * timer so a compromised NonSecure world cannot delay, skip, or time-game
 * the monitor's schedule; NonSecure is not involved at all.
 *
 * The verdict drives a secure independent watchdog (IWDG): the same tick
 * restarts the watchdog countdown ONLY on a scan that is clean on both IDS
 * parts -- the NV score here, plus the static-region hash state fed by the
 * logger's pre-write checks (never latched dirty, heartbeat advancing). An
 * anomaly on either part, or a failed/wedged scan, lets the countdown lapse
 * into a reset -- the MARS mechanism their paper never fully armed.
 */
#ifndef IDS_SCAN_H
#define IDS_SCAN_H

/* Build switch: 1 = the real IDS (scan tick + watchdog) -- every deploy,
   soak, and demo build. 0 = DISARMED: IdsScan_Init prints a loud banner and
   arms nothing -- no scan, no watchdog, no self-reset -- so an unattended
   benign data collection can run page cycles uninterrupted (a mid-fill
   watchdog reset would seam the very ring states being banked; the scan
   itself never writes the NV region, so the collected bytes are identical
   to deploy-build data). GOTCHA: a disarmed board has NO tamper response at
   all -- flip back to 1 and reflash Secure before any soak or demo; the
   boot banner says which build is running. */
#define IDS_SCAN_ARMED  0

/* Latency instrumentation switch: 0 = normal build (no timing code emitted,
   zero runtime overhead); 1 = a measurement build that prints a per-scan
   Part-2 cycle breakdown (invalidate | features | score | total) plus a
   one-time Part-1 static-hash timing, via the DWT cycle counter
   (dwt_cycles.h). Ships 0 on every deploy/soak/demo build. Measure with
   IDS_SCAN_ARMED 1 so the timed scan is the real armed path; the Part-1 boot
   timing prints regardless of the arm state. */
#define IDS_LATENCY  0

/* Scan period. A parameter until the post-latency cadence decision; the same
   tick feeds the watchdog, so this must stay under IDS_WATCHDOG_PERIOD_S,
   which is itself under the 32.7 s IWDG hardware ceiling. */
#define IDS_SCAN_PERIOD_S  25u

/* IWDG countdown. Longer than the scan period (a clean scan must refresh it
   with margin to spare) and under the 32.7 s ceiling (LSI ~32 kHz / 256 =
   12-bit reload). LSI drift makes it nominal, not exact; the margin absorbs
   that. */
#define IDS_WATCHDOG_PERIOD_S  30u

/* Arm the secure IDS: lock TIM2 to the Secure world and start the scan tick,
   then lock + start the IWDG. Call once after StaticHash_BootCheck (console
   up), before the NonSecure jump. Ticks fire during NonSecure execution by
   design; the first fires one full scan period in, so the first watchdog
   countdown must (and does) outlast it. */
void IdsScan_Init(void);

#endif /* IDS_SCAN_H */
