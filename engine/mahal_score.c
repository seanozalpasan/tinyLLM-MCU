/*
 * mahal_score.c -- squared Mahalanobis distance over the exported model
 * constants: d2(x) = (x - mean)^T * precision * (x - mean), float32 throughout.
 *
 * THIS loop order is the parity contract: diff vector first, then one
 * sequential multiply-accumulate per matrix row, rows accumulated in order.
 * offdevice/model/export.py's score_d2_f32 mirrors it op-for-op, and the
 * exported test vectors carry its expected results. GOTCHA: a compiler may
 * fuse a*b+c into one rounding (FMA) where the mirror rounds twice -- a
 * few-ulp drift the test-vector tolerance absorbs; verdicts cannot flip on it
 * because every vector keeps >= 1% margin from the alarm line.
 *
 * nv_model_params.h is GENERATED (python -m offdevice.model.export) and is
 * included here and nowhere else -- its ~58 KB of static arrays must live in
 * exactly one translation unit.
 */
#include "mahal_score.h"

#include "nv_model_params.h"

float mahal_score_d2(const float x[])
{
    float diff[NV_MODEL_DIMS];
    float d2 = 0.0f;

    for (unsigned i = 0U; i < NV_MODEL_DIMS; ++i) {
        diff[i] = x[i] - nv_model_mean[i];
    }
    for (unsigned i = 0U; i < NV_MODEL_DIMS; ++i) {
        float acc = 0.0f;
        for (unsigned j = 0U; j < NV_MODEL_DIMS; ++j) {
            acc += nv_model_precision[i][j] * diff[j];
        }
        d2 += acc * diff[i];
    }
    return d2;
}

int mahal_is_anomaly(float d2)
{
#if NV_MODEL_HAS_THRESHOLD
    return d2 > NV_MODEL_THRESHOLD_SQ;
#else
    (void)d2;
    return 0;
#endif
}

unsigned mahal_model_dims(void)
{
    return NV_MODEL_DIMS;
}
