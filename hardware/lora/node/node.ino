/*
  Indradhanu LoRa node  (Arduino Uno + SX1278 / Ra-02, 433 MHz)

  ONE sketch for BOTH Unos. Each one is, at the same time:
    1. a sensor node: every PERIOD_MS it reads whatever sensors are plugged in,
       MIMICS any that are missing (calm, plausible values), and
         - prints the reading to USB as   L<reading>
         - sends it over LoRa as           N<reading>
    2. a LoRa modem for lora_mesh_link.py: carries bitchat IDX1 packets
       between the two laptops (commands C/A/S/P, unchanged from modem.ino)
    3. a receiver: prints every LoRa packet heard as R<rssi>,<snr>,<hex>

  Reading format (pipe separated, one line):
    NODE|seq|up|mq2|mq135|temp|tilt|gyro|vib|mic|piezo|knocks|tiltsw|pir|sim
  `sim` is a hex bitmask of the channels that are MIMICKED, not measured:
    1 mq2   2 mq135   4 temp   8 imu (tilt/gyro/vib)   10 mic   20 piezo   40 tilt switch
  The API stores it and labels those values "simulated" on the console.

  Sensor detection (re-checked every 30 s, so you can plug things in live):
    MQ-2 A0, MQ-135 A1, piezo A2, mic A3 : the pin is briefly pulled up; a
        connected module pulls it back down, a bare pin stays at 1023.
        (The piezo needs its 10k from A2 to GND for this to work.)
    MPU-6050 (I2C 0x68 on A4/A5) : answers on the bus.
    DS18B20 on D4 : found on the 1-Wire bus.
    Tilt switch on D5 : counted as present once it has closed at least once.

  Extra serial commands (from lora_mesh_link.py or the Serial Monitor):
    E<f|c|t|0>   start a MIMICKED event on this node for 60 s:
                 f = fire/smoke, c = structural collapse, t = person trapped
                 (tapping + voices + lean), 0 = stop. Only mimicked channels
                 change; real sensors keep reporting what they measure.
    I            print which sensors are real and which are mimicked

  Libraries: "LoRa" (Sandeep Mistry), "OneWire", "DallasTemperature",
             "LiquidCrystal I2C" (Frank de Brabander)
*/

#include <SPI.h>
#include <Wire.h>
#include <LoRa.h>
#include <OneWire.h>
#include <DallasTemperature.h>
// The LCD library is optional: without it the sketch compiles and just skips the LCD.
#if __has_include(<LiquidCrystal_I2C.h>)
#include <LiquidCrystal_I2C.h>
#define HAVE_LCD_LIB 1
#else
#define HAVE_LCD_LIB 0
#endif

// ------------------------------------------------------------- settings ---
#define NODE_ID        "RN01"    // RN01 on one Uno, RN02 on the other
#define LORA_FREQ      433E6     // same on both
#define LORA_SYNC      0xA5      // same on both
#define LORA_SF        7         // 7 fast; 9-10 for more range (same on both)
#define TX_POWER_DBM   14        // 2 if both radios sit on one breadboard and packets garble
#define LORA_RST_PIN   9         // RST through a 10k to D9 (also fine if RST is left unwired)
#define PERIOD_MS      5000      // one reading every 5 s
#define KNOCK_LEVEL    40        // piezo level that counts as a knock (10k bleed)
#define KNOCK_GAP_MS   120
#define USE_LCD        1         // a 16x2 I2C LCD is used if one is found (and its library is installed)
#define LCD_ON         (USE_LCD && HAVE_LCD_LIB)

// ----------------------------------------------------------------- pins ---
const int LORA_NSS = 10, LORA_DIO0 = 2;
const int PIN_MQ2 = A0, PIN_MQ135 = A1, PIN_PIEZO = A2, PIN_MIC = A3;
const int PIN_DS = 4, PIN_TILT = 5, PIN_LED = 7;
const uint8_t MPU = 0x68;

OneWire oneWire(PIN_DS);
DallasTemperature ds(&oneWire);
#if LCD_ON
LiquidCrystal_I2C *lcd = nullptr;
#endif

// channel bits
enum { C_MQ2 = 1, C_MQ135 = 2, C_TEMP = 4, C_IMU = 8, C_MIC = 0x10, C_PIEZO = 0x20, C_TILT = 0x40 };
uint8_t present = 0;            // bits of channels that are really connected
bool tiltSeenClosed = false;
bool radioOk = false;           // false = no radio found: readings still go out over USB

// ------------------------------------------------------- modem (as modem.ino) ---
const int MAXPKT = 240;
uint8_t txbuf[MAXPKT];
int txlen = 0;
char line[72];
int linelen = 0;
bool overflow = false;

// ----------------------------------------------------------- accumulators ---
unsigned long periodStart = 0, lastImu = 0, lastKnock = 0, lastDetect = 0, lastBlink = 0;
int micMin = 1023, micMax = 0, piezoMax = 0;
unsigned int knocks = 0;
float sumA = 0, sumA2 = 0, sx = 0, sy = 0, sz = 0, gMax = 0;
int nImu = 0;
unsigned long seq = 0;

// mimicked event
char eventKind = 0;
unsigned long eventUntil = 0, eventStart = 0;

// ------------------------------------------------------------- helpers ---
int hexval(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}
void printHexByte(uint8_t b) {
  const char *d = "0123456789abcdef";
  Serial.write(d[b >> 4]);
  Serial.write(d[b & 15]);
}
void blink() { digitalWrite(PIN_LED, HIGH); lastBlink = millis(); }

float noise(float amp) { return (random(-1000, 1001) / 1000.0) * amp; }

bool analogPresent(int pin) {
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(300);
  analogRead(pin);
  int a = analogRead(pin);
  pinMode(pin, INPUT);
  delayMicroseconds(300);
  return a < 1000;
}

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
  ax = v[0] / 16384.0; ay = v[1] / 16384.0; az = v[2] / 16384.0;
  gx = v[4] / 131.0;   gy = v[5] / 131.0;   gz = v[6] / 131.0;
  return true;
}
bool i2cPresent(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

void detect() {
  // Analog sensors stay "real" once seen: a real MQ in thick smoke reads near
  // 1023 too, and must never be swapped for a calm mimicked value mid-event.
  uint8_t p = present & (C_MQ2 | C_MQ135 | C_PIEZO | C_MIC);
  if (analogPresent(PIN_MQ2)) p |= C_MQ2;
  if (analogPresent(PIN_MQ135)) p |= C_MQ135;
  if (analogPresent(PIN_PIEZO)) p |= C_PIEZO;
  if (analogPresent(PIN_MIC)) p |= C_MIC;
  if (i2cPresent(MPU) && mpuWrite(0x6B, 0x00) && mpuWrite(0x1C, 0x00) && mpuWrite(0x1B, 0x00)) p |= C_IMU;
  ds.begin();
  if (ds.getDeviceCount() > 0) {
    p |= C_TEMP;
    ds.setResolution(10);
    ds.setWaitForConversion(false);
  }
  if (tiltSeenClosed) p |= C_TILT;
  present = p;
}

void report() {
  Serial.print(F("#SENSORS real:"));
  const char *names[] = {"mq2", "mq135", "temp", "imu", "mic", "piezo", "tilt"};
  for (int i = 0; i < 7; i++) if (present & (1 << i)) { Serial.print(' '); Serial.print(names[i]); }
  Serial.print(F(" | mimicked:"));
  for (int i = 0; i < 7; i++) if (!(present & (1 << i))) { Serial.print(' '); Serial.print(names[i]); }
  Serial.println();
}

void resetWindow() {
  micMin = 1023; micMax = 0; piezoMax = 0; knocks = 0;
  sumA = sumA2 = sx = sy = sz = gMax = 0; nImu = 0;
}

// --------------------------------------------------------- serial commands ---
void command(char *s, int n) {
  if (n == 0) return;
  switch (s[0]) {
    case 'C': txlen = 0; Serial.println('+'); break;
    case 'A':
      for (int i = 1; i + 1 < n; i += 2) {
        int hi = hexval(s[i]), lo = hexval(s[i + 1]);
        if (hi < 0 || lo < 0) { Serial.println(F("!hex")); return; }
        if (txlen >= MAXPKT) { Serial.println(F("!full")); return; }
        txbuf[txlen++] = (hi << 4) | lo;
      }
      Serial.println('+');
      break;
    case 'S':
      if (txlen == 0) { Serial.println(F("!empty")); break; }
      if (!radioOk) { Serial.println(F("!noradio")); txlen = 0; break; }
      LoRa.beginPacket(); LoRa.write(txbuf, txlen); LoRa.endPacket();
      Serial.print('D'); Serial.println(txlen);
      txlen = 0; blink();
      break;
    case 'P':
      Serial.print('P'); Serial.print((long)(LORA_FREQ / 1000)); Serial.print(',');
      Serial.print(LORA_SF); Serial.print(','); Serial.println(TX_POWER_DBM);
      break;
    case 'E':
      eventKind = (n > 1 && s[1] != '0') ? s[1] : 0;
      eventStart = millis();
      eventUntil = millis() + 60000UL;
      Serial.print(F("#EVENT ")); Serial.println(eventKind ? eventKind : '0');
      break;
    case 'I': report(); break;
    default: Serial.println(F("!cmd"));
  }
}

// ----------------------------------------------------------- the reading ---
int avgAnalog(int pin) {
  analogRead(pin);
  long s = 0;
  for (int i = 0; i < 8; i++) s += analogRead(pin);
  return s / 8;
}

char pkt[110];
void addField(const char *v) { strcat(pkt, "|"); strcat(pkt, v); }
void addInt(long v) { char t[12]; ltoa(v, t, 10); addField(t); }
void addFloat(float v, int d) { char t[14]; dtostrf(v, 0, d, t); addField(t); }

// The same reading with a name on every value, for the Serial Monitor.
// Starts with '#' so lora_mesh_link.py leaves it out of its parsing.
void printF(const __FlashStringHelper *name, float v, int d, const __FlashStringHelper *unit) {
  Serial.print(' '); Serial.print(name); Serial.print('=');
  if (isnan(v)) Serial.print('-'); else Serial.print(v, d);
  Serial.print(unit);
}
void printNamed(int mq2, int mq135, float temp, float tilt, float gyro, float vib,
                int mic, int piezo, int kn, int tsw) {
  Serial.print(F("#READ " NODE_ID));
  printF(F("MQ2_smoke"), mq2, 0, F(""));
  printF(F("MQ135_air"), mq135, 0, F(""));
  printF(F("temp"), temp, 1, F("C"));
  printF(F("tilt"), tilt, 1, F("deg"));
  printF(F("gyro"), gyro, 1, F("dps"));
  printF(F("vibration"), vib, 3, F("g"));
  printF(F("sound"), mic, 0, F(""));
  printF(F("piezo"), piezo, 0, F(""));
  printF(F("knocks"), kn, 0, F(""));
  Serial.print(F(" tilt_switch=")); Serial.println(tsw ? F("closed") : F("open"));
}

void buildAndSend() {
  bool ev = eventKind && millis() < eventUntil;
  float k = ev ? min(1.0, (millis() - eventStart) / 20000.0) : 0;   // ramps up over 20 s
  char e = ev ? eventKind : 0;

  // ---- gas
  int mq2 = (present & C_MQ2) ? avgAnalog(PIN_MQ2) : (int)(180 + noise(6) + (e == 'f' ? 420 * k : 0));
  int mq135 = (present & C_MQ135) ? avgAnalog(PIN_MQ135) : (int)(260 + noise(8) + (e == 'f' ? 380 * k : e == 't' ? 110 * k : 0));
  // ---- temperature
  float temp;
  if (present & C_TEMP) {
    temp = ds.getTempCByIndex(0);
    if (temp <= -55 || temp >= 125 || temp == 85.0) temp = NAN;
  } else {
    temp = 29.0 + noise(0.3) + (e == 'f' ? 30 * k : e == 't' ? 3 * k : 0);
  }
  if (present & C_TEMP) ds.requestTemperatures();   // collected next period
  // ---- motion
  float tilt = NAN, vib = NAN, gyro = NAN;
  if (present & C_IMU) {
    if (nImu > 2) {
      float mx = sx / nImu, my = sy / nImu, mz = sz / nImu;
      float mag = sqrt(mx * mx + my * my + mz * mz);
      if (mag > 0.1) tilt = acos(constrain(mz / mag, -1.0, 1.0)) * 57.2958;
      float mean = sumA / nImu;
      vib = sqrt(max(0.0, sumA2 / nImu - mean * mean));
      gyro = gMax;
    }
  } else {
    tilt = 1.0 + noise(0.15) + ((e == 'c' || e == 't') ? 12 * k : 0);
    gyro = 0.8 + noise(0.6) + ((e == 'c') && random(3) == 0 ? 45 : 0);
    vib = 0.004 + noise(0.002) + ((e == 'c') ? 0.08 * k : 0);
  }
  // ---- sound / knocks
  int mic = (present & C_MIC) ? (micMax - micMin) : (int)(20 + noise(6) + (e == 't' && random(2) ? 160 : 0));
  int piezo; unsigned int kn;
  if (present & C_PIEZO) { piezo = piezoMax; kn = knocks; }
  else { kn = (e == 't') ? random(2, 5) : 0; piezo = 8 + (int)noise(4) + kn * 70; }
  int tsw = (present & C_TILT) ? (digitalRead(PIN_TILT) == LOW ? 1 : 0) : ((e == 'c' || e == 't') && k > 0.5 ? 0 : 1);

  seq++;
  strcpy(pkt, NODE_ID);
  addInt(seq);
  addInt(millis() / 1000);
  addInt(mq2);
  addInt(mq135);
  if (isnan(temp)) addField(""); else addFloat(temp, 1);
  if (isnan(tilt)) addField(""); else addFloat(tilt, 1);
  if (isnan(gyro)) addField(""); else addFloat(gyro, 1);
  if (isnan(vib)) addField(""); else addFloat(vib, 3);
  addInt(mic);
  addInt(piezo);
  addInt(kn);
  addInt(tsw);
  addField("");                         // pir: not fitted
  char sim[4];
  uint8_t mask = (~present) & 0x7F;
  sim[0] = "0123456789abcdef"[mask >> 4]; sim[1] = "0123456789abcdef"[mask & 15]; sim[2] = 0;
  addField(sim);

  Serial.print('L');
  Serial.println(pkt);
  printNamed(mq2, mq135, temp, tilt, gyro, vib, mic, piezo, kn, tsw);

  if (radioOk) {
    LoRa.beginPacket();
    LoRa.write('N');
    LoRa.print(pkt);
    LoRa.endPacket();
    blink();
  }

#if LCD_ON
  if (lcd) {
    char l1[17], l2[17];
    snprintf(l1, sizeof(l1), "%s #%lu %s", NODE_ID, seq, "LIVE");
    char t[8]; dtostrf(isnan(temp) ? 0 : temp, 0, 1, t);
    snprintf(l2, sizeof(l2), "G%d T%s K%u", mq2, t, kn);
    lcd->setCursor(0, 0); lcd->print(l1); for (int i = strlen(l1); i < 16; i++) lcd->print(' ');
    lcd->setCursor(0, 1); lcd->print(l2); for (int i = strlen(l2); i < 16; i++) lcd->print(' ');
  }
#endif

}

bool radioStart(bool first) {
  radioOk = LoRa.begin(LORA_FREQ);
  if (!radioOk) {
    if (first) {
      // Keep going: the sensors still report over USB, and the radio is retried every 10 s.
      Serial.println(F("#radio not found: check 3.3V, GND, NSS->D10, SCK->D13, MOSI->D11, MISO->D12. Retrying every 10 s."));
#if LCD_ON
      if (lcd) { lcd->setCursor(0, 1); lcd->print(F("NO RADIO usb-only")); }
#endif
    }
    return false;
  }
  LoRa.setSpreadingFactor(LORA_SF);
  LoRa.setSignalBandwidth(125E3);
  LoRa.setCodingRate4(5);
  LoRa.setSyncWord(LORA_SYNC);
  LoRa.enableCrc();
  LoRa.setTxPower(TX_POWER_DBM);
  if (!first) Serial.println(F("#radio found, LoRa on"));
  return true;
}

// ---------------------------------------------------------------- setup ---
void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_TILT, INPUT_PULLUP);
  randomSeed(analogRead(A6) ^ micros());
  Wire.begin();
  Wire.setClock(100000);

#if LCD_ON
  uint8_t addr = i2cPresent(0x27) ? 0x27 : i2cPresent(0x3F) ? 0x3F : 0;
  if (addr) { lcd = new LiquidCrystal_I2C(addr, 16, 2); lcd->init(); lcd->backlight(); lcd->print(F("INDRADHANU " NODE_ID)); }
#endif

  LoRa.setPins(LORA_NSS, LORA_RST_PIN, LORA_DIO0);
  LoRa.setSPIFrequency(1E6);              // 10k series resistors on NSS/SCK/MOSI
  radioStart(true);

  detect();
  lastDetect = millis();
  Serial.println(F("READY indradhanu-lora-modem 2 node " NODE_ID));
  // The sensor inventory is printed only on request (send "I").
  periodStart = millis();
  if (present & C_TEMP) ds.requestTemperatures();
}

// ----------------------------------------------------------------- loop ---
void loop() {
  // 1. laptop commands
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[linelen] = 0;
      if (overflow) Serial.println(F("!long")); else command(line, linelen);
      linelen = 0; overflow = false;
    } else if (linelen < (int)sizeof(line) - 1) line[linelen++] = c;
    else overflow = true;
  }

  // 2. radio
  int size = radioOk ? LoRa.parsePacket() : 0;
  if (size > 0 && size <= 12 && LoRa.peek() == 'E') {
    // "E<NODE_ID><f|c|t|0>": an event cue for one node, sent by the command centre.
    char cue[13]; int n = 0;
    while (LoRa.available() && n < 12) cue[n++] = (char)LoRa.read();
    cue[n] = 0;
    int idl = strlen(NODE_ID);
    if (n == idl + 2 && strncmp(cue + 1, NODE_ID, idl) == 0) {
      char cmd[3] = {'E', cue[n - 1], 0};
      command(cmd, 2);
    }
    size = 0;
  }
  if (size > 0) {
    Serial.print('R'); Serial.print(LoRa.packetRssi()); Serial.print(',');
    Serial.print(LoRa.packetSnr(), 1); Serial.print(',');
    while (LoRa.available()) printHexByte((uint8_t)LoRa.read());
    Serial.println();
    blink();
  }

  // 3. sample the fast sensors
  if (present & C_MIC) {
    int m = analogRead(PIN_MIC);
    if (m < micMin) micMin = m;
    if (m > micMax) micMax = m;
  }
  if (present & C_PIEZO) {
    analogRead(PIN_PIEZO);
    int p = analogRead(PIN_PIEZO);
    if (p > piezoMax) piezoMax = p;
    if (p > KNOCK_LEVEL && millis() - lastKnock > KNOCK_GAP_MS) { knocks++; lastKnock = millis(); }
  }
  if ((present & C_IMU) && millis() - lastImu >= 20) {
    lastImu = millis();
    float ax, ay, az, gx, gy, gz;
    if (mpuRead(ax, ay, az, gx, gy, gz)) {
      float a = sqrt(ax * ax + ay * ay + az * az), g = sqrt(gx * gx + gy * gy + gz * gz);
      sumA += a; sumA2 += a * a; sx += ax; sy += ay; sz += az;
      if (g > gMax) gMax = g;
      nImu++;
    }
  }
  if (!tiltSeenClosed && digitalRead(PIN_TILT) == LOW) { tiltSeenClosed = true; present |= C_TILT; }

  // 4. every PERIOD_MS: one reading
  if (millis() - periodStart >= PERIOD_MS) {
    buildAndSend();
    resetWindow();
    periodStart = millis();
  }

  // 5. re-detect sensors every 30 s (plug things in while it runs)
  static unsigned long lastRadioTry = 0;
  if (!radioOk && millis() - lastRadioTry >= 10000UL) {
    lastRadioTry = millis();
    radioStart(false);
  }
  if (millis() - lastDetect >= 30000UL) {
    uint8_t before = present;
    detect();
    lastDetect = millis();
    (void)before;  // inventory changes are reported only on request ("I")
  }

  if (lastBlink && millis() - lastBlink > 40) { digitalWrite(PIN_LED, LOW); lastBlink = 0; }
}
