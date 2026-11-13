// esp32_sender.ino -- companion sketch for esp32-serial-scope.
//
// Prints one line of "key:value" pairs per sample, which is exactly what
// esp32-serial-scope expects. Flash this, then run:
//
//     esp32-serial-scope -p COM3 -b 115200
//
// Board used while writing this: Seeed XIAO ESP32S3.
// Any ESP32 / ESP32-S3 / ESP32-C3 board works; only the pin numbers change.
//
// HARDWARE NOTES (both are classic "why does my chart look wrong" causes):
//   1. GPIO4 on the S3 is an ADC1 pin. Keep your analog inputs on ADC1 as
//      long as Wi-Fi is enabled -- ADC2 shares the radio and reads garbage.
//   2. If your sensor has its own supply, its GND must be tied to the ESP32
//      GND. Without a common ground the readings float.

#include <Arduino.h>

// ---- configuration -------------------------------------------------------
static const uint8_t  PIN_ANALOG_TEMP = 4;   // ADC1, e.g. an LM35 or NTC divider
static const uint8_t  PIN_POT        = 5;    // ADC1, a plain potentiometer
static const uint32_t SAMPLE_PERIOD  = 50;   // ms between samples (20 Hz)

// readTemperature: LM35 style linear sensor, 10 mV per degree C.
// If you use an NTC divider instead, replace the body with your own
// Steinhart-Hart / lookup conversion and keep the same return value.
float readTemperature() {
  // analogReadMilliVolts() applies the chip's calibration curve and is far
  // more accurate than analogRead() on the ESP32 family.
  uint32_t millivolts = analogReadMilliVolts(PIN_ANALOG_TEMP);
  return millivolts / 10.0f;   // 10 mV per degree C
}

// readRaw: a plain 12-bit ADC reading, useful for spotting noise and dropouts.
int readRaw(uint8_t pin) {
  return analogRead(pin);
}

void setup() {
  Serial.begin(115200);
  while (!Serial) {
    delay(10);   // native-USB boards wait for the host to attach
  }

  analogReadResolution(12);          // 0..4095
  // 11 dB attenuation covers roughly 0..3.1 V on the ESP32-S3.
  analogSetAttenuation(ADC_11db);

  Serial.println("boot: ok");        // a line without pairs is simply ignored
}

void loop() {
  float temperature = readTemperature();
  int   potRaw      = readRaw(PIN_POT);
  // Map the raw reading to a 0..100 percentage for a friendlier number.
  float potPercent  = potRaw * (100.0f / 4095.0f);

  // One line, comma separated key:value pairs, ending with a newline.
  Serial.printf("temp:%.2f,pot:%.1f,raw:%d\n",
                temperature, potPercent, potRaw);

  delay(SAMPLE_PERIOD);
}

// ---- extra notes ---------------------------------------------------------
//
// Adding an I2C or one-wire sensor later? Keep the print format identical:
//
//   #include <DHT.h>
//   DHT dht(4, DHT22);
//
//   void loop() {
//     float h = dht.readHumidity();
//     float t = dht.readTemperature();
//     Serial.printf("temp:%.2f,hum:%.1f\n", t, h);
//     delay(2000);
//   }
//
// To line up two different sample rates in one chart, filter the status
// lines away with a pattern:
//
//   esp32-serial-scope -p COM3 --pattern "^temp:"
//
// Four-servo robot note:
//   Servo current spikes change the analog baseline. If your chart gets
//   noisy only when the servos move, that is a supply and common-ground
//   problem, not a scope problem -- give the servos their own 5V/2A supply
//   and tie its GND to the ESP32 GND.
