/*
 * ospi_hash.c -- attestation for the OSPI XIP weight blob (Secure).
 *
 * Mirrors static_hash.c's compute_sha256 (same HW HASH peripheral, DataType 8B
 * => byte-for-byte standard SHA-256 == Python hashlib). 
 * The difference: the region hashed is the XIP-mapped external blob, and the golden digest 
 * is a compile-time constant (blob_export.py emits it), not an enrolled flash slot --
 * the internal image already protects it, so no separate golden write is needed.
 *
 * NOTE:
 *  - OSPI must be in memory-mapped read mode BEFORE this runs, or 0x90000000
 *    reads return garbage / bus-fault.
 *  - The MPCWM watermark must span OSPI_BLOB_LEN: a read past the watermark
 *    faults. MX_GTZC_S_Init sets 6 granules (768 KB); keep that >= OSPI_BLOB_LEN
 *    rounded up to the 128 KB granule.
 *  - Hashing hundreds of KB over OSPI is slower than internal flash; the timeout
 *    is generous. This runs once at boot.
 */

#include "ospi_hash.h"

#include <stdio.h>
#include <string.h>

#include "main.h"
#include "ospi_blob_attest.h"   /* OSPI_BLOB_ADDR, OSPI_BLOB_LEN, ospi_blob_sha256[32] */

#define OSPI_HASH_DIGEST_LEN  32u
#define OSPI_HASH_TIMEOUT_MS  20000u   /* generous: up to the 768 KB watermark window */

static int compute_sha256_ospi(uint8_t digest[OSPI_HASH_DIGEST_LEN])
{
  /* HASH_HandleTypeDef has NO Instance member on the L5 (single block).
     DataType 8B => standard SHA-256. Reads the XIP-mapped blob as plain loads. */
  HASH_HandleTypeDef hhash = {0};
  __HAL_RCC_HASH_CLK_ENABLE();
  hhash.Init.DataType = HASH_DATATYPE_8B;
  if (HAL_HASH_Init(&hhash) != HAL_OK) { return -1; }
  if (HAL_HASHEx_SHA256_Start(&hhash, (uint8_t *)OSPI_BLOB_ADDR, OSPI_BLOB_LEN,
                              digest, OSPI_HASH_TIMEOUT_MS) != HAL_OK) { return -1; }
  return 0;
}

static void print_digest(const char *label, const uint8_t *d)
{
  printf("%s", label);
  for (uint32_t i = 0u; i < OSPI_HASH_DIGEST_LEN; i++) { printf("%02x", d[i]); }
  printf("\r\n");
}

int OspiHash_BootCheck(void)
{
  uint8_t digest[OSPI_HASH_DIGEST_LEN];

  if (compute_sha256_ospi(digest) != 0)
  {
    printf("[OSPI-HASH] ERROR: SHA-256 over 0x%08lX failed "
           "(OSPI memory-mapped? MPCWM spans %lu B?)\r\n",
           (unsigned long)OSPI_BLOB_ADDR, (unsigned long)OSPI_BLOB_LEN);
    return -1;
  }

  if (memcmp(digest, ospi_blob_sha256, OSPI_HASH_DIGEST_LEN) == 0)
  {
    print_digest("[OSPI-HASH] weight blob OK: sha256=", digest);
    return 0;
  }

  printf("[OSPI-HASH] *** MISMATCH *** -- OSPI weights are not the shipped blob "
         "=> ANOMALY (refuse inference)\r\n");
  print_digest("[OSPI-HASH]   computed=", digest);
  print_digest("[OSPI-HASH]   expected=", ospi_blob_sha256);
  return -1;
}
