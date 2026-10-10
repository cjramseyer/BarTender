# Inline Keg Usage Monitor Design Guide

This guide describes a Pro-profile integration that measures beer dispensed
from a keg using a Titan Enterprises 800 Series beverage flowmeter and an ESP32. The meter
counts liquid passing through the line; it cannot distinguish a sale from a
line fill, cleaning, or waste.

## Design Overview

Use one flowmeter per keg line. The meter's pulse/frequency output is conditioned
to a safe 3.3 V logic signal, counted by an ESP32, and converted to volume using
the calibration for the exact meter variant. After flow stops for a configured
quiet interval, the ESP32 reports that pour to BarTender's tap-pour API.

```text
Keg and coupler -> Titan 800 Series meter -> beer line -> faucet
                           |
                     pulse output
                           v
                 signal interface -> ESP32 -> Wi-Fi -> BarTender API
```

Install the meter in the direction marked by its manufacturer. Confirm that its
orientation, pressure drop, and cleaning procedure are appropriate for the
beer system before putting the line into service.

## Required Hardware

| Item | Quantity | Requirements |
| --- | ---: | --- |
| Titan Enterprises 800 Series beverage flowmeter | 1 per keg line | Order a variant with a pulse/frequency output. Verify the exact model's wetted materials, pressure and temperature ratings, flow range, cleaning compatibility, and port/fitting standard. |
| ESP32 development board | 1 per meter node | 2.4 GHz Wi-Fi and one interrupt-capable GPIO per meter. The example below uses GPIO 27 for one meter. |
| Output-matched signal interface | 1 per meter output | Select a 3.3 V-safe level shifter or optoisolator for the exact flowmeter output type and voltage. Do not connect an unverified meter output directly to an ESP32 GPIO. |
| Regulated power supplies | As required | Supply the ESP32 and flowmeter at the voltages/currents specified for their exact variants. Do not assume the ESP32 board can power the meter. |
| Low-voltage wiring and connectors | As needed | Use secure, strain-relieved connections and protect them from moisture. |
| Beer-line fittings, seals, and compatible tubing | 1 set per meter | Match the meter ports and existing line standard; use beverage-compatible materials and avoid unnecessary bore reduction. |
| Protective enclosure | 1 per node | Protect the electronics from spills and condensation in a cold, humid environment. |
| Wi-Fi network access | 1 per node | Provide reliable 2.4 GHz coverage at the controller location. |
| Calibration vessel or scale | 1 | Measure known pours to calibrate and validate volume readings. |

Also have food-safe cleaning/sanitizing supplies compatible with the meter and
beer system, and a multimeter or logic probe for checking the supply and
conditioned pulse signal.

## Wiring Diagram

The standard Titan beverage meter lead uses red for supply, blue for the NPN
open-collector output, and black or the cable screen for 0 V. Production variants
can differ; verify the label and wiring sheet for the exact meter before wiring.
See the [Titan beverage meter instruction sheet](https://flowmeters.co.uk/wp-content/uploads/pdfs/tech-data/instruction-sheet-beverage-meter-08-24.pdf).

```text
  Regulated 5 V supply                 ESP32 development board
  +5 V -------------------------------> VIN / 5V input
    |                                   3V3 ---[10 kOhm]---+
    |                                                      |
    +-------------------------------> Meter RED (+V)      +----+----> GPIO 27
  0 V -------------------------------+ Meter BLACK/screen |    |
                          |                    | Meter BLUE (NPN out)
  ESP32 GND --------------------------+--------------------+----+
```

The issue's reference wiring identifies the output as NPN open collector and
uses an external 10 kOhm pull-up to 3.3 V. This keeps the GPIO high level at
3.3 V even when the meter itself is powered from 5 V. The meter supply must meet
the exact variant's minimum voltage (the cited sheet specifies 4.5 V); do not
power it from the ESP32 3.3 V pin. The example uses `INPUT` because the external
pull-up is installed, and counts falling edges. Never pull the signal up to 5 V.
If the ordered output differs from NPN open collector, stop and use an interface
designed for that output rather than this diagram.

## BarTender Integration

1. In **Settings -> Pour Presets**, select **Inline Device** mode.
2. In **Settings -> Inline Devices**, enter a name and select the tap served by
  this flowmeter, then choose **Register Device**.
3. Copy the one-time provisioning details shown by BarTender. Configure the
  device with that device ID, the event and heartbeat URLs, and its bearer
  token. The token is shown only at registration or rotation; BarTender stores
  only its hash.
4. The device is linked to this app instance by the registered device ID and
  token. The server-side registration binds it to one tap; each event follows
  that tap's current keg assignment, so keg changes do not require reflashing
  the device. A tap can have only one enabled inline device.
5. Ensure the device can reach this instance's external API listener on port
  `8110`. Use the address reachable from the device's network, not necessarily
  the address used to open the Home Assistant UI. If configured, the API IP
  allowlist must include the device.

Device requests use their own bearer token and do not need the shared external
API write token. Submit a pour to:

```text
POST http://<bartender-host>:8110/api/inline-devices/<device_id>/events
Authorization: Bearer <device-token>
Content-Type: application/json
```

```json
{
  "event_id": "f40eb8c0-e199-4d83-8df2-232c04ec0370",
  "occurred_at": "2026-10-10T12:34:56Z",
  "amount": 473,
  "unit": "ml",
  "metadata": {"meter": "titan-800"}
}
```

`event_id` must be a UUID and must remain unchanged when retrying the same
event. A retry with the same ID and payload returns success with
`"duplicate": true` without changing inventory again. Reusing an ID with
different event data returns `409 EVENT_ID_CONFLICT`. Timestamps must be
timezone-aware and within the last 30 days (or no more than five minutes in the
future). One event is limited to 20,000 ml; supported units are `ml`, `l`, `oz`,
and `gal`.

Send an authenticated `POST` with `{}` to
`/api/inline-devices/<device_id>/heartbeat` at least once every few minutes. The
Settings list reports **online** for contact in the last five minutes,
**stale** for older contact, and **never connected** before the first request.
Heartbeats do not change keg volume.

The listener uses plain HTTP. Keep it on a trusted private network, do not
forward port 8110 to the public internet, and do not commit device tokens to
source control. Use a TLS-terminating gateway and TLS-capable firmware if
traffic must cross an untrusted network. Rotating a token immediately revokes
the old one; update the device firmware/configuration before rotating in
production. Disabling or revoking a device blocks its events without deleting
historical pour events or replay records.

## ESP32 Arduino Example

The app provides this sketch as a downloadable template in **Settings -> Inline
Devices -> Download ESP32 firmware template**. Replace the network, registered
device ID/token, and calibration placeholders in the downloaded file before
flashing it.

Install the ESP32 Arduino board support package. Replace the network/API values
and `PULSES_PER_LITER` with values for the installation. The pulse constant is
only a placeholder: use the exact meter calibration, then verify it against
measured pours at serving flow rates.

```cpp
#include <Arduino.h>
#include <cstdio>
#include <cstring>
#include <HTTPClient.h>
#include <WiFi.h>
#include <esp_system.h>
#include <time.h>

constexpr uint8_t FLOW_PIN = 27;
constexpr float PULSES_PER_LITER = 4600.0f;  // Reference only; calibrate exact meter.
constexpr uint32_t POUR_QUIET_MS = 1200;
constexpr uint16_t BARTENDER_PORT = 8110;
constexpr uint32_t HEARTBEAT_INTERVAL_MS = 60000;
constexpr uint32_t EVENT_RETRY_INTERVAL_MS = 5000;

const char WIFI_SSID[] = "REPLACE_WITH_WIFI_SSID";
const char WIFI_PASSWORD[] = "REPLACE_WITH_WIFI_PASSWORD";
const char BARTENDER_HOST[] = "192.168.1.20";
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
```

The example assumes the NPN output/pull-up wiring shown above. Change the input
mode and interrupt edge if the ordered sensor variant differs. A retry resends
the same pending event ID and payload, so a lost HTTP response cannot double
count. This sketch keeps one pending event in RAM only: add a nonvolatile event
queue before using it for unattended service, or a power loss can discard the
pending event. While delivery is pending, further flow pulses are accumulated
for the next event. Tune the quiet interval so brief flow pauses do not split a
pour and successive pours are not merged.

## Calibration and Commissioning

1. Verify the meter supply voltage and output signal with test equipment before
   connecting the signal interface to the ESP32.
2. Confirm the device ID/token and listener reachability. Test with a small
  measured pour and verify that the mapped tap's current keg volume decreases.
3. Measure several pours at normal serving rates. Set `PULSES_PER_LITER` from
   the meter documentation, then refine it using the measured volume and pulse
   totals.
4. Check for leaks, abnormal foaming, and unacceptable flow restriction. Verify
   the selected meter tolerates the bar's cleaning/sanitizing process.
5. Disconnect Wi-Fi briefly and confirm queued retries reuse the same event ID.
  Test heartbeat freshness, token rotation, device disable/revoke, and replay
  of an accepted event. Do not use the sample for unattended inventory
  accounting until pending events are durably queued across power loss.