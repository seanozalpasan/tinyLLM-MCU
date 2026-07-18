/*
 * ids_scan.c -- the IDS runtime tick (Secure): TIM2 -> scan -> verdict.
 *
 * TIM2 is configured by direct register writes (same precedent as the
 * NonSecure Uart3_*): a basic up-counter needs ~6 registers, so the disabled
 * HAL TIM module stays disabled. 110 MHz APB1 / 11000 = 10 kHz counter;
 * TIM2's 32-bit reload covers any sane period.
 *
 * The scan runs INSIDE the timer interrupt (the decided preemptive design):
 * the NonSecure loop simply pauses for the scan's duration. Known-benign
 * interactions: a tick landing mid-record-write sees a torn head record
 * (an ~8 B disturbance -- measured far below the model's detection floor);
 * a paused mid-transaction sensor read just waits (I2C tolerates stalls,
 * worst case the logger skips that record); a tick landing mid-console-print
 * can garble one line (the UART is shared; verdict state never depends on
 * printf). GOTCHA: the Secure HAL tick is suspended once NonSecure runs, so
 * HAL timeouts in here cannot count -- fine on every normal path (they exit
 * on hardware flags), and a truly wedged flag spins forever, which the armed
 * watchdog turns into exactly the reset a wedged scanner deserves.
 *
 * Verdict -> watchdog: the secure IWDG's countdown is restarted ONLY when a
 * scan reads clean on BOTH parts -- Part-2 (this tick's NV score, with one
 * rescan to shrug off the page-erase transient) AND Part-1 (the static-region
 * hash that re-runs before every NV record write: not latched dirty, and its
 * clean-check count moved since the last tick). An anomaly, a failed scan, a
 * stalled pre-write heartbeat, or the tick never running all leave it to
 * lapse -> reset. HAL_IWDG_Refresh is a single key write (no timeout, no
 * tick), so it is safe in the ISR after HAL_SuspendTick.
 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>

#include "main.h"          /* HAL types + GTZC for the TIM2 secure grant */
#include "ids_scan.h"
#include "mahal_score.h"
#include "nv_features.h"
#include "nv_spec.h"       /* NV_LAYOUT_LOCK compile-time assert helper */
#include "static_hash.h"   /* Part-1 verdict + pre-write-check heartbeat */

/* 110 MHz APB1 timer clock -> 10 kHz counter; reload = period in 0.1 ms
   units. Both the /11000 and the clock it divides are build-time constants
   of this project (SystemClock_Config, APB1 DIV1). */
#define IDS_TIM_PSC        (11000u - 1u)
#define IDS_TIM_ARR        (IDS_SCAN_PERIOD_S * 10000u - 1u)

/* Below every runtime-armed secure IRQ priority in urgency (larger = lower
   on Cortex-M): a scan is seconds-long background work, never urgent. */
#define IDS_SCAN_IRQ_PRIO  6u

/* IWDG reload for IDS_WATCHDOG_PERIOD_S at the /256 prescaler: LSI 32 kHz /
   256 = 125 Hz, so reload = period_s * 125. Exact integer at these values. */
#define IDS_IWDG_RELOAD    (IDS_WATCHDOG_PERIOD_S * 32000u / 256u)

/* The scan MUST refresh the watchdog before it lapses, and the watchdog MUST
   stay inside the hardware ceiling -- both checked at compile time so no one
   can retune a period into a self-resetting board. */
NV_LAYOUT_LOCK(ids_scan_period, IDS_SCAN_PERIOD_S >= 1u && IDS_SCAN_PERIOD_S <= 32u);
NV_LAYOUT_LOCK(ids_wd_margin, IDS_SCAN_PERIOD_S < IDS_WATCHDOG_PERIOD_S);
NV_LAYOUT_LOCK(ids_wd_ceiling, IDS_IWDG_RELOAD <= 4095u);

static uint32_t scan_no;
static IWDG_HandleTypeDef ids_iwdg;

/* Display-only: score = sqrt(d2) as thousandths, so the console reads in the
   same units as every off-device number (13.874 = the alarm line). The
   VERDICT never takes this path -- it compares d2 against threshold^2 inside
   mahal_is_anomaly. Clamps rather than overflows on absurd d2. */
static uint32_t score_milli(float d2)
{
  const float s = sqrtf(d2) * 1000.0f + 0.5f;
  return (s < 4.0e9f) ? (uint32_t)s : 0xFFFFFFFFu;
}

/* One scoring pass over the live NV region. Returns 1 = benign (d2 in
   *d2_out), 0 = anomalous (d2 in *d2_out), -1 = failed (cache invalidate or
   a non-finite score -- either way never convertible to a clean verdict). */
static int score_pass(float *d2_out)
{
  /* Static, not stack: 480 B that would otherwise sit on the ISR stack. */
  static float feats[NV_FEAT_DIMS];

  if (NvFeatures_ScanRegion(feats) != 0)
  {
    /* Cache invalidate failed => the bytes could be stale => a fresh implant
       could hide. */
    printf("[IDS] scan #%lu FAILED (cache invalidate) -- withholding kick\r\n",
           (unsigned long)scan_no);
    return -1;
  }
  const float d2 = mahal_score_d2(feats);
  if (!(d2 >= 0.0f))
  {
    /* NaN (every compare against it is false) or negative: numerically
       corrupt scoring. GOTCHA: mahal_is_anomaly(NaN) would read benign --
       exactly the failure a scan must never convert to a clean verdict. */
    printf("[IDS] scan #%lu FAILED (non-finite score) -- withholding kick\r\n",
           (unsigned long)scan_no);
    return -1;
  }
  *d2_out = d2;
  return mahal_is_anomaly(d2) ? 0 : 1;
}

static void run_scan(void)
{
  /* Only writer of hash_seen; single-word Part-1 reads are preemption-safe
     (this ISR can interrupt a pre-write check, never the reverse). */
  static uint32_t hash_seen;
  float d2 = 0.0f;
  int clean = 0;

  scan_no++;

  const int      p1_dirty = StaticHash_Dirty();
  const uint32_t hc       = StaticHash_CheckCount();
  const int      p1_beat  = (hc != hash_seen);
  hash_seen = hc;

  int verdict = score_pass(&d2);
  if (verdict == 0)
  {
    /* One immediate rescan before alarming: a tick can land inside the
       logger's ~22 ms page erase and score a half-erased page. That
       transient is gone on the retry; a real implant is still in flash and
       alarms again right here. A second-pass failure stays a failure. */
    const float d2_first = d2;
    verdict = score_pass(&d2);
    const uint32_t m1 = score_milli(d2_first);
    const uint32_t m2 = score_milli(d2);
    if (verdict == 1)
    {
      printf("[IDS] scan #%lu score=%lu.%03lu then %lu.%03lu on rescan -- "
             "transient, benign\r\n",
             (unsigned long)scan_no,
             (unsigned long)(m1 / 1000u), (unsigned long)(m1 % 1000u),
             (unsigned long)(m2 / 1000u), (unsigned long)(m2 % 1000u));
    }
    else if (verdict == 0)
    {
      printf("[IDS] scan #%lu score=%lu.%03lu rescan=%lu.%03lu ANOMALY -- "
             "withholding watchdog kick, reset imminent\r\n",
             (unsigned long)scan_no,
             (unsigned long)(m1 / 1000u), (unsigned long)(m1 % 1000u),
             (unsigned long)(m2 / 1000u), (unsigned long)(m2 % 1000u));
    }
  }
  else if (verdict == 1)
  {
    const uint32_t milli = score_milli(d2);
    printf("[IDS] scan #%lu score=%lu.%03lu benign\r\n",
           (unsigned long)scan_no,
           (unsigned long)(milli / 1000u), (unsigned long)(milli % 1000u));
  }
  clean = (verdict == 1);

  /* Part-1 folds into the SAME gate: anomaly = Part-1 OR Part-2. Dirty =
     some hash check (boot or pre-write) saw a changed static region. A
     stalled heartbeat = the workload stopped running its pre-write checks
     (wedged, or a compromised image dodging the gate) -- silence is never
     clean. */
  if (clean && p1_dirty)
  {
    printf("[IDS] scan #%lu Part-1 static region DIRTY -- withholding "
           "watchdog kick, reset imminent\r\n", (unsigned long)scan_no);
    clean = 0;
  }
  else if (clean && !p1_beat)
  {
    printf("[IDS] scan #%lu no clean pre-write hash since last scan -- "
           "withholding watchdog kick, reset imminent\r\n",
           (unsigned long)scan_no);
    clean = 0;
  }

  /* The kick lives here and nowhere else: the watchdog countdown restarts
     ONLY when a scan completed and read clean on BOTH parts. Any anomaly,
     any scan failure, or the tick never running leaves the countdown to
     expire -> reset. This is the whole verdict -> watchdog contract. */
  if (clean)
  {
    HAL_IWDG_Refresh(&ids_iwdg);
  }
}

/* Lock the IWDG to the Secure world and start its countdown. Called last, so
   an early return from a failed TIM2 arm never leaves the watchdog running
   with no scan to feed it (which would just reset the board every period). */
static void iwdg_arm(void)
{
  if (HAL_GTZC_TZSC_ConfigPeriphAttributes(
          GTZC_PERIPH_IWDG, GTZC_TZSC_PERIPH_SEC | GTZC_TZSC_PERIPH_NPRIV) != HAL_OK)
  {
    /* Not fatal (NonSecure has no IWDG code), but the tamper-proof story
       wants NonSecure locked out of the reset arm -- say so if it slips. */
    printf("[IDS] warning: IWDG secure grant FAILED\r\n");
  }

  ids_iwdg.Instance       = IWDG;
  ids_iwdg.Init.Prescaler = IWDG_PRESCALER_256;
  ids_iwdg.Init.Reload    = IDS_IWDG_RELOAD;
  ids_iwdg.Init.Window    = IWDG_WINDOW_DISABLE;   /* plain watchdog: kick any time */
  if (HAL_IWDG_Init(&ids_iwdg) != HAL_OK)
  {
    printf("[IDS] ERROR: IWDG init FAILED -- reset arm ABSENT\r\n");
    return;
  }
  printf("[IDS] watchdog armed: %us countdown, secure (kick on clean scan)\r\n",
         IDS_WATCHDOG_PERIOD_S);
}

void IdsScan_Init(void)
{
  /* Lock TIM2 to the Secure world FIRST: from here on, NonSecure reads of
     its registers are zeros and writes are ignored -- the scan schedule is
     out of the attacker's reach. */
  if (HAL_GTZC_TZSC_ConfigPeriphAttributes(
          GTZC_PERIPH_TIM2, GTZC_TZSC_PERIPH_SEC | GTZC_TZSC_PERIPH_NPRIV) != HAL_OK)
  {
    printf("[IDS] init: TIM2 secure grant FAILED -- scan tick NOT armed\r\n");
    return;
  }
  __HAL_RCC_TIM2_CLK_ENABLE();

  TIM2->CR1 = 0u;                /* clean slate: counter stopped */
  TIM2->PSC = IDS_TIM_PSC;
  TIM2->ARR = IDS_TIM_ARR;
  TIM2->EGR = TIM_EGR_UG;        /* latch PSC/ARR now (side effect: raises UIF) */
  TIM2->SR  = 0u;                /* scrub the UG-raised flag: first tick = one full period out */
  TIM2->DIER = TIM_DIER_UIE;

  NVIC_SetPriority(TIM2_IRQn, IDS_SCAN_IRQ_PRIO);
  NVIC_EnableIRQ(TIM2_IRQn);

  TIM2->CR1 = TIM_CR1_CEN;
  printf("[IDS] scan tick armed: every %us (TIM2, secure)\r\n", IDS_SCAN_PERIOD_S);

  /* Arm the reset half last: the scan tick above is now live to feed it. */
  iwdg_arm();
}

/* The Secure vector table's TIM2 slot (ITNS is all-secure; the weak default
   from the startup file is overridden here, keeping the module self-contained
   -- stm32l5xx_it.c stays untouched). */
void TIM2_IRQHandler(void)
{
  if ((TIM2->SR & TIM_SR_UIF) != 0u)
  {
    TIM2->SR = ~TIM_SR_UIF;      /* rc_w0: writing 0 clears, 1s leave others untouched */
    run_scan();
  }
}
