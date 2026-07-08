/*
 * static_hash.h -- Part-1 IDS: static-region integrity hash (Secure).
 *
 * HW-SHA-256 of the never-changing NS CODE/.rodata (0x08040000..0x0807F000,
 * excluding the 4 KB NV pages the logger legitimately writes) compared to a
 * golden digest stored in SECURE flash, unreachable from the non-secure world.
 * Mismatch = the NS image changed without authorization => anomaly.
 *
 * Enrollment (deciding an image is trustworthy) cannot be fully automatic: a
 * device that re-enrolls whatever it finds would happily enroll malware's
 * image and never alarm. So: enroll automatically only when the golden slot is
 * blank (first provisioning), or when the USER button (B2) is held through a
 * reset -- the deliberate human act after a legitimate NonSecure rebuild.
 */
#ifndef STATIC_HASH_H
#define STATIC_HASH_H

/* Golden digest slot: secure Bank-1 page 123 (2 KB), just below the NSC
   veneers at 0x0C03E000 and far above the Secure image's code (confirm the
   Secure build's FLASH usage stays under ~246 KB). Blank = not enrolled. */
#define STATIC_HASH_GOLDEN_ADDR   0x0C03D800UL
#define STATIC_HASH_DIGEST_LEN    32u

/* Re-check cadence is a runtime knob decided after Week-3 latency numbers; the
   periodic path rides the watchdog integration. For now: boot-time check only. */
#define STATIC_HASH_PERIOD_S      0u

/* Boot-time entry: enroll if the golden slot is blank or B2 is held, else
   compute + compare and print the verdict. Call once, after USART1 is up. */
void StaticHash_BootCheck(void);

/* This is a re-check function: compute the hash of the static region and compare it with the golden digest.
   Returns: 1 = match, 0 = mismatch */
int StaticHash_Verify(void);

#endif /* STATIC_HASH_H */
