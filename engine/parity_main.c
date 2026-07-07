/*
 * parity_main.c -- host half of the model parity gate: runs the scorer over
 * the exported test vectors and compares against the float64 reference
 * distances baked in at export time. Week 3 reruns the same vectors on the
 * chip; agreement on both sides proves laptop and firmware compute the same
 * distance from the same bytes.
 *
 * Build + run from engine/ (any host C compiler, no libraries needed):
 *
 *     gcc -O2 -o nv_parity parity_main.c mahal_score.c
 *     .\nv_parity.exe
 *
 * PASS = every d2 within tolerance of its reference AND (when the export
 * carries a threshold) every verdict matching the export's float32 host
 * mirror. Exit code 0 on pass, 1 on any failure, 2 on a params/testvec
 * mismatch (headers from different exports).
 */
#include <math.h>
#include <stdio.h>

#include "mahal_score.h"
#include "nv_model_testvec.h"

int main(void)
{
    unsigned failures = 0U;

    if (mahal_model_dims() != NV_MODEL_TESTVEC_DIMS) {
        printf("PARITY FAIL: scorer dims %u != test-vector dims %u "
               "(params and testvec headers from different exports?)\n",
               mahal_model_dims(), (unsigned)NV_MODEL_TESTVEC_DIMS);
        return 2;
    }

    for (unsigned k = 0U; k < NV_MODEL_TESTVEC_COUNT; ++k) {
        const float  d2  = mahal_score_d2(nv_model_testvec_x[k]);
        const double ref = nv_model_testvec_d2_ref[k];
        const double err = fabs((double)d2 - ref);
        const double lim = NV_MODEL_TESTVEC_REL_TOL * fabs(ref)
                           + NV_MODEL_TESTVEC_ABS_TOL;
        const int d2_ok = (err <= lim);

#if NV_MODEL_TESTVEC_HAS_VERDICTS
        const int verdict = mahal_is_anomaly(d2);
        const int v_ok    = (verdict == nv_model_testvec_verdict[k]);
        const char *status = d2_ok ? (v_ok ? "ok" : "FAIL(verdict)") : "FAIL(d2)";

        printf("vec %2u  d2=%-15.9g ref=%-15.9g err=%-10.3g %-8s %s\n",
               k, (double)d2, ref, err,
               verdict ? "ANOMALY" : "benign", status);
        if (!(d2_ok && v_ok)) {
            failures++;
        }
#else
        printf("vec %2u  d2=%-15.9g ref=%-15.9g err=%-10.3g %s\n",
               k, (double)d2, ref, err, d2_ok ? "ok" : "FAIL(d2)");
        if (!d2_ok) {
            failures++;
        }
#endif
    }

    if (failures) {
        printf("PARITY FAIL: %u of %u vectors\n",
               failures, (unsigned)NV_MODEL_TESTVEC_COUNT);
        return 1;
    }
    printf("PARITY PASS: all %u vectors within tolerance\n",
           (unsigned)NV_MODEL_TESTVEC_COUNT);
    return 0;
}
