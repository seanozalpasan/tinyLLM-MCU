/* See flash_dump.h. The MD5 is computed by the hardware HASH peripheral through the
   HAL (HAL_HASH_MD5_Start) -- no software crypto here. DataType 8B makes the digest
   byte-for-byte equal to standard MD5 (Python hashlib), so the on-chip fingerprint
   and a laptop-side recompute compare directly. */

#include "flash_dump.h"
#include <stdio.h>

#define DUMP_HASH_TIMEOUT_MS  1000U

static HASH_HandleTypeDef hhash;

/** @brief Enable the HASH clock and init it for byte-stream MD5. Called once. */
static void Dump_HashInit(void)
{
  __HAL_RCC_HASH_CLK_ENABLE();
  hhash.Init.DataType = HASH_DATATYPE_8B;   /* byte-swap -> standard (hashlib) MD5; single HASH block, no Instance field */
  if (HAL_HASH_Init(&hhash) != HAL_OK)
  {
    Error_Handler();
  }
}

/** @brief Blocking raw UART transmit (the dump's binary frames bypass printf). */
static void Dump_TxRaw(UART_HandleTypeDef *huart, const uint8_t *data, uint16_t len)
{
  if (HAL_UART_Transmit(huart, (uint8_t *)data, len, HAL_MAX_DELAY) != HAL_OK)
  {
    Error_Handler();
  }
}

void Dump_NSFlash_ToUart(UART_HandleTypeDef *huart)
{
  static const uint8_t sentinel[8] = { 'M', 'A', 'R', 'S', 'D', 'M', 'P', '1' };
  const uint8_t *flash = (const uint8_t *)NSFLASH_DUMP_START;
  const uint32_t len   = NSFLASH_DUMP_BYTES;
  uint8_t  digest[16] __attribute__((aligned(4)));   /* HAL writes the digest word-wise */
  uint8_t  hdr[4];
  uint32_t off, i;

  /* Hash the whole image up front (secure may read NS flash directly), then frame it. */
  if (HAL_HASH_MD5_Start(&hhash, (uint8_t *)flash, len, digest, DUMP_HASH_TIMEOUT_MS) != HAL_OK)
  {
    printf("[S ] DUMP error: MD5 failed.\r\n");
    return;
  }

  hdr[0] = (uint8_t)(len & 0xFFu);
  hdr[1] = (uint8_t)((len >> 8) & 0xFFu);
  hdr[2] = (uint8_t)((len >> 16) & 0xFFu);
  hdr[3] = (uint8_t)((len >> 24) & 0xFFu);

  /* ASCII status frames the binary -- the host scans for the sentinel -- so nothing
     prints between the sentinel and the digest. */
  Dump_TxRaw(huart, sentinel, sizeof(sentinel));
  Dump_TxRaw(huart, hdr, sizeof(hdr));
  for (off = 0u; off < len; off += NSFLASH_DUMP_CHUNK)
  {
    Dump_TxRaw(huart, flash + off, NSFLASH_DUMP_CHUNK);
  }
  Dump_TxRaw(huart, digest, sizeof(digest));

  printf("[S ] DUMP done: %lu bytes, md5=", (unsigned long)len);
  for (i = 0u; i < 16u; i++) { printf("%02x", digest[i]); }
  printf("\r\n");
}

void Dump_NSFlash_Service(UART_HandleTypeDef *huart)
{
  uint8_t cmd;

  Dump_HashInit();
  printf("\r\n[S ] NS-flash dump service ready (8N1 @ 921600), variant=%s.\r\n", DUMP_VARIANT_TAG);
  printf("[S ] send 'D' to dump 0x%08lX..0x%08lX (%lu bytes).\r\n",
         (unsigned long)NSFLASH_DUMP_START,
         (unsigned long)(NSFLASH_DUMP_START + NSFLASH_DUMP_BYTES - 1u),
         (unsigned long)NSFLASH_DUMP_BYTES);

  for (;;)
  {
    if (HAL_UART_Receive(huart, &cmd, 1u, HAL_MAX_DELAY) != HAL_OK) { continue; }
    if (cmd == 'D' || cmd == 'd') { Dump_NSFlash_ToUart(huart); }
  }
}

void Dump_NSFlash_BootWindow(UART_HandleTypeDef *huart)
{
  uint8_t  cmd;
  uint32_t start = HAL_GetTick();

  Dump_HashInit();
  printf("[S ] capture window: send 'D' within %lu ms for one NS-flash dump.\r\n",
         (unsigned long)DUMP_BOOT_WINDOW_MS);

  while ((HAL_GetTick() - start) < DUMP_BOOT_WINDOW_MS)
  {
    if (HAL_UART_Receive(huart, &cmd, 1u, 50u) != HAL_OK) { continue; }
    if (cmd == 'D' || cmd == 'd')
    {
      /* The NS workload hasn't started this boot, so the NV ring is frozen for
         the whole transfer; extra queued 'D's are dropped with the window. */
      Dump_NSFlash_ToUart(huart);
      break;
    }
  }
}
