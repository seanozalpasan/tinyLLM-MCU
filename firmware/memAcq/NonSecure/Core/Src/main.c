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

/* ---- Test-bed A: dummy outbound-telemetry frame (STM32 SPI3 master -> ESP32 slave) ---- */
#define TELE_MAGIC0     0xA5u
#define TELE_MAGIC1     0x5Au
#define TELE_FRAME_LEN  9u

/* ESP32 -> STM32 reverse-path test frame (proves 2-way UART; this is the direction the
   attack scenario will later use to feed corrupt data in): [0x5A 0xA5][cnt u32 LE][xor]. */
#define RXTEST_MAGIC0     0x5Au
#define RXTEST_MAGIC1     0xA5u
#define RXTEST_FRAME_LEN  7u

/* Diagnostic build switch: 1 = GPIO loopback test (drive the 3 SPI signal pins as
   plain outputs so the ESP32 can read which sockets are electrically live); 0 =
   normal SPI master path. Flip back to 0 to restore the real pipeline. */
#define TESTBEDA_MODE_LOOPBACK  0

/* Under TESTBEDA_MODE_LOOPBACK: 1 = REVERSE READBACK (STM32 configures the SPI pins as
   INPUTS and the ESP32 drives them, to test the PB5/MOSI route in the other direction);
   0 = the SPI-MUX SWEEP (STM32 drives, ESP32 reads). Pair with ESP32_REVERSE_DRIVE. */
#define TESTBEDA_REVERSE_READBACK  1

/* Test-bed A transport: 1 = UART telemetry on USART3/PC10 (the verified working route --
   on this board the mikroBUS socket's data path is a UART, and SPI MOSI/PB5 is not
   reachable there, UM2617 Table 30); 0 = the legacy SPI3 path. Requires the Secure side
   to grant PC10 NSEC and the STMod+ mux in mikroBUS/UART mode (PF11=1, PF12=0). */
#define TESTBEDA_USE_UART  1

/* SPI3 chip-select: ARD D10 / PE0 (software NSS, driven as GPIO) */
#define TELE_CS_PORT    GPIOE
#define TELE_CS_PIN     GPIO_PIN_0

/* Slave-ready handshake from ESP32: ARD D2 / PD11 (input).
   Gating is deferred for now -- the pin is read + printed but NOT used to gate
   transmits; we verify it later with the ESP32 driving the line. */
#define TELE_HS_PORT    GPIOD
#define TELE_HS_PIN     GPIO_PIN_11

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
/* USER CODE BEGIN PV */

/* Test-bed A SPI3 master state */
SPI_HandleTypeDef hspi3;
static uint8_t  tele_frame[TELE_FRAME_LEN];
static uint32_t tele_seq = 0;

//this is the data that we'll send to the secure environment
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
uint32_t NSC_Mem_Buffer[BUFFER_SIZE];


//flags
static __IO uint32_t transferCompleteDetected; /* Set to 1 if transfer is correctly completed */
static __IO uint32_t transferErrorDetected; /* Set to 1 if an error transfer is detected */
/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
static void MX_GPIO_Init(void);
void SystemClock_Config(void);
//static void NonSecureTransferComplete(DMA_HandleTypeDef *hdma_memtomem_dma1_channel4);
//static void NonSecureTransferError(DMA_HandleTypeDef *hdma_memtomem_dma1_channel4);
//static void NonSecure_To_NonSecure_Mem_Transfer(uint32_t* src, uint32_t* dest, uint32_t size);

/* USER CODE BEGIN PFP */
static void MX_SPI3_Init(void);
static void Workload_GPIO_Init(void);
static void Tele_BuildFrame(uint8_t *buf, uint32_t seq, uint16_t value);
#if TESTBEDA_MODE_LOOPBACK
static void Loopback_GPIO_Init(void);
#if TESTBEDA_REVERSE_READBACK
static void Reverse_Readback_Init(void);
#endif
#endif
#if TESTBEDA_USE_UART
static void Uart3_Init(void);
static void Uart3_Write(const uint8_t *buf, uint32_t len);
static void Uart3_Poll(void);
#endif
static void NonSecureSecureTransferCompleteCallback(DMA_HandleTypeDef *hdma_memtomem_dma1_channelx);
static void NonSecureNonSecureTransferCompleteCallback(DMA_HandleTypeDef *hdma_memtomem_dma1_channelx);
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

  /* ---- Week 1 Phase 3: non-secure read-back of the OSPI XIP region ----
     The secure world already erased/programmed the pattern and left OCTOSPI1 in
     memory-mapped mode, and dropped a non-secure watermark over 0x90000000.
     This is the definitive proof of the path the inference engine will use:
     a plain non-secure load from external XIP flash. Reported via the secure
     UART veneer (USART1 is secure-attributed). */
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

#if 0  /* Week 1: full memory dump temporarily disabled so the OSPI XIP proof
          output is easy to read. Re-enable by flipping this back to #if 1. */
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
#endif /* memory dump disabled for Week 1 OSPI proof */

#if TESTBEDA_MODE_LOOPBACK
#if TESTBEDA_REVERSE_READBACK
  /* ---- Reverse-direction readback: STM32 LISTENS, ESP32 DRIVES (no rewiring). ----
     Verified CN3 STMod+ pinout (UM2617 Table 31): NSS=PB13 (pin1), MOSI=PB5 (pin2),
     MISO=PB4 (pin3), SCK=PG9 (pin4). The mux pairs pin1+pin2 (SEL_12) and pin3+pin4
     (SEL_34), so in SPI mode (PF11=PF12=0) the select that routes CS/PB13 also routes
     MOSI/PB5. The ESP32 drives one mikroBUS pad high at a time; we read all four here.
     PG9(SCK) and PB13(CS) are POSITIVE CONTROLS (proven to reach the ESP32): if their
     reads track the ESP32, the rig + reverse direction work. PB5(MOSI) is the unknown --
     if it ALSO tracks, the PB5<->mikroBUS path is continuous and the earlier failure was
     PB5's OUTPUT; if PB5 stays flat while the controls move, the PB5 physical route is
     broken (UCPD_DBn net / DK bridge), not firmware. PB4(MISO) is not wired -> stays 0. */
  Reverse_Readback_Init();
  SECURE_print_Log("[NS] Test-bed A: REVERSE READBACK -- STM32 reads, ESP32 drives.\r\n");
  SECURE_print_Log("[NS] PG9/SCK & PB13/CS = positive controls; PB5/MOSI = the unknown.\r\n");
  for (;;)
  {
    char rmsg[112];
    int sck  = (HAL_GPIO_ReadPin(GPIOG, GPIO_PIN_9)  == GPIO_PIN_SET);
    int cs   = (HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_13) == GPIO_PIN_SET);
    int mosi = (HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_5)  == GPIO_PIN_SET);
    int miso = (HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_4)  == GPIO_PIN_SET);
    snprintf(rmsg, sizeof(rmsg),
             "[NS] read  SCK/PG9=%d  CS/PB13=%d  MOSI/PB5=%d  (MISO/PB4=%d, unwired)\r\n",
             sck, cs, mosi, miso);
    SECURE_print_Log(rmsg);
    HAL_Delay(400u);
  }
#else
  /* ---- Diagnostic: GPIO loopback (no SPI). Drive D10/PE0, D11/PB5, D13/PG9 as
     plain push-pull outputs in a 3-bit counter (D10=bit0, D11=bit1, D13=bit2) so
     the ESP32 can digitalRead each socket and report which lines are electrically
     live. This branch loops forever -- the SPI path below is unreachable until the
     TESTBEDA_MODE_LOOPBACK switch is set back to 0. ---- */
  Loopback_GPIO_Init();
  SECURE_print_Log("[NS] Test-bed A: SPI-MUX SWEEP -- finding which PF11/PF12 routes MOSI/MISO\r\n");
  /* Sweep the SPI-routing mux (PF11=SEL_12, PF12=SEL_34) through all 4 combos. For each
     combo, hold the select lines and toggle ALL four SPI pins (CS/SCK/MOSI/MISO) together at
     ~2 Hz. On the ESP32, whichever reads toggle are the pins THIS combo routes to the
     connector. CS+SCK already survive (00); we are hunting the combo that ALSO wakes
     MOSI(g23)/MISO. Keep the ESP32's g23 wire on the mikroBUS MOSI pin and note the sel= the
     STM32 console prints when MOSI starts toggling. (The 00=SPI truth-table read was
     unreliable -- this measures it.) */
  for (uint32_t sel = 0; ; sel = (sel + 1u) & 0x3u)
  {
    const GPIO_PinState pf11 = (sel & 0x1u) ? GPIO_PIN_SET : GPIO_PIN_RESET;  /* SEL_12 */
    const GPIO_PinState pf12 = (sel & 0x2u) ? GPIO_PIN_SET : GPIO_PIN_RESET;  /* SEL_34 */
    char smsg[96];

    HAL_GPIO_WritePin(GPIOF, GPIO_PIN_11, pf11);
    HAL_GPIO_WritePin(GPIOF, GPIO_PIN_12, pf12);
    snprintf(smsg, sizeof(smsg),
             "[NS] >>> MUX sel=%lu  PF11/SEL_12=%d  PF12/SEL_34=%d  (watch ESP32 ~3s) <<<\r\n",
             (unsigned long)sel, (pf11 ? 1 : 0), (pf12 ? 1 : 0));
    SECURE_print_Log(smsg);

    /* Hold this combo ~3 s, toggling all four SPI pins together at ~2 Hz. */
    for (uint32_t t = 0; t < 12u; t++)
    {
      const GPIO_PinState tog = (t & 0x1u) ? GPIO_PIN_SET : GPIO_PIN_RESET;
      HAL_GPIO_WritePin(GPIOE, GPIO_PIN_0,  tog);   /* CS  (Arduino D10) */
      HAL_GPIO_WritePin(GPIOB, GPIO_PIN_13, tog);   /* CS  (mikroBUS)    */
      HAL_GPIO_WritePin(GPIOB, GPIO_PIN_5,  tog);   /* MOSI              */
      HAL_GPIO_WritePin(GPIOB, GPIO_PIN_4,  tog);   /* MISO              */
      HAL_GPIO_WritePin(GPIOG, GPIO_PIN_9,  tog);   /* SCK               */
      HAL_Delay(250u);
    }
  }
#endif /* TESTBEDA_REVERSE_READBACK */
#endif /* TESTBEDA_MODE_LOOPBACK */

#if TESTBEDA_USE_UART
  /* ---- Test-bed A: UART telemetry (the verified working path). Route the STMod+ mux to
     mikroBUS mode (SEL_12=PF11=1, SEL_34=PF12=0) so USART3 PC10/PC11 reach the mikroBUS
     UART pads (UM2617 Table 30), then bring up USART3 TX on PC10. ---- */
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
#else
  /* ---- Test-bed A: bring up the NonSecure SPI3 master + dummy telemetry ---- */
  Workload_GPIO_Init();
  MX_SPI3_Init();
  SECURE_print_Log("[NS] Test-bed A: SPI3 master init done\r\n");
#endif

  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    {
      /* Handshake deferred: transmit every cycle regardless of HS. The pin is still
         read + printed so we can watch it move once the ESP32 drives it; re-enable
         gating after the ESP32 side is verified. */
      GPIO_PinState hs = HAL_GPIO_ReadPin(TELE_HS_PORT, TELE_HS_PIN);
      uint16_t value   = (uint16_t)(1000u + (tele_seq % 50u));
      HAL_StatusTypeDef st;
      char msg[96];

      Tele_BuildFrame(tele_frame, tele_seq, value);

#if TESTBEDA_USE_UART
      Uart3_Write(tele_frame, TELE_FRAME_LEN);   /* same 9-byte frame, over USART3/PC10 */
      st = HAL_OK;
#else
      HAL_GPIO_WritePin(TELE_CS_PORT, TELE_CS_PIN, GPIO_PIN_RESET);   /* CS low  */
      st = HAL_SPI_Transmit(&hspi3, tele_frame, TELE_FRAME_LEN, 100u);
      /* HAL_SPI_Transmit waits for BSY to clear (master end-of-transaction) before
         returning, so the last bit is already shifted out -- safe to deassert CS. */
      HAL_GPIO_WritePin(TELE_CS_PORT, TELE_CS_PIN, GPIO_PIN_SET);     /* CS high */
#endif

      snprintf(msg, sizeof(msg), "[NS] HS=%d seq=%lu val=%u tx=%s\r\n",
               (hs == GPIO_PIN_SET) ? 1 : 0,
               (unsigned long)tele_seq, (unsigned)value,
               (st == HAL_OK) ? "OK" : "ERR");
      SECURE_print_Log(msg);
      tele_seq++;
#if TESTBEDA_USE_UART
      /* Spend the inter-frame second polling the ESP32->STM32 RX path (~20x) so the reverse
         channel is drained + logged each second -- proves 2-way UART. */
      for (int k = 0; k < 20; k++) { Uart3_Poll(); HAL_Delay(50u); }
#else
      HAL_Delay(1000u);
#endif
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


//static void NonSecure_To_NonSecure_Mem_Transfer(uint32_t* src, uint32_t* dest, uint32_t size){
//	  HAL_DMA_RegisterCallback(&hdma_memtomem_dma1_channel4, HAL_DMA_XFER_CPLT_CB_ID, NonSecureTransferComplete);
//	  HAL_DMA_RegisterCallback(&hdma_memtomem_dma1_channel4, HAL_DMA_XFER_ERROR_CB_ID, NonSecureTransferError);
//	 /* Reset global var transferCompleteDetected to 0, it will be set to 1 if a transfer is correctly completed */
//	  transferCompleteDetected = 0;
//	  /* Reset global vartransferErrorDetected to 0, it will be set to 1 if a transfer error is detected */
//	  transferErrorDetected = 0;
//
//	  /* Configure the source, destination and buffer size DMA fields and Start DMA channel transfer */
//	  /* Enable DMA TC and TE interrupts */
//	  if (HAL_DMA_Start_IT(&hdma_memtomem_dma1_channel4,
//	                       (uint32_t)&src,
//	                       (uint32_t)&dest,
//	                       size) != HAL_OK)
//	  {
//	    /* Transfer Error */
//	    Error_Handler();
//	  }
//
//	  /* Wait for end of DMA transfer */
//	  while ((transferCompleteDetected == 0) &&
//	         (transferErrorDetected == 0)){SECURE_print_Log("Waiting for interrupt to be serviced. \n\r");}
//
//	  if (transferErrorDetected == 1)
//	  {
//		SECURE_print_Log("There was an error in non-secure to non-secure memory transfer. \n\r");
//	    Error_Handler();  /* Infinite loop */
//	  }
//
//}


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
  * @brief  SPI3 master init -- Test-bed A outbound telemetry.
  *         Mode 0 (CPOL=0/CPHA=0), 8-bit, MSB-first, software NSS,
  *         ~1.72 MHz (PCLK1 110 MHz / 64). GPIO/clocks are in HAL_SPI_MspInit().
  */
static void MX_SPI3_Init(void)
{
  hspi3.Instance               = SPI3;
  hspi3.Init.Mode              = SPI_MODE_MASTER;
  hspi3.Init.Direction         = SPI_DIRECTION_2LINES;
  hspi3.Init.DataSize          = SPI_DATASIZE_8BIT;
  hspi3.Init.CLKPolarity       = SPI_POLARITY_LOW;          /* mode 0 */
  hspi3.Init.CLKPhase          = SPI_PHASE_1EDGE;           /* mode 0 */
  hspi3.Init.NSS               = SPI_NSS_SOFT;              /* CS = GPIO PE0 */
  hspi3.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_64;  /* 110 MHz / 64 ~ 1.72 MHz */
  hspi3.Init.FirstBit          = SPI_FIRSTBIT_MSB;
  hspi3.Init.TIMode            = SPI_TIMODE_DISABLE;
  hspi3.Init.CRCCalculation    = SPI_CRCCALCULATION_DISABLE;
  hspi3.Init.CRCPolynomial     = 7;
  hspi3.Init.CRCLength         = SPI_CRC_LENGTH_DATASIZE;
  hspi3.Init.NSSPMode          = SPI_NSS_PULSE_DISABLE;
  if (HAL_SPI_Init(&hspi3) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief  Chip-select (PE0) + slave-ready handshake (PD11) GPIO.
  *         CS idles high; the handshake is an input pulled low, so the loop
  *         reports "waiting" until the ESP32 drives it high.
  */
static void Workload_GPIO_Init(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};

  __HAL_RCC_GPIOE_CLK_ENABLE();
  __HAL_RCC_GPIOD_CLK_ENABLE();

  HAL_GPIO_WritePin(TELE_CS_PORT, TELE_CS_PIN, GPIO_PIN_SET);   /* CS idle high */
  GPIO_InitStruct.Pin   = TELE_CS_PIN;
  GPIO_InitStruct.Mode  = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull  = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_HIGH;
  HAL_GPIO_Init(TELE_CS_PORT, &GPIO_InitStruct);

  GPIO_InitStruct.Pin   = TELE_HS_PIN;
  GPIO_InitStruct.Mode  = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull  = GPIO_PULLDOWN;
  HAL_GPIO_Init(TELE_HS_PORT, &GPIO_InitStruct);
}

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

#if TESTBEDA_MODE_LOOPBACK
/**
  * @brief  Set the on-chip SPI-mux to SPI mode (PF11/PF12=0) and configure the SPI3
  *         signal pins (PG9 SCK, PB5 MOSI, PB13 + PE0 CS) as plain push-pull outputs
  *         for the GPIO loopback diagnostic. PG9 is on the VDDIO2 domain, so VddIO2
  *         must be enabled or SCK stays dead.
  */
static void Loopback_GPIO_Init(void)
{
  GPIO_InitTypeDef g = {0};

  HAL_PWREx_EnableVddIO2();          /* PG9 needs VDDIO2 */
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOE_CLK_ENABLE();
  __HAL_RCC_GPIOF_CLK_ENABLE();      /* SEL_12/SEL_34 mux-select (PF11/PF12) */
  __HAL_RCC_GPIOG_CLK_ENABLE();

  g.Mode  = GPIO_MODE_OUTPUT_PP;
  g.Pull  = GPIO_NOPULL;
  g.Speed = GPIO_SPEED_FREQ_LOW;

  /* ---- STMod+ routing: SPI3 reaches the STMod+ CN3 connector (and the Pmod CN4)
     through an on-board quad-SPDT analog switch selected by SEL_12 (PF11) and
     SEL_34 (PF12). SPI mode = both LOW. These I/Os float at reset, so the SPI3
     signals reached NO external connector until firmware drove them -- the missing
     step that made every wiring attempt read dead. Drive the mux FIRST. ---- */
  g.Pin = GPIO_PIN_11; HAL_GPIO_Init(GPIOF, &g);   /* PF11 = SEL_12 */
  g.Pin = GPIO_PIN_12; HAL_GPIO_Init(GPIOF, &g);   /* PF12 = SEL_34 */
  HAL_GPIO_WritePin(GPIOF, GPIO_PIN_11 | GPIO_PIN_12, GPIO_PIN_RESET);  /* -> SPI mode */

  g.Pin = GPIO_PIN_0;  HAL_GPIO_Init(GPIOE, &g);   /* PE0  = ARD D10 (Arduino-only CS)     */
  g.Pin = GPIO_PIN_13; HAL_GPIO_Init(GPIOB, &g);   /* PB13 = STMod+ CN3-1 / mikroBUS CS    */
  g.Pin = GPIO_PIN_5;  HAL_GPIO_Init(GPIOB, &g);   /* PB5  = STMod+ CN3-2 / mikroBUS MOSI  */
  g.Pin = GPIO_PIN_4;  HAL_GPIO_Init(GPIOB, &g);   /* PB4  = STMod+ CN3-3 / mikroBUS MISO  */
  g.Pin = GPIO_PIN_9;  HAL_GPIO_Init(GPIOG, &g);   /* PG9  = STMod+ CN3-4 / mikroBUS SCK   */
}

#if TESTBEDA_REVERSE_READBACK
/**
  * @brief  Reverse-readback init: mux to SPI mode (PF11=PF12=0) and configure the four
  *         STMod+ SPI pins (PB13 NSS, PB5 MOSI, PB4 MISO, PG9 SCK) as pulled-down INPUTS,
  *         so a disconnected line reads 0 and a line the ESP32 drives high reads 1.
  *         PG9 is on the VDDIO2 domain.
  */
static void Reverse_Readback_Init(void)
{
  GPIO_InitTypeDef g = {0};

  HAL_PWREx_EnableVddIO2();
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOF_CLK_ENABLE();
  __HAL_RCC_GPIOG_CLK_ENABLE();

  /* Mux -> SPI mode so the STMod+ pins map to PB13/PB5/PB4/PG9 */
  g.Mode = GPIO_MODE_OUTPUT_PP; g.Pull = GPIO_NOPULL; g.Speed = GPIO_SPEED_FREQ_LOW;
  g.Pin = GPIO_PIN_11; HAL_GPIO_Init(GPIOF, &g);
  g.Pin = GPIO_PIN_12; HAL_GPIO_Init(GPIOF, &g);
  HAL_GPIO_WritePin(GPIOF, GPIO_PIN_11 | GPIO_PIN_12, GPIO_PIN_RESET);

  /* The four SPI signal pins as pulled-down INPUTS */
  g.Mode = GPIO_MODE_INPUT; g.Pull = GPIO_PULLDOWN;
  g.Pin = GPIO_PIN_13; HAL_GPIO_Init(GPIOB, &g);   /* NSS / CS  (CN3 pin1) */
  g.Pin = GPIO_PIN_5;  HAL_GPIO_Init(GPIOB, &g);   /* MOSI      (CN3 pin2) */
  g.Pin = GPIO_PIN_4;  HAL_GPIO_Init(GPIOB, &g);   /* MISO      (CN3 pin3) */
  g.Pin = GPIO_PIN_9;  HAL_GPIO_Init(GPIOG, &g);   /* SCK       (CN3 pin4) */
}
#endif /* TESTBEDA_REVERSE_READBACK */
#endif /* TESTBEDA_MODE_LOOPBACK */

#if TESTBEDA_USE_UART
/**
  * @brief  Bring up USART3 TX on PC10 (AF7) for Test-bed A telemetry over the STMod+/
  *         mikroBUS UART. 115200 8N1, transmit-only, polled. No HAL UART module needed --
  *         direct registers (the HAL GPIO/RCC we already link handle the pin + clock).
  *         The STMod+ mux must already be in mikroBUS/UART mode (PF11=1, PF12=0) for PC10
  *         to reach the mikroBUS TX pad.
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
#endif /* TESTBEDA_USE_UART */

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
