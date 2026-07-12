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
#include <stdio.h>   /* snprintf for console report lines */
#include "nv_logger.h"   /* the NV-region sensor logger (writes the nv_spec.h layout) */
#include "bme280.h"      /* the real sensor: I2C1 PB6/PB7, forced-mode reads */
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* 1 = read back the OSPI XIP test pattern at 0x90000000 at boot. Flips
   TOGETHER with OSPI_XIP_BRINGUP in Secure main.c: with the Secure bring-up
   off, this region is not memory-mapped and a non-secure read of it faults.
   Kept for the parked LLM route; the ML workload never touches external flash. */
#define OSPI_XIP_CHECK  0

/* ---- Test-bed A: outbound telemetry frame (STM32 -> ESP32 over USART3) ----
   Carries the SAME reading the logger just wrote to NV (one source, two sinks),
   CONVERTED to the live display units -- flash keeps the canonical bytes, the
   frame says what the user asked for:
   [A5 5A][seq u32][ts u32][temp i32][hum u32][press u32][units][xor], all LE.
   units: bit0 = temp (0 degC / 1 degF), bit1 = press (0 hPa / 1 inHg), rest 0.
   seq = lifetime record count, so the listener can spot gaps across boots. */
#define TELE_MAGIC0     0xA5u
#define TELE_MAGIC1     0x5Au
#define TELE_FRAME_LEN  24u
#define TELE_UNITS_TEMP_F      0x01u
#define TELE_UNITS_PRESS_INHG  0x02u

/* ---- B2 (USER button, PC13, pressed = HIGH): runtime settings input ----
   Single quick press toggles the temperature unit, double press the pressure
   unit. Gestures register on RELEASE, so enrollment's hold-through-reset can
   never toggle anything (the release is never seen), and a press held past
   BTN_HOLD_MS is swallowed as a hold. Timings use HAL_GetTick; the ~50 ms
   loop cadence samples finely enough for deliberate human presses (a press
   must be held roughly 100-150 ms to register). */
#define BTN_CONFIRM_MS  100u   /* pin HIGH this long = a real press, not chatter */
#define BTN_HOLD_MS     1000u  /* held past this = a hold (enrollment), ignored  */
#define BTN_DOUBLE_MS   400u   /* window after release for a double's 2nd press  */

/* ESP32 -> STM32 reverse-path test frame (proves 2-way UART; the direction the attack
   scenario will later use to feed corrupt data in): [0x5A 0xA5][cnt u32 LE][xor]. */
#define RXTEST_MAGIC0     0x5Au
#define RXTEST_MAGIC1     0xA5u
#define RXTEST_FRAME_LEN  7u

/* Slave-ready handshake from ESP32: PD11 (input). Gating is deferred -- the pin is read
   + printed but does NOT gate transmits yet; re-enable once the ESP32 drives it. */
#define TELE_HS_PORT    GPIOD
#define TELE_HS_PIN     GPIO_PIN_11

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
/* USER CODE BEGIN PV */

static uint8_t  tele_frame[TELE_FRAME_LEN];

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
#if NV_LOGGER   /* these lean on nv_logger symbols that compile out with it */
static void Tele_Convert(const NvReading *r, int32_t *temp, uint32_t *press, uint8_t *units);
static void Tele_BuildFrame(uint8_t *buf, const NvReading *r);
static void Button_Poll(void);
#endif
static void Uart3_Init(void);
static void Uart3_Write(const uint8_t *buf, uint32_t len);
static void Uart3_Poll(void);
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

#if OSPI_XIP_CHECK
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
#endif /* OSPI_XIP_CHECK */

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

  /* The real sensor, initialized exactly once. A failure is loud but
     non-fatal: the logger then skips every record (nothing lands on flash)
     instead of writing garbage, and everything else keeps running. */
  if (BME280_Init() != 0)
  {
    SECURE_print_Log("[NS] BME280 unavailable: records will be skipped until reboot\r\n");
  }
#if BME280_SELFTEST
  /* Compensation parity surface: calibration bytes + trim words +
     raw/compensated vectors, printed for bme280_ref.py to check. */
  BME280_SelfTest();
#endif

#if NV_LOGGER
  /* The benign workload: recover (or clean-start) the NV ring + the display
     units, then let the main loop below log + telemeter on the configured
     record period. */
  NvLogger_Init();

  /* B2/PC13 as the runtime settings input (same input+pulldown config the
     Secure boot check uses). The Secure side hands the pin over only AFTER its
     enrollment read -- GOTCHA: an ungranted NonSecure read silently returns
     zeros, so a missing grant looks like "button never pressed", not an error. */
  {
    GPIO_InitTypeDef gb = {0};
    gb.Pin  = GPIO_PIN_13;
    gb.Mode = GPIO_MODE_INPUT;
    gb.Pull = GPIO_PULLDOWN;
    HAL_GPIO_Init(GPIOC, &gb);
  }
#endif

  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    {
#if NV_LOGGER
      NvReading r;
      Button_Poll();   /* B2 gesture decode: settings toggles land here */
      if (NvLogger_Poll(&r))
      {
        /* Sink 2: the same reading the logger just wrote to NV goes out USART3,
           converted to the live display units (the flash record stays
           canonical). Handshake still deferred: HS is read + printed, not
           gating. Console and frame use the SAME conversion, so the listener
           mirror check is 1:1 by construction. */
        GPIO_PinState hs = HAL_GPIO_ReadPin(TELE_HS_PORT, TELE_HS_PIN);
        int32_t  dtemp;
        uint32_t dpress;
        uint8_t  units;
        char msg[144];

        Tele_BuildFrame(tele_frame, &r);
        Uart3_Write(tele_frame, TELE_FRAME_LEN);

        Tele_Convert(&r, &dtemp, &dpress, &units);
        const uint32_t at = (dtemp < 0) ? (uint32_t)(-dtemp) : (uint32_t)dtemp;

        /* GOTCHA: SECURE_print_Log treats the message as a printf FORMAT string on
           the secure side, so a literal '%' (e.g. "44.95%") is eaten as a bogus
           conversion. Keep veneer messages %-free; RH is in percent by definition. */
        snprintf(msg, sizeof(msg),
                 "[NS] HS=%d op=%lu ts=%lu T=%s%lu.%02lu%s RH=%lu.%02lu P=%lu.%02lu%s tx=OK\r\n",
                 (hs == GPIO_PIN_SET) ? 1 : 0,
                 (unsigned long)r.op, (unsigned long)r.ts,
                 (dtemp < 0) ? "-" : "", (unsigned long)(at / 100u), (unsigned long)(at % 100u),
                 ((units & TELE_UNITS_TEMP_F) != 0u) ? "F" : "C",
                 (unsigned long)(r.hum / 100u), (unsigned long)(r.hum % 100u),
                 (unsigned long)(dpress / 100u), (unsigned long)(dpress % 100u),
                 ((units & TELE_UNITS_PRESS_INHG) != 0u) ? "inHg" : "hPa");
        SECURE_print_Log(msg);
      }
#endif
      /* Drain the ESP32->STM32 reverse channel ~20x/sec regardless of record rate. */
      Uart3_Poll();
      HAL_Delay(50u);
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

#if NV_LOGGER   /* telemetry conversion + B2 gestures ride the logger build */

static void Tele_PutU32(uint8_t *p, uint32_t v)
{
  p[0] = (uint8_t)(v         & 0xFFu);
  p[1] = (uint8_t)((v >> 8)  & 0xFFu);
  p[2] = (uint8_t)((v >> 16) & 0xFFu);
  p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

/* degF x100 from degC x100: (t*9)/5 + 3200, division rounded to NEAREST --
   the divisor 5 is odd so an exact half can't occur, and adding +/-2 before
   C99's truncate-toward-zero division lands on the nearest integer for either
   sign. 23.50 C -> 74.30 F; -40.00 C -> -40.00 F (the real crossover). */
static int32_t Conv_TempF100(int32_t c100)
{
  const int32_t x = c100 * 9;
  return ((x >= 0) ? (x + 2) : (x - 2)) / 5 + 3200;
}

/* inHg x100 from hPa x100 (numerically pascals): the conventional inch of
   mercury is 3386.389 Pa (NIST), so inHg x100 = Pa * 100000 / 3386389, rounded
   to nearest by adding half the divisor; 64-bit because 110000 Pa * 100000
   overflows 32 bits. 1013.25 hPa -> 29.92 inHg (the standard atmosphere). */
static uint32_t Conv_PressInHg100(uint32_t pa)
{
  return (uint32_t)(((uint64_t)pa * 100000ULL + 1693194ULL) / 3386389ULL);
}

/* Convert a canonical reading to the live display units + the frame's units
   byte. The single conversion point for BOTH the frame and the console line,
   so the two can never diverge. */
static void Tele_Convert(const NvReading *r, int32_t *temp, uint32_t *press, uint8_t *units)
{
  const NvSettings st = NvLogger_Settings();
  *temp  = (st.unit_temp  == NV_UNIT_TEMP_F)     ? Conv_TempF100(r->temp)      : r->temp;
  *press = (st.unit_press == NV_UNIT_PRESS_INHG) ? Conv_PressInHg100(r->press) : r->press;
  *units = (uint8_t)(((st.unit_temp  == NV_UNIT_TEMP_F)     ? TELE_UNITS_TEMP_F     : 0u)
                   | ((st.unit_press == NV_UNIT_PRESS_INHG) ? TELE_UNITS_PRESS_INHG : 0u));
}

/**
  * @brief  Build the 24-byte telemetry frame from a logged reading, converted
  *         to the live display units:
  *         [0]=0xA5 [1]=0x5A [2..5]=seq(op) [6..9]=ts [10..13]=temp(i32)
  *         [14..17]=hum [18..21]=press (all LE) [22]=units [23]=XOR of 0..22.
  */
static void Tele_BuildFrame(uint8_t *buf, const NvReading *r)
{
  int32_t  temp;
  uint32_t press;
  uint8_t  units;
  uint8_t  x = 0u;

  Tele_Convert(r, &temp, &press, &units);
  buf[0] = TELE_MAGIC0;
  buf[1] = TELE_MAGIC1;
  Tele_PutU32(&buf[2],  r->op);
  Tele_PutU32(&buf[6],  r->ts);
  Tele_PutU32(&buf[10], (uint32_t)temp);
  Tele_PutU32(&buf[14], r->hum);
  Tele_PutU32(&buf[18], press);
  buf[22] = units;
  for (uint32_t i = 0u; i < TELE_FRAME_LEN - 1u; i++) { x ^= buf[i]; }
  buf[TELE_FRAME_LEN - 1u] = x;
}

/**
  * @brief  B2 gesture decoder, called every main-loop pass (~50 ms). Single
  *         quick press -> toggle temperature unit; double press -> toggle
  *         pressure unit; anything held past BTN_HOLD_MS -> swallowed (that is
  *         enrollment's gesture, or a changed mind). Actions fire on RELEASE
  *         only, so a hold-through-reset never toggles anything. A second
  *         press shorter than BTN_CONFIRM_MS cancels the whole gesture rather
  *         than falling back to a single (toggling the WRONG unit would be
  *         worse than doing nothing).
  */
static void Button_Poll(void)
{
  /* States: 0 idle | 1 confirming 1st press | 2 1st press down |
             3 released, double window open | 4 confirming 2nd press |
             5 2nd press down | 6 over-long press, wait for release. */
  static uint8_t  st = 0u;
  static uint32_t t0 = 0u;   /* tick at the state's reference edge */
  const uint32_t now = HAL_GetTick();
  const int hi = (HAL_GPIO_ReadPin(GPIOC, GPIO_PIN_13) == GPIO_PIN_SET);

  switch (st)
  {
    case 0u:                                    /* idle: wait for a press edge */
      if (hi) { st = 1u; t0 = now; }
      break;
    case 1u:                                    /* chatter filter, 1st press */
      if (!hi)                     { st = 0u; }
      else if (now - t0 >= BTN_CONFIRM_MS) { st = 2u; }
      break;
    case 2u:                                    /* 1st press held (t0 = press edge) */
      if (!hi)                     { st = 3u; t0 = now; }
      else if (now - t0 >= BTN_HOLD_MS)    { st = 6u; }
      break;
    case 3u:                                    /* double window (t0 = release) */
      if (hi)                      { st = 4u; t0 = now; }
      else if (now - t0 >= BTN_DOUBLE_MS)
      {
        st = 0u;
        (void)NvLogger_ToggleTempUnit();        /* single press */
      }
      break;
    case 4u:                                    /* chatter filter, 2nd press */
      if (!hi)                     { st = 0u; } /* blip: cancel the gesture */
      else if (now - t0 >= BTN_CONFIRM_MS) { st = 5u; }
      break;
    case 5u:                                    /* 2nd press held (t0 = press edge) */
      if (!hi)
      {
        st = 0u;
        (void)NvLogger_TogglePressUnit();       /* double press */
      }
      else if (now - t0 >= BTN_HOLD_MS)    { st = 6u; }
      break;
    default:                                    /* 6: swallow the over-long press */
      if (!hi) { st = 0u; }
      break;
  }
}

#endif /* NV_LOGGER */

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

/* The NV-region churn-proof demo that lived here (append-log boot counter +
   setpoints, branch ns-flash_static_proof) has been evolved into the structured
   spec-driven logger in nv_logger.c; its flash primitives moved there with it. */

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
