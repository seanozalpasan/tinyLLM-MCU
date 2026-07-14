# M3 — first on-board CNN inference (CNN-quantization poster lane)

Per `MARS 2.0 Docs/Projects/tinyLLM-MCU/guides/bringup-guide.md` S6.5. This is the
CNN-quantization side-lane, **not** the IDS demo path — it shares nothing with
`memAcq`'s Mahalanobis/hash work and must not block it. The M3-0 flash-placement
blocker is already resolved: `mars_m10_svd_r32.tflite` (162,104 B) fits inside
internal flash next to app code, no OSPI route needed.

This folder has everything generated so far:
- `app_mars_m3.h` / `app_mars_m3.cc` — the on-board harness (new, this session)
- `mars_m10_model.h` — the model as a C array (162,104 B, 16-byte aligned)
- `mars_m10_vectors.h` — 8 embedded bit-exact-gate vectors (200 int8 in, 2 int8 out)

Everything below is stuff **you** run — board flashing and toolchain builds
aren't something to hand off blind. Ping me with whatever error output you get
at any step and I'll help bisect it.

## 1. Vendor + build TFLite Micro for the M33 (WSL)

You already have a working host build at `~/tflite-micro` (used for M2's
`mars_host.cc` — `Quantize MARS Sandbox/build_mars_host.sh`). Reuse that same
checkout; this just adds a second, cross-compiled variant of the lib.

```bash
# one-time, if arm-none-eabi-gcc isn't already in WSL's PATH
sudo apt-get update && sudo apt-get install -y gcc-arm-none-eabi
arm-none-eabi-gcc --version   # note the version — compare to CubeIDE's bundled
                               # toolchain (Help > About, or the .cproject) if
                               # you hit link errors later; mismatched newlib
                               # versions are the usual culprit

cd ~/tflite-micro
make -f tensorflow/lite/micro/tools/make/Makefile \
  TARGET=cortex_m_generic \
  TARGET_ARCH=cortex-m33 \
  OPTIMIZED_KERNEL_DIR=cmsis_nn \
  microlite

# find the exact output path -- it varies by TFLM version, don't assume it
find tensorflow/lite/micro/tools/make/gen -name 'libtensorflow-microlite.a'
```

This project's NonSecure `.cproject` is built with **`-mcpu=cortex-m33
-mfpu=fpv5-sp-d16 -mfloat-abi=hard -mthumb`** (confirmed from `.cproject`). If
the TFLM Makefile's autodetected flags for `TARGET_ARCH=cortex-m33` don't
already match, override them explicitly — a float-ABI mismatch between this
static lib and the final CubeIDE link is a silent-corruption risk, not just a
link error, so don't skip this check:

```bash
grep -rn "mfpu\|mfloat-abi\|mcpu" tensorflow/lite/micro/tools/make/targets/cortex_m_generic_makefile.inc
```

Copy the resulting `.a` and the three download dirs it depends on (same as
the host build) somewhere Windows-visible, e.g.:

```bash
mkdir -p /mnt/c/MARS\ 2.0/tinyLLM-MCU/firmware/m3-cnn-onboard/tflm-cortex-m33
cp tensorflow/lite/micro/tools/make/gen/*/lib/libtensorflow-microlite.a \
   /mnt/c/MARS\ 2.0/tinyLLM-MCU/firmware/m3-cnn-onboard/tflm-cortex-m33/
```

You'll also need these on the CubeIDE include path (same three as
`build_mars_host.sh` uses): the `tflite-micro` repo root itself, plus
`tensorflow/lite/micro/tools/make/downloads/{flatbuffers/include,gemmlowp,ruy}`.

## 2. Duplicate the NonSecure project in CubeIDE

Don't touch `memAcq` — copy it. In CubeIDE: right-click
`firmware/memAcq/NonSecure` project → **Copy** → paste as
`memAcq_m3_cnn_NonSecure`. Keep the same Secure project paired (the NSC
veneers, incl. `SECURE_print_Log`, don't need to change).

In the new project:
1. **Enable C++**: Project Properties → C/C++ Build → Settings → make sure a
   C++ toolchain (`arm-none-eabi-g++`) is active, or right-click the project →
   New → check "Convert to C++". `.cc` files need this or they won't compile.
2. Add this folder's `app_mars_m3.cc`, `app_mars_m3.h`, `mars_m10_model.h`,
   `mars_m10_vectors.h` into `Core/Src` / `Core/Inc` (or a new `Core/M3`
   source folder — CubeIDE picks up new folders on refresh).
3. **Include paths** (C/C++ Build → Settings → Includes, for both C and C++
   compilers): the `tflite-micro` repo root, and the three download dirs from
   step 1.
4. **Library**: C/C++ Build → Settings → Libraries — add
   `tensorflow-microlite` and the library search path pointing at
   `tflm-cortex-m33/`.
5. **Compiler flag**: add `-DTF_LITE_STATIC_MEMORY` (matches the host build;
   TFLM needs it to avoid dynamic allocation paths that don't exist here).
6. In `Core/Src/main.c`, inside `/* USER CODE BEGIN Includes */` add
   `#include "app_mars_m3.h"`, and call `Mars_M3_Run();` once early in
   `/* USER CODE BEGIN 2 */` (after the existing init, before the `while(1)`
   loop — this is a one-shot report, not a periodic task).

## 3. Build, flash, read the console

Build in CubeIDE. If it links clean, flash via the on-board ST-LINK (same as
every other `memAcq` variant — §3 of the bringup guide). Open a serial
terminal at **921600 8N1** on the ST-LINK VCP port (USART1 — same console the
rest of the firmware already uses via `SECURE_print_Log`).

Expected output (order matches the harness):

```
[M3] bit-exact: 8/8
[M3] tensor arena used: 13096 bytes (budget 16384)
[M3] latency: avg X.XXX ms, min X.XXX ms (SystemCoreClock=110000000 Hz)
```

**If bit-exact isn't 8/8**: don't chase SIMD noise — CMSIS-NN int8 kernels are
designed bit-exact vs. the TFLM reference kernels (learning-guide R2). A
mismatch is a real bug: rebuild with `OPTIMIZED_KERNEL_DIR=` unset (plain
reference kernels) to bisect whether it's a CMSIS-NN kernel issue or something
in how the model/vectors got embedded (endianness, alignment, wrong array).

**If `AllocateTensors` fails**: the 16 KB arena is generous (x86 floor was
13,096 B) — first suspect the model array wasn't linked at a 16-byte aligned
address (check the `.map` file for `mars_m10_model`'s address), not the arena
size itself.

## 4. What to report (the M3 deliverable — poster row "CNN, on-device, measured")

Three numbers, read straight from the console + the `.map` file:
1. **Flash used** (from the `.map` — should land near 162 KB model + a few KB
   app code, comfortably inside the 252 KB SHA-256-attested region).
2. **RAM / arena high-water** (`tensor arena used` line above).
3. **ms/inference** (the `latency` line above).

Plus the bit-exact verdict (8/8 or explain why not). State the accuracy
caveat every time you show this: the shipped model never learned anything
real (SVD-factored from a constant predictor's random-matrix weights) — these
are engineering numbers, not detection numbers. Predict before you measure:
scaling the 35 s/inference LLM anchor by parameter ratio (~389K/30M) puts
this around ~0.5 s on reference kernels; CMSIS-NN should beat that
meaningfully. Write your prediction down before reading the console output.
