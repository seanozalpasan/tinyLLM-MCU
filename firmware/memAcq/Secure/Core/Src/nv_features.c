/*
 * nv_features.c -- Part-2 IDS: the on-chip feature chain (Secure).
 *
 * Op-for-op float32 mirror of offdevice/features/tables.py
 * extract_features_ref() -- that Python reference is the parity contract;
 * change neither side without the other and without re-running parity gate
 * (a). The dataflow per 4 KB window:
 *
 *   33 frames of 512 samples (hop 128, centered: zeros outside the window)
 *   -> Hann -> real FFT -> power (257 bins)
 *   -> mel bank        -> 40x33 matrix -> mel feature (row means)
 *                                      -> dB (floor, GLOBAL-max clamp) -> DCT
 *                                         -> 40 MFCCs
 *   -> chroma bank     -> per-frame peak-normalize -> 40 chroma means
 *
 * Working set: ~10.3 KB of static buffers (sized by the frozen contract).
 * The mel matrix must exist in full before MFCC's dB step because the clamp
 * is relative to the max over ALL 40x33 values, unknown until the last frame.
 */
#include <float.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "main.h"            /* HAL_ICACHE_Invalidate */
#include "nv_features.h"
#include "nv_spec.h"         /* NV_REGION_BASE + the NV_LAYOUT_LOCK assert helper */
#include "ids_scan.h"          /* IDS_LATENCY switch */
#if IDS_LATENCY
#include "dwt_cycles.h"        /* DWT cycle counter for the invalidate/extract split */
#endif
#include "arm_math.h"          /* arm_rfft_fast_f32 (prebuilt CMSIS-DSP) */
#include "arm_common_tables.h" /* the 512/256-point FFT tables the manual init references */
#include "nv_feat_tables.h"    /* GENERATED window/mel/DCT/chroma + constants */

/* The contract seams, checked at compile time: the API's dimension matches
   the generated tables', the feature window IS the NV region, and the FFT
   length is the 512 the manual instance fill below hard-codes tables for. */
NV_LAYOUT_LOCK(feat_dims, NV_FEAT_DIMS == NVF_N_DIMS);
NV_LAYOUT_LOCK(feat_window, NVF_WINDOW_BYTES == NV_REGION_SIZE);
NV_LAYOUT_LOCK(feat_fft_len, NVF_N_FFT == 512U);

/* ===== working buffers (static .bss -- ~10.3 KB, no heap, no big stack) ===== */

static float frame_buf[NVF_N_FFT];               /* windowed frame; the RFFT scratches it */
static float fft_out[NVF_N_FFT];                 /* packed spectrum [DC, Nyq, re1, im1, ...] */
static float power[NVF_N_SPEC_BINS];
static float mel_pf[NVF_N_MELS][NVF_N_FRAMES];   /* mel power per frame; becomes dB in place */
static float chroma_sum[NVF_N_CHROMA];

/* ===== the chain ===== */

int NvFeatures_ExtractBuffer(const uint8_t *bytes, float out[NV_FEAT_DIMS])
{
  /* 512-point RFFT instance, filled by hand instead of calling
     arm_rfft_fast_init_f32: that dispatcher takes the length at runtime, so
     its code references the twiddle + bit-reversal tables of EVERY size the
     library supports (32..4096) and the linker keeps ~78 KB of flash for the
     one size used here. Assigning the members ourselves references only the
     512/256-point tables (~5 KB); the compute path (arm_rfft_fast_f32) and
     its arithmetic are untouched. Values mirror the library's own
     arm_rfft_512_fast_init_f32 verbatim. The fill cannot fail, so the old
     -1 (init-failure) return is retired; 0 is the only return value. */
  arm_rfft_fast_instance_f32 rfft;
  rfft.Sint.fftLen       = 256U;
  rfft.Sint.pTwiddle     = twiddleCoef_256;
  rfft.Sint.pBitRevTable = armBitRevIndexTable256;
  rfft.Sint.bitRevLength = ARMBITREVINDEXTABLE_256_TABLE_LENGTH;
  rfft.fftLenRFFT        = NVF_N_FFT;
  rfft.pTwiddleRFFT      = twiddleCoef_rfft_512;

  memset(chroma_sum, 0, sizeof chroma_sum);

  for (uint32_t t = 0u; t < NVF_N_FRAMES; t++)
  {
    /* Windowed frame straight from the bytes: one byte = one sample, as
       0..255 scaled by 1/32768 (the off-device int16 widening is numerically
       a no-op for 0..255), zero outside the window (librosa center=True,
       zero padding), times Hann. Rebuilt every frame -- the RFFT below uses
       its input as scratch. */
    int32_t start = (int32_t)(t * NVF_HOP) - (int32_t)NVF_PAD;
    for (uint32_t i = 0u; i < NVF_N_FFT; i++)
    {
      int32_t s = start + (int32_t)i;
      float sample = (s < 0 || s >= (int32_t)NVF_WINDOW_BYTES)
                         ? 0.0f
                         : (float)bytes[s] * NVF_SIGNAL_SCALE;
      frame_buf[i] = sample * nvf_hann[i];
    }

    arm_rfft_fast_f32(&rfft, frame_buf, fft_out, 0);

    /* Packed output -> power (re^2 + im^2): fft_out[0] is the DC real,
       fft_out[1] the Nyquist real (both imaginaries are zero by symmetry),
       then re/im pairs for bins 1..255. Unscaled forward FFT == numpy's. */
    power[0] = fft_out[0] * fft_out[0];
    power[NVF_N_SPEC_BINS - 1u] = fft_out[1] * fft_out[1];
    for (uint32_t k = 1u; k < NVF_N_SPEC_BINS - 1u; k++)
    {
      float re = fft_out[2u * k];
      float im = fft_out[2u * k + 1u];
      power[k] = re * re + im * im;
    }

    for (uint32_t m = 0u; m < NVF_N_MELS; m++)
    {
      float acc = 0.0f;
      for (uint32_t b = 0u; b < NVF_N_SPEC_BINS; b++)
        acc += nvf_mel_bank[m][b] * power[b];
      mel_pf[m][t] = acc;
    }

    /* Chroma folds in per frame -- its normalization is per-frame (each
       frame divided by its own peak; values are non-negative), so it cannot
       wait for the end like MFCC's global clamp can. */
    float cvec[NVF_N_CHROMA];
    float peak = 0.0f;
    for (uint32_t c = 0u; c < NVF_N_CHROMA; c++)
    {
      float acc = 0.0f;
      for (uint32_t b = 0u; b < NVF_N_SPEC_BINS; b++)
        acc += nvf_chroma_bank[c][b] * power[b];
      cvec[c] = acc;
      if (acc > peak)
        peak = acc;
    }
    float div = (peak < NVF_NORM_TINY) ? 1.0f : peak;  /* librosa's tiny guard */
    for (uint32_t c = 0u; c < NVF_N_CHROMA; c++)
      chroma_sum[c] += cvec[c] / div;
  }

  /* mel feature first -- the same matrix flips to dB in place right after. */
  for (uint32_t m = 0u; m < NVF_N_MELS; m++)
  {
    float acc = 0.0f;
    for (uint32_t t = 0u; t < NVF_N_FRAMES; t++)
      acc += mel_pf[m][t];
    out[NVF_N_MFCC + m] = acc / (float)NVF_N_FRAMES;
  }

  /* dB in place, tracking the max. GOTCHA: the TOP_DB clamp is relative to
     the max over the WHOLE matrix -- nothing may be clamped until every
     frame's dB exists. */
  float db_max = -FLT_MAX;
  for (uint32_t m = 0u; m < NVF_N_MELS; m++)
    for (uint32_t t = 0u; t < NVF_N_FRAMES; t++)
    {
      float v = 10.0f * log10f(fmaxf(NVF_AMIN, mel_pf[m][t]));
      mel_pf[m][t] = v;
      if (v > db_max)
        db_max = v;
    }

  /* MFCC = DCT of the time-averaged clamped dB. The DCT is linear, so
     transforming the mean equals the reference's mean-of-per-frame-DCTs
     exactly in real arithmetic; the float32 difference sits far inside the
     parity tolerance and this order needs no 40x33 MFCC matrix. */
  float db_floor = db_max - NVF_TOP_DB;
  float db_mean[NVF_N_MELS];
  for (uint32_t m = 0u; m < NVF_N_MELS; m++)
  {
    float acc = 0.0f;
    for (uint32_t t = 0u; t < NVF_N_FRAMES; t++)
      acc += fmaxf(mel_pf[m][t], db_floor);
    db_mean[m] = acc / (float)NVF_N_FRAMES;
  }
  for (uint32_t k = 0u; k < NVF_N_MFCC; k++)
  {
    float acc = 0.0f;
    for (uint32_t n = 0u; n < NVF_N_MELS; n++)
      acc += nvf_dct[k][n] * db_mean[n];
    out[k] = acc;
  }

  for (uint32_t c = 0u; c < NVF_N_CHROMA; c++)
    out[NVF_N_MFCC + NVF_N_MELS + c] = chroma_sum[c] / (float)NVF_N_FRAMES;

  return 0;
}

#if IDS_LATENCY
/* Sub-timings of the last ScanRegion (cycles): the invalidate, then the
   feature extraction. Written every scan under a latency build; read by the
   scan tick right after, through NvFeatures_LastLatency. */
static uint32_t s_last_inv_cyc;
static uint32_t s_last_feat_cyc;

void NvFeatures_LastLatency(uint32_t *inv_cyc, uint32_t *feat_cyc)
{
  *inv_cyc  = s_last_inv_cyc;
  *feat_cyc = s_last_feat_cyc;
}
#endif

int NvFeatures_ScanRegion(float out[NV_FEAT_DIMS])
{
  /* HARD REQUIREMENT -- invalidate BEFORE every scan, no exceptions. The
     logger programs NV flash in this same boot, and an L5 read-after-write
     can serve a stale cached line ('volatile' does not bypass a hardware
     cache). A stale image here is the known gotcha weaponized: the scan
     scores the innocent pre-attack bytes and a fresh implant hides. An
     invalidate failure is therefore a FAILED scan, never a benign skip. */
#if IDS_LATENCY
  const uint32_t lat_c0 = dwt_cycles_now();
#endif
  if (HAL_ICACHE_Invalidate() != HAL_OK)
    return -2;
#if IDS_LATENCY
  const uint32_t lat_c1 = dwt_cycles_now();
#endif
  const int rc = NvFeatures_ExtractBuffer((const uint8_t *)NV_REGION_BASE, out);
#if IDS_LATENCY
  s_last_inv_cyc  = lat_c1 - lat_c0;
  s_last_feat_cyc = dwt_cycles_now() - lat_c1;
#endif
  return rc;
}

/* ===== parity harness (parity builds only) ===== */

#if NV_FEAT_PARITY

#include "nv_feat_fixture.h"   /* GENERATED: the golden vector's exact input */

NV_LAYOUT_LOCK(fixture_size, sizeof(nvf_fixture) == NVF_WINDOW_BYTES);

void NvFeatures_ParityPrint(void)
{
  float out[NV_FEAT_DIMS];
  printf("\r\n[NVFPAR] begin dims=%u\r\n", (unsigned)NV_FEAT_DIMS);
  if (NvFeatures_ExtractBuffer(nvf_fixture, out) != 0)
  {
    printf("[NVFPAR] EXTRACT FAILED\r\n[NVFPAR] end\r\n");
    return;
  }
  /* Raw IEEE-754 bit patterns: bit-exact transfer to the laptop, and no
     dependency on newlib-nano's (disabled-by-default) float printf. */
  for (uint32_t i = 0u; i < NV_FEAT_DIMS; i += 8u)
  {
    printf("[NVFPAR] %3u:", (unsigned)i);
    for (uint32_t j = i; (j < i + 8u) && (j < NV_FEAT_DIMS); j++)
    {
      uint32_t bits;
      memcpy(&bits, &out[j], sizeof bits);
      printf(" %08lX", (unsigned long)bits);
    }
    printf("\r\n");
  }
  printf("[NVFPAR] end\r\n");
}

#endif /* NV_FEAT_PARITY */
