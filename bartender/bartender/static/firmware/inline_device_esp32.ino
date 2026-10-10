#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <cstdio>
#include <cstring>
#include <esp_system.h>
#include <time.h>

constexpr uint8_t FLOW_PIN = 27;
constexpr float PULSES_PER_LITER = 4600.0f;  // Reference only; calibrate the exact meter.
constexpr uint32_t POUR_QUIET_MS = 1200;
constexpr uint16_t BARTENDER_PORT = 8110;
constexpr uint32_t HEARTBEAT_INTERVAL_MS = 60000;
constexpr uint32_t EVENT_RETRY_INTERVAL_MS = 5000;

const char WIFI_SSID[] = "REPLACE_WITH_WIFI_SSID";
const char WIFI_PASSWORD[] = "REPLACE_WITH_WIFI_PASSWORD";
const char BARTENDER_HOST[] = "REPLACE_WITH_BARTENDER_HOST";
const char DEVICE_ID[] = "REPLACE_WITH_REGISTERED_DEVICE_ID";
// Paste the one-time Bearer token from Settings -> Inline Devices between these quotes.
const char DEVICE_TOKEN[] = "PASTE_DEVICE_TOKEN_HERE";

portMUX_TYPE pulseMux = portMUX_INITIALIZER_UNLOCKED;
volatile uint32_t pulseCount = 0;

struct PendingPour {
  bool valid = false;
  uint32_t pulses = 0;
  String eventId;
  String occurredAt;
};

PendingPour pendingPour;
uint32_t nextHeartbeatMs = 0;
uint32_t nextRetryMs = 0;

void IRAM_ATTR countPulse() {
  portENTER_CRITICAL_ISR(&pulseMux);
  ++pulseCount;
  portEXIT_CRITICAL_ISR(&pulseMux);
}

bool takeCompletedPour(uint32_t &completedPulses) {
  static uint32_t observedPulseCount = 0;
  static uint32_t lastPulseChangeMs = 0;
  const uint32_t now = millis();

  portENTER_CRITICAL(&pulseMux);
  const uint32_t currentPulseCount = pulseCount;

  if (currentPulseCount != observedPulseCount) {
    observedPulseCount = currentPulseCount;
    lastPulseChangeMs = now;
  }

  const bool pourEnded = currentPulseCount > 0 &&
                         now - lastPulseChangeMs >= POUR_QUIET_MS;
  if (pourEnded) {
    completedPulses = currentPulseCount;
    pulseCount = 0;
    observedPulseCount = 0;
    lastPulseChangeMs = 0;
  }
  portEXIT_CRITICAL(&pulseMux);

  return pourEnded;
}

String makeEventId() {
  uint8_t bytes[16];
  for (size_t index = 0; index < sizeof(bytes); index += sizeof(uint32_t)) {
    const uint32_t randomValue = esp_random();
    memcpy(bytes + index, &randomValue, sizeof(randomValue));
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  char value[37];
  snprintf(value, sizeof(value),
           "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
           bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5],
           bytes[6], bytes[7], bytes[8], bytes[9], bytes[10], bytes[11],
           bytes[12], bytes[13], bytes[14], bytes[15]);
  return String(value);
}

String utcTimestamp() {
  const time_t now = time(nullptr);
  if (now < 1700000000) return "";
  struct tm utc;
  gmtime_r(&now, &utc);
  char value[25];
  strftime(value, sizeof(value), "%Y-%m-%dT%H:%M:%SZ", &utc);
  return String(value);
}

bool postDeviceRequest(const String &path, const String &body) {
  WiFiClient client;
  HTTPClient http;
  const String url = String("http://") + BARTENDER_HOST + ":" +
                     String(BARTENDER_PORT) + path;
  if (!http.begin(client, url)) return false;
  http.setTimeout(5000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);
  const int status = http.POST(body);
  const bool accepted = status >= 200 && status < 300;
  if (!accepted) {
    Serial.printf("Device request failed with HTTP %d: %s\n", status,
                  http.getString().c_str());
  }
  http.end();
  return accepted;
}

bool reportPendingPour() {
  if (PULSES_PER_LITER <= 0.0f) {
    Serial.println("Invalid PULSES_PER_LITER calibration.");
    return false;
  }

  const float amountMl = (pendingPour.pulses * 1000.0f) / PULSES_PER_LITER;
  const String path = String("/api/inline-devices/") + DEVICE_ID + "/events";
  const String body = String("{\"event_id\":\"") + pendingPour.eventId +
                      "\",\"occurred_at\":\"" + pendingPour.occurredAt +
                      "\",\"amount\":" + String(amountMl, 2) +
                      ",\"unit\":\"ml\",\"metadata\":{\"meter\":\"titan-800\"}}";
  const bool accepted = postDeviceRequest(path, body);
  if (accepted) {
    Serial.printf("Recorded event %s: %lu pulses (%.2f ml).\n",
                  pendingPour.eventId.c_str(),
                  static_cast<unsigned long>(pendingPour.pulses), amountMl);
    pendingPour = PendingPour{};
  }
  return accepted;
}

void sendHeartbeat() {
  const String path = String("/api/inline-devices/") + DEVICE_ID + "/heartbeat";
  postDeviceRequest(path, "{}");
}

void setup() {
  Serial.begin(115200);
  pinMode(FLOW_PIN, INPUT);  // External 10 kOhm pull-up to 3.3 V.
  attachInterrupt(digitalPinToInterrupt(FLOW_PIN), countPulse, FALLING);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  Serial.println("Inline keg monitor starting.");
}

void loop() {
  const uint32_t now = millis();
  if (WiFi.status() == WL_CONNECTED && now >= nextHeartbeatMs) {
    sendHeartbeat();
    nextHeartbeatMs = now + HEARTBEAT_INTERVAL_MS;
  }

  if (!pendingPour.valid && utcTimestamp().length() > 0) {
    uint32_t completedPulses = 0;
    if (takeCompletedPour(completedPulses)) {
      pendingPour.valid = true;
      pendingPour.pulses = completedPulses;
      pendingPour.eventId = makeEventId();
      pendingPour.occurredAt = utcTimestamp();
      nextRetryMs = 0;
    }
  }

  if (pendingPour.valid && WiFi.status() == WL_CONNECTED && now >= nextRetryMs) {
    if (!reportPendingPour()) {
      nextRetryMs = now + EVENT_RETRY_INTERVAL_MS;
    }
  }

  delay(10);
}