/* NS-flash dump over USART1 (raw 8N1 @ 921600) for dev-phase dataset capture.
   A secure routine reads the NonSecure flash image directly (SAU marks the range
   NS, so each load is a NS bus access -- no DMA, no veneer) and streams it framed
   [sentinel "MARSDMP1"][len u32 LE][payload][md5 16B]. The whole-dump MD5 (hardware
   HASH peripheral) is both the transfer-integrity check and the manifest fingerprint.
   GOTCHA: flash is static, so the NS image is byte-identical whether or not the
   workload runs -- dump builds capture it without running it. Toggling DUMP_NSFLASH
   changes only the secure binary; the NS image being dumped is untouched. */

#ifndef FLASH_DUMP_H
#define FLASH_DUMP_H

#include "main.h"

#define DUMP_NSFLASH        0                /* 1 = dump-capture build (no NS jump); 0 = normal boot */
#define NSFLASH_DUMP_START  0x08040000UL     /* NS internal flash, bank 2 (..0x0807FFFF) */
#define NSFLASH_DUMP_BYTES  0x00040000UL     /* 256 KB = 262144 bytes */
#define NSFLASH_DUMP_CHUNK  1024U            /* 1 KB UART tx granularity */
#define DUMP_VARIANT_TAG    "tbA-dumpfw-v1"  /* dump-firmware id (boot banner); the benign NS-build
                                                variant is tagged by the host capture script */

/* Stream one framed dump (sentinel/len/payload/md5) over the given UART. */
void Dump_NSFlash_ToUart(UART_HandleTypeDef *huart);

/* Init the HASH peripheral, then wait for a host 'D' on the UART and dump on demand.
   Loops so snapshots are repeatable; never returns. */
void Dump_NSFlash_Service(UART_HandleTypeDef *huart);

#endif /* FLASH_DUMP_H */
