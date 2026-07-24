// Test-bed A - ESP32 UART receiver + Wi-Fi/TCP relay.
//
// Pipeline:  STM32 (USART3) --UART--> ESP32 (this) --Wi-Fi/TCP--> ncat (laptop).
//
// The STM32 sends ONE 24-byte telemetry frame per logged sensor record (once per record
// period: 1 s dev / 15 s deploy) over the mikroBUS UART -- the SAME reading its NV logger
// just wrote to flash (one source, two sinks), CONVERTED to the user's display units
// (flash keeps canonical degC/hPa; the frame says what the user asked for, and the units
// byte says which that is). We accumulate UART bytes, find the 0xA5 0x5A magic, validate
// the XOR checksum, decode, and forward a human-readable line to the laptop listener
// (tools/listen.py, or ncat).
//
//   Frame (24 bytes): [0]=0xA5 [1]=0x5A [2..5]=seq u32 (lifetime record count)
//                     [6..9]=ts u32 (s since boot)  [10..13]=temp i32 (x100, unit per [22])
//                     [14..17]=hum u32 (%RH x100)   [18..21]=press u32 (x100, unit per [22])
//                     [22]=units (bit0: 0=degC 1=degF; bit1: 0=hPa 1=inHg)
//                     [23]=XOR of bytes 0..22 -- all little-endian
//
// Magic = frame-sync sentinel ("is a real frame starting here, am I aligned?").
// Checksum = content integrity ("did the bytes arrive uncorrupted?"). We scan the RX
// buffer for the magic instead of trusting byte 0, so leading idle/garbage or a
// byte-misaligned read self-corrects on the next valid frame.
//
// Reverse path (ESP32 -> STM32): a framed counter sent once/sec proves 2-way UART -- the
// direction the attack scenario will later use to feed corrupt data in.
//
// Handshake: this board drives HS_OUT_PIN high once Wi-Fi+TCP is ready. That wire goes to
// the STM32's PD11, which currently just prints `HS=` -- re-enable real gating on the STM32
// side only after you've confirmed HS tracks this pin.
//
// Board: ESP32-WROOM-32D (PlatformIO env esp32dev). Serial monitor: 115200.

#include <Arduino.h>
#include <WiFi.h>

// Wi-Fi + laptop-endpoint config (WIFI_SSID / WIFI_PASS / LAPTOP_IP / LAPTOP_PORT)
// lives in secrets.h, which is gitignored so credentials never enter git history.
#include "secrets.h"

// Slave-ready handshake OUT -> STM32 PD11. GPIO21 is not a strapping pin.
static const int HS_OUT_PIN = 21;

// ---- Telemetry frame layout (must match the STM32 Tele_BuildFrame contract) ----
static const uint8_t  TELE_MAGIC0  = 0xA5;
static const uint8_t  TELE_MAGIC1  = 0x5A;
static const size_t   TELE_FRAME_LEN = 24;
static const uint8_t  TELE_UNITS_TEMP_F     = 0x01;  // units byte, bit0: temp in degF
static const uint8_t  TELE_UNITS_PRESS_INHG = 0x02;  // units byte, bit1: press in inHg

// ---- Networking ----
static WiFiClient client;

// ---- Diagnostics counters ----
static uint32_t frames_ok   = 0;  // magic + checksum both passed
static uint32_t frames_bad  = 0;  // bytes arrived but no valid frame found

static void connectWiFi() {
  Serial.printf("Joining Wi-Fi SSID \"%s\" ", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.print("\nWi-Fi connected. ESP32 IP: ");
  Serial.println(WiFi.localIP());
}

// Keep a live TCP connection to the laptop listener (listen.py / ncat). Returns true when connected.
static bool ensureTcp() {
  if (client.connected()) {
    return true;
  }
  Serial.printf("Connecting to %s:%u ...\n", LAPTOP_IP, LAPTOP_PORT);
  if (!client.connect(LAPTOP_IP, LAPTOP_PORT)) {
    Serial.println("  TCP connect failed; will retry.");
    return false;
  }
  Serial.println("  TCP connected.");
  return true;
}

// Little-endian u32 at p (the STM32 packs every payload field this way).
static uint32_t rdU32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

// Scan [0, n) for a frame whose magic AND XOR checksum both pass.
// Returns the start index of the first valid frame, or -1 if none.
static int findValidFrame(const uint8_t* buf, size_t n) {
  if (n < TELE_FRAME_LEN) {
    return -1;
  }
  for (size_t i = 0; i + TELE_FRAME_LEN <= n; ++i) {
    if (buf[i] != TELE_MAGIC0 || buf[i + 1] != TELE_MAGIC1) {
      continue;  // not a frame start -- keep sliding (this is the magic's whole job)
    }
    uint8_t x = 0;
    for (size_t k = 0; k < TELE_FRAME_LEN - 1; ++k) {
      x ^= buf[i + k];
    }
    if (x == buf[i + TELE_FRAME_LEN - 1]) {
      return (int)i;  // magic located AND payload integrity verified
    }
    // Magic matched but checksum failed: corrupted, or a coincidental 0xA5 0x5A in
    // the data. Keep scanning in case a real frame follows.
  }
  return -1;
}

// ---- UART RX from the STM32 (USART3 over the mikroBUS UART) + Wi-Fi/TCP relay. The STM32
// sends one 24-byte frame per record period (see the file header); we accumulate UART bytes,
// scan for the 0xA5 0x5A magic + XOR checksum (findValidFrame), decode, print, and forward
// to ncat. ----
static const int PIN_UART_RX = 23;     // g23 <- mikroBUS TX pad (STM32 PC10 / USART3_TX)
static const int PIN_UART_TX = 17;     // g17 -> mikroBUS RX pad (STM32 PC11 / USART3_RX)
static uint8_t  acc[64];               // rolling accumulator for incoming UART bytes
static size_t   accLen = 0;

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[testbedA] ESP32 UART RX + Wi-Fi/TCP relay boot");

  // Handshake output: start LOW (not ready) so the STM32 sees HS=0 until we are up.
  pinMode(HS_OUT_PIN, OUTPUT);
  digitalWrite(HS_OUT_PIN, LOW);

  // UART2 RX + TX, 115200 8N1 -- matches the STM32 USART3 config. RX = STM32 telemetry;
  // TX = the reverse path (the direction the attack will later use: ESP32 -> STM32).
  Serial2.begin(115200, SERIAL_8N1, PIN_UART_RX, PIN_UART_TX);
  Serial.println("UART2 started (115200 8N1; RX g23 <- mikroBUS TX, TX g17 -> mikroBUS RX).");

  connectWiFi();
}

void loop() {
  // ---- Reverse-path proof: send a framed counter to the STM32 once/sec (this is the
  // direction the attack will later use to feed corrupt data in). Frame: [5A A5][cnt u32
  // LE][xor]. The STM32 logs "RX ok: cnt=N" -- if N tracks txCount, 2-way UART is proven. ----
  static uint32_t lastTx  = 0;
  static uint32_t txCount = 0;
  if (millis() - lastTx >= 1000) {
    lastTx = millis();
    uint8_t fr[7];
    fr[0] = 0x5A; fr[1] = 0xA5;
    fr[2] = (uint8_t)(txCount & 0xFF);
    fr[3] = (uint8_t)((txCount >> 8) & 0xFF);
    fr[4] = (uint8_t)((txCount >> 16) & 0xFF);
    fr[5] = (uint8_t)((txCount >> 24) & 0xFF);
    fr[6] = fr[0] ^ fr[1] ^ fr[2] ^ fr[3] ^ fr[4] ^ fr[5];
    Serial2.write(fr, sizeof(fr));
    Serial.printf("-> STM32: sent cnt=%lu\n", (unsigned long)txCount);
    txCount++;
  }

  // Keep Wi-Fi up across hotspot blips.
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi dropped; reconnecting...");
    digitalWrite(HS_OUT_PIN, LOW);  // not ready while the link is down
    connectWiFi();
  }

  // Drive the slave-ready handshake from the actual relay state: high only when the
  // laptop TCP path is live. The STM32 prints this as HS= (gating re-enabled later).
  const bool ready = ensureTcp();
  digitalWrite(HS_OUT_PIN, ready ? HIGH : LOW);

  // Drain whatever UART bytes have arrived into the accumulator.
  while (Serial2.available() > 0 && accLen < sizeof(acc)) {
    acc[accLen++] = (uint8_t)Serial2.read();
  }
  if (accLen < TELE_FRAME_LEN) {
    delay(5);
    return;  // not a full frame's worth of bytes yet
  }

  const int idx = findValidFrame(acc, accLen);
  if (idx < 0) {
    // No frame yet. If the buffer is full of junk, slide off the oldest half so a frame
    // that starts later can still align (the magic-scan self-corrects).
    if (accLen == sizeof(acc)) {
      const size_t keep = sizeof(acc) / 2;
      memmove(acc, acc + (sizeof(acc) - keep), keep);
      accLen = keep;
      frames_bad++;
      Serial.printf("UART: no valid frame in buffer (magic/xor) [bad=%lu]\n",
                    (unsigned long)frames_bad);
    }
    delay(5);
    return;
  }

  // Decode the validated payload (little-endian, matching the STM32 builder). temp/press
  // arrive already converted to the display units the units byte names.
  const uint8_t* f = &acc[idx];
  const uint32_t seq   = rdU32(&f[2]);
  const uint32_t ts    = rdU32(&f[6]);
  const int32_t  temp  = (int32_t)rdU32(&f[10]);  // x100, degC or degF per units
  const uint32_t hum   = rdU32(&f[14]);           // %RH x100 (no unit setting)
  const uint32_t press = rdU32(&f[18]);           // x100, hPa or inHg per units
  const uint8_t  units = f[22];
  const char* tUnit = (units & TELE_UNITS_TEMP_F)     ? "F"    : "C";
  const char* pUnit = (units & TELE_UNITS_PRESS_INHG) ? "inHg" : "hPa";
  frames_ok++;

  Serial.printf("UART ok: seq=%lu ts=%lu T=%.2f%s RH=%.2f%% P=%.2f%s (off=%d) [ok=%lu bad=%lu]\n",
                (unsigned long)seq, (unsigned long)ts,
                temp / 100.0, tUnit, hum / 100.0, press / 100.0, pUnit, idx,
                (unsigned long)frames_ok, (unsigned long)frames_bad);

  // Forward a decoded, human-readable line to ncat -- verifiable by eye against the STM32
  // console line (same values, same units). (Display floats only; the wire stays
  // fixed-point integer, and the flash record stays canonical degC/hPa regardless.)
  if (client.connected()) {
    char line[96];
    const int n = snprintf(line, sizeof(line), "seq=%lu ts=%lu T=%.2f%s RH=%.2f P=%.2f%s\n",
                           (unsigned long)seq, (unsigned long)ts,
                           temp / 100.0, tUnit, hum / 100.0, press / 100.0, pUnit);
    client.write(reinterpret_cast<const uint8_t*>(line), (size_t)n);
  }

  // Consume through the end of this frame; keep any trailing bytes for the next scan.
  const size_t consumed = (size_t)idx + TELE_FRAME_LEN;
  const size_t remain   = accLen - consumed;
  if (remain > 0) {
    memmove(acc, acc + consumed, remain);
  }
  accLen = remain;
}
