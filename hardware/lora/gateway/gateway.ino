/*
  Indradhanu LoRa gateway  (Arduino Uno + SX1278 / Ra-02 + optional 16x2 I2C LCD)

  Sits on the command-centre laptop's USB. Every LoRa packet heard is printed
  on one line for lora_bridge.py:

    RX|<packet exactly as sent>|<rssi dBm>|<snr dB>
    e.g. RX|RN01|184|300|382|614|34.7|2.4|0.2|0.012|63|120|3|1|-1|-67|9.5

  Plus, every 10 s:   GW|ALIVE|<packets heard>|<seconds up>
  Lines starting with '#' are diagnostics; the bridge ignores them.

  The LCD is optional: with none connected (or not enough power for its
  backlight) everything else still works.

  Libraries: "LoRa" by Sandeep Mistry, "LiquidCrystal I2C" by Frank de Brabander
*/

#include <SPI.h>
#include <Wire.h>
#include <LoRa.h>
#include <LiquidCrystal_I2C.h>

#define LORA_FREQ  433E6     // must match the field node
#define LORA_SYNC  0xA5      // must match the field node
#define USE_LCD    1         // 0 to skip the LCD entirely

#define LORA_RST_WIRED 1   // 1 = RST on D9 through a 10k (R4/R10); 0 = RST left unconnected
const int LORA_NSS = 10, LORA_RST = LORA_RST_WIRED ? 9 : -1, LORA_DIO0 = 2;
const int PIN_LED = 7;

LiquidCrystal_I2C *lcd = nullptr;
unsigned long heard = 0, lastRx = 0, lastAlive = 0, lastPage = 0;
char last[120] = "";
int lastRssi = 0;
uint8_t page = 0;

bool i2cPresent(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

// field i (0-based) of the '|' separated packet, copied into out
void field(const char *s, int i, char *out, int n) {
  int k = 0;
  while (i > 0 && *s) { if (*s++ == '|') i--; }
  while (*s && *s != '|' && k < n - 1) out[k++] = *s++;
  out[k] = 0;
}

void lcdLine(uint8_t row, const char *text) {
  if (!lcd) return;
  char buf[17];
  snprintf(buf, sizeof(buf), "%-16s", text);
  lcd->setCursor(0, row);
  lcd->print(buf);
}

void drawLcd() {
  if (!lcd) return;
  char a[10], b[10], c[10], line[24];
  if (heard == 0 || millis() - lastRx > 15000) {
    lcdLine(0, "INDRADHANU GW");
    snprintf(line, sizeof(line), heard ? "NO SIGNAL %lus" : "WAITING LoRa", (millis() - lastRx) / 1000);
    lcdLine(1, line);
    return;
  }
  field(last, 0, a, sizeof(a));
  field(last, 1, b, sizeof(b));
  snprintf(line, sizeof(line), "%s #%s %ddB", a, b, lastRssi);
  lcdLine(0, line);
  switch (page) {
    case 0:  field(last, 3, a, 6); field(last, 4, b, 6);
             snprintf(line, sizeof(line), "MQ2 %s 135 %s", a, b); break;
    case 1:  field(last, 5, a, 7); field(last, 6, b, 7);
             snprintf(line, sizeof(line), "T%sC TILT%s", a, b); break;
    default: field(last, 9, a, 5); field(last, 10, b, 5); field(last, 11, c, 4);
             snprintf(line, sizeof(line), "MIC%s PZ%s K%s", a, b, c); break;
  }
  lcdLine(1, line);
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  Wire.begin();

#if USE_LCD
  uint8_t addr = i2cPresent(0x27) ? 0x27 : i2cPresent(0x3F) ? 0x3F : 0;
  if (addr) {
    lcd = new LiquidCrystal_I2C(addr, 16, 2);
    lcd->init();
    lcd->backlight();
  }
  Serial.print(F("#LCD ")); Serial.println(addr ? F("found") : F("not connected - fine"));
#endif

  LoRa.setPins(LORA_NSS, LORA_RST, LORA_DIO0);
  // 1 MHz SPI: the 10k/20k resistor dividers on the SX1278 lines are too slow
  // for the library's default 8 MHz. 1 MHz is still far faster than LoRa needs.
  LoRa.setSPIFrequency(1E6);
  if (!LoRa.begin(LORA_FREQ)) {
    Serial.println(F("#ERR LoRa not found - check SX1278 wiring and 3.3V"));
    lcdLine(0, "LoRa NOT FOUND");
    lcdLine(1, "check wiring");
    while (true) { digitalWrite(PIN_LED, !digitalRead(PIN_LED)); delay(150); }
  }
  LoRa.setSpreadingFactor(9);
  LoRa.setSignalBandwidth(125E3);
  LoRa.setCodingRate4(5);
  LoRa.setSyncWord(LORA_SYNC);
  LoRa.enableCrc();
  LoRa.receive();

  Serial.println(F("GW|READY|433"));
  drawLcd();
}

void loop() {
  int size = LoRa.parsePacket();
  if (size > 0) {
    char buf[120];
    int n = 0;
    bool printable = true;
    while (LoRa.available()) {
      char c = (char)LoRa.read();
      if (n < (int)sizeof(buf) - 1) buf[n++] = c;
      if (c < 32 || c > 126) printable = false;
    }
    buf[n] = 0;
    int rssi = LoRa.packetRssi();
    float snr = LoRa.packetSnr();

    if (!printable || n < 6) {
      Serial.print(F("#BAD packet, ")); Serial.print(n); Serial.println(F(" bytes"));
    } else {
      heard++;
      lastRx = millis();
      lastRssi = rssi;
      strncpy(last, buf, sizeof(last) - 1);
      Serial.print(F("RX|"));
      Serial.print(buf);
      Serial.print('|');
      Serial.print(rssi);
      Serial.print('|');
      Serial.println(snr, 1);
      digitalWrite(PIN_LED, HIGH); delay(30); digitalWrite(PIN_LED, LOW);
      drawLcd();
    }
  }

  if (millis() - lastAlive > 10000) {
    lastAlive = millis();
    Serial.print(F("GW|ALIVE|")); Serial.print(heard);
    Serial.print('|'); Serial.println(millis() / 1000);
  }
  if (millis() - lastPage > 2000) {
    lastPage = millis();
    page = (page + 1) % 3;
    drawLcd();
  }
}
