/*
 * nv_model_parity.h -- parity gate (b) harness: rerun the exported model test
 * vectors on-chip and print the scores for laptop-side grading (Secure).
 *
 * The laptop half is offdevice/model/parity_check.py: the chip prints each
 * squared distance as a raw IEEE-754 bit pattern, and the checker compares
 * them against nv_model_testvec.h's float64 references at the header's own
 * baked-in tolerances. Agreement proves the firmware computes the same model
 * the laptop proved -- so the threshold and every off-device eval number
 * transfer to the chip unchanged.
 */
#ifndef NV_MODEL_PARITY_H
#define NV_MODEL_PARITY_H

/* 1 = parity build: boot scores the exported test vectors and prints
   [MDLPAR] lines for offdevice/model/parity_check.py. Links the ~58 KB model
   constants plus ~4.4 KB of vectors into the image -- never ship a deploy
   build with this on. */
#ifndef NV_MODEL_PARITY
#define NV_MODEL_PARITY 0
#endif

#if NV_MODEL_PARITY
/* Score every exported test vector and print the results for the laptop
   checker. Call once after USART1 is up. */
void NvModelParity_Print(void);
#endif

#endif /* NV_MODEL_PARITY_H */
