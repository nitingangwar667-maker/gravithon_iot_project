#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <TinyGPSPlus.h>

// Configure these for the Wi-Fi network shared by the ESP32 and Flask server.
const char* WIFI_SSID = "Govind";
const char* WIFI_PASSWORD = "govind123";
const char* BACKEND_BASE = "http://10.141.177.243:5000";
const char* VEHICLE_ID = "IND-PB-9842";

const double DEFAULT_LAT = 30.68114;
const double DEFAULT_LON = 76.60584;

#define SMOKE_PIN 34
#define FLAME_PIN 25
#define TOUCH_ABORT 4
#define BUZZER_PIN 18
#define GPS_RX_PIN 16
#define GPS_TX_PIN 17

const float IMPACT_G_THRESHOLD = 3.0;
// The current clean-air baseline is about 985 ADC. Keep this safely above it;
// recalibrate if your clean-air reading changes after the MQ sensor warms up.
const int SMOKE_ALERT_LEVEL = 1300;
const unsigned long LIVE_STREAM_INTERVAL_MS = 1000;
const unsigned long INCIDENT_COOLDOWN_MS = 3000;
// Keeps the dashboard active if the MPU6050 is absent. This is display-only;
// simulated values never trigger a collision alert.
const bool SIMULATE_IDLE_MOTION_WITHOUT_MPU = true;

Adafruit_MPU6050 mpu;
TinyGPSPlus gps;
HardwareSerial gpsSerial(2);
bool mpuReady = false;
bool inIncidentState = false;
unsigned long lastStreamTime = 0;
unsigned long lastIncidentTime = 0;

const char* wifiStatusText(wl_status_t status) {
  switch (status) {
    case WL_NO_SSID_AVAIL: return "network not found";
    case WL_CONNECT_FAILED: return "authentication failed";
    case WL_CONNECTION_LOST: return "connection lost";
    case WL_DISCONNECTED: return "disconnected";
    default: return "timed out";
  }
}

void scanI2C() {
  Serial.println("[I2C] Scanning addresses...");
  int devices = 0;
  for (byte address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      Serial.printf("[I2C] Device found at 0x%02X\n", address);
      devices++;
    }
  }
  if (devices == 0) Serial.println("[I2C] No devices found. Check VCC, GND, SDA, and SCL wiring.");
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("[WIFI] Connecting to %s", WIFI_SSID);
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 15000) {
    delay(400);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WIFI] Connected. ESP32 IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.printf("\n[WIFI] Connection failed: %s (%d). Retrying in the background.\n", wifiStatusText(WiFi.status()), WiFi.status());
  }
}

bool postJson(const char* path, const String& payload) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  http.setConnectTimeout(3000);
  http.setTimeout(5000);
  http.begin(String(BACKEND_BASE) + path);
  http.addHeader("Content-Type", "application/json");
  int httpCode = http.POST(payload);
  if (httpCode < 200 || httpCode >= 300) {
    Serial.printf("[HTTP] POST %s failed: %d\n", path, httpCode);
    http.end();
    return false;
  }
  http.end();
  return true;
}

float simulatedIdleGForce() {
  // Normal, stationary vehicles read close to 1 G due to gravity. Add only
  // slight variation and keep it safely below the collision threshold.
  static float gForce = 1.0;
  gForce += random(-3, 4) / 100.0f;
  return constrain(gForce, 0.94f, 1.08f);
}

void setup() {
  Serial.begin(115200);
  randomSeed((uint32_t)ESP.getEfuseMac());
  delay(500);
  Serial.println("\nSafeDrive ESP32 starting");
  Serial.println("Set Serial Monitor to 115200 baud.");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  pinMode(FLAME_PIN, INPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
  gpsSerial.begin(9600, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);

  Wire.begin(21, 22);
  scanI2C();
  mpuReady = mpu.begin(0x68, &Wire) || mpu.begin(0x69, &Wire);
  if (mpuReady) {
    mpu.setAccelerometerRange(MPU6050_RANGE_16_G);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
    Serial.println("[MPU] MPU6050 online.");
  } else {
    Serial.println("[MPU] Not detected; using simulated idle G-force for dashboard only. Collision detection is disabled.");
  }
  connectWiFi();
}

bool driverPressedAbort() {
  // GPIO 4 is ESP32 touch channel T0. A lower value indicates touch.
  return touchRead(TOUCH_ABORT) < 40;
}

void updateGps() {
  while (gpsSerial.available() > 0) gps.encode(gpsSerial.read());
}

void postLiveTelemetry(float gForce, int smoke, bool flame, double lat, double lon, int satellites) {
  String payload = "{\"vehicle_id\":\"" + String(VEHICLE_ID) + "\",";
  payload += "\"g_force\":" + String(gForce, 2) + ",";
  payload += "\"smoke_adc\":" + String(smoke) + ",";
  payload += "\"flame_detected\":" + String(flame ? "true" : "false") + ",";
  payload += "\"latitude\":" + String(lat, 6) + ",";
  payload += "\"longitude\":" + String(lon, 6) + ",";
  payload += "\"satellites\":" + String(satellites) + "}";
  postJson("/api/live_stream", payload);
}

bool runVerificationWindow() {
  unsigned long started = millis();
  while (millis() - started < 10000) {
    int secondsLeft = 10 - ((millis() - started) / 1000);
    Serial.printf("[VERIFY] Driver abort window: %d seconds\n", secondsLeft);
    digitalWrite(BUZZER_PIN, HIGH);
    delay(60);
    digitalWrite(BUZZER_PIN, LOW);
    if (driverPressedAbort()) {
      postJson("/api/telemetry", "{\"status\":\"aborted\",\"vehicle_id\":\"" + String(VEHICLE_ID) + "\"}");
      Serial.println("[ABORT] Driver cancelled dispatch.");
      return false;
    }
    delay(900);
  }
  return true;
}

void sendIncident(const String& reason, float gForce, int smoke, double lat, double lon) {
  String payload = "{\"vehicle_id\":\"" + String(VEHICLE_ID) + "\",";
  payload += "\"status\":\"confirmed\",";
  payload += "\"event\":\"" + reason + "\",";
  payload += "\"impact_g\":" + String(gForce, 2) + ",";
  payload += "\"smoke_val\":" + String(smoke) + ",";
  payload += "\"latitude\":" + String(lat, 6) + ",";
  payload += "\"longitude\":" + String(lon, 6) + "}";
  if (postJson("/api/telemetry", payload)) Serial.println("[SOS] Incident sent to SafeDrive server.");
}

void executeIncidentPipeline(const String& reason, float gForce, int smoke, double lat, double lon) {
  inIncidentState = true;
  Serial.printf("[INCIDENT] %s (%.2f G). Starting verification.\n", reason.c_str(), gForce);
  String pendingPayload = "{\"vehicle_id\":\"" + String(VEHICLE_ID) + "\",";
  pendingPayload += "\"status\":\"pending\",";
  pendingPayload += "\"event\":\"" + reason + "\",";
  pendingPayload += "\"impact_g\":" + String(gForce, 2) + ",";
  pendingPayload += "\"smoke_val\":" + String(smoke) + ",";
  pendingPayload += "\"latitude\":" + String(lat, 6) + ",";
  pendingPayload += "\"longitude\":" + String(lon, 6) + "}";
  postJson("/api/telemetry", pendingPayload);
  if (runVerificationWindow()) sendIncident(reason, gForce, smoke, lat, lon);
  lastIncidentTime = millis();
  inIncidentState = false;
}

void loop() {
  updateGps();
  if (WiFi.status() != WL_CONNECTED) connectWiFi();

  float currentG = 1.0;
  if (mpuReady) {
    sensors_event_t acceleration, gyro, temperature;
    mpu.getEvent(&acceleration, &gyro, &temperature);
    currentG = sqrt(sq(acceleration.acceleration.x) + sq(acceleration.acceleration.y) + sq(acceleration.acceleration.z)) / 9.80665;
  } else if (SIMULATE_IDLE_MOTION_WITHOUT_MPU) {
    currentG = simulatedIdleGForce();
  }
  int rawSmoke = analogRead(SMOKE_PIN);
  bool firePresent = digitalRead(FLAME_PIN) == LOW;
  double lat = gps.location.isValid() ? gps.location.lat() : DEFAULT_LAT;
  double lon = gps.location.isValid() ? gps.location.lng() : DEFAULT_LON;
  int satellites = gps.satellites.isValid() ? gps.satellites.value() : 0;

  if (!inIncidentState && millis() - lastStreamTime >= LIVE_STREAM_INTERVAL_MS) {
    lastStreamTime = millis();
    postLiveTelemetry(currentG, rawSmoke, firePresent, lat, lon, satellites);
  }

  bool collision = mpuReady && currentG >= IMPACT_G_THRESHOLD;
  bool smokeAlert = rawSmoke >= SMOKE_ALERT_LEVEL;
  if (!inIncidentState && millis() - lastIncidentTime >= INCIDENT_COOLDOWN_MS && (collision || firePresent || smokeAlert)) {
    String reason = collision ? "High-G Vehicle Collision" : firePresent ? "Cabin Fire Detected" : "Cabin Smoke Detected";
    executeIncidentPipeline(reason, currentG, rawSmoke, lat, lon);
  }
  delay(20);
}
