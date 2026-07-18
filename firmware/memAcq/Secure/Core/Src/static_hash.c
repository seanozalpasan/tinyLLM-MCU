/*
 * static_hash.c -- Part-1 IDS: static-region integrity hash (Secure).
 *
 * The two-part IDS's "easy half": the static CODE/.rodata never changes at
 * runtime (hardware-proven), so a cryptographic hash strictly dominates ML
 * there -- 1-bit sensitivity. SHA-256 over the proven MD5 path's block: same
 * HW peripheral, but no constructible-collision argument against the claim.
 * (flash_dump keeps MD5 -- there it is a transfer checksum, not a guarantee.)
 *
 * GOTCHA: an enroll boot never reads back or compares the just-programmed
 * golden (same-boot flash read-after-write can be stale through the cache);
 * it prints what it wrote, and comparison starts on the next boot.
 *
 * Runtime, the check re-runs before EVERY NV record write (the NonSecure
 * logger calls in through the SECURE_StaticHash_PreWriteCheck veneer): no
 * record is appended by an image that has not just re-proven its integrity.
 * The verdict feeds the secure watchdog gate two ways -- a mismatch latches
 * dirty (withhold every future kick), and each clean check advances a
 * heartbeat counter the scan tick requires to move between kicks, so a
 * workload that stops checking goes silent into a reset.
 */

#include "static_hash.h"

#include <stdio.h>
#include <string.h>

#include "main.h"
#include "nv_spec.h"   /* NV_NS_FLASH_BASE + NV_STATIC_SIZE: the hash ends where NV begins */

/* ===== runtime gate state (read by the secure scan tick) ===== */

/* Set on any enroll attempt: the golden was (re)programmed THIS boot, and a
   same-boot flash read can serve stale bytes, so runtime comparison is
   meaningless until the next boot -- the boot check's own rule. */
static int      s_enroll_boot;
static int      s_dirty;        /* latched: some check saw a mismatch */
static uint32_t s_clean_count;  /* completed clean runtime checks (heartbeat) */

/* ===== SHA-256 over the static NS range (HW HASH peripheral) ===== */

static int compute_sha256(uint8_t digest[STATIC_HASH_DIGEST_LEN])
{
  /* GOTCHA: HASH_HandleTypeDef has NO Instance member on the L5 (single block).
     DataType 8B makes the digest byte-for-byte standard SHA-256 (== hashlib),
     same as the proven MD5 setup. Secure code reads NS flash directly. */
  HASH_HandleTypeDef hhash = {0};
  __HAL_RCC_HASH_CLK_ENABLE();
  hhash.Init.DataType = HASH_DATATYPE_8B;
  if (HAL_HASH_Init(&hhash) != HAL_OK) { return -1; }
  if (HAL_HASHEx_SHA256_Start(&hhash, (uint8_t *)NV_NS_FLASH_BASE, NV_STATIC_SIZE,
                              digest, 5000u) != HAL_OK) { return -1; }
  return 0;
}

/* ===== golden slot I/O (secure Bank-1 flash, direct-register) ===== */

#define GOLDEN_PAGE      ((STATIC_HASH_GOLDEN_ADDR - 0x0C000000UL) / 0x800UL)   /* 123 */

#define SEC_SR_ERR_MASK  (FLASH_SECSR_SECPROGERR | FLASH_SECSR_SECWRPERR | FLASH_SECSR_SECPGAERR | \
                          FLASH_SECSR_SECSIZERR  | FLASH_SECSR_SECPGSERR)
#define SEC_SR_CLR_MASK  (FLASH_SECSR_SECEOP | FLASH_SECSR_SECOPERR | SEC_SR_ERR_MASK)

static void Sec_Unlock(void)
{
  if ((FLASH->SECCR & FLASH_SECCR_SECLOCK) != 0u)
  {
    FLASH->SECKEYR = 0x45670123u;   /* architectural FLASH keys (RM0438) */
    FLASH->SECKEYR = 0xCDEF89ABu;
  }
}

static void Sec_Lock(void) { FLASH->SECCR |= FLASH_SECCR_SECLOCK; }

/* Erase the golden page. We execute from the same bank, so the CPU stalls on
   fetch until the erase completes (~a few ms) -- acceptable at boot. */
static int Sec_EraseGoldenPage(void)
{
  while ((FLASH->SECSR & FLASH_SECSR_SECBSY) != 0u) { }
  FLASH->SECSR = SEC_SR_CLR_MASK;                     /* write-1-to-clear stale flags */
  const uint32_t cr = FLASH_SECCR_SECPER | (GOLDEN_PAGE << FLASH_SECCR_SECPNB_Pos);  /* Bank 1: no SECBKER */
  FLASH->SECCR = cr;
  FLASH->SECCR = cr | FLASH_SECCR_SECSTRT;
  while ((FLASH->SECSR & FLASH_SECSR_SECBSY) != 0u) { }
  const int err = ((FLASH->SECSR & SEC_SR_ERR_MASK) != 0u) ? -1 : 0;
  FLASH->SECCR = 0u;
  return err;
}

static int Sec_ProgramDW(uint32_t addr, uint64_t value)
{
  while ((FLASH->SECSR & FLASH_SECSR_SECBSY) != 0u) { }
  FLASH->SECSR = SEC_SR_CLR_MASK;
  FLASH->SECCR = FLASH_SECCR_SECPG;
  *(volatile uint32_t *)(addr)      = (uint32_t)(value & 0xFFFFFFFFu);
  *(volatile uint32_t *)(addr + 4u) = (uint32_t)(value >> 32);
  while ((FLASH->SECSR & FLASH_SECSR_SECBSY) != 0u) { }
  const int err = ((FLASH->SECSR & SEC_SR_ERR_MASK) != 0u) ? -1 : 0;
  FLASH->SECCR = 0u;
  return err;
}

static int golden_blank(void)
{
  const uint8_t *g = (const uint8_t *)STATIC_HASH_GOLDEN_ADDR;
  for (uint32_t i = 0u; i < STATIC_HASH_DIGEST_LEN; i++)
  {
    if (g[i] != 0xFFu) { return 0; }
  }
  return 1;
}

static int write_golden(const uint8_t digest[STATIC_HASH_DIGEST_LEN])
{
  uint64_t dw[STATIC_HASH_DIGEST_LEN / 8u];
  memcpy(dw, digest, sizeof(dw));
  Sec_Unlock();
  int err = Sec_EraseGoldenPage();
  for (uint32_t i = 0u; (i < STATIC_HASH_DIGEST_LEN / 8u) && (err == 0); i++)
  {
    err = Sec_ProgramDW(STATIC_HASH_GOLDEN_ADDR + 8u * i, dw[i]);
  }
  Sec_Lock();
  return err;
}

/* ===== enrollment trigger: the USER button (B2, PC13, active HIGH) ===== */

/* Pressed = 1 on this board (UM2617: B2/WKUP2 on PC13). Secure code reads the
   pin directly -- no NSEC grant needed. The boot line prints the raw level so
   a polarity surprise would be visible immediately. */
static int button_held(void)
{
  GPIO_InitTypeDef g = {0};
  __HAL_RCC_GPIOC_CLK_ENABLE();
  g.Pin  = GPIO_PIN_13;
  g.Mode = GPIO_MODE_INPUT;
  g.Pull = GPIO_PULLDOWN;
  HAL_GPIO_Init(GPIOC, &g);
  return (HAL_GPIO_ReadPin(GPIOC, GPIO_PIN_13) == GPIO_PIN_SET) ? 1 : 0;
}

/* ===== boot entry ===== */

static void print_digest(const char *label, const uint8_t *d)
{
  printf("%s", label);
  for (uint32_t i = 0u; i < STATIC_HASH_DIGEST_LEN; i++) { printf("%02x", d[i]); }
  printf("\r\n");
}

void StaticHash_BootCheck(void)
{
  uint8_t digest[STATIC_HASH_DIGEST_LEN];
  const int held  = button_held();
  const int blank = golden_blank();

  if (compute_sha256(digest) != 0)
  {
    printf("[HASH] ERROR: SHA-256 compute failed\r\n");
    return;
  }

  if (blank || held)
  {
    /* From here the golden is (being) reprogrammed this boot: runtime
       comparison is off until the next boot whether the write works or not. */
    s_enroll_boot = 1;
    if (write_golden(digest) != 0)
    {
      printf("[HASH] ERROR: golden write failed\r\n");
      return;
    }
    printf("[HASH] ENROLLED (%s, B2=%d) -- comparison starts next boot\r\n",
           blank ? "golden was blank" : "B2 held", held);
    print_digest("[HASH] golden sha256=", digest);
    return;
  }

  if (memcmp(digest, (const uint8_t *)STATIC_HASH_GOLDEN_ADDR,
             STATIC_HASH_DIGEST_LEN) == 0)
  {
    print_digest("[HASH] static region OK: sha256=", digest);
  }
  else
  {
    s_dirty = 1;   /* the scan tick reads this: no kick until B2 re-enroll */
    printf("[HASH] *** MISMATCH *** -- NS static region changed without enrollment => ANOMALY\r\n");
    print_digest("[HASH]   computed=", digest);
    print_digest("[HASH]   golden  =", (const uint8_t *)STATIC_HASH_GOLDEN_ADDR);
  }
}

/* ===== runtime re-check (reached from the NonSecure logger, pre-write) ===== */

int StaticHash_RuntimeCheck(void)
{
  uint8_t digest[STATIC_HASH_DIGEST_LEN];

  if (s_enroll_boot)
  {
    /* The golden was programmed THIS boot and a same-boot read of it can be
       stale -- comparing would be noise either way. Count the heartbeat so an
       enroll boot survives the scan gate; comparison starts next boot. */
    s_clean_count++;
    return 0;
  }

  /* Invalidate before hashing, same law as the NV scan: a payload written
     into static flash this boot must not hide behind a stale cached line.
     GOTCHA: the scan interrupt also invalidates this cache; masking
     interrupts for the few microseconds of the invalidate keeps the two
     invalidations from interleaving inside one hardware operation (the wait
     loops share one done-flag, and the HAL tick that would time out a
     confused wait is suspended). */
  __disable_irq();
  const HAL_StatusTypeDef inv = HAL_ICACHE_Invalidate();
  __enable_irq();
  if (inv != HAL_OK)
  {
    printf("[HASH] runtime check: cache invalidate FAILED\r\n");
    return -1;   /* not evidence of tamper, but never a clean verdict */
  }

  if (compute_sha256(digest) != 0)
  {
    printf("[HASH] runtime check: SHA-256 compute FAILED\r\n");
    return -1;
  }

  if (memcmp(digest, (const uint8_t *)STATIC_HASH_GOLDEN_ADDR,
             STATIC_HASH_DIGEST_LEN) != 0)
  {
    s_dirty = 1;
    printf("[HASH] *** MISMATCH *** (pre-write check) -- static region changed => ANOMALY latched\r\n");
    return -1;
  }

  s_clean_count++;
  return 0;
}

int      StaticHash_Dirty(void)      { return s_dirty; }
uint32_t StaticHash_CheckCount(void) { return s_clean_count; }
