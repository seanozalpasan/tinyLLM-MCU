/*
 * nv_logger.c -- NV-region sensor logger (NonSecure benign workload).
 *
 * Evolved from the ns-flash_static_proof append-log demo: the same
 * direct-register NS-flash doubleword programming, now writing the structured
 * nv_spec.h layout (per 2 KB page: [NvHeader 64 B][4 x NvJournalEntry 8 B]
 * [122 x NvRecord 16 B]) into pages 126/127. This is the byte surface the
 * one-class ML learns. Values come from the BME280 (bme280.c), all three
 * channels read fresh every record tick -- real physics supplies the bounded
 * ranges and cross-channel correlations the model learns; the byte LAYOUT
 * rules (fixed stride, monotonic timestamps, ring discipline) live here.
 *
 * A failed sensor read SKIPS its record (nothing is written, the cadence
 * holds) rather than logging garbage -- an out-of-range record would trip
 * the training data's benign gate and cost a whole capture. *
 *
 * The settings journal persists the display units (B2 presses): page-open
 * stamps J0 from RAM, each runtime change programs the next blank slot, and
 * the live setting is the end of the contiguous chain, found once at boot.
 * Records stay canonical regardless -- units change what telemetry says,
 * never what flash stores.
 *
 * Flash discipline: after NvLogger_Init() nothing is ever read back from flash;
 * all write-side state (page, slot, counters, stats, settings) lives in RAM.
 * That dodges the L5 stale read-after-write hazard (a just-programmed
 * doubleword can read stale through the flash cache in the same boot; the
 * write itself is correct).
 */

#include "nv_logger.h"

#include <stdio.h>
#include <string.h>

#include "main.h"   /* device headers (FLASH_NS) + the SECURE_print_Log veneer */
#include "bme280.h" /* the real sensor: BME280_Measure per record tick */

#if NV_LOGGER

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
static uint32_t  s_journal_used;   /* written journal slots in the current page */
static uint8_t   s_unit_temp;      /* live display units -- the RAM shadow of   */
static uint8_t   s_unit_press;     /*   the journal chain end (read at Init)    */
static uint32_t  s_page_seq;       /* seq of the current page (next open = +1)  */
static uint32_t  s_boot_count;     /* this boot's number, stamped at page-opens */
static uint32_t  s_op_count;       /* records fully programmed, lifetime        */
static uint32_t  s_last_ms;        /* HAL tick of the last record               */
static uint32_t  s_ms_hi;          /* upper word of the 64-bit uptime in ms     */
static uint32_t  s_ms_last;        /* last raw HAL tick seen (wrap detector)    */
static uint8_t   s_known_blank[NV_NUM_PAGES];  /* page verified/erased blank    */
static uint8_t   s_fault;          /* a flash op failed: stop, don't corrupt    */
static ChanStats s_stats[3];       /* temp / hum / press, this boot             */

/* ===== sensor readings (BME280, all channels fresh every tick) ===== */

static int32_t clampi(int32_t v, int32_t lo, int32_t hi)
{
  return (v < lo) ? lo : ((v > hi) ? hi : v);
}

/* Consecutive BME280_Measure failures. One failure is a shrug (a lone blip
   self-clears); the second in a row means the bus is presumed wedged -- a
   mid-transfer glitch leaves the chip holding SDA low, and no number of plain
   retries ever clears that -- so from then on every tick runs BME280_Recover
   before giving up. Success on any path zeroes the streak. */
#define NV_SENSOR_FAILS_BEFORE_RECOVER  2u
static uint32_t s_sensor_fails;

/* One fresh reading of all three channels, clamped to the spec ranges.
   Returns 0 and fills the outputs, or -1 when the record must be SKIPPED:
   bus fault, measurement timeout, or the Bosch divide-by-zero guard's
   pressure==0 sentinel -- any of which would otherwise put an out-of-range
   record on flash and cost the whole capture at the benign gate. A clamp
   should be impossible (the chip's operating range IS the spec range), so
   firing one prints a loud note to investigate. */
static int read_sensor(int32_t *temp, int32_t *hum, int32_t *press)
{
  BME280_Sample smp;

  if (BME280_Measure(&smp) != 0 || smp.comp_p == 0u)
  {
    s_sensor_fails++;
    if (s_sensor_fails < NV_SENSOR_FAILS_BEFORE_RECOVER)
    {
      SECURE_print_Log("[NVLOG] sensor read failed; record skipped\r\n");
      return -1;
    }
    /* Heal the bus and retry the measurement in the same tick, so a
       successful recovery costs exactly the one already-skipped record. */
    SECURE_print_Log("[NVLOG] consecutive sensor failures; attempting bus recovery\r\n");
    if (BME280_Recover() != 0 || BME280_Measure(&smp) != 0 || smp.comp_p == 0u)
    {
      SECURE_print_Log("[NVLOG] recovery FAILED; record skipped (will retry next tick)\r\n");
      return -1;
    }
    SECURE_print_Log("[NVLOG] bus recovered; logging resumes\r\n");
  }
  s_sensor_fails = 0u;

  *temp  = clampi(smp.temp, NV_TEMP_LO, NV_TEMP_HI);
  *hum   = clampi((int32_t)smp.hum, (int32_t)NV_HUM_LO, (int32_t)NV_HUM_HI);
  *press = clampi((int32_t)smp.press, (int32_t)NV_PRESS_LO, (int32_t)NV_PRESS_HI);
  if (*temp != smp.temp || *hum != (int32_t)smp.hum || *press != (int32_t)smp.press)
  {
    SECURE_print_Log("[NVLOG] WARNING: reading clamped to spec range -- investigate\r\n");
  }
  return 0;
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

/* ===== display-unit settings (the spec's per-page journal) ===== */

/* Toggle one display unit (which: 0 = temperature, 1 = pressure) by programming
   the NEXT BLANK journal slot -- FLASH FIRST, RAM SECOND, so a flash fault
   refuses the change and RAM can never disagree with the journal. A journal
   slot is one doubleword, so a reset mid-write leaves it fully written or
   still blank, never half a setting. Returns 0 on success, -1 refused. */
static int settings_toggle(int which)
{
  union { NvJournalEntry e; uint64_t dw; } u;
  char msg[80];
  int err;

  if (s_fault != 0u) { return -1; }
  if (s_page_base == 0u)
  {
    /* Virgin/wiped ring with the first record still pending: no page to
       journal into yet. Self-heals within one record period (that record's
       page-open stamps J0); refusing keeps RAM == journal. */
    SECURE_print_Log("[NVSET] refused: no page open yet (first record pending)\r\n");
    return -1;
  }
  if (s_journal_used >= NV_JOURNAL_SLOTS)
  {
    SECURE_print_Log("[NVSET] REFUSED!: journal full until next page rotation\r\n");
    return -1;
  }

  memset(&u, 0, sizeof(u));
  u.e.unit_temp  = (which == 0) ? (uint8_t)(s_unit_temp ^ 1u) : s_unit_temp;
  u.e.unit_press = (which == 1) ? (uint8_t)(s_unit_press ^ 1u) : s_unit_press;
  u.e.reserved0  = 0u;
  u.e.op_count   = s_op_count;   /* binds the change to its spot in the record stream */

  Nv_Unlock();
  err = Nv_ProgramDW(s_page_base + NV_JOURNAL_OFFSET
                     + s_journal_used * NV_JOURNAL_ENTRY_SIZE, u.dw);
  Nv_Lock();
  if (err != 0)
  {
    SECURE_print_Log("[NVSET] refused: journal flash write failed (RAM unchanged)\r\n");
    return -1;
  }

  s_journal_used++;
  s_unit_temp  = u.e.unit_temp;
  s_unit_press = u.e.unit_press;
  snprintf(msg, sizeof(msg), "[NVSET] units now: temp=deg%s press=%s\r\n",
           (s_unit_temp == NV_UNIT_TEMP_F) ? "F" : "C",
           (s_unit_press == NV_UNIT_PRESS_INHG) ? "inHg" : "hPa");
  SECURE_print_Log(msg);
  return 0;
}

#if NV_SETTINGS_EXERCISE
/* ===== settings-exercise schedule (campaign builds only) =====
   A page's plan is a pure function of (NV_EXERCISE_SEED, page index), where
   page index = lifetime record index / records-per-page -- valid because every
   page holds exactly NV_RECORDS_PER_PAGE records and op_count only resets with
   a ring wipe, so page boundaries sit at exact op_count multiples. Change
   count per page: 0/1/2/3 with weights 184/56/10/6 of 256 (~72% of pages stay
   J0-only, ~2% fill the journal), at most the 3 free slots after J0 -- the
   schedule can never hit the journal-full refusal by construction. */

/* 32-bit avalanche mixer (murmur3-style finalizer): consecutive page indices
   and seed bits decorrelate fully, so the plan sequence has no visible pattern
   while staying exactly reproducible from (seed, page). */
static uint32_t ex_mix(uint32_t x)
{
  x ^= x >> 16;  x *= 0x7FEB352Du;
  x ^= x >> 15;  x *= 0x846CA68Bu;
  x ^= x >> 16;
  return x;
}

/* This page's planned changes: fills off[] (record index within the page,
   strictly ascending) and press[] (0 = toggle temperature, 1 = pressure);
   returns the count, 0..3. */
static uint32_t ex_plan(uint32_t page, uint8_t off[3], uint8_t press[3])
{
  uint32_t h = ex_mix(NV_EXERCISE_SEED ^ (page * 2654435761u));
  const uint32_t b = h >> 24;
  uint32_t n = (b < 184u) ? 0u : ((b < 240u) ? 1u : ((b < 250u) ? 2u : 3u));

  for (uint32_t i = 0u; i < n; i++)
  {
    h = ex_mix(h + 0x9E3779B9u);
    off[i]   = (uint8_t)(h % NV_RECORDS_PER_PAGE);
    press[i] = (uint8_t)((h >> 8) & 1u);
  }

  /* Sort ascending (insertion, n <= 3), then nudge collisions one record
     forward; a nudge past the last record just drops that change. */
  for (uint32_t i = 1u; i < n; i++)
  {
    const uint8_t o = off[i], p = press[i];
    uint32_t j = i;
    while ((j > 0u) && (off[j - 1u] > o)) { off[j] = off[j - 1u]; press[j] = press[j - 1u]; j--; }
    off[j] = o;  press[j] = p;
  }
  for (uint32_t i = 1u; i < n; i++)
  {
    if (off[i] <= off[i - 1u])
    {
      if ((uint32_t)off[i - 1u] + 1u >= NV_RECORDS_PER_PAGE) { n = i; break; }
      off[i] = (uint8_t)(off[i - 1u] + 1u);
    }
  }
  return n;
}

/* Called once per record, right after it programs: fire any change planned at
   this record index. A refusal (e.g. a manual B2 press consumed the slots)
   just skips -- the schedule never retries, so its ATTEMPTS stay a pure
   function of (seed, op_count) even when reality interferes. */
static void ex_tick(uint32_t rec_index)
{
  uint8_t off[3], press[3];
  const uint32_t n = ex_plan(rec_index / NV_RECORDS_PER_PAGE, off, press);
  const uint32_t r = rec_index % NV_RECORDS_PER_PAGE;

  for (uint32_t i = 0u; i < n; i++)
  {
    if ((uint32_t)off[i] == r)
    {
      (void)settings_toggle((press[i] != 0u) ? 1 : 0);
    }
  }
}
#endif /* NV_SETTINGS_EXERCISE */

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

/* Erase (unless known blank) + stamp the other page from RAM state, making it
   the current page. Program order is the crash-safety design: J0 (the current
   settings) FIRST, then the header body, and the header's validity doubleword
   (version/reserved0/page_seq -- the bytes that make this page "count as
   existing") LAST OF ALL. A reset anywhere mid-open leaves a header that
   header_valid() rejects, so Init wipes the fragment -- never a valid-looking
   page with a missing J0 or garbage counters. (The old ascending order
   programmed the validity fields first: a real crash window, now closed.)
   Returns 0 on success. */
static int page_open_next(void)
{
  const uint32_t target = (s_page_base == NV_PAGE0_BASE) ? NV_PAGE1_BASE : NV_PAGE0_BASE;
  const uint32_t idx = (target == NV_PAGE0_BASE) ? 0u : 1u;
  union { NvHeader h; uint64_t dw[NV_HEADER_SIZE / 8u]; } u;
  union { NvJournalEntry e; uint64_t dw; } j0;
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

  /* J0 carries the settings live at page-open; its op_count equals the
     header's by definition (both mean "records before this page"). */
  memset(&j0, 0, sizeof(j0));
  j0.e.unit_temp  = s_unit_temp;
  j0.e.unit_press = s_unit_press;
  j0.e.reserved0  = 0u;
  j0.e.op_count   = s_op_count;

  Nv_Unlock();
  if (s_known_blank[idx] == 0u) { err = Nv_ErasePage(target); }
  s_known_blank[idx] = 0u;
  if (err == 0) { err = Nv_ProgramDW(target + NV_JOURNAL_OFFSET, j0.dw); }
  for (uint32_t i = 1u; (i < NV_HEADER_SIZE / 8u) && (err == 0); i++)
  {
    err = Nv_ProgramDW(target + 8u * i, u.dw[i]);
  }
  if (err == 0) { err = Nv_ProgramDW(target, u.dw[0]); }   /* validity word LAST */
  Nv_Lock();
  if (err != 0) { return -1; }

  s_page_base = target;
  s_page_seq += 1u;
  s_slot = 0u;
  s_journal_used = 1u;   /* J0 stamped; the 3 change slots are free again */
  return 0;
}

/* ===== public API ===== */

void NvLogger_Init(void)
{
  const NvHeader *h0 = (const NvHeader *)NV_PAGE0_BASE;
  const NvHeader *h1 = (const NvHeader *)NV_PAGE1_BASE;
  int v0 = header_valid(h0);
  int v1 = header_valid(h1);
  char msg[128];

  /* Settings start at the defaults; only a valid journal chain end below may
     override them. Garbage or virgin flash keeps the defaults -- the IDS, not
     the boot path, raises any alarm. */
  s_unit_temp    = NV_UNIT_TEMP_C;
  s_unit_press   = NV_UNIT_PRESS_HPA;
  s_journal_used = 0u;

  /* Equal sequence numbers can't be written by this logger -- treat as corrupt. */
  if (v0 && v1 && (h0->page_seq == h1->page_seq)) { v0 = 0; v1 = 0; }

  if (!v0 && !v1)
  {
    /* Virgin flash or foreign leftovers (e.g. the old proof demo's append log):
       wipe so dumps only ever contain spec-defined bytes. Already-blank pages
       (routine after a host-side --fresh erase) are skipped -- erase cycles are
       the wear budget, and a blank page needs none. */
    Nv_Unlock();
    if (!page_blank(NV_PAGE0_BASE) && (Nv_ErasePage(NV_PAGE0_BASE) != 0)) { fault("erase"); }
    if (!page_blank(NV_PAGE1_BASE) && (Nv_ErasePage(NV_PAGE1_BASE) != 0)) { fault("erase"); }
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
      const uint64_t *dw = (const uint64_t *)(s_page_base + NV_RECORDS_OFFSET
                                              + s_slot * NV_RECORD_SIZE);
      if ((dw[0] == NV_ERASED_DW) && (dw[1] == NV_ERASED_DW)) { break; }
      s_slot++;
    }
    s_op_count = cur->op_count + s_slot;

    /* The live settings are the END of the contiguous journal chain, found the
       same way as the head: walk J0->J3, stop at the first blank slot. A
       written slot past a blank gap is benignly impossible and never adopted;
       a garbage chain end (reserved0 != 0 or a unit outside {0,1}) keeps the
       defaults -- in both cases the foreign ink is the IDS's to flag, and the
       write path below still targets the first blank slot either way. */
    {
      const NvJournalEntry *jrn = (const NvJournalEntry *)(s_page_base + NV_JOURNAL_OFFSET);
      const uint64_t *jdw = (const uint64_t *)(s_page_base + NV_JOURNAL_OFFSET);
      while (s_journal_used < NV_JOURNAL_SLOTS && jdw[s_journal_used] != NV_ERASED_DW)
      {
        s_journal_used++;
      }
      if (s_journal_used > 0u)
      {
        const NvJournalEntry *live = &jrn[s_journal_used - 1u];
        if ((live->reserved0 == 0u) && (live->unit_temp <= 1u) && (live->unit_press <= 1u))
        {
          s_unit_temp  = live->unit_temp;
          s_unit_press = live->unit_press;
        }
      }
    }

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

  /* Stats start fresh each boot (the spec's per-boot rule). No generator state
     to restore: the sensor is the state, and physics doesn't reset with the
     board (the old dummy generator's wave-phase resume existed to fake exactly
     that). All flash reads above happen before any write this boot, so the
     stale read-after-write hazard can't bite. */
  s_last_ms  = HAL_GetTick();
  s_ms_last  = s_last_ms;   /* seed the 64-bit uptime wrap detector (see Poll) */

  snprintf(msg, sizeof(msg),
           "[NVLOG] init: seq=%lu boot=%lu op=%lu slot=%lu/%u jrnl=%lu/%u units=%s,%s period=%us\r\n",
           (unsigned long)s_page_seq, (unsigned long)s_boot_count, (unsigned long)s_op_count,
           (unsigned long)s_slot, (unsigned)NV_RECORDS_PER_PAGE,
           (unsigned long)s_journal_used, (unsigned)NV_JOURNAL_SLOTS,
           (s_unit_temp == NV_UNIT_TEMP_F) ? "F" : "C",
           (s_unit_press == NV_UNIT_PRESS_INHG) ? "inHg" : "hPa",
           (unsigned)NV_LOGGER_PERIOD_S);
  SECURE_print_Log(msg);

#if NV_SETTINGS_EXERCISE
  /* Every capture's console log self-documents the active schedule. */
  snprintf(msg, sizeof(msg),
           "[NVLOG] settings-exercise ON: seed=0x%08lX changes-per-page 0/1/2/3 = 184/56/10/6 of 256\r\n",
           (unsigned long)NV_EXERCISE_SEED);
  SECURE_print_Log(msg);
#endif
}

int NvLogger_Poll(NvReading *out)
{
  const uint32_t now = HAL_GetTick();
  union { NvRecord r; uint64_t dw[NV_RECORD_SIZE / 8u]; } u;
  int32_t temp, hum, press;
  int err = 0;

  /* Extend the tick to 64 bits BEFORE any early return: HAL_GetTick() is u32
     MILLISECONDS and wraps at 49.7 days, and a wrapped ts would mimic the exact
     non-monotonic-timestamp anomaly the detector hunts (the byte spec promises
     ts never wraps). Poll runs every main-loop pass (~50 ms), so a raw wrap
     cannot slip between two observations. */
  if (now < s_ms_last) { s_ms_hi++; }
  s_ms_last = now;

  if (s_fault != 0u) { return 0; }
  /* u32 modular subtraction stays correct across the raw-tick wrap. */
  if ((now - s_last_ms) < (NV_LOGGER_PERIOD_S * 1000u)) { return 0; }
  s_last_ms = now;

  /* Part-1 pre-write gate: the static CODE region must re-prove clean before
     this image appends anything to NV -- a tampered image is caught at its
     first write attempt, not at the next periodic scan. Sits before the
     sensor read so a withheld record perturbs nothing (no stats, no page
     turn). The secure side latches its own verdict; the reset arrives via
     the watchdog whether or not this loop keeps polling. */
  if (SECURE_StaticHash_PreWriteCheck() != 0)
  {
    SECURE_print_Log("[NVLOG] record withheld: static-region check not clean\r\n");
    return 0;
  }

  /* Order is the spec's: reading -> stats -> (page-open if needed, stamping a
     header that already includes this reading) -> program the record. A failed
     read skips the record but keeps the cadence (s_last_ms already advanced):
     one lost record, not a stalled logger. */
  if (read_sensor(&temp, &hum, &press) != 0) { return 0; }
  stats_add(&s_stats[0], temp);
  stats_add(&s_stats[1], hum);
  stats_add(&s_stats[2], press);

  if ((s_page_base == 0u) || (s_slot >= NV_RECORDS_PER_PAGE))
  {
    if (page_open_next() != 0) { fault("page-open"); return 0; }
  }

  u.r.ts    = (uint32_t)((((uint64_t)s_ms_hi << 32) | now) / 1000u);   /* u32 s since boot, wrap-free */
  u.r.temp  = temp;
  u.r.hum   = (uint32_t)hum;
  u.r.press = (uint32_t)press;

  Nv_Unlock();
  for (uint32_t i = 0u; (i < NV_RECORD_SIZE / 8u) && (err == 0); i++)
  {
    err = Nv_ProgramDW(s_page_base + NV_RECORDS_OFFSET + s_slot * NV_RECORD_SIZE + 8u * i,
                       u.dw[i]);
  }
  Nv_Lock();
  if (err != 0) { fault("program"); return 0; }

  s_slot++;
  s_op_count++;

#if NV_SETTINGS_EXERCISE
  /* After the record is on flash, so a change fired here stamps an op_count
     that already includes it (exactly what a button press between records
     would stamp). */
  ex_tick(s_op_count - 1u);
#endif

  out->ts    = u.r.ts;
  out->temp  = u.r.temp;
  out->hum   = u.r.hum;
  out->press = u.r.press;
  out->op    = s_op_count;
  return 1;
}

NvSettings NvLogger_Settings(void)
{
  NvSettings s;
  s.unit_temp  = s_unit_temp;
  s.unit_press = s_unit_press;
  return s;
}

int NvLogger_ToggleTempUnit(void)  { return settings_toggle(0); }
int NvLogger_TogglePressUnit(void) { return settings_toggle(1); }

#endif /* NV_LOGGER */
