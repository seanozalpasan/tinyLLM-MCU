// Test-bed A · ESP32 — half 4b: SPI slave + Wi-Fi/TCP relay.
//
// Pipeline:  STM32 (SPI3 master) --SPI--> ESP32 (this, SPI slave) --Wi-Fi/TCP--> ncat (laptop).
//
// The STM32 master pulses CS low, clocks ONE 9-byte telemetry frame, then raises
// CS — once per second. We catch each CS-framed transaction as an SPI slave,
// find the 0xA5 0x5A magic, validate the XOR checksum, decode seq/value, and
// forward a human-readable line to the laptop's `ncat -lk -p 9000` listener.
//
//   Frame (9 bytes):  [0]=0xA5  [1]=0x5A  [2..5]=seq u32 LE  [6..7]=value u16 LE
//                     [8]=XOR of bytes 0..7
//
// Magic = frame-sync sentinel (answers "is a real frame starting here, am I aligned?").
// Checksum = content integrity (answers "did the bytes arrive uncorrupted?"). We scan
// the RX buffer for the magic instead of trusting byte 0, so leading idle/garbage or a
// byte-misaligned slave transaction self-corrects on the next valid frame.
//
// Handshake: this board drives HS_OUT_PIN high once it is Wi-Fi+TCP ready. That wire
// goes to the STM32's PD11 (ARD D2), which currently just prints `HS=` — so you can
// watch HS flip 0->1 on the STM32 console the moment the relay path comes up. Re-enable
// real gating on the STM32 side only after you've confirmed HS tracks this pin.
//
// Board: ESP32-WROOM-32D (PlatformIO env esp32dev). Serial monitor: 115200.

#include <Arduino.h>
#include <WiFi.h>
#include <ESP32DMASPISlave.h>

// Wi-Fi + laptop-endpoint config (WIFI_SSID / WIFI_PASS / LAPTOP_IP / LAPTOP_PORT)
// lives in secrets.h, which is gitignored so credentials never enter git history.
#include "secrets.h"

// Diagnostic build switch: 1 = GPIO loopback test (read the SPI signal pins as plain
// inputs to find which wires are electrically live); 0 = normal SPI relay. Pair with
// the STM32 TESTBEDA_MODE_LOOPBACK switch. Flip back to 0 to restore the real pipeline.
#define ESP32_MODE_LOOPBACK 0

// Under ESP32_MODE_LOOPBACK: 1 = REVERSE DRIVE (ESP32 drives the 3 wired SPI pads while the
// STM32 reads them, to test the PB5/MOSI route in the other direction); 0 = original read
// test (ESP32 reads, STM32 drives). Pair with the STM32 TESTBEDA_REVERSE_READBACK switch.
#define ESP32_REVERSE_DRIVE 1

// ---- SPI slave pins (VSPI). STM32 is the master; these are the ESP32 inputs. ----
//   SCK  = GPIO18  <- STM32 SCK  (PG9)
//   MISO = GPIO19  -> STM32 MISO (PB4)  -- NOT wired (TX-only link), declared for the API
//   MOSI = GPIO23  <- STM32 MOSI (PB5)
//   SS   = GPIO22  <- STM32 CS   (PE0 / ARD D10).  Deliberately NOT the VSPI default SS
//                     (GPIO5) -- GPIO5 is a boot strapping pin; GPIO22 is free, so wiring
//                     CS to it can't disturb the ESP32 boot. (GPIO matrix routes any pin.)
static const int PIN_SCK  = 18;
static const int PIN_MISO = 19;
static const int PIN_MOSI = 23;
static const int PIN_SS   = 22;

// Slave-ready handshake OUT -> STM32 PD11 (ARD D2). GPIO21 is not a strapping pin.
static const int HS_OUT_PIN = 21;

// ---- Telemetry frame layout (must match the STM32 Tele_BuildFrame contract) ----
static const uint8_t  TELE_MAGIC0  = 0xA5;
static const uint8_t  TELE_MAGIC1  = 0x5A;
static const size_t   TELE_FRAME_LEN = 9;

// ---- SPI slave (DMA) ----
// BUFFER_SIZE must be a multiple of 4 (DMA). 32 holds one 9-byte frame with slack for
// any leading idle bytes the magic-scan will skip over. QUEUE_SIZE 1 = one frame at a time.
ESP32DMASPI::Slave slave;
static const size_t  BUFFER_SIZE = 32;
static const size_t  QUEUE_SIZE  = 1;
static const uint32_t SPI_TIMEOUT_MS = 2000;  // wake to service Wi-Fi/TCP if no frame arrives
uint8_t* dma_tx_buf = nullptr;
uint8_t* dma_rx_buf = nullptr;

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

// Keep a live TCP connection to the laptop's ncat listener. Returns true when connected.
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
    for (size_t k = 0; k < 8; ++k) {
      x ^= buf[i + k];
    }
    if (x == buf[i + 8]) {
      return (int)i;  // magic located AND payload integrity verified
    }
    // Magic matched but checksum failed: corrupted, or a coincidental 0xA5 0x5A in
    // the data. Keep scanning in case a real frame follows.
  }
  return -1;
}

#if ESP32_MODE_LOOPBACK
#if ESP32_REVERSE_DRIVE
// ---- Reverse-direction drive: the ESP32 DRIVES the 3 wired SPI pads; the STM32 reads them.
// One pad HIGH at a time, held ~2 s, so the STM32 console shows exactly one pin move per
// phase (unambiguous). SAME 3 wires, no rewiring: g18->SCK pad(PG9), g22->CS pad(PB13),
// g23->MOSI pad(PB5). SCK + CS are the positive controls; MOSI/PB5 is the unknown. ----
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[testbedA reverse] ESP32 DRIVES g18/g22/g23; STM32 reads (no SPI/Wi-Fi)");
  pinMode(PIN_SCK,  OUTPUT);  // g18 -> SCK pad  (PG9)
  pinMode(PIN_SS,   OUTPUT);  // g22 -> CS pad   (PB13)
  pinMode(PIN_MOSI, OUTPUT);  // g23 -> MOSI pad (PB5)
  digitalWrite(PIN_SCK,  LOW);
  digitalWrite(PIN_SS,   LOW);
  digitalWrite(PIN_MOSI, LOW);
}

void loop() {
  struct Phase { int sck; int cs; int mosi; const char* label; };
  static const Phase phases[] = {
    {1, 0, 0, "SCK (g18->PG9)  HIGH"},
    {0, 1, 0, "CS  (g22->PB13) HIGH"},
    {0, 0, 1, "MOSI(g23->PB5)  HIGH"},
    {0, 0, 0, "ALL LOW"},
  };
  for (const Phase& p : phases) {
    digitalWrite(PIN_SCK,  p.sck);
    digitalWrite(PIN_SS,   p.cs);
    digitalWrite(PIN_MOSI, p.mosi);
    Serial.printf("driving: %s   -> STM32 should read  SCK=%d CS=%d MOSI=%d\n",
                  p.label, p.sck, p.cs, p.mosi);
    delay(2000);
  }
}
#else
// ---- Diagnostic: read the 3 SPI signal pins as plain GPIO inputs and print them
// (no SPI, no Wi-Fi). Pair with the STM32 loopback switch. SAME 5 wires, no rewiring.
// A pin that follows the STM32's 3-bit counter is alive end-to-end; a pin stuck at 0
// is a dead socket or a broken wire. INPUT_PULLDOWN makes a disconnected line read 0. ----
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[testbedA loopback] ESP32 GPIO read test (no SPI/Wi-Fi)");
  pinMode(PIN_SS,   INPUT_PULLDOWN);  // GPIO22 <- STM32 D10/CS/PE0
  pinMode(PIN_MOSI, INPUT_PULLDOWN);  // GPIO23 <- STM32 D11/MOSI/PB5
  pinMode(PIN_SCK,  INPUT_PULLDOWN);  // GPIO18 <- STM32 D13/SCK/PG9
}

void loop() {
  const int d10 = digitalRead(PIN_SS);
  const int d11 = digitalRead(PIN_MOSI);
  const int d13 = digitalRead(PIN_SCK);
  Serial.printf("D10/CS(g22)=%d  D11/MOSI(g23)=%d  D13/SCK(g18)=%d\n", d10, d11, d13);
  delay(250);
}
#endif  // ESP32_REVERSE_DRIVE
#else
// ---- Real path: UART RX from the STM32 (USART3/PC10 over the mikroBUS UART) + Wi-Fi/TCP
// relay. The STM32 sends the SAME 9-byte frame once/sec; we accumulate UART bytes, scan for
// the 0xA5 0x5A magic + XOR checksum (findValidFrame), decode, print, and forward to ncat.
// Wire change: the data jumper moves from the mikroBUS MOSI pad to the mikroBUS TX pad; the
// ESP32 end stays on g23, now used as UART2 RX. ----
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

  // Decode the validated payload (little-endian, matching the STM32 builder).
  const uint8_t* f = &acc[idx];
  const uint32_t seq   = (uint32_t)f[2] | ((uint32_t)f[3] << 8) |
                         ((uint32_t)f[4] << 16) | ((uint32_t)f[5] << 24);
  const uint16_t value = (uint16_t)f[6] | ((uint16_t)f[7] << 8);
  frames_ok++;

  Serial.printf("UART ok: seq=%lu val=%u (off=%d) [ok=%lu bad=%lu]\n",
                (unsigned long)seq, (unsigned)value, idx,
                (unsigned long)frames_ok, (unsigned long)frames_bad);

  // Forward a decoded, human-readable line to ncat. (Decoded text rather than raw bytes
  // so the listener is verifiable by eye and lines up with the STM32 console; swap to
  // raw payload later when real telemetry replaces the dummy value.)
  if (client.connected()) {
    char line[48];
    const int n = snprintf(line, sizeof(line), "seq=%lu val=%u\n",
                           (unsigned long)seq, (unsigned)value);
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
#endif  // ESP32_MODE_LOOPBACK
