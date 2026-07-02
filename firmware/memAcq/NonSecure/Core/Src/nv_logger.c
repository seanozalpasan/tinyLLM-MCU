/*
 * nv_logger.c -- NV-region dummy sensor logger (NonSecure benign workload).
 *
 * Evolved from the ns-flash_static_proof append-log demo: the same
 * direct-register NS-flash doubleword programming, now writing the structured
 * nv_spec.h layout (per 2 KB page: [NvHeader 64 B][124 x NvRecord 16 B]) into
 * pages 126/127. This is the byte surface the one-class ML learns, so realism
 * comes from RULES -- bounded ranges, correlated channels, per-channel refresh
 * periods, monotonic timestamps -- never from unpredictable churn: churn widens
 * the benign spread, and detectability = anomaly distance / benign spread.
 *
 * Flash discipline: after NvLogger_Init() nothing is ever read back from flash;
 * all write-side state (page, slot, counters, stats) lives in RAM. That dodges
 * the L5 stale read-after-write hazard (a just-programmed doubleword can read
 * stale through the flash cache in the same boot; the write itself is correct).
 */

#include "nv_logger.h"

#include <stdio.h>
#include <string.h>

#include "main.h"   /* device headers (FLASH_NS) + the SECURE_print_Log veneer */

#if NV_LOGGER

/* ===== dummy-signal shape (scaled units, matching nv_spec.h) =====
   Slow triangle waves + LSB-scale jitter: realistic-looking, strictly bounded,
   fully rule-governed. Periods count REFRESH events, not seconds, so the byte
   texture is the same in every rate regime. */
#define GEN_TEMP_MID     2350    /* 23.50 degC                     */
#define GEN_TEMP_AMP      150    /* +-1.50 degC per wave           */
#define GEN_TEMP_PERIOD   240    /* refreshes per wave             */
#define GEN_TEMP_JIT        2

#define GEN_HUM_MID      4500    /* 45.00 %RH                      */
#define GEN_HUM_AMP       300
#define GEN_HUM_PERIOD    360
#define GEN_HUM_JIT         3
#define GEN_HUM_K           2    /* anti-correlation vs temp (see gen_tick) */

#define GEN_PRESS_MID  101325    /* 1013.25 hPa                    */
#define GEN_PRESS_AMP     200    /* +-2.00 hPa                     */
#define GEN_PRESS_PERIOD  480
#define GEN_PRESS_JIT       2

/* ===== NS-flash program/erase primitives (from the static-proof demo) ===== */

#define NV_ERASED_DW  0xFFFFFFFFFFFFFFFFULL   /* an un-programmed doubleword */

#define NV_SR_ERR_MASK  (FLASH_NSSR_NSPROGERR | FLASH_NSSR_NSWRPERR | FLASH_NSSR_NSPGAERR | \
                         FLASH_NSSR_NSSIZERR  | FLASH_NSSR_NSPGSERR)
#define NV_SR_CLR_MASK  (FLASH_NSSR_NSEOP | FLASH_NSSR_NSOPERR | NV_SR_ERR_MASK)

static void Nv_Unlock(void)
{
  /* NSKEYR unlock sequence; the constants are the architectural FLASH keys (RM0438). */
  if ((FLASH_NS->NSCR & FLASH_NSCR_NSLOCK) != 0u)
  {
    FLASH_NS->NSKEYR = 0x45670123u;
    FLASH_NS->NSKEYR = 0xCDEF89ABu;
  }
}

static void Nv_Lock(void) { FLASH_NS->NSCR |= FLASH_NSCR_NSLOCK; }

/* Erase one 2 KB Bank-2 page (addr = page base). BKER selects bank 2; PNB is the
   page index within the bank. Returns 0 on success. */
static int Nv_ErasePage(uint32_t addr)
{
  const uint32_t page = (addr - NV_NS_FLASH_BASE) / NV_PAGE_SIZE;
  while ((FLASH_NS->NSSR & FLASH_NSSR_NSBSY) != 0u) { }
  FLASH_NS->NSSR = NV_SR_CLR_MASK;                         /* clear stale flags (write-1-to-clear) */
  const uint32_t cr = FLASH_NSCR_NSPER | FLASH_NSCR_NSBKER | (page << FLASH_NSCR_NSPNB_Pos);
  FLASH_NS->NSCR = cr;
  FLASH_NS->NSCR = cr | FLASH_NSCR_NSSTRT;
  while ((FLASH_NS->NSSR & FLASH_NSSR_NSBSY) != 0u) { }
  const int err = ((FLASH_NS->NSSR & NV_SR_ERR_MASK) != 0u) ? -1 : 0;
  FLASH_NS->NSCR = 0u;
  return err;
}

/* Program one 64-bit doubleword at addr (8-byte aligned, currently erased). The pair
   of 32-bit stores forms the doubleword the controller commits with one ECC. */
static int Nv_ProgramDW(uint32_t addr, uint64_t value)
{
  while ((FLASH_NS->NSSR & FLASH_NSSR_NSBSY) != 0u) { }
  FLASH_NS->NSSR = NV_SR_CLR_MASK;
  FLASH_NS->NSCR = FLASH_NSCR_NSPG;
  *(volatile uint32_t *)(addr)      = (uint32_t)(value & 0xFFFFFFFFu);
  *(volatile uint32_t *)(addr + 4u) = (uint32_t)(value >> 32);
  while ((FLASH_NS->NSSR & FLASH_NSSR_NSBSY) != 0u) { }
  const int err = ((FLASH_NS->NSSR & NV_SR_ERR_MASK) != 0u) ? -1 : 0;
  FLASH_NS->NSCR = 0u;
  return err;
}

/* ===== logger state (RAM only after Init -- see the file header) ===== */

typedef struct
{
  int64_t  sum;
  int32_t  min;
  int32_t  max;
  uint32_t n;
} ChanStats;

static uint32_t  s_page_base;      /* current page base; 0 = none opened yet    */
static uint32_t  s_slot;           /* next record slot in the current page      */
static uint32_t  s_page_seq;       /* seq of the current page (next open = +1)  */
static uint32_t  s_boot_count;     /* this boot's number, stamped at page-opens */
static uint32_t  s_op_count;       /* records fully programmed, lifetime        */
static uint32_t  s_last_ms;        /* HAL tick of the last record               */
static uint32_t  s_tick;           /* records this boot (drives refresh cadence)*/
static uint8_t   s_known_blank[NV_NUM_PAGES];  /* page verified/erased blank    */
static uint8_t   s_fault;          /* a flash op failed: stop, don't corrupt    */
static ChanStats s_stats[3];       /* temp / hum / press, this boot             */
static int32_t   s_temp, s_hum, s_press;       /* held channel values           */
static uint32_t  s_ph_temp, s_ph_hum, s_ph_press;  /* wave phases (refreshes)   */
static uint32_t  s_lcg = 0x13572468u;          /* jitter PRNG state             */

/* ===== rule-governed dummy readings ===== */

/* Symmetric triangle: phase 0..period-1 -> -amp .. +amp .. -amp. */
static int32_t tri_wave(uint32_t phase, int32_t period, int32_t amp)
{
  const int32_t p = (int32_t)(phase % (uint32_t)period);
  const int32_t half = period / 2;
  return (2 * amp * ((p < half) ? p : (period - p))) / half - amp;
}

/* Bounded pseudo-jitter in [-j, +j]: mimics LSB sensor noise. Deliberately tiny --
   jitter variance raises the benign noise floor the detector must see over. */
static int32_t jitter(int32_t j)
{
  s_lcg = s_lcg * 1664525u + 1013904223u;
  return (int32_t)((s_lcg >> 16) % (uint32_t)(2 * j + 1)) - j;
}

static int32_t clampi(int32_t v, int32_t lo, int32_t hi)
{
  return (v < lo) ? lo : ((v > hi) ? hi : v);
}

/* Advance the generator one record tick. Channels refresh on their own cadence
   and HOLD in between (three interleaved byte periodicities). */
static void gen_tick(void)
{
  if ((s_tick % NV_LOGGER_TEMP_EVERY) == 0u)
  {
    s_temp = clampi(GEN_TEMP_MID + tri_wave(s_ph_temp++, GEN_TEMP_PERIOD, GEN_TEMP_AMP)
                    + jitter(GEN_TEMP_JIT), NV_TEMP_LO, NV_TEMP_HI);
  }
  if ((s_tick % NV_LOGGER_HUM_EVERY) == 0u)
  {
    /* Humidity tracks temperature inversely (warmer air, lower relative humidity)
       plus its own slower wave -- a cross-channel rule a foreign payload breaks. */
    s_hum = clampi(GEN_HUM_MID - GEN_HUM_K * (s_temp - GEN_TEMP_MID)
                   + tri_wave(s_ph_hum++, GEN_HUM_PERIOD, GEN_HUM_AMP)
                   + jitter(GEN_HUM_JIT), (int32_t)NV_HUM_LO, (int32_t)NV_HUM_HI);
  }
  if ((s_tick % NV_LOGGER_PRESS_EVERY) == 0u)
  {
    s_press = clampi(GEN_PRESS_MID + tri_wave(s_ph_press++, GEN_PRESS_PERIOD, GEN_PRESS_AMP)
                     + jitter(GEN_PRESS_JIT), (int32_t)NV_PRESS_LO, (int32_t)NV_PRESS_HI);
  }
  s_tick++;
}

static void stats_add(ChanStats *s, int32_t v)
{
  if (s->n == 0u || v < s->min) { s->min = v; }
  if (s->n == 0u || v > s->max) { s->max = v; }
  s->sum += v;
  s->n++;
}

/* Mean = 64-bit sum / count, truncated toward zero -- exactly what C99 division
   does, and exactly what the spec promises the Python side. */
static int32_t stats_mean(const ChanStats *s)
{
  return (s->n != 0u) ? (int32_t)(s->sum / (int64_t)s->n) : 0;
}

static void fault(const char *what)
{
  char msg[64];
  s_fault = 1u;
  snprintf(msg, sizeof(msg), "[NVLOG] FAULT: flash %s failed; logging stopped\r\n", what);
  SECURE_print_Log(msg);
}

/* ===== ring maintenance ===== */

static int header_valid(const NvHeader *h)
{
  /* Strict enough to reject the old proof-demo leftovers: a lone counter
     doubleword yields page_seq == 0, which we never write. */
  return (h->version == NV_SPEC_VERSION) && (h->reserved0 == 0u)
      && (h->page_seq >= 1u) && (h->page_seq != 0xFFFFFFFFu);
}

static int page_blank(uint32_t base)
{
  const uint64_t *dw = (const uint64_t *)base;
  for (uint32_t i = 0u; i < NV_PAGE_SIZE / 8u; i++)
  {
    if (dw[i] != NV_ERASED_DW) { return 0; }
  }
  return 1;
}

/* Erase (unless known blank) + stamp the other page's header from RAM state,
   making it the current page. Returns 0 on success. */
static int page_open_next(void)
{
  const uint32_t target = (s_page_base == NV_PAGE0_BASE) ? NV_PAGE1_BASE : NV_PAGE0_BASE;
  const uint32_t idx = (target == NV_PAGE0_BASE) ? 0u : 1u;
  union { NvHeader h; uint64_t dw[NV_HEADER_SIZE / 8u]; } u;
  int err = 0;

  memset(&u, NV_HEADER_PAD_FILL, sizeof(u));
  u.h.version    = NV_SPEC_VERSION;
  u.h.reserved0  = 0u;
  u.h.page_seq   = s_page_seq + 1u;
  u.h.boot_count = s_boot_count;
  u.h.op_count   = s_op_count;     /* records programmed BEFORE this page */
  u.h.temp_min   = s_stats[0].min;
  u.h.temp_max   = s_stats[0].max;
  u.h.temp_mean  = stats_mean(&s_stats[0]);
  u.h.hum_min    = (uint32_t)s_stats[1].min;
  u.h.hum_max    = (uint32_t)s_stats[1].max;
  u.h.hum_mean   = (uint32_t)stats_mean(&s_stats[1]);
  u.h.press_min  = (uint32_t)s_stats[2].min;
  u.h.press_max  = (uint32_t)s_stats[2].max;
  u.h.press_mean = (uint32_t)stats_mean(&s_stats[2]);

  Nv_Unlock();
  if (s_known_blank[idx] == 0u) { err = Nv_ErasePage(target); }
  s_known_blank[idx] = 0u;
  for (uint32_t i = 0u; (i < NV_HEADER_SIZE / 8u) && (err == 0); i++)
  {
    err = Nv_ProgramDW(target + 8u * i, u.dw[i]);
  }
  Nv_Lock();
  if (err != 0) { return -1; }

  s_page_base = target;
  s_page_seq += 1u;
  s_slot = 0u;
  return 0;
}

/* ===== public API ===== */

void NvLogger_Init(void)
{
  const NvHeader *h0 = (const NvHeader *)NV_PAGE0_BASE;
  const NvHeader *h1 = (const NvHeader *)NV_PAGE1_BASE;
  int v0 = header_valid(h0);
  int v1 = header_valid(h1);
  char msg[112];

  /* Equal sequence numbers can't be written by this logger -- treat as corrupt. */
  if (v0 && v1 && (h0->page_seq == h1->page_seq)) { v0 = 0; v1 = 0; }

  if (!v0 && !v1)
  {
    /* Virgin flash or foreign leftovers (e.g. the old proof demo's append log):
       wipe so dumps only ever contain spec-defined bytes. */
    Nv_Unlock();
    if (Nv_ErasePage(NV_PAGE0_BASE) != 0 || Nv_ErasePage(NV_PAGE1_BASE) != 0) { fault("erase"); }
    Nv_Lock();
    s_known_blank[0] = 1u;
    s_known_blank[1] = 1u;
    s_page_base  = 0u;   /* first record triggers the first page-open */
    s_page_seq   = 0u;
    s_boot_count = 1u;
    s_op_count   = 0u;
  }
  else
  {
    const NvHeader *cur = (!v1 || (v0 && (h0->page_seq > h1->page_seq))) ? h0 : h1;
    const uint32_t oth_base = ((uint32_t)cur == NV_PAGE0_BASE) ? NV_PAGE1_BASE : NV_PAGE0_BASE;
    const int oth_valid = ((uint32_t)cur == NV_PAGE0_BASE) ? v1 : v0;
    const uint32_t oth_idx = (oth_base == NV_PAGE0_BASE) ? 0u : 1u;

    s_page_base  = (uint32_t)cur;
    s_page_seq   = cur->page_seq;
    s_boot_count = cur->boot_count + 1u;

    /* The write head is FOUND, not stored: first all-0xFF slot of the newest page. */
    while (s_slot < NV_RECORDS_PER_PAGE)
    {
      const uint64_t *dw = (const uint64_t *)(s_page_base + NV_HEADER_SIZE
                                              + s_slot * NV_RECORD_SIZE);
      if ((dw[0] == NV_ERASED_DW) && (dw[1] == NV_ERASED_DW)) { break; }
      s_slot++;
    }
    s_op_count = cur->op_count + s_slot;

    /* A non-current page that is neither valid ring data nor blank is foreign --
       wipe it now so it can't pollute benign dumps. */
    if (!oth_valid)
    {
      if (page_blank(oth_base)) { s_known_blank[oth_idx] = 1u; }
      else
      {
        Nv_Unlock();
        if (Nv_ErasePage(oth_base) != 0) { fault("erase"); }
        Nv_Lock();
        s_known_blank[oth_idx] = 1u;
      }
    }
  }

  /* Stats and the generator start fresh each boot (the spec's per-boot rule);
     all flash reads above happen before any write this boot, so the stale
     read-after-write hazard can't bite. */
  s_last_ms = HAL_GetTick();

  snprintf(msg, sizeof(msg), "[NVLOG] init: seq=%lu boot=%lu op=%lu slot=%lu/%u period=%us\r\n",
           (unsigned long)s_page_seq, (unsigned long)s_boot_count, (unsigned long)s_op_count,
           (unsigned long)s_slot, (unsigned)NV_RECORDS_PER_PAGE, (unsigned)NV_LOGGER_PERIOD_S);
  SECURE_print_Log(msg);
}

int NvLogger_Poll(NvReading *out)
{
  const uint32_t now = HAL_GetTick();
  union { NvRecord r; uint64_t dw[NV_RECORD_SIZE / 8u]; } u;
  int err = 0;

  if (s_fault != 0u) { return 0; }
  if ((now - s_last_ms) < (NV_LOGGER_PERIOD_S * 1000u)) { return 0; }
  s_last_ms = now;

  /* Order is the spec's: reading -> stats -> (page-open if needed, stamping a
     header that already includes this reading) -> program the record. */
  gen_tick();
  stats_add(&s_stats[0], s_temp);
  stats_add(&s_stats[1], s_hum);
  stats_add(&s_stats[2], s_press);

  if ((s_page_base == 0u) || (s_slot >= NV_RECORDS_PER_PAGE))
  {
    if (page_open_next() != 0) { fault("page-open"); return 0; }
  }

  u.r.ts    = now / 1000u;   /* u32 seconds since boot */
  u.r.temp  = s_temp;
  u.r.hum   = (uint32_t)s_hum;
  u.r.press = (uint32_t)s_press;

  Nv_Unlock();
  for (uint32_t i = 0u; (i < NV_RECORD_SIZE / 8u) && (err == 0); i++)
  {
    err = Nv_ProgramDW(s_page_base + NV_HEADER_SIZE + s_slot * NV_RECORD_SIZE + 8u * i,
                       u.dw[i]);
  }
  Nv_Lock();
  if (err != 0) { fault("program"); return 0; }

  s_slot++;
  s_op_count++;

  out->ts    = u.r.ts;
  out->temp  = u.r.temp;
  out->hum   = u.r.hum;
  out->press = u.r.press;
  out->op    = s_op_count;
  return 1;
}

#endif /* NV_LOGGER */
