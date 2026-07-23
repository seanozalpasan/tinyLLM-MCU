/*
 * dwt_cycles.h -- zero-overhead cycle timing for the on-chip latency
 * measurement (Secure world, header-only; compiled in only under IDS_LATENCY).
 *
 * The Cortex-M33's DWT unit has a free-running 32-bit cycle counter (CYCCNT)
 * clocked at the core frequency (110 MHz on this board). Reading it before and
 * after a code region gives that region's exact cycle cost with no measurement
 * overhead beyond two register reads -- the standard way to time code on
 * Cortex-M. This is a header-only helper on purpose: no new .c compile unit,
 * so no CubeIDE project re-import is needed to add it.
 *
 * GOTCHA (running from flash, no debugger): CYCCNT only advances once TRCENA
 * (trace enable) and CYCCNTENA are set. A debugger sets TRCENA for you; a
 * board running standalone does NOT -- so dwt_cycles_init() must run once
 * before any measurement, or every delta reads zero. (If a delta ever does
 * read zero on hardware, the only other cause is a DWT software lock; this
 * M33's DWT has no lock register, so TRCENA+CYCCNTENA is the whole recipe.)
 *
 * Wraparound: CYCCNT rolls over every 2^32 / 110e6 ~= 39 s. Every region we
 * time (one NV scan, one 252 KB hash) is far shorter, so a plain uint32_t
 * (end - start) is always the true delta -- unsigned subtraction is correct
 * across at most one wrap.
 */
#ifndef DWT_CYCLES_H
#define DWT_CYCLES_H

#include <stdint.h>

#include "main.h"   /* CMSIS core: CoreDebug, DWT, and the *_Msk bit masks */

/* Core clock in MHz for the cycles -> microseconds conversion. Tied to
   SystemClock_Config's 110 MHz SYSCLK -- the same constant ids_scan.c divides
   down for its 10 kHz timer base. */
#define DWT_CPU_MHZ  110u

/* Enable the cycle counter. Idempotent: safe to call from more than one
   module's init (re-zeroing CYCCNT between boot phases is harmless). Uses the
   Secure CoreDebug alias (0xE000EDF0) -- correct because every timed region
   runs in the Secure world. */
static inline void dwt_cycles_init(void)
{
  CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;   /* power the trace/DWT block */
  DWT->CYCCNT = 0u;
  DWT->CTRL  |= DWT_CTRL_CYCCNTENA_Msk;
}

static inline uint32_t dwt_cycles_now(void)
{
  return DWT->CYCCNT;
}

/* A cycle delta split into whole + thousandths of a microsecond, overflow-safe
   (the fractional term is always < DWT_CPU_MHZ * 1000). Feed LAT_US(c) straight
   into a "%lu.%03lu us" printf field pair. */
static inline uint32_t dwt_us_whole(uint32_t cycles) { return cycles / DWT_CPU_MHZ; }
static inline uint32_t dwt_us_milli(uint32_t cycles)
{
  return ((cycles % DWT_CPU_MHZ) * 1000u) / DWT_CPU_MHZ;
}
#define LAT_US(c)  (unsigned long)dwt_us_whole(c), (unsigned long)dwt_us_milli(c)

#endif /* DWT_CYCLES_H */
