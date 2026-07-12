/*
 * bme280.c -- BME280 sensor driver (NonSecure): I2C1 on PB6/PB7, forced-mode
 * reads, Bosch factory-trim compensation to the nv_spec.h fixed-point scales.
 *
 * The compensation functions transcribe the datasheet's integer reference code
 * (BST-BME280-DS002 ch. 4.2.3, "rev.1.1") verbatim -- int32 temperature, int64
 * pressure, int32 humidity. Their arithmetic IS the parity contract with
 * offdevice/sensor/bme280_ref.py: do not "simplify" a shift or reorder an
 * operation here without re-running the parity gate.
 *
 * Bus notes: PB6/PB7 reach the fanout board's mikroBUS SCL/SDA (CN10 pins
 * 5/6) through STMod+ pins 7/10 -- a fixed route, NOT behind the PF11/PF12
 * mux. The DK and the breakout both carry I2C pull-ups; the pins are
 * open-drain with no internal pull. The Secure side must grant PB6/PB7 NSEC
 * before any of this runs (pins reset SECURE; ungranted writes are silent).
 */

#include "bme280.h"

#include <stdio.h>

#include "main.h"   /* HAL + the SECURE_print_Log veneer */

/* ===== chip constants (BST-BME280-DS002) ===== */

#define BME_ADDR_LOW    (0x76u << 1)  /* SDO strapped to GND (our wiring) */
#define BME_ADDR_HIGH   (0x77u << 1)  /* SDO strapped to VDDIO            */

#define BME_REG_ID        0xD0u   /* reads 0x60 on a BME280 (0x58 = BMP280!) */
#define BME_REG_RESET     0xE0u
#define BME_REG_CTRLHUM   0xF2u
#define BME_REG_STATUS    0xF3u
#define BME_REG_CTRLMEAS  0xF4u
#define BME_REG_CONFIG    0xF5u
#define BME_REG_DATA      0xF7u   /* burst 0xF7..0xFE: press[3] temp[3] hum[2] */
#define BME_REG_CALIB_A   0x88u   /* calib00..25: dig_T*, dig_P*, dig_H1 at 0xA1 */
#define BME_REG_CALIB_B   0xE1u   /* calib26..32: dig_H2..dig_H6 */

#define BME_ID_VALUE      0x60u
#define BME_RESET_CMD     0xB6u
#define BME_ST_MEASURING  0x08u
#define BME_ST_IM_UPDATE  0x01u

/* Weather-monitoring recipe (datasheet 3.5.1): oversampling x1 everywhere,
   filter off, forced mode per record tick. ctrl_hum only latches on the next
   ctrl_meas write, so CTRLMEAS always goes out after CTRLHUM. */
#define BME_CTRLHUM_X1    0x01u
#define BME_CTRLMEAS_X1_FORCED  0x25u   /* osrs_t=1 osrs_p=1 mode=forced */
#define BME_CONFIG_OFF    0x00u         /* t_sb n/a in forced, filter off, 4-wire */

/* Measurement at x1/x1/x1 typically finishes in ~8 ms (t_measure,max ~9.9 ms,
   datasheet appendix 9.1); 50 ms of 1 ms polls is comfortable margin. */
#define BME_MEASURE_TIMEOUT_MS  50u
#define BME_XFER_TIMEOUT_MS     100u

/* I2C1 kernel clock = PCLK1 = 110 MHz (reset CCIPR1 routing; APB1 is DIV1).
   TIMINGR for ~95 kHz standard mode: PRESC=10 -> 10 MHz time base (100 ns),
   SCLL=0x38 (5.7 us low >= 4.7), SCLH=0x2C (4.5 us high >= 4.0),
   SDADEL=1 (100 ns hold), SCLDEL=3 (400 ns setup >= 250). */
#define BME_I2C_TIMING  0xA0312C38u

/* ===== driver state ===== */

typedef struct
{
  uint16_t T1;  int16_t T2;  int16_t T3;
  uint16_t P1;  int16_t P2;  int16_t P3;  int16_t P4;  int16_t P5;
  int16_t  P6;  int16_t P7;  int16_t P8;  int16_t P9;
  uint8_t  H1;  int16_t H2;  uint8_t H3;  int16_t H4;  int16_t H5;  int8_t H6;
} BmeTrim;

static I2C_HandleTypeDef s_i2c;
static BmeTrim  s_trim;
static uint16_t s_addr;                 /* live 8-bit HAL address, 0 = no chip */
static int32_t  s_t_fine;               /* T -> P/H carrier, per measurement   */
static uint8_t  s_calib_a[26];          /* raw 0x88..0xA1, kept for self-test  */
static uint8_t  s_calib_b[7];           /* raw 0xE1..0xE7                      */

static void bme_print(const char *msg) { SECURE_print_Log((char *)msg); }

/* ===== low-level register access (HAL blocking mode; no IRQs) ===== */

static int bme_read(uint8_t reg, uint8_t *buf, uint16_t len)
{
  return (HAL_I2C_Mem_Read(&s_i2c, s_addr, reg, I2C_MEMADD_SIZE_8BIT,
                           buf, len, BME_XFER_TIMEOUT_MS) == HAL_OK) ? 0 : -1;
}

static int bme_write(uint8_t reg, uint8_t val)
{
  return (HAL_I2C_Mem_Write(&s_i2c, s_addr, reg, I2C_MEMADD_SIZE_8BIT,
                            &val, 1u, BME_XFER_TIMEOUT_MS) == HAL_OK) ? 0 : -1;
}

/* ===== Bosch compensation (datasheet 4.2.3, transcribed verbatim) =====
   adc inputs are the positive raw ADC codes in int32; outputs: T in degC x100,
   P in Pa as unsigned Q24.8, H in RH as unsigned Q22.10. */

static int32_t bme_compensate_T(int32_t adc_T)
{
  int32_t var1, var2, T;
  var1 = ((((adc_T >> 3) - ((int32_t)s_trim.T1 << 1))) * ((int32_t)s_trim.T2)) >> 11;
  var2 = (((((adc_T >> 4) - ((int32_t)s_trim.T1)) * ((adc_T >> 4) - ((int32_t)s_trim.T1))) >> 12) *
          ((int32_t)s_trim.T3)) >> 14;
  s_t_fine = var1 + var2;
  T = (s_t_fine * 5 + 128) >> 8;
  return T;
}

static uint32_t bme_compensate_P(int32_t adc_P)
{
  int64_t var1, var2, p;
  var1 = ((int64_t)s_t_fine) - 128000;
  var2 = var1 * var1 * (int64_t)s_trim.P6;
  var2 = var2 + ((var1 * (int64_t)s_trim.P5) << 17);
  var2 = var2 + (((int64_t)s_trim.P4) << 35);
  var1 = ((var1 * var1 * (int64_t)s_trim.P3) >> 8) + ((var1 * (int64_t)s_trim.P2) << 12);
  var1 = ((((int64_t)1) << 47) + var1) * ((int64_t)s_trim.P1) >> 33;
  if (var1 == 0)
  {
    return 0;   /* avoid exception caused by division by zero */
  }
  p = 1048576 - adc_P;
  p = (((p << 31) - var2) * 3125) / var1;
  var1 = (((int64_t)s_trim.P9) * (p >> 13) * (p >> 13)) >> 25;
  var2 = (((int64_t)s_trim.P8) * p) >> 19;
  p = ((p + var1 + var2) >> 8) + (((int64_t)s_trim.P7) << 4);
  return (uint32_t)p;
}

static uint32_t bme_compensate_H(int32_t adc_H)
{
  int32_t v_x1_u32r;
  v_x1_u32r = (s_t_fine - ((int32_t)76800));
  v_x1_u32r = (((((adc_H << 14) - (((int32_t)s_trim.H4) << 20) - (((int32_t)s_trim.H5) *
              v_x1_u32r)) + ((int32_t)16384)) >> 15) * (((((((v_x1_u32r *
              ((int32_t)s_trim.H6)) >> 10) * (((v_x1_u32r * ((int32_t)s_trim.H3)) >> 11) +
              ((int32_t)32768))) >> 10) + ((int32_t)2097152)) * ((int32_t)s_trim.H2) +
              8192) >> 14));
  v_x1_u32r = (v_x1_u32r - (((((v_x1_u32r >> 15) * (v_x1_u32r >> 15)) >> 7) *
              ((int32_t)s_trim.H1)) >> 4));
  v_x1_u32r = (v_x1_u32r < 0 ? 0 : v_x1_u32r);
  v_x1_u32r = (v_x1_u32r > 419430400 ? 419430400 : v_x1_u32r);
  return (uint32_t)(v_x1_u32r >> 12);
}

/* ===== trim assembly (datasheet Table 16; H4/H5 share the 0xE5 byte) ===== */

static void bme_unpack_trim(void)
{
  const uint8_t *a = s_calib_a;
  const uint8_t *b = s_calib_b;

  s_trim.T1 = (uint16_t)((a[1] << 8) | a[0]);
  s_trim.T2 = (int16_t)((a[3] << 8) | a[2]);
  s_trim.T3 = (int16_t)((a[5] << 8) | a[4]);
  s_trim.P1 = (uint16_t)((a[7] << 8) | a[6]);
  s_trim.P2 = (int16_t)((a[9] << 8) | a[8]);
  s_trim.P3 = (int16_t)((a[11] << 8) | a[10]);
  s_trim.P4 = (int16_t)((a[13] << 8) | a[12]);
  s_trim.P5 = (int16_t)((a[15] << 8) | a[14]);
  s_trim.P6 = (int16_t)((a[17] << 8) | a[16]);
  s_trim.P7 = (int16_t)((a[19] << 8) | a[18]);
  s_trim.P8 = (int16_t)((a[21] << 8) | a[20]);
  s_trim.P9 = (int16_t)((a[23] << 8) | a[22]);
  s_trim.H1 = a[25];                       /* 0xA1 (0xA0 is a skipped byte) */
  s_trim.H2 = (int16_t)((b[1] << 8) | b[0]);
  s_trim.H3 = b[2];
  /* 12-bit signed pair packed around 0xE5: H4 = E4[11:4] + E5.lo[3:0],
     H5 = E6[11:4] + E5.hi[3:0]; the int8 cast sign-extends the top byte
     (matches Bosch's reference driver bit-for-bit). */
  s_trim.H4 = (int16_t)(((int16_t)(int8_t)b[3] * 16) | (int16_t)(b[4] & 0x0Fu));
  s_trim.H5 = (int16_t)(((int16_t)(int8_t)b[5] * 16) | (int16_t)(b[4] >> 4));
  s_trim.H6 = (int8_t)b[6];
}

/* GOTCHA (warm-reset, same class as the OSPI one): an MCU reset does not
   reset the sensor, so a reset landing mid-read leaves the BME280 driving
   SDA low -- the bus is dead and the chip cannot hear a soft-reset command.
   Every capture IS a reset, so an unattended campaign would eventually hit
   this. Standard recovery: clock SCL by hand (up to 9 pulses) until the
   slave releases SDA, then issue a STOP. Runs on bare GPIO before the pins
   switch to their I2C alternate function. */
static void bme_bus_clear(void)
{
  GPIO_InitTypeDef g = {0};
  uint32_t i;

  /* Pins reset to ANALOG mode, where a GPIO read returns 0 -- PB7 must be a
     real input BEFORE the stuck-low check or every boot false-triggers. */
  g.Pin = GPIO_PIN_7;                    /* SDA as plain input for the check */
  g.Mode = GPIO_MODE_INPUT;
  g.Pull = GPIO_NOPULL;
  g.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOB, &g);

  if (HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_7) == GPIO_PIN_SET) { return; }   /* SDA idle high: nothing to do */

  g.Pin = GPIO_PIN_6;                    /* SCL as open-drain GPIO, idle high */
  g.Mode = GPIO_MODE_OUTPUT_OD;
  HAL_GPIO_Init(GPIOB, &g);
  HAL_GPIO_WritePin(GPIOB, GPIO_PIN_6, GPIO_PIN_SET);

  for (i = 0u; i < 9u; i++)
  {
    if (HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_7) == GPIO_PIN_SET) { break; }
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_6, GPIO_PIN_RESET);
    HAL_Delay(1u);
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_6, GPIO_PIN_SET);
    HAL_Delay(1u);
  }

  /* STOP condition (SDA rising while SCL high) resets the slave's protocol
     state machine cleanly. */
  g.Pin = GPIO_PIN_7;
  HAL_GPIO_Init(GPIOB, &g);
  HAL_GPIO_WritePin(GPIOB, GPIO_PIN_7, GPIO_PIN_RESET);
  HAL_Delay(1u);
  HAL_GPIO_WritePin(GPIOB, GPIO_PIN_7, GPIO_PIN_SET);
  HAL_Delay(1u);
  bme_print("[BME] bus: SDA was stuck low, clocked a recovery\r\n");
}

/* ===== public API ===== */

int BME280_Init(void)
{
  GPIO_InitTypeDef g = {0};
  uint8_t id = 0u;
  uint8_t st = 0u;
  uint32_t t0;
  char msg[160];

  /* PB6/PB7 as I2C1 open-drain AF. No internal pull -- the DK's I2C1 bus and
     the breakout both already carry pull-up resistors. */
  __HAL_RCC_GPIOB_CLK_ENABLE();
  bme_bus_clear();
  g.Pin = GPIO_PIN_6 | GPIO_PIN_7;
  g.Mode = GPIO_MODE_AF_OD;
  g.Pull = GPIO_NOPULL;
  g.Speed = GPIO_SPEED_FREQ_LOW;
  g.Alternate = GPIO_AF4_I2C1;
  HAL_GPIO_Init(GPIOB, &g);

  __HAL_RCC_I2C1_CLK_ENABLE();
  __HAL_RCC_I2C1_FORCE_RESET();
  __HAL_RCC_I2C1_RELEASE_RESET();

  s_i2c.Instance = I2C1;
  s_i2c.Init.Timing = BME_I2C_TIMING;
  s_i2c.Init.OwnAddress1 = 0u;
  s_i2c.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
  s_i2c.Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
  s_i2c.Init.OwnAddress2 = 0u;
  s_i2c.Init.OwnAddress2Masks = I2C_OA2_NOMASK;
  s_i2c.Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
  s_i2c.Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;
  if (HAL_I2C_Init(&s_i2c) != HAL_OK
      || HAL_I2CEx_ConfigAnalogFilter(&s_i2c, I2C_ANALOGFILTER_ENABLE) != HAL_OK
      || HAL_I2CEx_ConfigDigitalFilter(&s_i2c, 0u) != HAL_OK)
  {
    bme_print("[BME] ERROR: I2C1 init failed\r\n");
    return -1;
  }

  /* Probe 0x76 (SDO->GND, our wiring) then 0x77, reading the ID register.
     A NACK on both = no chip answering: recheck wiring, then suspect the
     PB6/PB7 NSEC grant (a secure pin fails silently, pad never moves). */
  s_addr = BME_ADDR_LOW;
  if (bme_read(BME_REG_ID, &id, 1u) != 0)
  {
    s_addr = BME_ADDR_HIGH;
    if (bme_read(BME_REG_ID, &id, 1u) != 0)
    {
      s_addr = 0u;
      bme_print("[BME] ERROR: no response at 0x76 or 0x77 (wiring? NSEC grant?)\r\n");
      return -1;
    }
  }
  if (id != BME_ID_VALUE)
  {
    /* 0x58 would mean the breakout carries a BMP280 -- same PCB, no humidity
       channel. That is a hardware swap conversation, not a driver retry. */
    snprintf(msg, sizeof(msg), "[BME] ERROR: chip id=0x%02X, want 0x60 (0x58 = BMP280, no humidity)\r\n",
             (unsigned)id);
    bme_print(msg);
    s_addr = 0u;
    return -1;
  }

  /* Known state every boot: soft reset, then wait for the NVM copy of the
     trim registers to finish (im_update clears; spec 2 ms startup). */
  if (bme_write(BME_REG_RESET, BME_RESET_CMD) != 0) { bme_print("[BME] ERROR: reset write failed\r\n"); return -1; }
  HAL_Delay(3u);
  t0 = HAL_GetTick();
  do
  {
    if (bme_read(BME_REG_STATUS, &st, 1u) != 0) { bme_print("[BME] ERROR: status read failed\r\n"); return -1; }
    if ((HAL_GetTick() - t0) > 20u) { bme_print("[BME] ERROR: NVM copy timeout\r\n"); return -1; }
  } while ((st & BME_ST_IM_UPDATE) != 0u);

  if (bme_read(BME_REG_CALIB_A, s_calib_a, sizeof(s_calib_a)) != 0
      || bme_read(BME_REG_CALIB_B, s_calib_b, sizeof(s_calib_b)) != 0)
  {
    bme_print("[BME] ERROR: trim read failed\r\n");
    return -1;
  }
  bme_unpack_trim();

  /* config is only writable in sleep mode (we are, post-reset); ctrl_hum
     latches on the ctrl_meas write in BME280_Measure. */
  if (bme_write(BME_REG_CONFIG, BME_CONFIG_OFF) != 0
      || bme_write(BME_REG_CTRLHUM, BME_CTRLHUM_X1) != 0)
  {
    bme_print("[BME] ERROR: config write failed\r\n");
    return -1;
  }

  snprintf(msg, sizeof(msg), "[BME] id=0x%02X at addr=0x%02X\r\n",
           (unsigned)id, (unsigned)(s_addr >> 1));
  bme_print(msg);
  return 0;
}

int BME280_Measure(BME280_Sample *s)
{
  uint8_t d[8];
  uint8_t st;
  uint32_t t0;

  if (s_addr == 0u) { return -1; }

  /* Forced mode: one conversion, chip returns to sleep. Writing ctrl_meas
     also latches the ctrl_hum oversampling set at Init. */
  if (bme_write(BME_REG_CTRLMEAS, BME_CTRLMEAS_X1_FORCED) != 0) { return -1; }

  t0 = HAL_GetTick();
  do
  {
    HAL_Delay(1u);
    if (bme_read(BME_REG_STATUS, &st, 1u) != 0) { return -1; }
    if ((HAL_GetTick() - t0) > BME_MEASURE_TIMEOUT_MS) { return -1; }
  } while ((st & BME_ST_MEASURING) != 0u);

  /* One burst for all three channels -- the datasheet requires it (register
     shadowing only guarantees a consistent measurement within a burst). */
  if (bme_read(BME_REG_DATA, d, sizeof(d)) != 0) { return -1; }

  s->up = ((uint32_t)d[0] << 12) | ((uint32_t)d[1] << 4) | ((uint32_t)d[2] >> 4);
  s->ut = ((uint32_t)d[3] << 12) | ((uint32_t)d[4] << 4) | ((uint32_t)d[5] >> 4);
  s->uh = ((uint32_t)d[6] << 8)  |  (uint32_t)d[7];

  /* T first: it produces t_fine, which P and H consume. */
  s->comp_t = bme_compensate_T((int32_t)s->ut);
  s->t_fine = s_t_fine;
  s->comp_p = bme_compensate_P((int32_t)s->up);
  s->comp_h = bme_compensate_H((int32_t)s->uh);

  /* Spec scales, round-to-nearest (mirrored exactly in bme280_ref.py):
     temp is already degC x100; press drops Q24.8 fraction bits to whole Pa;
     hum converts Q22.10 RH to RH x100 (x100/1024 == x25/256). */
  s->temp  = s->comp_t;
  s->press = (s->comp_p + 128u) >> 8;
  s->hum   = (s->comp_h * 25u + 128u) >> 8;
  return 0;
}

#if BME280_SELFTEST

/* Console dump in the exact format offdevice/sensor/bme280_ref.py parses:
   calibration bytes as hex (so Python re-derives the trim words itself and
   cross-checks our assembly), assembled trim words, then raw/compensated/
   spec-scaled vectors. GOTCHA: SECURE_print_Log re-interprets messages as
   printf format strings -- no literal '%' in any of these lines. */
void BME280_SelfTest(void)
{
  char msg[192];
  char hex[64];
  BME280_Sample s;
  uint32_t i, n;

  bme_print("[BME] selftest: begin\r\n");
  if (s_addr == 0u)
  {
    bme_print("[BME] selftest: ABORT (sensor not initialized -- did BME280_Init fail?)\r\n");
    return;
  }

  for (i = 0u; i < sizeof(s_calib_a); i++) { snprintf(&hex[2u * i], 3u, "%02X", (unsigned)s_calib_a[i]); }
  snprintf(msg, sizeof(msg), "[BME] calibA %s\r\n", hex);
  bme_print(msg);
  for (i = 0u; i < sizeof(s_calib_b); i++) { snprintf(&hex[2u * i], 3u, "%02X", (unsigned)s_calib_b[i]); }
  snprintf(msg, sizeof(msg), "[BME] calibB %s\r\n", hex);
  bme_print(msg);

  snprintf(msg, sizeof(msg), "[BME] trimT T1=%u T2=%d T3=%d\r\n",
           (unsigned)s_trim.T1, (int)s_trim.T2, (int)s_trim.T3);
  bme_print(msg);
  snprintf(msg, sizeof(msg), "[BME] trimP P1=%u P2=%d P3=%d P4=%d P5=%d P6=%d P7=%d P8=%d P9=%d\r\n",
           (unsigned)s_trim.P1, (int)s_trim.P2, (int)s_trim.P3, (int)s_trim.P4,
           (int)s_trim.P5, (int)s_trim.P6, (int)s_trim.P7, (int)s_trim.P8, (int)s_trim.P9);
  bme_print(msg);
  snprintf(msg, sizeof(msg), "[BME] trimH H1=%u H2=%d H3=%u H4=%d H5=%d H6=%d\r\n",
           (unsigned)s_trim.H1, (int)s_trim.H2, (unsigned)s_trim.H3,
           (int)s_trim.H4, (int)s_trim.H5, (int)s_trim.H6);
  bme_print(msg);

  for (n = 1u; n <= BME280_SELFTEST_VECTORS; n++)
  {
    if (BME280_Measure(&s) != 0)
    {
      bme_print("[BME] selftest: ABORT (measure failed)\r\n");
      return;
    }
    snprintf(msg, sizeof(msg), "[BME] vec%lu raw ut=%lu up=%lu uh=%lu\r\n",
             (unsigned long)n, (unsigned long)s.ut, (unsigned long)s.up, (unsigned long)s.uh);
    bme_print(msg);
    snprintf(msg, sizeof(msg), "[BME] vec%lu cmp tfine=%ld T=%ld P=%lu H=%lu\r\n",
             (unsigned long)n, (long)s.t_fine, (long)s.comp_t,
             (unsigned long)s.comp_p, (unsigned long)s.comp_h);
    bme_print(msg);
    snprintf(msg, sizeof(msg), "[BME] vec%lu rec temp=%ld hum=%lu press=%lu\r\n",
             (unsigned long)n, (long)s.temp, (unsigned long)s.hum, (unsigned long)s.press);
    bme_print(msg);
    HAL_Delay(1000u);
  }
  bme_print("[BME] selftest: done\r\n");
}

#endif /* BME280_SELFTEST */
