/* NS-flash dump over USART1 (raw 8N1 @ 921600) for dev-phase dataset capture.
   A secure routine reads the NonSecure flash image directly (SAU marks the range
   NS, so each load is a NS bus access -- no DMA, no veneer) and streams it framed
   [sentinel "MARSDMP1"][len u32 LE][payload][md5 16B]. The whole-dump MD5 (hardware
   HASH peripheral) is both the transfer-integrity check and the manifest fingerprint.
   Every dump is taken before the NS workload starts (mode 1 never jumps NS; mode 2
   dumps at boot, pre-jump), so the mutable NV ring is frozen during the transfer
   and a snapshot can never contain a torn, half-programmed record. Toggling
   DUMP_NSFLASH changes only the secure binary; the NS image being dumped is
   untouched. */

#ifndef FLASH_DUMP_H
#define FLASH_DUMP_H

#include "main.h"

/* 0 = normal boot (no dump path).
   1 = dump-service build: block waiting for host 'D's forever; no NS jump.
   2 = capture-window build: at each boot, serve at most one 'D'-triggered dump
       within DUMP_BOOT_WINDOW_MS, then boot normally -- lets the unattended
       collector (offdevice/data/collect.py) snapshot via reset + 'D' with no
       reflashing between captures. */
#define DUMP_NSFLASH        2
#define DUMP_BOOT_WINDOW_MS 2000U            /* mode-2 trigger window; the dump itself may exceed it */
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

/* Init the HASH peripheral, then serve at most ONE 'D'-triggered dump within
   DUMP_BOOT_WINDOW_MS and return so boot continues into the NS workload. */
void Dump_NSFlash_BootWindow(UART_HandleTypeDef *huart);

#endif /* FLASH_DUMP_H */
