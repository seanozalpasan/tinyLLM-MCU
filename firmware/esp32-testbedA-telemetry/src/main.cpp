// Test-bed A · ESP32 — half 4a: Wi-Fi station + TCP client (no SPI yet).
//
// Joins the configured Wi-Fi hotspot, opens a TCP connection to the laptop's `ncat`
// listener, and sends one canned, sequence-numbered line per second. This
// proves the Wi-Fi + IP + firewall path end-to-end BEFORE SPI is introduced
// (half 4b). Once this works, 4b only adds the SPI-slave receive path and
// swaps the canned string for the bytes received from the STM32.
//
// Board: ESP32-WROOM-32D (PlatformIO env esp32dev). Serial monitor: 115200.

#include <Arduino.h>
#include <WiFi.h>

// Wi-Fi + laptop-endpoint config (WIFI_SSID / WIFI_PASS / LAPTOP_IP /
// LAPTOP_PORT) lives in secrets.h, which is gitignored so credentials never
// enter git history.
#include "secrets.h"

static WiFiClient client;
static uint32_t   seq = 0;

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

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[testbedA 4a] ESP32 Wi-Fi + TCP client boot");
  connectWiFi();
}

void loop() {
  // Keep Wi-Fi up across hotspot blips.
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi dropped; reconnecting...");
    connectWiFi();
  }

  // Ensure a live TCP connection to the laptop's ncat listener.
  if (!client.connected()) {
    Serial.printf("Connecting to %s:%u ...\n", LAPTOP_IP, LAPTOP_PORT);
    if (!client.connect(LAPTOP_IP, LAPTOP_PORT)) {
      Serial.println("  TCP connect failed; retrying in 2 s");
      delay(2000);
      return;
    }
    Serial.println("  TCP connected.");
  }

  // One canned line per second — this is what should appear in the ncat window.
  // The seq number lets you confirm liveness and ordering at the listener.
  String line = "habibi from esp32 seq=" + String(seq++) + "\n";
  client.print(line);
  Serial.print("sent: " + line);

  delay(1000);
}
