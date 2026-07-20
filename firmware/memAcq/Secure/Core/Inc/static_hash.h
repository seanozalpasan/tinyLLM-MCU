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

#include <stdint.h>

/* Golden digest slot: secure Bank-1 page 123 (2 KB), just below the NSC
   veneers at 0x0C03E000 and far above the Secure image's code (confirm the
   Secure build's FLASH usage stays under ~246 KB). Blank = not enrolled. */
#define STATIC_HASH_GOLDEN_ADDR   0x0C03D800UL
#define STATIC_HASH_DIGEST_LEN    32u

/* Boot-time entry: enroll if the golden slot is blank or B2 is held, else
   compute + compare and print the verdict. Call once, after USART1 is up.
   A boot-time mismatch latches StaticHash_Dirty, so the first scan tick
   withholds the watchdog kick: an unenrolled NS rebuild boot-loops until B2
   re-enrollment -- the deliberate human act, same as enrollment itself. */
void StaticHash_BootCheck(void);

/* Runtime re-check, reached from the NonSecure logger through the
   SECURE_StaticHash_PreWriteCheck veneer before EVERY record write:
   invalidate the ICACHE (a fresh implant must not hide behind a stale cached
   line), re-hash the static region, compare against the golden. Returns
   0 = clean, -1 = mismatch or hash failure -- the caller must skip its NV
   write on -1. A mismatch latches StaticHash_Dirty for the rest of the boot;
   enroll boots skip the compare (the just-programmed golden can read stale
   in the same boot) and report clean, mirroring the boot check's own rule. */
int StaticHash_RuntimeCheck(void);

/* The watchdog gate's view of Part-1 (read from the scan interrupt).
   Dirty = a mismatch was seen (latched). CheckCount = completed CLEAN
   runtime checks -- the liveness heartbeat the scan tick requires to have
   advanced between kicks. Single aligned-word reads, so the scan interrupt
   preempting a check in progress can never observe a torn value. */
int      StaticHash_Dirty(void);
uint32_t StaticHash_CheckCount(void);

#endif /* STATIC_HASH_H */
