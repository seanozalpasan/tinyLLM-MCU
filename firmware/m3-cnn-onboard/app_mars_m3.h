#ifndef APP_MARS_M3_H
#define APP_MARS_M3_H

#ifdef __cplusplus
extern "C" {
#endif

/* M3 milestone: runs the m10 SVD r=32 MARS CNN (162,104 B, fits the 252 KB
   SHA-256-attested NS region) over the 8 embedded bit-exact-gate vectors from
   mars_m10_vectors.h, then reports PASS count, tensor arena high-water, and
   per-inference latency over SECURE_print_Log.

   Call once from NonSecure main.c's USER CODE, after peripheral init (needs
   the Secure console veneer already up). This is the CNN-quantization poster
   lane -- independent of the IDS demo path, see bringup-guide.md S6.5. */
void Mars_M3_Run(void);

#ifdef __cplusplus
}
#endif

#endif /* APP_MARS_M3_H */
