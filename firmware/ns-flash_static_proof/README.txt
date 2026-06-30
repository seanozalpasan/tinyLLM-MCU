WHAT THIS BRANCH PROVES
-----------------------
During normal operation, non-secure flash (Bank 2, 0x08040000..0x0807FFFF)
splits cleanly into two parts:
  * CODE/.rodata (the firmware image) -- byte-for-byte IMMUTABLE at runtime.
  * one small, declared NV region (the top two 2 KB pages) -- the ONLY place
    any runtime change is allowed to land.
The constant fraction is fixed by the page RESERVATION, not by what the program
writes. This is the claim the region-aware flash scanner depends on.

Flash never changes by itself: the only way a flash byte changes is an explicit
flash-controller erase/program of a named address. This demo's firmware issues
those commands for ONLY two pages, so only those two pages change. The boot
counter / setpoint cannot live in SRAM -- SRAM is wiped on every reset, and
these values must survive reset, which requires flash.

THE DEMO
--------
A non-secure app (added behind "#define NV_PROOF_DEMO 1" in the existing
firmware/memAcq/NonSecure/Core/Src/main.c) runs on the UNCHANGED secure
bootloader and prints over the existing secure UART veneer. A hand-rolled
append-log writes only:
  page 126 @ 0x0807F000 -> boot counter  (one 8-byte record every boot)
  page 127 @ 0x0807F800 -> settings log  (one 8-byte setpoint record every 5th boot)
In-place overwrite is impossible here: each 64-bit doubleword has ECC fixed at
program time, so a written doubleword can't be rewritten without erasing its
whole 2 KB page -- hence the append + page-erase (GC) pattern.

FILES
-----
  firmware/memAcq/NonSecure/Core/Src/main.c  - the NV proof (NV_PROOF_DEMO block)
  firmware/ns-flash_static_proof/capture.py  - prints the Bank-2 dump command
  firmware/ns-flash_static_proof/analyze.py  - slices, hashes, diffs, pass/fail
  firmware/ns-flash_static_proof/README.txt  - this file

THE .bin CAPTURES
-----------------
Each capture is all 256 KB of Bank 2, read over SWD. File offset = address -
0x08040000, so:
  0x00000..0x3F000  (0..252 KB)  = CODE/.rodata (the firmware image)
  0x3F000..0x40000  (252..256 KB)= NV region (the two pages); NV_OFFSET = 0x3F000

STEPS TO REPRODUCE  (run from this folder)
------------------------------------------
0. In main.c confirm  #define NV_PROOF_DEMO 1.  Build in CubeIDE: Secure first,
   then NonSecure (NonSecure links the secure veneer; the secure side is NOT
   reflashed). Keep the flash erase mode at "selected sectors", NOT full chip.

1. Erase the two NV pages so the log starts clean (Bank 1 + the rest untouched):
     STM32_Programmer_CLI -c port=SWD mode=UR -e 254 255

2. Flash the NonSecure image (normal CubeIDE Run). It boots once; CoolTerm
   (COM port, 8-N-1 @ 921600) should show:  [NVPROOF] boot=1 setpoint=20 ...

3. Capture the baseline (HOTPLUG = no reset, so no phantom boot):
     STM32_Programmer_CLI -c port=SWD mode=HOTPLUG -u 0x08040000 0x40000 capture_0.bin

4. Tap NRST until the banner reads  boot=4 setpoint=20 , then:
     STM32_Programmer_CLI -c port=SWD mode=HOTPLUG -u 0x08040000 0x40000 capture_1.bin

5. Tap NRST once more -> banner reads  boot=5 setpoint=25  (settings changed), then:
     STM32_Programmer_CLI -c port=SWD mode=HOTPLUG -u 0x08040000 0x40000 capture_2.bin

6. Analyze:
     python analyze.py capture_0.bin capture_1.bin capture_2.bin

EXPECTED (and what we got)
--------------------------
  CODE sha256 IDENTICAL across capture_0/1/2  -> firmware image immutable.
  NV sha256 differs each capture; every differing byte at offset >= 0x3F000.
  cap0 vs cap1: 24 bytes differ, 0x3F008..0x3F01F (3 new boot-counter records)
  cap1 vs cap2: 16 bytes differ, 0x3F020..0x3F80F (1 counter + 1 setpoint record)
  cap0 vs cap2: 40 bytes differ, 0x3F008..0x3F80F
  Constant region = 0x3F000/0x40000 = 98.44% of Bank 2  -> ALL CLAIMS PASS.
These diffs are exactly the 8-byte append-log records the firmware had to write,
at the exact offsets -- the result is predictable from the design, not tuned.

FINDINGS / NOTES
----------------
- Claim confirmed on real hardware: NS flash = static, hashable code + one
  bounded, legitimately-mutable NV region; the bound is set by the reservation.
- Bug found + fixed (cosmetic): an in-same-boot read-back of a freshly-programmed
  doubleword returned stale data via the flash read cache, so the banner showed
  the new setpoint one boot late. The flash WRITE was always correct (the SWD
  dump, which bypasses the CPU, proved it). Fix: report the value we wrote, not a
  read-back. Watch for this in any future on-chip read-after-write of flash.
- A larger NV reservation just moves the split (e.g. 4 pages -> 96.9% constant);
  for the scanner the NV region is a blind spot, so keep it as small as the
  workload needs.

REVERT
------
Set NV_PROOF_DEMO 0 and rebuild for the normal app, or reflash the STM32 to
commit 200224ac1e7a8b8050cbade0725491051f641241. The demo only ever touches Bank 2.
==============================================================================
