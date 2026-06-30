/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2023 STMicroelectronics.
  * All rights reserved.
  *
  * Modified by Karley W. for STM32L562E-DK; added support for UART logging
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include <string.h>
#include <stdio.h>   /* snprintf for the OSPI XIP report */
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* ---- Test-bed A: outbound telemetry frame (STM32 -> ESP32 over USART3) ---- */
#define TELE_MAGIC0     0xA5u
#define TELE_MAGIC1     0x5Au
#define TELE_FRAME_LEN  9u

/* ESP32 -> STM32 reverse-path test frame (proves 2-way UART; the direction the attack
   scenario will later use to feed corrupt data in): [0x5A 0xA5][cnt u32 LE][xor]. */
#define RXTEST_MAGIC0     0x5Au
#define RXTEST_MAGIC1     0xA5u
#define RXTEST_FRAME_LEN  7u

/* Slave-ready handshake from ESP32: PD11 (input). Gating is deferred -- the pin is read
   + printed but does NOT gate transmits yet; re-enable once the ESP32 drives it. */
#define TELE_HS_PORT    GPIOD
#define TELE_HS_PIN     GPIO_PIN_11

/* ns-flash_static_proof: 1 = run the NV-region churn proof at boot then idle; 0 = the
   normal OSPI/telemetry boot. The proof writes ONLY Bank-2 pages 126-127; flip back to 0
   to restore the original NonSecure app. */
#define NV_PROOF_DEMO   1

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
/* USER CODE BEGIN PV */

static uint8_t  tele_frame[TELE_FRAME_LEN];
static uint32_t tele_seq = 0;

/* sample payload for the secure-transfer demo */
uint32_t aSRC_Const_Buffer[32] =
{
  0x01020304, 0x05060708, 0x090A0B0C, 0x0D0E0F10,
  0x11121314, 0x15161718, 0x191A1B1C, 0x1D1E1F20,
  0x21222324, 0x25262728, 0x292A2B2C, 0x2D2E2F30,
  0x31323334, 0x35363738, 0x393A3B3C, 0x3D3E3F40,
  0x41424344, 0x45464748, 0x494A4B4C, 0x4D4E4F50,
  0x51525354, 0x55565758, 0x595A5B5C, 0x5D5E5F60,
  0x61626364, 0x65666768, 0x696A6B6C, 0x6D6E6F70,
  0x71727374, 0x75767778, 0x797A7B7C, 0x7D7E7F80
};
uint32_t NSC_Mem_Buffer[BUFFER_SIZE];   /* dump destination for the #if 0 acquisition block */

static __IO uint32_t transferCompleteDetected;
static __IO uint32_t transferErrorDetected;
/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
static void MX_GPIO_Init(void);
void SystemClock_Config(void);

/* USER CODE BEGIN PFP */
static void Tele_BuildFrame(uint8_t *buf, uint32_t seq, uint16_t value);
static void Uart3_Init(void);
static void Uart3_Write(const uint8_t *buf, uint32_t len);
static void Uart3_Poll(void);
static void NonSecureSecureTransferCompleteCallback(DMA_HandleTypeDef *hdma_memtomem_dma1_channelx);
static void NonSecureNonSecureTransferCompleteCallback(DMA_HandleTypeDef *hdma_memtomem_dma1_channelx);
#if NV_PROOF_DEMO
static void NvProof_Run(void);
#endif
/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{
  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();
  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  /* USER CODE BEGIN 2 */

#if NV_PROOF_DEMO
  /* NV-region churn proof: write only the two reserved NV pages, report over the secure
     veneer, then idle so the flash image is stable for an SWD capture. The OSPI/telemetry
     path below is intentionally skipped while the proof is active. */
  NvProof_Run();
  while (1) { __WFI(); }
#endif

  /* ---- Non-secure read-back of the OSPI XIP region ----
     The secure world already programmed the pattern, left OCTOSPI1 memory-mapped, and
     dropped a non-secure watermark over 0x90000000. This proves the path the inference
     engine will use: a plain non-secure load from external XIP flash. Reported via the
     secure UART veneer (USART1 is secure-attributed). */
  {
    volatile uint32_t *xip = (volatile uint32_t *)0x90000000UL;
    const uint32_t expect[4] = { 0xDEADBEEFUL, 0xCAFEBABEUL, 0x12345678UL, 0xA5A5A5A5UL };
    uint32_t got[4];
    char ospi_msg[128];
    int ospi_ok = 1;
    for (int i = 0; i < 4; i++)
    {
      got[i] = xip[i];
      if (got[i] != expect[i]) { ospi_ok = 0; }
    }
    snprintf(ospi_msg, sizeof(ospi_msg),
             "[NS] read @0x90000000: 0x%08lX 0x%08lX 0x%08lX 0x%08lX -> %s\r\n",
             (unsigned long)got[0], (unsigned long)got[1],
             (unsigned long)got[2], (unsigned long)got[3],
             ospi_ok ? "PASS" : "FAIL");
    SECURE_print_Log(ospi_msg);
  }

#if 0  /* Full memory-acquisition dump (NS flash -> secure via DMA veneers). Disabled for
          now to keep the OSPI XIP boot output readable; flip to #if 1 to re-enable. */
  /* Step 3 */
    /* Provide non-secure data to secure */
    /* through secure DMA channels via Non-Secure Callable secure service */

  int remainder = 0;
  uint32_t* current_address = (uint32_t*) NSEC_MEM_START;
  //while we haven't reached the end of non-secure memory and we have at least 1024 bytes (256 words) to transfer
  while((uint32_t) current_address <= NSEC_MEM_END && (NSEC_MEM_END - (uint32_t)current_address) +1 >= BUFFER_SIZE*4){
	  	//move 1024 bytes into the memory buffer
  	  	  transferCompleteDetected = 0;
  	  	  if(SECURE_DMA_NonSecure_Mem_Transfer(current_address,
  	  			  	  	  	  	  	  	  	  	  (uint32_t*)NSC_Mem_Buffer,
												  (uint32_t) BUFFER_SIZE,
												  (void *)NonSecureNonSecureTransferCompleteCallback) == ERROR)
  	  	  {
  	  		SECURE_print_Log("There was an error with non-secure to secure transfer.\n\r");
  	  		Error_Handler();
  	  	  }

  	  	while (transferCompleteDetected == 0);

  	  	//SECURE_print_Buffer(NSC_Mem_Buffer, BUFFER_SIZE);

	    //perform a transfer to the secure environment
	    /* Reset transferCompleteDetected to 0, it will be set to 1 if a transfer is correctly completed */
	    transferCompleteDetected = 0;
	    if (SECURE_DMA_Fetch_NonSecure_Mem((uint32_t *)NSC_Mem_Buffer,
	                                       BUFFER_SIZE,
	                                       (void *)NonSecureSecureTransferCompleteCallback) == ERROR)
	    {
	    	SECURE_print_Log("There was an error with non-secure to secure transfer.\n\r");
	    	Error_Handler();
	    }

	    /* Wait for notification completion */
	    while (transferCompleteDetected == 0);
	    //print out to screen
	    SECURE_DATA_Last_Buffer_Compare((uint32_t*)current_address);
	    //increment the address variable by 1024 bytes
	    current_address += BUFFER_SIZE;
  }
  //we incremented one too many before checking the while condition, so undo the last increment
  current_address -= BUFFER_SIZE;

  //check if there's anything left over
  if((NSEC_MEM_END - (uint32_t) current_address) + 1 > 0){
	  //how many words left over?
	  remainder = ((NSEC_MEM_END - (uint32_t)current_address) + 1)/4;
	  //clear out the buffer
	  for(int i = 0; i < BUFFER_SIZE; i++){
		  NSC_Mem_Buffer[i] = 0;
	  }
	  //put in the remainder
	  if(SECURE_DMA_NonSecure_Mem_Transfer(current_address,
										  (uint32_t*)NSC_Mem_Buffer,
										  (uint32_t) remainder,
										  (void *)NonSecureNonSecureTransferCompleteCallback) == ERROR)
	  {
		SECURE_print_Log("There was an error with non-secure to secure transfer.\n\r");
		Error_Handler();
	  }

  while (transferCompleteDetected == 0);

  //perform one last non-secure to secure transfer
	transferCompleteDetected = 0;
	if (SECURE_DMA_Fetch_NonSecure_Mem((uint32_t *)NSC_Mem_Buffer,
									   BUFFER_SIZE,
									   (void *)NonSecureSecureTransferCompleteCallback) == ERROR)
	{
		SECURE_print_Log("There was an error with non-secure to secure transfer.\n\r");
		Error_Handler();
	}

	/* Wait for notification completion */
	while (transferCompleteDetected == 0);

  }
#endif /* memory-acquisition dump (disabled) */

  /* ---- Test-bed A: UART telemetry (the working path). Route the STMod+ mux to mikroBUS
     mode (SEL_12=PF11=1, SEL_34=PF12=0) so USART3 PC10/PC11 reach the mikroBUS UART pads
     (UM2617 Table 30), then bring up USART3. ---- */
  __HAL_RCC_GPIOF_CLK_ENABLE();
  {
    GPIO_InitTypeDef gm = {0};
    gm.Mode = GPIO_MODE_OUTPUT_PP; gm.Pull = GPIO_NOPULL; gm.Speed = GPIO_SPEED_FREQ_LOW;
    gm.Pin = GPIO_PIN_11 | GPIO_PIN_12; HAL_GPIO_Init(GPIOF, &gm);
    HAL_GPIO_WritePin(GPIOF, GPIO_PIN_11, GPIO_PIN_SET);    /* SEL_12 = 1 -> mikroBUS */
    HAL_GPIO_WritePin(GPIOF, GPIO_PIN_12, GPIO_PIN_RESET);  /* SEL_34 = 0             */
  }
  Uart3_Init();
  SECURE_print_Log("[NS] Test-bed A: USART3 telemetry init (PC10 TX -> mikroBUS UART)\r\n");

  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    {
      /* Handshake deferred: transmit every cycle regardless of HS. The pin is still read
         + printed so we can watch it move once the ESP32 drives it; re-enable gating later. */
      GPIO_PinState hs = HAL_GPIO_ReadPin(TELE_HS_PORT, TELE_HS_PIN);
      uint16_t value   = (uint16_t)(1000u + (tele_seq % 50u));
      char msg[96];

      Tele_BuildFrame(tele_frame, tele_seq, value);
      Uart3_Write(tele_frame, TELE_FRAME_LEN);   /* 9-byte frame over USART3/PC10 */

      snprintf(msg, sizeof(msg), "[NS] HS=%d seq=%lu val=%u tx=OK\r\n",
               (hs == GPIO_PIN_SET) ? 1 : 0,
               (unsigned long)tele_seq, (unsigned)value);
      SECURE_print_Log(msg);
      tele_seq++;

      /* Spend the inter-frame second polling the ESP32->STM32 RX path (~20x) so the reverse
         channel is drained + logged each second -- proves 2-way UART. */
      for (int k = 0; k < 20; k++) { Uart3_Poll(); HAL_Delay(50u); }
    }
  }
  /* USER CODE END 3 */
}


/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure the main internal regulator output voltage
  */
  if (HAL_PWREx_ControlVoltageScaling(PWR_REGULATOR_VOLTAGE_SCALE0) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_MSI;
  RCC_OscInitStruct.MSIState = RCC_MSI_ON;
  RCC_OscInitStruct.MSICalibrationValue = RCC_MSICALIBRATION_DEFAULT;
  RCC_OscInitStruct.MSIClockRange = RCC_MSIRANGE_11;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_MSI;
  RCC_OscInitStruct.PLL.PLLM = 12;
  RCC_OscInitStruct.PLL.PLLN = 55;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV7;
  RCC_OscInitStruct.PLL.PLLQ = RCC_PLLQ_DIV2;
  RCC_OscInitStruct.PLL.PLLR = RCC_PLLR_DIV2;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_5) != HAL_OK)
  {
    Error_Handler();
  }
}




/**
  * @brief GPIO Initialization Function
  * @param None
  * @retval None
  */
static void MX_GPIO_Init(void)
{
/* USER CODE BEGIN MX_GPIO_Init_1 */
/* USER CODE END MX_GPIO_Init_1 */

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOC_CLK_ENABLE();

/* USER CODE BEGIN MX_GPIO_Init_2 */
/* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */


/**
  * @brief  DMA non-secure to secure transfer complete callback
  * @note   This function is executed when the transfer complete interrupt
  *         is generated
  * @retval None
  */
static void NonSecureSecureTransferCompleteCallback(DMA_HandleTypeDef *hdma_memtomem_dma1_channelx)
{
  transferCompleteDetected = 1;
}


/**
  * @brief  DMA non-secure to secure transfer complete callback
  * @note   This function is executed when the transfer complete interrupt
  *         is generated
  * @retval None
  */
static void NonSecureNonSecureTransferCompleteCallback(DMA_HandleTypeDef *hdma_memtomem_dma1_channelx)
{
  transferCompleteDetected = 1;
}


/* USER CODE BEGIN 4 */

/**
  * @brief  Build a 9-byte dummy telemetry frame:
  *         [0]=0xA5 [1]=0x5A [2..5]=seq (LE) [6..7]=value (LE) [8]=XOR checksum.
  */
static void Tele_BuildFrame(uint8_t *buf, uint32_t seq, uint16_t value)
{
  buf[0] = TELE_MAGIC0;
  buf[1] = TELE_MAGIC1;
  buf[2] = (uint8_t)(seq         & 0xFFu);
  buf[3] = (uint8_t)((seq >> 8)  & 0xFFu);
  buf[4] = (uint8_t)((seq >> 16) & 0xFFu);
  buf[5] = (uint8_t)((seq >> 24) & 0xFFu);
  buf[6] = (uint8_t)(value        & 0xFFu);
  buf[7] = (uint8_t)((value >> 8) & 0xFFu);
  buf[8] = (uint8_t)(buf[0] ^ buf[1] ^ buf[2] ^ buf[3] ^
                     buf[4] ^ buf[5] ^ buf[6] ^ buf[7]);
}

/**
  * @brief  Bring up USART3 on PC10 TX / PC11 RX (AF7) for Test-bed A telemetry over the
  *         mikroBUS UART. 115200 8N1, polled, direct-register (no HAL UART module needed).
  *         GOTCHA: the STMod+ mux must already be in mikroBUS mode (PF11=1, PF12=0) for
  *         PC10/PC11 to reach the mikroBUS pads.
  */
static void Uart3_Init(void)
{
  GPIO_InitTypeDef g = {0};

  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_USART3_CLK_ENABLE();

  /* PC10 = USART3_TX, PC11 = USART3_RX (both AF7); the mux mikroBUS mode routes both. */
  g.Pin       = GPIO_PIN_10 | GPIO_PIN_11;
  g.Mode      = GPIO_MODE_AF_PP;
  g.Pull      = GPIO_NOPULL;
  g.Speed     = GPIO_SPEED_FREQ_HIGH;
  g.Alternate = GPIO_AF7_USART3;
  HAL_GPIO_Init(GPIOC, &g);

  USART3->CR1 = 0u;                                  /* disable while configuring  */
  USART3->BRR = HAL_RCC_GetPCLK1Freq() / 115200u;    /* 16x oversampling, 115200   */
  /* FIFOEN: the 8-byte RX FIFO buffers a whole multi-byte burst between our ~50ms polls,
     so a 7-byte frame isn't truncated to its first byte (1-deep RDR would overrun). */
  USART3->CR1 = USART_CR1_TE | USART_CR1_RE | USART_CR1_FIFOEN;   /* TX + RX + FIFO, 8N1 */
  USART3->CR1 |= USART_CR1_UE;                       /* enable USART               */
}

/**
  * @brief  Blocking, polled USART3 transmit of len bytes.
  */
static void Uart3_Write(const uint8_t *buf, uint32_t len)
{
  for (uint32_t i = 0u; i < len; i++)
  {
    while ((USART3->ISR & USART_ISR_TXE_TXFNF) == 0u) { }  /* wait TX reg free */
    USART3->TDR = buf[i];
  }
  while ((USART3->ISR & USART_ISR_TC) == 0u) { }           /* wait last bit out */
}

/**
  * @brief  Non-blocking poll of the USART3 RX path (ESP32 -> STM32). Drains the RX register
  *         into a small buffer, scans for the ESP32 test frame [0x5A 0xA5 | cnt u32 LE | xor],
  *         and logs "RX ok: cnt=N" per valid frame. This is the reverse channel the attack
  *         scenario will later use to feed corrupt data into the device.
  */
static void Uart3_Poll(void)
{
  static uint8_t  rx_buf[32];
  static uint32_t rx_len   = 0u;
  static uint32_t rx_total = 0u;     /* DIAGNOSTIC: total raw bytes ever seen on PC11 */
  uint32_t got  = 0u;
  uint8_t  last = 0u;

  if ((USART3->ISR & USART_ISR_ORE) != 0u) { USART3->ICR = USART_ICR_ORECF; }  /* clear overrun */

  while ((USART3->ISR & USART_ISR_RXNE_RXFNE) != 0u)
  {
    const uint8_t b = (uint8_t)USART3->RDR;
    last = b; got++; rx_total++;
    if (rx_len >= sizeof(rx_buf))                  /* full w/o a frame: slide off oldest half */
    {
      memmove(rx_buf, rx_buf + sizeof(rx_buf) / 2u, sizeof(rx_buf) / 2u);
      rx_len = sizeof(rx_buf) / 2u;
    }
    rx_buf[rx_len++] = b;
  }

  /* DIAGNOSTIC: did ANY raw byte arrive on PC11 this poll? Separates "nothing reaches the
     RX pin" (physical/mux) from "bytes arrive but don't frame" (baud/format). */
  if (got > 0u)
  {
    char d[72];
    snprintf(d, sizeof(d), "[NS] RX raw: +%lu (total=%lu, last=0x%02X)\r\n",
             (unsigned long)got, (unsigned long)rx_total, (unsigned)last);
    SECURE_print_Log(d);
  }

  for (uint32_t i = 0u; i + RXTEST_FRAME_LEN <= rx_len; i++)
  {
    if (rx_buf[i] != RXTEST_MAGIC0 || rx_buf[i + 1] != RXTEST_MAGIC1) { continue; }
    uint8_t x = 0u;
    for (uint32_t k = 0u; k < RXTEST_FRAME_LEN - 1u; k++) { x ^= rx_buf[i + k]; }
    if (x != rx_buf[i + RXTEST_FRAME_LEN - 1u]) { continue; }

    const uint32_t cnt = (uint32_t)rx_buf[i + 2] | ((uint32_t)rx_buf[i + 3] << 8) |
                         ((uint32_t)rx_buf[i + 4] << 16) | ((uint32_t)rx_buf[i + 5] << 24);
    char m[64];
    snprintf(m, sizeof(m), "[NS] RX ok: from ESP32 cnt=%lu\r\n", (unsigned long)cnt);
    SECURE_print_Log(m);

    const uint32_t consumed = i + RXTEST_FRAME_LEN;
    const uint32_t remain   = rx_len - consumed;
    if (remain > 0u) { memmove(rx_buf, rx_buf + consumed, remain); }
    rx_len = remain;
    return;  /* one frame per poll is plenty at 1 Hz */
  }
}

#if NV_PROOF_DEMO
/* ===== NV-region churn proof =================================================
   Splits NS Bank-2 flash into a static, hashable CODE/.rodata region and one small
   mutable NV region (the top two 2 KB pages). A hand-rolled append-log writes ONLY
   those pages: page 126 = boot counter (bumped every boot), page 127 = settings
   (a setpoint appended every Nth boot). An SWD dump + the host analyze.py then show
   the CODE region is byte-identical across boots while 100% of changed bytes fall in NV.

   In-place overwrite is impossible here: each 64-bit doubleword has ECC fixed at program
   time, so a programmed doubleword cannot be rewritten without erasing its whole 2 KB
   page -- which is exactly why real NV drivers append + garbage-collect instead. */

/* Top two pages of Bank 2 (0x08040000..0x0807FFFF); a Bank-2 dump's NV_OFFSET is 0x3F000.
   Code/.rodata is only a few KB above the base, far below 0x0807F000, so these pages stay
   erased (0xFF) until the log writes them. */
#define NV_BOOTCNT_ADDR     0x0807F000UL
#define NV_SETTINGS_ADDR    0x0807F800UL
#define NV_BANK2_BASE       0x08040000UL
#define NV_PAGE_SIZE        0x800UL
#define NV_SLOTS_PER_PAGE   (NV_PAGE_SIZE / 8u)
#define NV_ERASED_DW        0xFFFFFFFFFFFFFFFFULL   /* an un-programmed doubleword reads as this */

#define NV_SETPOINT_BASE    20u
#define NV_SETPOINT_STEP    5u
#define NV_SETTINGS_EVERY_N 5u           /* a new setpoint is appended on every Nth boot */
#define NV_SETTINGS_MARKER  0x55AAu      /* tags a valid settings record (vs erased 0xFFFF) */

#define NV_SR_ERR_MASK  (FLASH_NSSR_NSPROGERR | FLASH_NSSR_NSWRPERR | FLASH_NSSR_NSPGAERR | \
                         FLASH_NSSR_NSSIZERR  | FLASH_NSSR_NSPGSERR)
#define NV_SR_CLR_MASK  (FLASH_NSSR_NSEOP | FLASH_NSSR_NSOPERR | NV_SR_ERR_MASK)

static void Nv_Unlock(void)
{
  /* NSKEYR unlock sequence; the constants are the architectural FLASH keys (RM0438). */
  if ((FLASH_NS->NSCR & FLASH_NSCR_NSLOCK) != 0u)
  {
    FLASH_NS->NSKEYR = 0x45670123u;
    FLASH_NS->NSKEYR = 0xCDEF89ABu;
  }
}

static void Nv_Lock(void) { FLASH_NS->NSCR |= FLASH_NSCR_NSLOCK; }

/* Erase one 2 KB Bank-2 page (addr = page base). BKER selects bank 2; PNB is the page
   index within the bank. Returns 0 on success. */
static int Nv_ErasePage(uint32_t addr)
{
  const uint32_t page = (addr - NV_BANK2_BASE) / NV_PAGE_SIZE;
  while ((FLASH_NS->NSSR & FLASH_NSSR_NSBSY) != 0u) { }
  FLASH_NS->NSSR = NV_SR_CLR_MASK;                         /* clear stale flags (write-1-to-clear) */
  const uint32_t cr = FLASH_NSCR_NSPER | FLASH_NSCR_NSBKER | (page << FLASH_NSCR_NSPNB_Pos);
  FLASH_NS->NSCR = cr;
  FLASH_NS->NSCR = cr | FLASH_NSCR_NSSTRT;
  while ((FLASH_NS->NSSR & FLASH_NSSR_NSBSY) != 0u) { }
  const int err = ((FLASH_NS->NSSR & NV_SR_ERR_MASK) != 0u) ? -1 : 0;
  FLASH_NS->NSCR = 0u;
  return err;
}

/* Program one 64-bit doubleword at addr (8-byte aligned, currently erased). The pair of
   32-bit stores forms the doubleword the controller commits with one ECC. */
static int Nv_ProgramDW(uint32_t addr, uint64_t value)
{
  while ((FLASH_NS->NSSR & FLASH_NSSR_NSBSY) != 0u) { }
  FLASH_NS->NSSR = NV_SR_CLR_MASK;
  FLASH_NS->NSCR = FLASH_NSCR_NSPG;
  *(volatile uint32_t *)(addr)      = (uint32_t)(value & 0xFFFFFFFFu);
  *(volatile uint32_t *)(addr + 4u) = (uint32_t)(value >> 32);
  while ((FLASH_NS->NSSR & FLASH_NSSR_NSBSY) != 0u) { }
  const int err = ((FLASH_NS->NSSR & NV_SR_ERR_MASK) != 0u) ? -1 : 0;
  FLASH_NS->NSCR = 0u;
  return err;
}

/* Append count+1 to the boot-counter log and return it; erase + restart when full (GC). */
static uint32_t Nv_BumpBootCount(void)
{
  volatile uint64_t *slot = (volatile uint64_t *)NV_BOOTCNT_ADDR;
  uint32_t count = 0u, write_idx = 0u;
  for (uint32_t i = 0u; i < NV_SLOTS_PER_PAGE; i++)
  {
    if (slot[i] == NV_ERASED_DW) { break; }   /* first erased slot ends the append log */
    count = (uint32_t)slot[i];
    write_idx = i + 1u;
  }
  Nv_Unlock();
  if (write_idx >= NV_SLOTS_PER_PAGE) { Nv_ErasePage(NV_BOOTCNT_ADDR); write_idx = 0u; }
  Nv_ProgramDW(NV_BOOTCNT_ADDR + write_idx * 8u, (uint64_t)(count + 1u));
  Nv_Lock();
  return count + 1u;
}

/* Settings record = [marker u16 | reserved u16 | value u32]; the last marked record wins. */
static uint32_t Nv_GetSetpoint(void)
{
  volatile uint64_t *slot = (volatile uint64_t *)NV_SETTINGS_ADDR;
  uint32_t setpoint = NV_SETPOINT_BASE;
  for (uint32_t i = 0u; i < NV_SLOTS_PER_PAGE; i++)
  {
    if (slot[i] == NV_ERASED_DW) { break; }
    if ((uint16_t)(slot[i] & 0xFFFFu) == NV_SETTINGS_MARKER) { setpoint = (uint32_t)(slot[i] >> 32); }
  }
  return setpoint;
}

static void Nv_AppendSetpoint(uint32_t value)
{
  volatile uint64_t *slot = (volatile uint64_t *)NV_SETTINGS_ADDR;
  uint32_t write_idx = 0u;
  while (write_idx < NV_SLOTS_PER_PAGE && slot[write_idx] != NV_ERASED_DW) { write_idx++; }
  Nv_Unlock();
  if (write_idx >= NV_SLOTS_PER_PAGE) { Nv_ErasePage(NV_SETTINGS_ADDR); write_idx = 0u; }
  Nv_ProgramDW(NV_SETTINGS_ADDR + write_idx * 8u,
               (uint64_t)NV_SETTINGS_MARKER | ((uint64_t)value << 32));
  Nv_Lock();
}

/* Bump the boot counter every boot (high-freq churn); append a new setpoint on every Nth
   boot (low-freq churn); report both over the secure UART veneer. Both writes land only in
   the two NV pages -- the rest of Bank 2 never changes. */
static void NvProof_Run(void)
{
  const uint32_t boot = Nv_BumpBootCount();
  uint32_t setpoint;

  /* GOTCHA: report the value we just wrote, not a read-back. An in-same-boot read of a
     freshly-programmed doubleword can return stale data through the flash read cache (the
     SWD dump bypasses the CPU, so it always shows the true record). On boots that don't
     write the settings page, reading the current record is correct. */
  if (*(volatile uint64_t *)NV_SETTINGS_ADDR == NV_ERASED_DW)
  {
    setpoint = NV_SETPOINT_BASE;             /* first boot: define the baseline setpoint */
    Nv_AppendSetpoint(setpoint);
  }
  else if ((boot % NV_SETTINGS_EVERY_N) == 0u)
  {
    setpoint = NV_SETPOINT_BASE + (boot / NV_SETTINGS_EVERY_N) * NV_SETPOINT_STEP;
    Nv_AppendSetpoint(setpoint);
  }
  else
  {
    setpoint = Nv_GetSetpoint();
  }

  char msg[128];
  snprintf(msg, sizeof(msg),
           "[NVPROOF] boot=%lu setpoint=%lu | wrote only NV pages 0x0807F000/0x0807F800 (dump offset 0x3F000)\r\n",
           (unsigned long)boot, (unsigned long)setpoint);
  SECURE_print_Log(msg);
}
#endif /* NV_PROOF_DEMO */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}

#ifdef  USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
