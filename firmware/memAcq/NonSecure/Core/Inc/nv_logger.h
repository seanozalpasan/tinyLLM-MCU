/*
 * nv_logger.h -- NV-region dummy sensor logger (NonSecure benign workload).
 *
 * Writes temp/humidity/pressure readings into the 4 KB NV flash region per the
 * generated nv_spec.h layout (the byte surface the one-class ML monitors), and
 * hands each logged reading back to the caller so main.c can telemeter the same
 * values over USART3 -- one data source, two sinks.
 */
#ifndef NV_LOGGER_H
#define NV_LOGGER_H

#include <stdint.h>

#include "nv_spec.h"

/* 1 = run the NV logger; 0 = leave the NV region untouched this boot (the
   telemetry loop then idles on RX polling only). */
#define NV_LOGGER  1

/* Record period: pick a preset from nv_spec.h, or any custom seconds value.
   NV_RATE_DEV_PERIOD_S (1 s) wraps the ring in ~4 min for bring-up only;
   NV_RATE_DEPLOY_PERIOD_S (45 s) is the ~3.5-year-endurance deployment default.
   The model trains at the deploy rate only -- the dev preset never produces
   training data, or the model trains on one distribution and infers on another. */
#define NV_LOGGER_PERIOD_S   NV_RATE_DEPLOY_PERIOD_S

/* Per-channel refresh cadence, in records: a channel's value is re-generated
   every Nth record and HELD in between, giving three interleaved byte
   periodicities for the spectral features to learn. */
#define NV_LOGGER_TEMP_EVERY   1u
#define NV_LOGGER_HUM_EVERY    2u
#define NV_LOGGER_PRESS_EVERY  4u

/* CAMPAIGN BUILDS ONLY: exercise the display-unit settings on a deterministic
   schedule so the training data carries journal change entries in realistic
   proportion (most pages J0-only, a few with 1-3 changes, the occasional full
   journal). The whole schedule is a pure function of (seed, lifetime record
   count) -- no clocks, no true randomness -- so it survives capture reboots
   and any capture's journal can be re-derived exactly afterwards. It plans at
   most 3 changes per page (the free slots after J0), so it can never hit the
   journal-full refusal by itself. The deploy build NEVER sets this. */
#define NV_SETTINGS_EXERCISE   0
/* Seed chosen so a fresh ring exercises the schedule early (page 0: two
   changes; page 3: a full journal) -- bench-verifiable in minutes at the dev
   rate, while the long-run distribution stays ~72/22/4/2 (checked over 100k
   pages). */
#define NV_EXERCISE_SEED       0x5EED000Fu

/* One logged reading: the NvRecord fields plus the lifetime record count
   (op_count after this record), which telemetry uses as its frame sequence. */
typedef struct
{
  uint32_t ts;      /* seconds since boot        */
  int32_t  temp;    /* degC x100                 */
  uint32_t hum;     /* %RH x100                  */
  uint32_t press;   /* hPa x100 (== Pa)          */
  uint32_t op;      /* lifetime records written  */
} NvReading;

/* Live display-unit settings -- the RAM shadow of the journal chain end.
   Records stay canonical no matter what these say: settings change what the
   device SAYS over telemetry, never what it STORES in flash. */
typedef struct
{
  uint8_t unit_temp;    /* NV_UNIT_TEMP_C / NV_UNIT_TEMP_F        */
  uint8_t unit_press;   /* NV_UNIT_PRESS_HPA / NV_UNIT_PRESS_INHG */
} NvSettings;

/* Recover ring state from the page headers (or wipe foreign/leftover bytes so
   dumps only ever contain spec-defined content) and the live settings from the
   newest page's journal chain, then start this boot's RAM counters and stats.
   Call once, before the main loop. */
void NvLogger_Init(void);

/* Rate-limited tick: when a record period has elapsed, generate readings, write
   the record (opening/erasing a page as needed), and fill *out. Returns 1 when
   a record was written this call, else 0. Call freely from the main loop. */
int NvLogger_Poll(NvReading *out);

/* The current display units (for the telemetry conversion). */
NvSettings NvLogger_Settings(void);

/* Toggle one display unit. The journal slot is programmed FIRST and RAM updated
   only on success, so RAM and journal can never disagree. Returns 0 on success,
   -1 refused (journal full until the next page rotation, no page open yet, or a
   flash fault) -- every refusal prints its own console note. */
int NvLogger_ToggleTempUnit(void);
int NvLogger_TogglePressUnit(void);

#endif /* NV_LOGGER_H */
