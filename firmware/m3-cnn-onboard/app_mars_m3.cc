// M3 on-board harness: SVD r=32 MARS CNN under TFLite Micro on the M33.
// Mirrors the host harness (Quantize MARS Sandbox/mars_host.cc) op-for-op --
// same 5-op resolver, same PASS criterion -- swapping file I/O for the
// embedded test vectors and printf for the Secure console veneer (USART1 is
// Secure-attributed on this project; NS code cannot touch it directly).

#include <cstdint>
#include <cstdio>
#include <cstring>

#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"

#include "app_mars_m3.h"
#include "mars_m10_model.h"
#include "mars_m10_vectors.h"
#include "main.h"         // CMSIS device defs (CoreDebug/DWT) + SystemCoreClock
#include "secure_nsc.h"   // SECURE_print_Log NSC veneer

namespace {

// ARMv8-M (M33) gates DWT register writes behind a lock register that CMSIS
// doesn't consistently name across header versions (this tree's core_cm33.h
// has DWT LSR but no LAR field). The address is architectural (DWT base
// 0xE0001000 + offset 0xFB0), so poke it directly instead of trusting a
// struct field that may not exist.
constexpr uint32_t kDwtLarAddr = 0xE0001FB0UL;
constexpr uint32_t kDwtUnlockKey = 0xC5ACCE55UL;

void DwtInit() {
  CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
  *reinterpret_cast<volatile uint32_t*>(kDwtLarAddr) = kDwtUnlockKey;
  DWT->CYCCNT = 0;
  DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

}  // namespace

extern "C" void Mars_M3_Run(void) {
  char msg[160];

  const tflite::Model* model = tflite::GetModel(mars_m10_model);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    snprintf(msg, sizeof(msg), "[M3] schema mismatch: model=%lu runtime=%d\r\n",
             (unsigned long)model->version(), TFLITE_SCHEMA_VERSION);
    SECURE_print_Log(msg);
    return;
  }

  // Config B's payoff, unchanged by the SVD factorization: exactly these
  // five ops, no Quantize/Dequantize anywhere in the graph.
  static tflite::MicroMutableOpResolver<5> resolver;
  resolver.AddConv2D();
  resolver.AddMaxPool2D();
  resolver.AddReshape();
  resolver.AddFullyConnected();
  resolver.AddLogistic();

  // Board budget per bringup-guide.md S6.5 (x86-measured floor was 13,096 B).
  constexpr size_t kArenaSize = 16 * 1024;
  alignas(16) static uint8_t arena[kArenaSize];
  // static: avoids putting MicroInterpreter's internal state on the stack.
  static tflite::MicroInterpreter interpreter(model, resolver, arena, kArenaSize);

  if (interpreter.AllocateTensors() != kTfLiteOk) {
    SECURE_print_Log("[M3] AllocateTensors FAILED -- arena too small or missing op\r\n");
    return;
  }

  TfLiteTensor* in = interpreter.input(0);
  TfLiteTensor* out = interpreter.output(0);

  if (in->type != kTfLiteInt8 || out->type != kTfLiteInt8 ||
      (size_t)in->bytes != MARS_M10_N_FEAT || (size_t)out->bytes != MARS_M10_N_OUT) {
    snprintf(msg, sizeof(msg), "[M3] tensor shape/type mismatch: in type=%d bytes=%d out type=%d bytes=%d\r\n",
             (int)in->type, (int)in->bytes, (int)out->type, (int)out->bytes);
    SECURE_print_Log(msg);
    return;
  }

  DwtInit();

  int pass = 0;
  uint32_t cycles_total = 0;
  uint32_t cycles_min = 0xFFFFFFFFUL;

  for (int i = 0; i < MARS_M10_N_VEC; ++i) {
    memcpy(in->data.int8, mars_m10_input[i], in->bytes);

    uint32_t t0 = DWT->CYCCNT;
    TfLiteStatus status = interpreter.Invoke();
    uint32_t dt = DWT->CYCCNT - t0;

    if (status != kTfLiteOk) {
      snprintf(msg, sizeof(msg), "[M3] Invoke FAILED on vector %d\r\n", i);
      SECURE_print_Log(msg);
      continue;
    }

    cycles_total += dt;
    if (dt < cycles_min) cycles_min = dt;

    if (memcmp(out->data.int8, mars_m10_expected[i], out->bytes) == 0) {
      ++pass;
    } else {
      snprintf(msg, sizeof(msg), "[M3] vector %d MISMATCH: got [%d %d] want [%d %d]\r\n", i,
               out->data.int8[0], out->data.int8[1],
               mars_m10_expected[i][0], mars_m10_expected[i][1]);
      SECURE_print_Log(msg);
    }
  }

  const float khz = SystemCoreClock / 1000.0f;
  const float ms_avg = (cycles_total / (float)MARS_M10_N_VEC) / khz;
  const float ms_min = cycles_min / khz;

  snprintf(msg, sizeof(msg), "[M3] bit-exact: %d/%d\r\n", pass, MARS_M10_N_VEC);
  SECURE_print_Log(msg);

  snprintf(msg, sizeof(msg), "[M3] tensor arena used: %u bytes (budget %u)\r\n",
           (unsigned)interpreter.arena_used_bytes(), (unsigned)kArenaSize);
  SECURE_print_Log(msg);

  // NOTE: printf's on this toolchain don't reliably support %f without extra
  // linker flags (-u _printf_float) -- report ms as a fixed-point value to
  // sidestep that instead of discovering it live on-target.
  snprintf(msg, sizeof(msg), "[M3] latency: avg %lu.%03lu ms, min %lu.%03lu ms (SystemCoreClock=%lu Hz)\r\n",
           (unsigned long)ms_avg, (unsigned long)((ms_avg - (unsigned long)ms_avg) * 1000.0f),
           (unsigned long)ms_min, (unsigned long)((ms_min - (unsigned long)ms_min) * 1000.0f),
           (unsigned long)SystemCoreClock);
  SECURE_print_Log(msg);
}
