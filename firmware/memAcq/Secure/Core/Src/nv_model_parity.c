/*
 * nv_model_parity.c -- parity gate (b): the on-chip half (Secure).
 *
 * Runs the exact vectors nv_model_testvec.h ships through the ported scorer
 * and prints each d2 verbatim as a raw IEEE-754 bit pattern (bit-exact
 * transfer to the laptop, and newlib-nano's float printf is off anyway). The
 * chip never grades itself -- offdevice/model/parity_check.py owns the
 * comparison, mirroring how parity gate (a) was run. The chip's verdict
 * prints ARE evidence, not grading: the two threshold-bracketing vectors
 * (0.99x / 1.01x) can only land on opposite sides of the alarm line if
 * NV_MODEL_THRESHOLD_SQ compiled in correctly.
 */
#include "nv_model_parity.h"

#if NV_MODEL_PARITY

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "mahal_score.h"
#include "nv_model_testvec.h"

void NvModelParity_Print(void)
{
  printf("\r\n[MDLPAR] begin count=%u dims=%u verdicts=%u\r\n",
         (unsigned)NV_MODEL_TESTVEC_COUNT, (unsigned)NV_MODEL_TESTVEC_DIMS,
         (unsigned)NV_MODEL_TESTVEC_HAS_VERDICTS);
  if (mahal_model_dims() != NV_MODEL_TESTVEC_DIMS)
  {
    /* Params and testvec headers from different exports -- refuse to print
       scores that would grade one model against another's references. */
    printf("[MDLPAR] DIMS MISMATCH scorer=%u testvec=%u\r\n[MDLPAR] end\r\n",
           mahal_model_dims(), (unsigned)NV_MODEL_TESTVEC_DIMS);
    return;
  }
  for (uint32_t k = 0u; k < NV_MODEL_TESTVEC_COUNT; k++)
  {
    const float d2 = mahal_score_d2(nv_model_testvec_x[k]);
    uint32_t bits;
    memcpy(&bits, &d2, sizeof bits);
#if NV_MODEL_TESTVEC_HAS_VERDICTS
    printf("[MDLPAR] vec %u d2=%08lX verdict=%d\r\n",
           (unsigned)k, (unsigned long)bits, mahal_is_anomaly(d2));
#else
    printf("[MDLPAR] vec %u d2=%08lX\r\n", (unsigned)k, (unsigned long)bits);
#endif
  }
  printf("[MDLPAR] end\r\n");
}

#endif /* NV_MODEL_PARITY */
