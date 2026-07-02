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
   NV_RATE_DEV_PERIOD_S (1 s) wraps the ring in ~4 min for bring-up and fast
   benign captures; NV_RATE_DEPLOY_PERIOD_S (90 s) is the ~7-year-endurance
   deployment default. The training set must cover every rate we claim. */
#define NV_LOGGER_PERIOD_S   NV_RATE_DEV_PERIOD_S

/* Per-channel refresh cadence, in records: a channel's value is re-generated
   every Nth record and HELD in between, giving three interleaved byte
   periodicities for the spectral features to learn. */
#define NV_LOGGER_TEMP_EVERY   1u
#define NV_LOGGER_HUM_EVERY    2u
#define NV_LOGGER_PRESS_EVERY  4u

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

/* Recover ring state from the page headers (or wipe foreign/leftover bytes so
   dumps only ever contain spec-defined content), then start this boot's RAM
   counters and stats. Call once, before the main loop. */
void NvLogger_Init(void);

/* Rate-limited tick: when a record period has elapsed, generate readings, write
   the record (opening/erasing a page as needed), and fill *out. Returns 1 when
   a record was written this call, else 0. Call freely from the main loop. */
int NvLogger_Poll(NvReading *out);

#endif /* NV_LOGGER_H */
