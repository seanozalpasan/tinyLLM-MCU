/*
 * nv_spec.h -- NV-region byte spec. GENERATED from offdevice/nv/spec.py -- DO NOT EDIT.
 *
 * Regenerate (repo root):  python -m offdevice.nv.gen_header
 *
 * The NonSecure logger writes this layout; the Python reader parses it
 * byte-for-byte. A hand edit here drifts the two apart -- edit spec.py.
 * Layout per 2 KB page (little-endian == the M33's native order):
 *
 *     [NvHeader 64 B][124 x NvRecord 16 B]
 *
 * The header is programmed once when a page is erased+reopened; records then
 * append into fixed slots (two 8 B flash doublewords each -- nothing in flash
 * is ever rewritten in place). An all-0xFF slot is blank. Newest page =
 * highest page_seq; the write head is the first blank slot of that page.
 */
#ifndef NV_SPEC_H
#define NV_SPEC_H

#include <stddef.h>
#include <stdint.h>

/* ===== region geometry (mirrors the locked TrustZone partition) ===== */
#define NV_NS_FLASH_BASE            0x08040000UL  /* NS Bank-2 base == flash_dump origin */
#define NV_REGION_BASE              0x0807F000UL  /* top two 2 KB pages (126/127) */
#define NV_REGION_SIZE              0x1000UL
#define NV_PAGE_SIZE                0x800UL
#define NV_NUM_PAGES                2U
#define NV_PAGE0_BASE               0x0807F000UL
#define NV_PAGE1_BASE               0x0807F800UL
#define NV_DUMP_OFFSET              0x3F000UL     /* region offset inside a 256 KB dump */
#define NV_STATIC_SIZE              0x3F000UL     /* Part-1 hash = [base, base + this) */
#define NV_FLASH_DOUBLEWORD         8U            /* program granularity; strides are multiples */
#define NV_ERASED_BYTE              0xFFU         /* un-programmed flash reads as this */

/* ===== layout ===== */
#define NV_SPEC_VERSION             1U
#define NV_HEADER_SIZE              64U
#define NV_HEADER_PAD_FILL          0x00U         /* trailing reserve programmed as zeros */
#define NV_RECORD_SIZE              16U
#define NV_RECORDS_PER_PAGE         124U
#define NV_RECORDS_TOTAL            248U

/* ===== value semantics: fixed-point integers, BME280 measurement ranges ===== */
#define NV_TEMP_SCALE               100           /* int32_t = degC x100 */
#define NV_TEMP_LO                  (-4000L)
#define NV_TEMP_HI                  8500L
#define NV_HUM_SCALE                100           /* uint32_t = %RH x100 */
#define NV_HUM_LO                   0UL
#define NV_HUM_HI                   10000UL
#define NV_PRESS_SCALE              100           /* uint32_t = hPa x100 == Pa */
#define NV_PRESS_LO                 30000UL
#define NV_PRESS_HI                 110000UL

/* ===== update-rate presets (period between records, seconds) ===== */
/* Flash ages by erase count (~10k cycles/page rated): 1 s wraps the ring in
   ~4 min for bring-up only; 45 s erases each page every ~3.1 h
   => ~3.5 years to the rated minimum. Training data = deploy rate only. */
#define NV_RATE_DEV_PERIOD_S        1U
#define NV_RATE_DEPLOY_PERIOD_S     45U

/* Programmed once per page-open; counters + stats are RAM-kept and snapshotted
   at that moment (flash cannot rewrite in place). page_seq: monotonic page-open
   counter, 1 on virgin flash -- doubles as the wrap counter, and the highest
   one marks the current page. boot_count: boots as of this page-open.
   op_count: records fully programmed before this page opened (lifetime total
   = op_count + the page's non-blank slots). Stats: per channel over all
   readings THIS boot, including the one that triggered the page-open;
   mean = 64-bit sum / count, truncated toward zero (C99 division). */
typedef struct
{
  uint16_t version;
  uint16_t reserved0;
  uint32_t page_seq;
  uint32_t boot_count;
  uint32_t op_count;
  int32_t  temp_min;
  int32_t  temp_max;
  int32_t  temp_mean;
  uint32_t hum_min;
  uint32_t hum_max;
  uint32_t hum_mean;
  uint32_t press_min;
  uint32_t press_max;
  uint32_t press_mean;
  uint8_t  reserved1[12];
} NvHeader;

typedef struct
{
  uint32_t ts;      /* seconds since boot */
  int32_t  temp;    /* degC x100 */
  uint32_t hum;     /* %RH x100 */
  uint32_t press;   /* hPa x100 == Pa */
} NvRecord;

/* Compile-time layout locks: if one fires, this header and spec.py have
   drifted -- regenerate, never patch here. GOTCHA: CubeIDE's editor parser
   (not GCC -- every build dialect accepts _Static_assert) flags it as a
   syntax error, so the indexer branch gets a plain C90 negative-array-size
   check instead: same hard compile failure, no phantom squiggles. */
#ifdef __CDT_PARSER__
#define NV_LAYOUT_LOCK(tag, cond)  typedef char nv_layout_lock_##tag[(cond) ? 1 : -1]
#else
#define NV_LAYOUT_LOCK(tag, cond)  _Static_assert(cond, "nv_spec drifted: " #tag)
#endif

NV_LAYOUT_LOCK(header_size,           sizeof(NvHeader) == NV_HEADER_SIZE);
NV_LAYOUT_LOCK(record_size,           sizeof(NvRecord) == NV_RECORD_SIZE);
NV_LAYOUT_LOCK(header_off_page_seq,   offsetof(NvHeader, page_seq) == 4U);
NV_LAYOUT_LOCK(header_off_temp_min,   offsetof(NvHeader, temp_min) == 16U);
NV_LAYOUT_LOCK(record_off_temp,       offsetof(NvRecord, temp) == 4U);
NV_LAYOUT_LOCK(record_dw_aligned,     (NV_RECORD_SIZE % NV_FLASH_DOUBLEWORD) == 0U);
NV_LAYOUT_LOCK(page_fill_exact,       NV_HEADER_SIZE + NV_RECORDS_PER_PAGE * NV_RECORD_SIZE == NV_PAGE_SIZE);

#endif /* NV_SPEC_H */
