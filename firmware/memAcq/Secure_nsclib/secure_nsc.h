/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    Secure_nsclib/secure_nsc.h
  * @author  MCD Application Team
  * @brief   Header for secure non-secure callable APIs list
  ******************************************************************************
    * @attention
  *
  * Copyright (c) 2023 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */

/* USER CODE BEGIN Non_Secure_CallLib_h */
/* Define to prevent recursive inclusion -------------------------------------*/
#ifndef SECURE_NSC_H
#define SECURE_NSC_H

/* Includes ------------------------------------------------------------------*/
#include <stdint.h>

/* Exported types ------------------------------------------------------------*/
/**
  * @brief  non-secure callback ID enumeration definition
  */
typedef enum
{
  SECURE_FAULT_CB_ID     = 0x00U, /*!< System secure fault callback ID */
  GTZC_ERROR_CB_ID       = 0x01U  /*!< GTZC secure error callback ID */
} SECURE_CallbackIDTypeDef;

/* Exported constants --------------------------------------------------------*/
/* Exported macro ------------------------------------------------------------*/
/* Exported functions ------------------------------------------------------- */
void SECURE_RegisterCallback(SECURE_CallbackIDTypeDef CallbackId, void *func);

void SECURE_print_Log(char* string);

/* Part-1 IDS pre-write gate: re-hash the static NS region against the golden
   digest. The logger MUST call this before every NV record write and skip the
   write unless it returns 0 -- no record is appended by an image that has not
   just re-proven its integrity. 0 = clean; nonzero = mismatch or check
   failure (the mismatch also latches the secure watchdog gate dirty, so the
   board resets within one scan period regardless of what the caller does). */
int SECURE_StaticHash_PreWriteCheck(void);
ErrorStatus SECURE_DMA_Fetch_NonSecure_Mem(uint32_t *nsc_mem_buffer, uint32_t Size, void *pCallback);
ErrorStatus SECURE_DATA_Last_Buffer_Compare(uint32_t* addr);
ErrorStatus SECURE_print_Buffer(uint32_t * buf, uint32_t size);
ErrorStatus SECURE_DMA_NonSecure_Mem_Transfer(uint32_t *src_buffer, uint32_t *dest_buffer, uint32_t size, void* func);

#endif /* SECURE_NSC_H */
/* USER CODE END Non_Secure_CallLib_h */

