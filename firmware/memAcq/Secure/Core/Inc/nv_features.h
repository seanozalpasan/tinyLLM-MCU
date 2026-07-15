/*
 * nv_features.h -- Part-2 IDS: on-chip feature extraction over the NV region (Secure).
 *
 * Mirrors the frozen off-device pipeline exactly (params in
 * offdevice/features/params.py; the op-for-op blueprint is
 * offdevice/features/tables.py, extract_features_ref): 4096 bytes -> 33
 * windowed 512-point real FFTs -> ONE shared mel bank -> MFCC / mel / chroma
 * -> the 120-float vector the Mahalanobis scorer consumes, feature-major
 * ([40 mfcc][40 mel][40 chroma]). Every table and constant comes from the
 * GENERATED nv_feat_tables.h; the only library dependency is CMSIS-DSP's
 * real FFT (prebuilt libarm_ARMv8MMLldfsp_math.a).
 */
#ifndef NV_FEATURES_H
#define NV_FEATURES_H

#include <stdint.h>

/* 1 = parity build: boot prints the golden fixture's 120 outputs as raw
   IEEE-754 bit patterns ([NVFPAR] lines), and the laptop-side
   offdevice/features/parity_check.py compares them to the committed golden
   at the pre-agreed tolerance. Embeds 4 KB of fixture bytes -- never ship a
   deploy build with this on. */
#ifndef NV_FEAT_PARITY
#define NV_FEAT_PARITY 0
#endif

/* The model's input dimension; nv_features.c compile-time-asserts this equals
   the generated tables' NVF_N_DIMS (the tables header holds static arrays and
   is included by nv_features.c ONLY, so the dimension is re-stated here). */
#define NV_FEAT_DIMS 120u

/* THE scan entry point: invalidates the ICACHE FIRST -- the logger programs
   NV flash in this same boot, and an L5 flash read-after-write can serve a
   stale cached line, which here would mean scoring the innocent pre-attack
   bytes and missing a fresh implant -- then extracts features from the live
   NV region. Returns 0 on success, -2 if the cache invalidate failed (treat
   as a failed scan, never as benign). */
int NvFeatures_ScanRegion(float out[NV_FEAT_DIMS]);

/* The math only, over any 4096-byte buffer. NO cache handling -- scanning
   real flash goes through NvFeatures_ScanRegion. Always returns 0 (kept as
   an int so both entry points share one check-nonzero contract). */
int NvFeatures_ExtractBuffer(const uint8_t *bytes, float out[NV_FEAT_DIMS]);

#if NV_FEAT_PARITY
/* Feed the embedded golden fixture through the chain and print the results
   for parity_check.py. Call once after USART1 is up. */
void NvFeatures_ParityPrint(void);
#endif

#endif /* NV_FEATURES_H */
