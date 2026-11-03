/*
  Indradhanu LoRa field node  (Arduino Uno + SX1278 / Ra-02, 433 MHz)

  Reads every sensor for ~1.5 s, then sends one line over LoRa:

    RN01|seq|up|mq2|mq135|temp|tilt|gyro|vib|mic|piezo|knocks|tiltsw|pir

      seq     packet counter (gaps = lost packets)
      up      seconds since boot (backend ignores the MQ sensors until they
              have pre-heated, ~120 s)
      mq2     MQ-2 analog 0..1023      (smoke / LPG / combustible gas)
      mq135   MQ-135 analog 0..1023    (air quality: CO2, NH3, smoke)
      temp    deg C from DS18B20 or LM35
      tilt    deg from vertical, from the MPU-6050 accelerometer
      gyro    peak rotation in the window, deg/s
      vib     std-dev of acceleration in the window, g (shaking)
      mic     sound level: peak-to-peak of the mic module, 0..1023
      piezo   peak of the piezo disc, 0..1023 (contact vibration / knocks)
      knocks  sharp hits on the piezo disc in the window (tapping on rubble)
      tiltsw  ball tilt switch, 0/1 raw
      pir     PIR motion 0/1, or -1 if no PIR is fitted

  A sensor that is missing or fails sends an empty field, never a fake number.

  Libraries (Arduino IDE -> Library Manager):
    "LoRa" by Sandeep Mistry
    "OneWire" and "DallasTemperature"   (only if THERMAL_DS18B20 is 1)

  Wiring: see docs/IOT_LORA_SETUP.md
*/

#include <SPI.h>
#include <Wire.h>
#include <LoRa.h>

// ------------------------------------------------------------- settings ---
#define NODE_ID          "RN01"   // give every field node its own id
#define THERMAL_DS18B20  1        // 1 = DS18B20 on D4,  0 = LM35 on A2
#define HAS_PIR          0        // 1 if a PIR (HC-SR501) is on D6
#define HAS_MIC          0        // 1 if an ANALOG sound module (KY-037/KY-038/MAX4466 "AO"/"OUT")
                                  // is on A3. 0 for none, and for an INMP441 (SD/WS/SCK/L-R pins):
                                  // that mic speaks I2S, which an Uno cannot read.
#define HAS_PIEZO        0        // 1 if a piezo disc is on A2 (red) / GND (black)
#define HAS_TILT         1        // 1 if a ball tilt switch is on D5 / GND
                                  // A missing sensor sends an empty field, never a made-up 0.
                                  // The MPU-6050 (gyro) is detected on its own; none = empty fields.
#define LORA_FREQ        433E6    // must match the gateway
#define LORA_SYNC        0xA5     // must match the gateway
#define TX_POWER_DBM     14       // 14 is plenty indoors and easy on the Uno's 3.3 V pin
#define SAMPLE_MS        1500     // how long each reading window is
#define PERIOD_MS        2000     // one packet every PERIOD_MS
#define KNOCK_THRESHOLD  25       // piezo level that counts as a knock. 25 suits a 10k bleed
                                  // resistor; with 1M across the disc use ~60. Raise if it
                                  // counts knocks in silence, lower if light taps are missed.
#define KNOCK_GAP_MS     120      // one knock cannot be counted twice inside this
#define PIEZO_PULLUP     1        // 1 = no resistor on the piezo: the Uno's internal pull-up
                                  //     drains it and a knock shows as a dip (read inverted).
                                  // 0 = a resistor (10k-1M) from the piezo pin to GND.
#define LORA_RST_WIRED   1        // 0 = SX1278 RST left unconnected (saves a resistor; the
                                  //     module resets itself at power-up). 1 = RST on D9.

// ----------------------------------------------------------------- pins ---
// SX1278: NSS D10, SCK D13, MOSI D11, MISO D12 (hardware SPI), RST D9, DIO0 D2
const int LORA_NSS = 10, LORA_RST = LORA_RST_WIRED ? 9 : -1, LORA_DIO0 = 2;
const int PIN_MQ2   = A0;
const int PIN_MQ135 = A1;
const int PIN_MIC   = A3;
// MPU-6050 on A4 (SDA) / A5 (SCL)
const int PIN_TILTSW = 5;
const int PIN_PIR    = 6;
const int PIN_LED    = 7;   // optional status LED (D13 is busy with SPI)

#if THERMAL_DS18B20
  #include <OneWire.h>
  #include <DallasTemperature.h>
  const int PIN_DS18B20 = 4;
  OneWire oneWire(PIN_DS18B20);
  DallasTemperature ds(&oneWire);
  const int PIN_PIEZO_A = A2;            // piezo disc on an analog pin: level + knocks
  #define PIEZO_ANALOG 1
#else
  const int PIN_LM35 = A2;               // LM35 takes A2 ...
  const int PIN_PIEZO_D = 3;             // ... so the piezo counts knocks on D3 (interrupt)
  #define PIEZO_ANALOG 0
  volatile unsigned int isrKnocks = 0;
  volatile unsigned long isrLast = 0;
  void onKnock() {
    unsigned long now = millis();
    if (now - isrLast > KNOCK_GAP_MS) { isrKnocks++; isrLast = now; }
  }
#endif

const uint8_t MPU = 0x68;
bool imuOk = false;
bool tempOk = false;
unsigned long seq = 0;

// ------------------------------------------------------------- helpers ---
bool mpuWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

bool mpuRead(float &ax, float &ay, float &az, float &gx, float &gy, float &gz) {
  Wire.beginTransmission(MPU);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU, (uint8_t)14) != 14) return false;
  int16_t v[7];
  for (int i = 0; i < 7; i++) v[i] = (Wire.read() << 8) | Wire.read();
  ax = v[0] / 16384.0; ay = v[1] / 16384.0; az = v[2] / 16384.0;   // +-2 g
  gx = v[4] / 131.0;   gy = v[5] / 131.0;   gz = v[6] / 131.0;     // +-250 dps
  return true;
}

int avgAnalog(int pin, int n) {
  analogRead(pin);                       // settle the ADC after a channel switch
  long s = 0;
  for (int i = 0; i < n; i++) s += analogRead(pin);
  return s / n;
}

// Append a field to the packet. Empty when the sensor is not available.
char pkt[120];
void addInt(long v, bool ok = true) {
  char t[16];
  strcat(pkt, "|");
  if (ok) { ltoa(v, t, 10); strcat(pkt, t); }
}
void addFloat(float v, int decimals, bool ok = true) {
  char t[16];
  strcat(pkt, "|");
  if (ok && !isnan(v)) { dtostrf(v, 0, decimals, t); strcat(pkt, t); }
}

// --------------------------------------------------------------- setup ---
void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_TILTSW, INPUT_PULLUP);
#if HAS_PIR
  pinMode(PIN_PIR, INPUT);
#endif

  Wire.begin();
  Wire.setClock(400000);
  imuOk = mpuWrite(0x6B, 0x00) && mpuWrite(0x1C, 0x00) && mpuWrite(0x1B, 0x00);

#if THERMAL_DS18B20
  ds.begin();
  tempOk = ds.getDeviceCount() > 0;
  pinMode(PIN_PIEZO_A, PIEZO_PULLUP ? INPUT_PULLUP : INPUT);
  ds.setResolution(10);                  // 0.25 C, ~190 ms conversion
  ds.setWaitForConversion(false);        // start it, collect it at the end of the window
#else
  tempOk = true;
  pinMode(PIN_PIEZO_D, PIEZO_PULLUP ? INPUT_PULLUP : INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_PIEZO_D), onKnock, PIEZO_PULLUP ? FALLING : RISING);
#endif

  LoRa.setPins(LORA_NSS, LORA_RST, LORA_DIO0);
  // 1 MHz SPI: the 10k/20k resistor dividers on the SX1278 lines are too slow
  // for the library's default 8 MHz. 1 MHz is still far faster than LoRa needs.
  LoRa.setSPIFrequency(1E6);
  if (!LoRa.begin(LORA_FREQ)) {
    Serial.println(F("#ERR LoRa not found - check SX1278 wiring and 3.3V"));
    while (true) { digitalWrite(PIN_LED, !digitalRead(PIN_LED)); delay(150); }
  }
  LoRa.setSpreadingFactor(9);
  LoRa.setSignalBandwidth(125E3);
  LoRa.setCodingRate4(5);
  LoRa.setSyncWord(LORA_SYNC);
  LoRa.enableCrc();
  LoRa.setTxPower(TX_POWER_DBM);

  Serial.print(F("#NODE ")); Serial.print(F(NODE_ID));
  Serial.print(F(" imu=")); Serial.print(imuOk);
  Serial.print(F(" temp=")); Serial.println(tempOk);
  Serial.println(F("#MQ sensors need ~2 min to heat up before their numbers mean anything"));
}

// ---------------------------------------------------------------- loop ---
void loop() {
  unsigned long start = millis();

#if THERMAL_DS18B20
  if (tempOk) ds.requestTemperatures();
#else
  noInterrupts(); isrKnocks = 0; interrupts();
#endif

  int micMin = 1023, micMax = 0, piezoMax = 0;
  unsigned int knocks = 0;
  unsigned long lastKnock = 0, lastImu = 0;
  float sumA = 0, sumA2 = 0, sx = 0, sy = 0, sz = 0, gMax = 0;
  int nImu = 0;

  while (millis() - start < SAMPLE_MS) {
#if HAS_MIC
    int m = analogRead(PIN_MIC);
    if (m < micMin) micMin = m;
    if (m > micMax) micMax = m;
#endif

#if PIEZO_ANALOG && HAS_PIEZO
    analogRead(PIN_PIEZO_A);             // piezo is high impedance: throw away the first read
    int p = analogRead(PIN_PIEZO_A);
    if (PIEZO_PULLUP) p = 1023 - p;      // pulled up: a knock pulls the pin down
    if (p > piezoMax) piezoMax = p;
    if (p > KNOCK_THRESHOLD && millis() - lastKnock > KNOCK_GAP_MS) {
      knocks++;
      lastKnock = millis();
    }
#endif

    if (imuOk && millis() - lastImu >= 20) {
      lastImu = millis();
      float ax, ay, az, gx, gy, gz;
      if (mpuRead(ax, ay, az, gx, gy, gz)) {
        float a = sqrt(ax * ax + ay * ay + az * az);
        float g = sqrt(gx * gx + gy * gy + gz * gz);
        sumA += a; sumA2 += a * a; sx += ax; sy += ay; sz += az;
        if (g > gMax) gMax = g;
        nImu++;
      }
    }
  }

#if !PIEZO_ANALOG && HAS_PIEZO
  noInterrupts(); knocks = isrKnocks; interrupts();
#endif

  int mq2 = avgAnalog(PIN_MQ2, 10);
  int mq135 = avgAnalog(PIN_MQ135, 10);

  float temp = NAN;
#if THERMAL_DS18B20
  if (tempOk) {
    float t = ds.getTempCByIndex(0);
    if (t > -55 && t < 125 && t != 85.0) temp = t;    // 85.0 = conversion not ready
  }
#else
  temp = avgAnalog(PIN_LM35, 10) * (5000.0 / 1023.0) / 10.0;  // 10 mV per deg C
#endif

  bool imuData = imuOk && nImu > 2;
  float tilt = NAN, vib = NAN;
  if (imuData) {
    float mx = sx / nImu, my = sy / nImu, mz = sz / nImu;
    float mag = sqrt(mx * mx + my * my + mz * mz);
    if (mag > 0.1) tilt = acos(constrain(mz / mag, -1.0, 1.0)) * 57.2958;
    float mean = sumA / nImu;
    vib = sqrt(max(0.0, sumA2 / nImu - mean * mean));
  }

  int tiltSw = digitalRead(PIN_TILTSW) == LOW ? 1 : 0;
#if HAS_PIR
  int pir = digitalRead(PIN_PIR) == HIGH ? 1 : 0;
#else
  int pir = -1;
#endif

  seq++;
  strcpy(pkt, NODE_ID);
  addInt(seq);
  addInt(millis() / 1000);
  addInt(mq2);
  addInt(mq135);
  addFloat(temp, 1, !isnan(temp));
  addFloat(tilt, 1, imuData);
  addFloat(gMax, 1, imuData);
  addFloat(vib, 3, imuData);
  addInt(micMax - micMin, HAS_MIC);
  addInt(piezoMax, PIEZO_ANALOG && HAS_PIEZO);   // digital piezo: no level, only knocks
  addInt(knocks, HAS_PIEZO);
  addInt(tiltSw, HAS_TILT);
  addInt(pir);

  digitalWrite(PIN_LED, HIGH);
  LoRa.beginPacket();
  LoRa.print(pkt);
  LoRa.endPacket();
  digitalWrite(PIN_LED, LOW);
  Serial.println(pkt);

  long rest = PERIOD_MS - (long)(millis() - start);
  if (rest > 0) delay(rest);
}
