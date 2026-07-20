/*
 * bme280.h -- BME280 sensor driver (NonSecure): I2C1 on PB6/PB7, forced-mode
 * reads, Bosch factory-trim compensation to the nv_spec.h fixed-point scales.
 *
 * Sits between the physical sensor and the NV logger: NvLogger's record tick
 * calls BME280_Measure() and stores the spec-scaled outputs (temp degC x100,
 * hum RH x100, press Pa == hPa x100). The compensation arithmetic here is
 * PARITY-SENSITIVE: offdevice/sensor/bme280_ref.py re-runs the identical
 * integer math on the self-test's printed raw values and must reproduce every
 * output integer-for-integer before any campaign data is collected.
 */
#ifndef BME280_H
#define BME280_H

#include <stdint.h>

/* 1 = at boot, probe the chip, dump ID + calibration bytes + trim words, then
   print BME280_SELFTEST_VECTORS raw/compensated measurement vectors -- the
   console text offdevice/sensor/bme280_ref.py checks parity against. Costs a
   few seconds of boot time and console noise; 0 for campaign/deploy builds.
   Flip back to 1 (one boot) whenever a breakout is swapped: trim is per-die,
   so a replacement sensor must re-pass the parity gate before its data counts. */
#define BME280_SELFTEST          0
#define BME280_SELFTEST_VECTORS  5u

/* One measurement, every intermediate exposed so the self-test can print the
   full raw -> compensated -> spec-scaled chain (the parity surface). */
typedef struct
{
  uint32_t ut;        /* raw temperature ADC (20-bit)                  */
  uint32_t up;        /* raw pressure ADC (20-bit)                     */
  uint32_t uh;        /* raw humidity ADC (16-bit)                     */
  int32_t  t_fine;    /* Bosch fine-resolution temperature carrier     */
  int32_t  comp_t;    /* compensated temperature, degC x100 (Bosch T)  */
  uint32_t comp_p;    /* compensated pressure, Pa in Q24.8 (Bosch P)   */
  uint32_t comp_h;    /* compensated humidity, RH in Q22.10 (Bosch H)  */
  int32_t  temp;      /* spec scale: degC x100 (== comp_t)             */
  uint32_t hum;       /* spec scale: RH x100                           */
  uint32_t press;     /* spec scale: Pa (== hPa x100)                  */
} BME280_Sample;

/* Bring up I2C1 (PB6 SCL / PB7 SDA, 100 kHz), soft-reset the chip, verify the
   chip ID (0x60), read the factory trim once, and configure oversampling x1 on
   all channels with the IIR filter off (the datasheet's "weather monitoring"
   recipe; the chip then sleeps between forced measurements). Prints its own
   [BME] console lines. Returns 0 on success, -1 on any failure (no chip, wrong
   ID, bus fault) -- the caller decides whether to proceed without a sensor. */
int BME280_Init(void);

/* Trigger one forced measurement, wait for completion, burst-read the raw
   ADC values and fill *s with the full compensation chain. Returns 0 on
   success, -1 on a bus fault or measurement timeout. */
int BME280_Measure(BME280_Sample *s);

/* Mid-run bus heal: re-runs the full bring-up (stuck-SDA bus clear, I2C1
   force-reset, re-probe, soft reset, trim re-read -- same die, same trim).
   A glitch landing inside a transfer can leave the chip holding SDA low,
   and boot is otherwise the only place that state gets cleared. Idempotent
   on a healthy bus; ~30 ms. Returns 0 on success, -1 if the sensor is
   still unreachable (caller retries later). */
int BME280_Recover(void);

#if BME280_SELFTEST
/* Calibration dump + measurement vectors, printed in the exact line format
   bme280_ref.py parses. Call AFTER BME280_Init (init happens exactly once, in
   main.c); if init failed or never ran, this prints an abort note and returns. */
void BME280_SelfTest(void);
#endif

#endif /* BME280_H */
