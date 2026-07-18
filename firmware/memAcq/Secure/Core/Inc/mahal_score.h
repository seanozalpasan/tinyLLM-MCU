/*
 * mahal_score.h -- the one-class scorer's public face: squared Mahalanobis
 * distance of a flat feature vector against the exported benign model, plus
 * the threshold verdict. Lives in engine/ so the exact on-chip arithmetic is
 * host-compiled and parity-checked on the laptop first; the same .h/.c pair
 * moves into the firmware unchanged at the port (the arithmetic IS the
 * model+threshold contract -- host and chip must match).
 */
#ifndef MAHAL_SCORE_H
#define MAHAL_SCORE_H

/* Squared distance d^2 of x (length mahal_model_dims(), filled in the exported
   feature-major order). Verdicts compare d^2 against threshold^2 -- sqrt never
   runs anywhere. */
float mahal_score_d2(const float x[]);

/* 1 if d2 crosses the exported alarm line. Always 0 for a plumbing export
   (no threshold baked in). */
int mahal_is_anomaly(float d2);

/* The exported NV_MODEL_DIMS -- lets callers built without nv_model_params.h
   (the parity runner) verify their vectors match the model. */
unsigned mahal_model_dims(void);

#endif /* MAHAL_SCORE_H */
