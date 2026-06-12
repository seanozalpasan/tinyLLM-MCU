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
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* Non-secure Vector table to jump to (internal Flash Bank2 here)             */
/* Caution: address must correspond to non-secure internal Flash where is     */
/*          mapped in the non-secure vector table                             */
#define VTOR_TABLE_NS_START_ADDR  0x08040000UL

/* ---- Week 1 Phase 3: OSPI XIP proof (Macronix MX25LM51245G on OCTOSPI1) ---- */
/* Command set + timings copied verbatim from the ST OSPI_NOR_MemoryMapped      */
/* example (STM32Cube_FW_L5 V1.5.0) so we reuse a known-good init sequence.     */
/* Flash commands (octal) */
#define OCTAL_IO_READ_CMD           0xEC13
#define OCTAL_PAGE_PROG_CMD         0x12ED
#define OCTAL_READ_STATUS_REG_CMD   0x05FA
#define OCTAL_SECTOR_ERASE_CMD      0x21DE
#define OCTAL_WRITE_ENABLE_CMD      0x06F9
#define READ_STATUS_REG_CMD         0x05
#define WRITE_CFG_REG_2_CMD         0x72
#define WRITE_ENABLE_CMD            0x06
/* Dummy clock cycles */
#define DUMMY_CLOCK_CYCLES_READ     6
#define DUMMY_CLOCK_CYCLES_READ_REG 4
/* Auto-polling match/mask values */
#define WRITE_ENABLE_MATCH_VALUE    0x02
#define WRITE_ENABLE_MASK_VALUE     0x02
#define MEMORY_READY_MATCH_VALUE    0x00
#define MEMORY_READY_MASK_VALUE     0x01
#define AUTO_POLLING_INTERVAL       0x10
/* Memory CR2 register addresses / values */
#define CONFIG_REG2_ADDR1           0x00000000
#define CR2_STR_OPI_ENABLE          0x01
#define CONFIG_REG2_ADDR3           0x00000300
#define CR2_DUMMY_CYCLES_66MHZ      0x07
/* Memory delays (ms) */
#define MEMORY_REG_WRITE_DELAY      40
#define MEMORY_PAGE_PROG_DELAY      2
/* Software-reset commands (force the flash back to 1-line SPI). Octal encoding
   is {cmd, ~cmd}, same scheme as the other OCTAL_* commands above. */
#define RESET_ENABLE_CMD            0x66
#define RESET_MEMORY_CMD            0x99
#define OCTAL_RESET_ENABLE_CMD      0x6699
#define OCTAL_RESET_MEMORY_CMD      0x9966
#define MEMORY_RESET_MAX_DELAY      100   /* ms; reset recovery worst case (reset during erase) */

/* ---- Build switch: OSPI self-test ----
   1 = run the destructive erase/program/verify self-test at boot (Week 1 proof).
   0 = NON-destructive path only: init + memory-mapped READ. Flip to 0 in W5
       once real model weights live in OSPI flash, or the boot path will erase
       them on every reset. */
#define OSPI_XIP_SELFTEST  1

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

UART_HandleTypeDef huart1;

//The original mem to mem example used three channels in secure to handle secure-to-secure and secure-to-non-secure transfers as well
//here, I'm just using channel three for non-secure to secure transfer
DMA_HandleTypeDef hdma_memtomem_dma1_channel3;
//this channel is used for non-secure to non-secure transfer
DMA_HandleTypeDef hdma_memtomem_dma1_channel2;

/* USER CODE BEGIN PV */

//this is a copy of the buffer that exists in the non-secure environment; the SECURE_DATA_Last_Buffer_Compare function
//uses it to check for successful nonsecure to secure memory transfer.
const uint32_t aSRC_SEC_ROM_Buffer[BUFFER_SIZE] =
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

//this is the buffer that non-secure will copy to; it holds 1024 bytes (256 words) of memory
uint32_t SEC_Mem_Buffer[BUFFER_SIZE];

/* Week 1 Phase 3: OSPI XIP state */
OSPI_HandleTypeDef hospi1;
#if OSPI_XIP_SELFTEST
/* Known pattern programmed into the external flash and checked from both worlds */
static const uint32_t OSPI_Test_Pattern[4] =
{
  0xDEADBEEFUL, 0xCAFEBABEUL, 0x12345678UL, 0xA5A5A5A5UL
};
#endif

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
static void NonSecure_Init(void);
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_DMA_Init(void);
static void MX_ICACHE_Init(void);
static void MX_GTZC_S_Init(void);
static void MX_USART1_UART_Init(void);
/* USER CODE BEGIN PFP */
/* Week 1 Phase 3: OSPI XIP. Reusable bring-up (OSPI_Init + OSPI_EnableMemoryMapped)
   is kept separate from the destructive self-test so the latter can be compiled
   out (OSPI_XIP_SELFTEST) once weights live in flash. */
static void MX_OCTOSPI1_Init(void);
static void OSPI_WriteEnable(OSPI_HandleTypeDef *hospi);
static void OSPI_AutoPollingMemReady(OSPI_HandleTypeDef *hospi);
static void OSPI_OctalModeCfg(OSPI_HandleTypeDef *hospi);
static void OSPI_SendCommandNoData(OSPI_HandleTypeDef *hospi, uint32_t instruction,
                                   uint32_t instr_mode, uint32_t instr_size,
                                   uint32_t instr_dtr);
static void OSPI_ResetFlash(OSPI_HandleTypeDef *hospi);
static void OSPI_Init(void);
static void OSPI_EnableMemoryMapped(int with_write);
#if OSPI_XIP_SELFTEST
static void OSPI_XIP_SelfTest(void);
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
  /* SAU/IDAU, FPU and interrupts secure/non-secure allocation setup done */
  /* in SystemInit() based on partition_stm32l562xx.h file's definitions. */
  /* USER CODE BEGIN 1 */
  SCB->SHCSR |= SCB_SHCSR_SECUREFAULTENA_Msk;
  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();
  /* GTZC initialisation */
  MX_GTZC_S_Init();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_DMA_Init();
  MX_ICACHE_Init();
  MX_USART1_UART_Init();

  /* USER CODE BEGIN 2 */

  /* Week 1 Phase 3: bring up OSPI XIP at 0x90000000. Runs before HAL_SuspendTick()
     below because it uses HAL_Delay(). Leaves OCTOSPI1 in memory-mapped mode so the
     non-secure world can read it after the jump. */
  printf("\r\n[S ] OSPI XIP: bringing up OCTOSPI1 (init + flash reset + octal mode)...\r\n");
  OSPI_Init();
#if OSPI_XIP_SELFTEST
  OSPI_XIP_SelfTest();          /* erases + programs + verifies; leaves memory-mapped mode on */
#else
  OSPI_EnableMemoryMapped(0);   /* production: read-only XIP, no erase/program */
#endif
  printf("[S ] OCTOSPI1 in memory-mapped mode @0x90000000.\r\n");

  /* DMA1 Channel3: Select Callbacks functions called after Transfer complete and Transfer error */
    HAL_DMA_RegisterCallback(&hdma_memtomem_dma1_channel3, HAL_DMA_XFER_CPLT_CB_ID, NonSecureToSecureTransferComplete);
    HAL_DMA_RegisterCallback(&hdma_memtomem_dma1_channel3, HAL_DMA_XFER_ERROR_CB_ID, NonSecureToSecureTransferError);

    HAL_DMA_RegisterCallback(&hdma_memtomem_dma1_channel2, HAL_DMA_XFER_CPLT_CB_ID, NonSecureToNonSecureTransferComplete);
    HAL_DMA_RegisterCallback(&hdma_memtomem_dma1_channel2, HAL_DMA_XFER_ERROR_CB_ID, NonSecureToNonSecureTransferError);

    HAL_SuspendTick();
  /* USER CODE END 2 */

  /*************** Setup and jump to non-secure *******************************/

  NonSecure_Init();

  /* Non-secure software does not return, this code is not executed */
  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
  }
  /* USER CODE END 3 */
}

/**
  * @brief  Non-secure call function
  *         This function is responsible for Non-secure initialization and switch
  *         to non-secure state
  * @retval None
  */
static void NonSecure_Init(void)
{
  funcptr_NS NonSecure_ResetHandler;

  SCB_NS->VTOR = VTOR_TABLE_NS_START_ADDR;

  /* Set non-secure main stack (MSP_NS) */
  __TZ_set_MSP_NS((*(uint32_t *)VTOR_TABLE_NS_START_ADDR));

  /* Get non-secure reset handler */
  NonSecure_ResetHandler = (funcptr_NS)(*((uint32_t *)((VTOR_TABLE_NS_START_ADDR) + 4U)));

  /* Start non-secure state software application */
  NonSecure_ResetHandler();
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
  * @brief GTZC_S Initialization Function
  * @param None
  * @retval None
  */
static void MX_GTZC_S_Init(void)
{

  /* USER CODE BEGIN GTZC_S_Init 0 */

  /* USER CODE END GTZC_S_Init 0 */

  MPCBB_ConfigTypeDef MPCBB_NonSecureArea_Desc = {0};

  /* USER CODE BEGIN GTZC_S_Init 1 */

  /* USER CODE END GTZC_S_Init 1 */
  if (HAL_GTZC_TZSC_ConfigPeriphAttributes(GTZC_PERIPH_USART1, GTZC_TZSC_PERIPH_SEC|GTZC_TZSC_PERIPH_NPRIV) != HAL_OK)
  {
    Error_Handler();
  }
  MPCBB_NonSecureArea_Desc.SecureRWIllegalMode = GTZC_MPCBB_SRWILADIS_ENABLE;
  MPCBB_NonSecureArea_Desc.InvertSecureState = GTZC_MPCBB_INVSECSTATE_NOT_INVERTED;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[0] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[1] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[2] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[3] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[4] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[5] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[6] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[7] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[8] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[9] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[10] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[11] =   0xFFFFFFFF;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[12] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[13] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[14] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[15] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[16] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[17] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[18] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[19] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[20] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[21] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[22] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[23] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_LockConfig_array[0] =   0x00000000;
  if (HAL_GTZC_MPCBB_ConfigMem(SRAM1_BASE, &MPCBB_NonSecureArea_Desc) != HAL_OK)
  {
    Error_Handler();
  }
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[0] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[1] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[2] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[3] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[4] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[5] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[6] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_SecConfig_array[7] =   0x00000000;
  MPCBB_NonSecureArea_Desc.AttributeConfig.MPCBB_LockConfig_array[0] =   0x00000000;
  if (HAL_GTZC_MPCBB_ConfigMem(SRAM2_BASE, &MPCBB_NonSecureArea_Desc) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN GTZC_S_Init 2 */

  /* Week 1 Phase 3: open the OCTOSPI1 memory-mapped region to non-secure access.
     External memory defaults to SECURE in GTZC. SAU region 4 already marks
     0x60000000-0x9FFFFFFF non-secure, so every access to 0x90000000 (even from
     secure code) is issued as a NON-secure bus transaction and would be blocked
     until we drop a non-secure watermark here. This driver's MPCWM writes the
     non-secure watermark register (NSWMR): the configured area becomes
     non-secure, the remainder stays secure. One 128 KB granule from offset 0
     covers our 4 KB test sector. W5 TODO: grow Length to span the whole model
     weight blob (still 128 KB-granular) so the engine can XIP all of it. */
  {
    MPCWM_ConfigTypeDef MPCWM_OSPI_Desc = {0};
    MPCWM_OSPI_Desc.AreaId = GTZC_TZSC_MPCWM_ID1;
    MPCWM_OSPI_Desc.Offset = 0U;
    MPCWM_OSPI_Desc.Length = GTZC_TZSC_MPCWM_GRANULARITY; /* 128 KB */
    if (HAL_GTZC_TZSC_MPCWM_ConfigMemAttributes(OCTOSPI1_BASE, &MPCWM_OSPI_Desc) != HAL_OK)
    {
      Error_Handler();
    }
  }

  /* USER CODE END GTZC_S_Init 2 */

}

/**
  * @brief ICACHE Initialization Function
  * @param None
  * @retval None
  */
static void MX_ICACHE_Init(void)
{

  /* USER CODE BEGIN ICACHE_Init 0 */

  /* USER CODE END ICACHE_Init 0 */

  /* USER CODE BEGIN ICACHE_Init 1 */

  /* USER CODE END ICACHE_Init 1 */

  /** Enable instruction cache in 1-way (direct mapped cache)
  */
  if (HAL_ICACHE_ConfigAssociativityMode(ICACHE_1WAY) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_ICACHE_Enable() != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN ICACHE_Init 2 */

  /* USER CODE END ICACHE_Init 2 */

}

/**
  * @brief USART1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_USART1_UART_Init(void)
{

  /* USER CODE BEGIN USART1_Init 0 */

  /* USER CODE END USART1_Init 0 */

  /* USER CODE BEGIN USART1_Init 1 */

  /* USER CODE END USART1_Init 1 */
  huart1.Instance = USART1;
  huart1.Init.BaudRate = 115200;
  huart1.Init.WordLength = UART_WORDLENGTH_8B;
  huart1.Init.StopBits = UART_STOPBITS_1;
  huart1.Init.Parity = UART_PARITY_ODD;
  huart1.Init.Mode = UART_MODE_TX_RX;
  huart1.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  huart1.Init.OverSampling = UART_OVERSAMPLING_16;
  huart1.Init.OneBitSampling = UART_ONE_BIT_SAMPLE_DISABLE;
  huart1.Init.ClockPrescaler = UART_PRESCALER_DIV1;
  huart1.AdvancedInit.AdvFeatureInit = UART_ADVFEATURE_NO_INIT;
  if (HAL_UART_Init(&huart1) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_UARTEx_SetTxFifoThreshold(&huart1, UART_TXFIFO_THRESHOLD_1_8) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_UARTEx_SetRxFifoThreshold(&huart1, UART_RXFIFO_THRESHOLD_1_8) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_UARTEx_DisableFifoMode(&huart1) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN USART1_Init 2 */

  /* USER CODE END USART1_Init 2 */

}

/**
  * Enable DMA controller clock
  * Configure DMA for memory to memory transfers
  *   hdma_memtomem_dma1_channel1
  *   hdma_memtomem_dma1_channel2
  *   hdma_memtomem_dma1_channel3
  */
static void MX_DMA_Init(void)
{

  /* DMA controller clock enable */
  __HAL_RCC_DMAMUX1_CLK_ENABLE();
  __HAL_RCC_DMA1_CLK_ENABLE();

  /* Configure DMA request hdma_memtomem_dma1_channel3 on DMA1_Channel3 */
  hdma_memtomem_dma1_channel3.Instance = DMA1_Channel3;
  hdma_memtomem_dma1_channel3.Init.Request = DMA_REQUEST_MEM2MEM;
  hdma_memtomem_dma1_channel3.Init.Direction = DMA_MEMORY_TO_MEMORY;
  hdma_memtomem_dma1_channel3.Init.PeriphInc = DMA_PINC_ENABLE;
  hdma_memtomem_dma1_channel3.Init.MemInc = DMA_MINC_ENABLE;
  hdma_memtomem_dma1_channel3.Init.PeriphDataAlignment = DMA_PDATAALIGN_WORD;
  hdma_memtomem_dma1_channel3.Init.MemDataAlignment = DMA_MDATAALIGN_WORD;
  hdma_memtomem_dma1_channel3.Init.Mode = DMA_NORMAL;
  hdma_memtomem_dma1_channel3.Init.Priority = DMA_PRIORITY_LOW;
  if (HAL_DMA_Init(&hdma_memtomem_dma1_channel3) != HAL_OK)
  {
    Error_Handler( );
  }

  /*  */
  if (HAL_DMA_ConfigChannelAttributes(&hdma_memtomem_dma1_channel3, DMA_CHANNEL_NPRIV) != HAL_OK)
  {
    Error_Handler( );
  }

  /*  */
  if (HAL_DMA_ConfigChannelAttributes(&hdma_memtomem_dma1_channel3, DMA_CHANNEL_SEC) != HAL_OK)
  {
    Error_Handler( );
  }

  /*  */
  if (HAL_DMA_ConfigChannelAttributes(&hdma_memtomem_dma1_channel3, DMA_CHANNEL_SRC_NSEC) != HAL_OK)
  {
    Error_Handler( );
  }

  /*  */
  if (HAL_DMA_ConfigChannelAttributes(&hdma_memtomem_dma1_channel3, DMA_CHANNEL_DEST_SEC) != HAL_OK)
  {
    Error_Handler( );
  }


   hdma_memtomem_dma1_channel2.Instance = DMA1_Channel2;
   hdma_memtomem_dma1_channel2.Init.Request = DMA_REQUEST_MEM2MEM;
   hdma_memtomem_dma1_channel2.Init.Direction = DMA_MEMORY_TO_MEMORY;
   hdma_memtomem_dma1_channel2.Init.PeriphInc = DMA_PINC_ENABLE;
   hdma_memtomem_dma1_channel2.Init.MemInc = DMA_MINC_ENABLE;
   hdma_memtomem_dma1_channel2.Init.PeriphDataAlignment = DMA_PDATAALIGN_WORD;
   hdma_memtomem_dma1_channel2.Init.MemDataAlignment = DMA_MDATAALIGN_WORD;
   hdma_memtomem_dma1_channel2.Init.Mode = DMA_NORMAL;
   hdma_memtomem_dma1_channel2.Init.Priority = DMA_PRIORITY_LOW;
   if (HAL_DMA_Init(&hdma_memtomem_dma1_channel2) != HAL_OK)
   {
     Error_Handler( );
   }

   /*  */
   if (HAL_DMA_ConfigChannelAttributes(&hdma_memtomem_dma1_channel2, DMA_CHANNEL_NPRIV) != HAL_OK)
   {
     Error_Handler( );
   }

   /*  */
   if (HAL_DMA_ConfigChannelAttributes(&hdma_memtomem_dma1_channel2, DMA_CHANNEL_SEC) != HAL_OK)
   {
     Error_Handler( );
   }

   /*  */
   if (HAL_DMA_ConfigChannelAttributes(&hdma_memtomem_dma1_channel2, DMA_CHANNEL_SRC_NSEC) != HAL_OK)
   {
     Error_Handler( );
   }

   /*  */
   if (HAL_DMA_ConfigChannelAttributes(&hdma_memtomem_dma1_channel2, DMA_CHANNEL_DEST_NSEC) != HAL_OK)
   {
     Error_Handler( );
   }

   /* DMA1_Channel2_IRQn interrupt configuration */
     HAL_NVIC_SetPriority(DMA1_Channel2_IRQn, 0, 0);
     HAL_NVIC_EnableIRQ(DMA1_Channel2_IRQn);

	/* DMA1_Channel3_IRQn interrupt configuration */
	HAL_NVIC_SetPriority(DMA1_Channel3_IRQn, 0, 0);
	HAL_NVIC_EnableIRQ(DMA1_Channel3_IRQn);

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
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();

  /*IO attributes management functions */
  HAL_GPIO_ConfigPinAttributes(GPIOC, GPIO_PIN_14|GPIO_PIN_15, GPIO_PIN_NSEC);

/* USER CODE BEGIN MX_GPIO_Init_2 */
/* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */

/* ======================= Week 1 Phase 3: OSPI XIP proof =======================
   Self-contained OCTOSPI1 bring-up + erase/program/read of a known pattern at
   0x90000000, ported by hand from the ST OSPI_NOR_MemoryMapped example
   (STM32Cube_FW_L5 V1.5.0) into the secure world of this TrustZone project.
   Everything here is polling/blocking (no OCTOSPI interrupt) to avoid touching
   stm32l5xx_it.c. The non-secure watermark for this region is dropped in
   MX_GTZC_S_Init(). ============================================================ */

/**
  * @brief OCTOSPI1 MSP init: peripheral clock source, clock enables and the
  *        11 OSPI pins. Overrides the __weak HAL_OSPI_MspInit; called from
  *        HAL_OSPI_Init(). No NVIC config (we poll).
  */
void HAL_OSPI_MspInit(OSPI_HandleTypeDef* hospi)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  RCC_PeriphCLKInitTypeDef PeriphClkInit = {0};

  if (hospi->Instance == OCTOSPI1)
  {
    /* OSPI kernel clock = SYSCLK (110 MHz) */
    PeriphClkInit.PeriphClockSelection = RCC_PERIPHCLK_OSPI;
    PeriphClkInit.OspiClockSelection   = RCC_OSPICLKSOURCE_SYSCLK;
    if (HAL_RCCEx_PeriphCLKConfig(&PeriphClkInit) != HAL_OK)
    {
      Error_Handler();
    }

    __HAL_RCC_OSPI1_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();
    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_GPIOB_CLK_ENABLE();

    /* PC1->IO4, PC2->IO5, PC3->IO6 (AF10) */
    GPIO_InitStruct.Pin       = GPIO_PIN_2 | GPIO_PIN_3 | GPIO_PIN_1;
    GPIO_InitStruct.Mode      = GPIO_MODE_AF_PP;
    GPIO_InitStruct.Pull      = GPIO_NOPULL;
    GPIO_InitStruct.Speed     = GPIO_SPEED_FREQ_VERY_HIGH;
    GPIO_InitStruct.Alternate = GPIO_AF10_OCTOSPI1;
    HAL_GPIO_Init(GPIOC, &GPIO_InitStruct);

    /* PC0->IO7 (AF3) */
    GPIO_InitStruct.Pin       = GPIO_PIN_0;
    GPIO_InitStruct.Alternate = GPIO_AF3_OCTOSPI1;
    HAL_GPIO_Init(GPIOC, &GPIO_InitStruct);

    /* PA2->NCS (pull-up, AF10) */
    GPIO_InitStruct.Pin       = GPIO_PIN_2;
    GPIO_InitStruct.Pull      = GPIO_PULLUP;
    GPIO_InitStruct.Alternate = GPIO_AF10_OCTOSPI1;
    HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

    /* PA3->CLK, PA6->IO3, PA7->IO2 (AF10) */
    GPIO_InitStruct.Pin       = GPIO_PIN_7 | GPIO_PIN_3 | GPIO_PIN_6;
    GPIO_InitStruct.Pull      = GPIO_NOPULL;
    GPIO_InitStruct.Alternate = GPIO_AF10_OCTOSPI1;
    HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

    /* PB0->IO1, PB1->IO0, PB2->DQS (AF10) */
    GPIO_InitStruct.Pin       = GPIO_PIN_2 | GPIO_PIN_1 | GPIO_PIN_0;
    HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);
  }
}

/**
  * @brief OCTOSPI1 controller init (identical params to the ST example).
  */
static void MX_OCTOSPI1_Init(void)
{
  hospi1.Instance                = OCTOSPI1;
  hospi1.Init.FifoThreshold      = 4;
  hospi1.Init.DualQuad           = HAL_OSPI_DUALQUAD_DISABLE;
  hospi1.Init.MemoryType         = HAL_OSPI_MEMTYPE_MICRON;
  hospi1.Init.DeviceSize         = 26;   /* 2^26 = 64 MB */
  hospi1.Init.ChipSelectHighTime = 2;
  hospi1.Init.FreeRunningClock   = HAL_OSPI_FREERUNCLK_DISABLE;
  hospi1.Init.ClockMode          = HAL_OSPI_CLOCK_MODE_0;
  hospi1.Init.WrapSize           = HAL_OSPI_WRAP_NOT_SUPPORTED;
  hospi1.Init.ClockPrescaler     = 2;
  hospi1.Init.SampleShifting     = HAL_OSPI_SAMPLE_SHIFTING_NONE;
  hospi1.Init.DelayHoldQuarterCycle = HAL_OSPI_DHQC_ENABLE;
  hospi1.Init.ChipSelectBoundary = 0;
  hospi1.Init.DelayBlockBypass   = HAL_OSPI_DELAY_BLOCK_USED;
  hospi1.Init.Refresh            = 0;
  if (HAL_OSPI_Init(&hospi1) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief Send Write Enable and poll until effective (octal mode). Verbatim ST.
  */
static void OSPI_WriteEnable(OSPI_HandleTypeDef *hospi)
{
  OSPI_RegularCmdTypeDef  sCommand;
  OSPI_AutoPollingTypeDef sConfig;

  sCommand.OperationType      = HAL_OSPI_OPTYPE_COMMON_CFG;
  sCommand.FlashId            = HAL_OSPI_FLASH_ID_1;
  sCommand.Instruction        = OCTAL_WRITE_ENABLE_CMD;
  sCommand.InstructionMode    = HAL_OSPI_INSTRUCTION_8_LINES;
  sCommand.InstructionSize    = HAL_OSPI_INSTRUCTION_16_BITS;
  sCommand.InstructionDtrMode = HAL_OSPI_INSTRUCTION_DTR_DISABLE;
  sCommand.AddressMode        = HAL_OSPI_ADDRESS_NONE;
  sCommand.AlternateBytesMode = HAL_OSPI_ALTERNATE_BYTES_NONE;
  sCommand.DataMode           = HAL_OSPI_DATA_NONE;
  sCommand.DummyCycles        = 0;
  sCommand.DQSMode            = HAL_OSPI_DQS_DISABLE;
  sCommand.SIOOMode           = HAL_OSPI_SIOO_INST_EVERY_CMD;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  sCommand.Instruction    = OCTAL_READ_STATUS_REG_CMD;
  sCommand.Address        = 0x0;
  sCommand.AddressMode    = HAL_OSPI_ADDRESS_8_LINES;
  sCommand.AddressSize    = HAL_OSPI_ADDRESS_32_BITS;
  sCommand.AddressDtrMode = HAL_OSPI_ADDRESS_DTR_DISABLE;
  sCommand.DataMode       = HAL_OSPI_DATA_8_LINES;
  sCommand.DataDtrMode    = HAL_OSPI_DATA_DTR_DISABLE;
  sCommand.NbData         = 1;
  sCommand.DummyCycles    = DUMMY_CLOCK_CYCLES_READ_REG;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  sConfig.Match         = WRITE_ENABLE_MATCH_VALUE;
  sConfig.Mask          = WRITE_ENABLE_MASK_VALUE;
  sConfig.MatchMode     = HAL_OSPI_MATCH_MODE_AND;
  sConfig.Interval      = AUTO_POLLING_INTERVAL;
  sConfig.AutomaticStop = HAL_OSPI_AUTOMATIC_STOP_ENABLE;
  if (HAL_OSPI_AutoPolling(hospi, &sConfig, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief Poll the status register until the memory is ready (WIP=0). Verbatim ST.
  */
static void OSPI_AutoPollingMemReady(OSPI_HandleTypeDef *hospi)
{
  OSPI_RegularCmdTypeDef  sCommand;
  OSPI_AutoPollingTypeDef sConfig;

  sCommand.OperationType      = HAL_OSPI_OPTYPE_COMMON_CFG;
  sCommand.FlashId            = HAL_OSPI_FLASH_ID_1;
  sCommand.Instruction        = OCTAL_READ_STATUS_REG_CMD;
  sCommand.InstructionMode    = HAL_OSPI_INSTRUCTION_8_LINES;
  sCommand.InstructionSize    = HAL_OSPI_INSTRUCTION_16_BITS;
  sCommand.InstructionDtrMode = HAL_OSPI_INSTRUCTION_DTR_DISABLE;
  sCommand.Address            = 0x0;
  sCommand.AddressMode        = HAL_OSPI_ADDRESS_8_LINES;
  sCommand.AddressSize        = HAL_OSPI_ADDRESS_32_BITS;
  sCommand.AddressDtrMode     = HAL_OSPI_ADDRESS_DTR_DISABLE;
  sCommand.AlternateBytesMode = HAL_OSPI_ALTERNATE_BYTES_NONE;
  sCommand.DataMode           = HAL_OSPI_DATA_8_LINES;
  sCommand.DataDtrMode        = HAL_OSPI_DATA_DTR_DISABLE;
  sCommand.NbData             = 1;
  sCommand.DummyCycles        = DUMMY_CLOCK_CYCLES_READ_REG;
  sCommand.DQSMode            = HAL_OSPI_DQS_DISABLE;
  sCommand.SIOOMode           = HAL_OSPI_SIOO_INST_EVERY_CMD;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  sConfig.Match         = MEMORY_READY_MATCH_VALUE;
  sConfig.Mask          = MEMORY_READY_MASK_VALUE;
  sConfig.MatchMode     = HAL_OSPI_MATCH_MODE_AND;
  sConfig.Interval      = AUTO_POLLING_INTERVAL;
  sConfig.AutomaticStop = HAL_OSPI_AUTOMATIC_STOP_ENABLE;
  if (HAL_OSPI_AutoPolling(hospi, &sConfig, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief Switch the Macronix flash into octal STR mode. Verbatim ST.
  */
static void OSPI_OctalModeCfg(OSPI_HandleTypeDef *hospi)
{
  OSPI_RegularCmdTypeDef  sCommand;
  OSPI_AutoPollingTypeDef sConfig;
  uint8_t reg;

  /* Write enable (1-line SPI) */
  sCommand.OperationType      = HAL_OSPI_OPTYPE_COMMON_CFG;
  sCommand.FlashId            = HAL_OSPI_FLASH_ID_1;
  sCommand.Instruction        = WRITE_ENABLE_CMD;
  sCommand.InstructionMode    = HAL_OSPI_INSTRUCTION_1_LINE;
  sCommand.InstructionSize    = HAL_OSPI_INSTRUCTION_8_BITS;
  sCommand.InstructionDtrMode = HAL_OSPI_INSTRUCTION_DTR_DISABLE;
  sCommand.AddressMode        = HAL_OSPI_ADDRESS_NONE;
  sCommand.AlternateBytesMode = HAL_OSPI_ALTERNATE_BYTES_NONE;
  sCommand.DataMode           = HAL_OSPI_DATA_NONE;
  sCommand.DummyCycles        = 0;
  sCommand.DQSMode            = HAL_OSPI_DQS_DISABLE;
  sCommand.SIOOMode           = HAL_OSPI_SIOO_INST_EVERY_CMD;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  sCommand.Instruction = READ_STATUS_REG_CMD;
  sCommand.DataMode    = HAL_OSPI_DATA_1_LINE;
  sCommand.DataDtrMode = HAL_OSPI_DATA_DTR_DISABLE;
  sCommand.NbData      = 1;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  sConfig.Match         = WRITE_ENABLE_MATCH_VALUE;
  sConfig.Mask          = WRITE_ENABLE_MASK_VALUE;
  sConfig.MatchMode     = HAL_OSPI_MATCH_MODE_AND;
  sConfig.Interval      = AUTO_POLLING_INTERVAL;
  sConfig.AutomaticStop = HAL_OSPI_AUTOMATIC_STOP_ENABLE;
  if (HAL_OSPI_AutoPolling(hospi, &sConfig, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  /* Write CR2 (dummy cycles) */
  sCommand.Instruction    = WRITE_CFG_REG_2_CMD;
  sCommand.Address        = CONFIG_REG2_ADDR3;
  sCommand.AddressMode    = HAL_OSPI_ADDRESS_1_LINE;
  sCommand.AddressSize    = HAL_OSPI_ADDRESS_32_BITS;
  sCommand.AddressDtrMode = HAL_OSPI_ADDRESS_DTR_DISABLE;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
  reg = CR2_DUMMY_CYCLES_66MHZ;
  if (HAL_OSPI_Transmit(hospi, &reg, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  sCommand.Instruction = READ_STATUS_REG_CMD;
  sCommand.AddressMode = HAL_OSPI_ADDRESS_NONE;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
  sConfig.Match = MEMORY_READY_MATCH_VALUE;
  sConfig.Mask  = MEMORY_READY_MASK_VALUE;
  if (HAL_OSPI_AutoPolling(hospi, &sConfig, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  /* Write enable again before switching the interface to octal */
  sCommand.Instruction = WRITE_ENABLE_CMD;
  sCommand.DataMode    = HAL_OSPI_DATA_NONE;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
  sCommand.Instruction = READ_STATUS_REG_CMD;
  sCommand.DataMode    = HAL_OSPI_DATA_1_LINE;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
  sConfig.Match = WRITE_ENABLE_MATCH_VALUE;
  sConfig.Mask  = WRITE_ENABLE_MASK_VALUE;
  if (HAL_OSPI_AutoPolling(hospi, &sConfig, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  /* Write CR2 (enable octal STR) */
  sCommand.Instruction = WRITE_CFG_REG_2_CMD;
  sCommand.Address     = CONFIG_REG2_ADDR1;
  sCommand.AddressMode = HAL_OSPI_ADDRESS_1_LINE;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
  reg = CR2_STR_OPI_ENABLE;
  if (HAL_OSPI_Transmit(hospi, &reg, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  /* Wait the config to take effect, then confirm ready in octal mode */
  HAL_Delay(MEMORY_REG_WRITE_DELAY);
  OSPI_AutoPollingMemReady(hospi);
}

/**
  * @brief Send a no-data command with an explicit instruction encoding. Used by
  *        the flash software-reset, which must be issued in several interface
  *        modes (we don't know which one the flash is currently in).
  */
static void OSPI_SendCommandNoData(OSPI_HandleTypeDef *hospi, uint32_t instruction,
                                   uint32_t instr_mode, uint32_t instr_size,
                                   uint32_t instr_dtr)
{
  OSPI_RegularCmdTypeDef sCommand = {0};
  sCommand.OperationType      = HAL_OSPI_OPTYPE_COMMON_CFG;
  sCommand.FlashId            = HAL_OSPI_FLASH_ID_1;
  sCommand.Instruction        = instruction;
  sCommand.InstructionMode    = instr_mode;
  sCommand.InstructionSize    = instr_size;
  sCommand.InstructionDtrMode = instr_dtr;
  sCommand.AddressMode        = HAL_OSPI_ADDRESS_NONE;
  sCommand.AlternateBytesMode = HAL_OSPI_ALTERNATE_BYTES_NONE;
  sCommand.DataMode           = HAL_OSPI_DATA_NONE;
  sCommand.DummyCycles        = 0;
  sCommand.DQSMode            = HAL_OSPI_DQS_DISABLE;
  sCommand.SIOOMode           = HAL_OSPI_SIOO_INST_EVERY_CMD;
  if (HAL_OSPI_Command(hospi, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief Force the Macronix flash back to 1-line SPI STR, whatever mode it is
  *        currently in. Essential for warm resets: an MCU reset (NRST/debug/SW)
  *        does NOT power-cycle the external flash, so it can still be in octal
  *        mode from the previous boot while our config code assumes SPI. We send
  *        Reset-Enable + Reset in each possible mode (SPI/STR, OPI/STR, OPI/DTR);
  *        only the one matching the current mode is understood. Mirrors the ST
  *        BSP OSPI_NOR_ResetMemory() sequence.
  */
static void OSPI_ResetFlash(OSPI_HandleTypeDef *hospi)
{
  /* Currently in 1-line SPI STR */
  OSPI_SendCommandNoData(hospi, RESET_ENABLE_CMD,       HAL_OSPI_INSTRUCTION_1_LINE,
                         HAL_OSPI_INSTRUCTION_8_BITS,  HAL_OSPI_INSTRUCTION_DTR_DISABLE);
  OSPI_SendCommandNoData(hospi, RESET_MEMORY_CMD,       HAL_OSPI_INSTRUCTION_1_LINE,
                         HAL_OSPI_INSTRUCTION_8_BITS,  HAL_OSPI_INSTRUCTION_DTR_DISABLE);
  /* Currently in 8-line octal STR */
  OSPI_SendCommandNoData(hospi, OCTAL_RESET_ENABLE_CMD, HAL_OSPI_INSTRUCTION_8_LINES,
                         HAL_OSPI_INSTRUCTION_16_BITS, HAL_OSPI_INSTRUCTION_DTR_DISABLE);
  OSPI_SendCommandNoData(hospi, OCTAL_RESET_MEMORY_CMD, HAL_OSPI_INSTRUCTION_8_LINES,
                         HAL_OSPI_INSTRUCTION_16_BITS, HAL_OSPI_INSTRUCTION_DTR_DISABLE);
  /* Currently in 8-line octal DTR */
  OSPI_SendCommandNoData(hospi, OCTAL_RESET_ENABLE_CMD, HAL_OSPI_INSTRUCTION_8_LINES,
                         HAL_OSPI_INSTRUCTION_16_BITS, HAL_OSPI_INSTRUCTION_DTR_ENABLE);
  OSPI_SendCommandNoData(hospi, OCTAL_RESET_MEMORY_CMD, HAL_OSPI_INSTRUCTION_8_LINES,
                         HAL_OSPI_INSTRUCTION_16_BITS, HAL_OSPI_INSTRUCTION_DTR_ENABLE);
  /* Reset recovery (worst case: reset landed during an erase) */
  HAL_Delay(MEMORY_RESET_MAX_DELAY);
}

/**
  * @brief Reusable, non-destructive OSPI bring-up: init the controller, reset the
  *        flash to a known state, and switch it to octal STR mode. After this the
  *        controller is in indirect/command mode (ready for erase/program or for
  *        OSPI_EnableMemoryMapped()).
  */
static void OSPI_Init(void)
{
  MX_OCTOSPI1_Init();         /* controller init (runs HAL_OSPI_MspInit) */
  OSPI_ResetFlash(&hospi1);   /* known starting state, warm-reset safe */
  OSPI_OctalModeCfg(&hospi1); /* flash -> octal STR */
}

/**
  * @brief Configure and enter memory-mapped mode so 0x90000000 is readable in
  *        place (XIP). Pass with_write != 0 to also allow programming via bus
  *        writes (only the self-test needs that); the production weight-read path
  *        uses with_write == 0 (read-only).
  */
static void OSPI_EnableMemoryMapped(int with_write)
{
  OSPI_RegularCmdTypeDef   sCommand = {0};
  OSPI_MemoryMappedTypeDef sMemMappedCfg = {0};

  /* Fields common to the octal-STR read/write configs */
  sCommand.FlashId            = HAL_OSPI_FLASH_ID_1;
  sCommand.InstructionMode    = HAL_OSPI_INSTRUCTION_8_LINES;
  sCommand.InstructionSize    = HAL_OSPI_INSTRUCTION_16_BITS;
  sCommand.InstructionDtrMode = HAL_OSPI_INSTRUCTION_DTR_DISABLE;
  sCommand.AddressMode        = HAL_OSPI_ADDRESS_8_LINES;
  sCommand.AddressSize        = HAL_OSPI_ADDRESS_32_BITS;
  sCommand.AddressDtrMode     = HAL_OSPI_ADDRESS_DTR_DISABLE;
  sCommand.AlternateBytesMode = HAL_OSPI_ALTERNATE_BYTES_NONE;
  sCommand.DataMode           = HAL_OSPI_DATA_8_LINES;
  sCommand.DataDtrMode        = HAL_OSPI_DATA_DTR_DISABLE;
  sCommand.NbData             = 1;
  sCommand.SIOOMode           = HAL_OSPI_SIOO_INST_EVERY_CMD;

  if (with_write)
  {
    sCommand.OperationType = HAL_OSPI_OPTYPE_WRITE_CFG;
    sCommand.Instruction   = OCTAL_PAGE_PROG_CMD;
    sCommand.DummyCycles   = 0;
    sCommand.DQSMode       = HAL_OSPI_DQS_ENABLE;
    if (HAL_OSPI_Command(&hospi1, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
    {
      Error_Handler();
    }
  }

  sCommand.OperationType = HAL_OSPI_OPTYPE_READ_CFG;
  sCommand.Instruction   = OCTAL_IO_READ_CMD;
  sCommand.DummyCycles   = DUMMY_CLOCK_CYCLES_READ;
  sCommand.DQSMode       = HAL_OSPI_DQS_DISABLE;
  if (HAL_OSPI_Command(&hospi1, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }

  sMemMappedCfg.TimeOutActivation = HAL_OSPI_TIMEOUT_COUNTER_ENABLE;
  sMemMappedCfg.TimeOutPeriod     = 0x50;
  if (HAL_OSPI_MemoryMapped(&hospi1, &sMemMappedCfg) != HAL_OK)
  {
    Error_Handler();
  }
}

#if OSPI_XIP_SELFTEST
/**
  * @brief Week-1 destructive proof: erase the first sector, program the known
  *        pattern via memory-mapped writes, and read it back. ERASES the flash,
  *        so it is compiled out (OSPI_XIP_SELFTEST 0) once weights are resident.
  *        Assumes OSPI_Init() already ran; leaves the controller in memory-mapped
  *        mode so the non-secure side can read too.
  */
static void OSPI_XIP_SelfTest(void)
{
  OSPI_RegularCmdTypeDef sCommand = {0};
  volatile uint32_t     *xip = (volatile uint32_t *)OCTOSPI1_BASE;
  volatile uint8_t      *xip_b;
  const uint8_t         *src;
  uint32_t got[4];
  uint32_t i;
  int ok = 1;

  /* Erase the first 4 KB sector (indirect/command mode, octal STR) */
  OSPI_WriteEnable(&hospi1);
  sCommand.OperationType      = HAL_OSPI_OPTYPE_COMMON_CFG;
  sCommand.FlashId            = HAL_OSPI_FLASH_ID_1;
  sCommand.Instruction        = OCTAL_SECTOR_ERASE_CMD;
  sCommand.InstructionMode    = HAL_OSPI_INSTRUCTION_8_LINES;
  sCommand.InstructionSize    = HAL_OSPI_INSTRUCTION_16_BITS;
  sCommand.InstructionDtrMode = HAL_OSPI_INSTRUCTION_DTR_DISABLE;
  sCommand.Address            = 0;
  sCommand.AddressMode        = HAL_OSPI_ADDRESS_8_LINES;
  sCommand.AddressSize        = HAL_OSPI_ADDRESS_32_BITS;
  sCommand.AddressDtrMode     = HAL_OSPI_ADDRESS_DTR_DISABLE;
  sCommand.AlternateBytesMode = HAL_OSPI_ALTERNATE_BYTES_NONE;
  sCommand.DataMode           = HAL_OSPI_DATA_NONE;
  sCommand.DummyCycles        = 0;
  sCommand.DQSMode            = HAL_OSPI_DQS_DISABLE;
  sCommand.SIOOMode           = HAL_OSPI_SIOO_INST_EVERY_CMD;
  if (HAL_OSPI_Command(&hospi1, &sCommand, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
  OSPI_AutoPollingMemReady(&hospi1);
  printf("[S ] sector erase complete.\r\n");

  /* Enter memory-mapped mode with write enabled so we can program via the bus */
  OSPI_WriteEnable(&hospi1);
  OSPI_EnableMemoryMapped(1);

  /* Program the known pattern (16 bytes, within one 256-byte page) */
  xip_b = (volatile uint8_t *)OCTOSPI1_BASE;
  src   = (const uint8_t *)OSPI_Test_Pattern;
  for (i = 0; i < sizeof(OSPI_Test_Pattern); i++)
  {
    xip_b[i] = src[i];
  }
  /* Cannot poll status in memory-mapped mode; wait max page-program time. */
  HAL_Delay(MEMORY_PAGE_PROG_DELAY);

  /* Secure read-back (issued as a NON-secure bus access; see MX_GTZC_S_Init) */
  for (i = 0; i < 4; i++)
  {
    got[i] = xip[i];
    if (got[i] != OSPI_Test_Pattern[i])
    {
      ok = 0;
    }
  }
  printf("[S ] read @0x90000000: 0x%08lX 0x%08lX 0x%08lX 0x%08lX -> %s\r\n",
         (unsigned long)got[0], (unsigned long)got[1],
         (unsigned long)got[2], (unsigned long)got[3],
         ok ? "PASS" : "FAIL");
}
#endif /* OSPI_XIP_SELFTEST */
/* ===================== end Week 1 Phase 3: OSPI XIP proof ==================== */

/* USER CODE BEGIN 4 */
PUTCHAR_PROTOTYPE
{
  /* Place your implementation of fputc here */
  /* e.g. write a character to the USART3 and Loop until the end of transmission */
  HAL_UART_Transmit(&huart1, (uint8_t *)&ch, 1, 0xFFFF);

  return ch;
}

/* USER CODE BEGIN 4 */

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
