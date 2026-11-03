/*
  Indradhanu LoRa mesh link modem  (Arduino Uno + SX1278 / Ra-02, 433 MHz)

  The SAME sketch goes on both Unos. It only moves bytes between the laptop's
  USB and the radio; lora_mesh_link.py on each laptop decides what to send and
  what a received packet means (bitchat BLE mesh, the Indradhanu API, or both).

  Wiring (radio only, 7 jumpers, RST left unconnected):
    SX1278 3.3V -> Uno 3.3V      SX1278 GND  -> Uno GND
    SX1278 NSS  -> 10k -> D10    SX1278 SCK  -> 10k -> D13
    SX1278 MOSI -> 10k -> D11    SX1278 MISO -> D12
    SX1278 DIO0 -> D2            SX1278 RST  -> not connected
    Antenna on before power.

  Serial protocol, 115200 baud, one command or event per line:
    laptop -> Uno
      C            clear the send buffer                 -> "+"
      A<hex>       append bytes (<= 24 bytes per line)    -> "+"   or "!full"
      S            transmit the buffer as one packet      -> "D<bytes>"  or "!empty"
      P            ping                                   -> "P<freq>,<sf>,<power>"
    Uno -> laptop
      READY ...                                         after boot
      R<rssi>,<snr>,<hex>                               a packet heard
      !...                                              an error
  Hex keeps every byte safe on a text line; short command lines never overrun
  the Uno's 64-byte serial buffer, even while it is busy printing a packet.

  Library: "LoRa" by Sandeep Mistry
*/

#include <SPI.h>
#include <LoRa.h>

#define LORA_FREQ     433E6   // same on both Unos
#define LORA_SYNC     0xA5    // same on both Unos
#define LORA_SF       7       // 7 = fastest (~0.4 s for a full packet); 9-10 for more range
#define TX_POWER_DBM  14      // drop to 2 if both radios sit on the same breadboard and packets come out garbled
#define LORA_RST_PIN  -1      // -1 = RST not wired. 9 if you wire RST through a 10k to D9

const int LORA_NSS = 10, LORA_DIO0 = 2;
const int MAXPKT = 240;

uint8_t txbuf[MAXPKT];
int txlen = 0;
char line[72];
int linelen = 0;
bool overflow = false;

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

void command(char *s, int n) {
  if (n == 0) return;
  switch (s[0]) {
    case 'C':
      txlen = 0;
      Serial.println('+');
      break;
    case 'A': {
      for (int i = 1; i + 1 < n; i += 2) {
        int hi = hexval(s[i]), lo = hexval(s[i + 1]);
        if (hi < 0 || lo < 0) { Serial.println(F("!hex")); return; }
        if (txlen >= MAXPKT) { Serial.println(F("!full")); return; }
        txbuf[txlen++] = (hi << 4) | lo;
      }
      Serial.println('+');
      break;
    }
    case 'S':
      if (txlen == 0) { Serial.println(F("!empty")); break; }
      LoRa.beginPacket();
      LoRa.write(txbuf, txlen);
      LoRa.endPacket();          // blocks until sent; the laptop waits for "D"
      Serial.print('D');
      Serial.println(txlen);
      txlen = 0;
      break;
    case 'P':
      Serial.print('P');
      Serial.print((long)(LORA_FREQ / 1000));
      Serial.print(',');
      Serial.print(LORA_SF);
      Serial.print(',');
      Serial.println(TX_POWER_DBM);
      break;
    default:
      Serial.println(F("!cmd"));
  }
}

void setup() {
  Serial.begin(115200);
  LoRa.setPins(LORA_NSS, LORA_RST_PIN, LORA_DIO0);
  // 1 MHz SPI: the 10k series resistors on NSS/SCK/MOSI are too slow for 8 MHz.
  LoRa.setSPIFrequency(1E6);
  if (!LoRa.begin(LORA_FREQ)) {
    Serial.println(F("!radio not found: check 3.3V, GND, NSS->D10, SCK->D13, MOSI->D11, MISO->D12"));
    while (true) delay(1000);
  }
  LoRa.setSpreadingFactor(LORA_SF);
  LoRa.setSignalBandwidth(125E3);
  LoRa.setCodingRate4(5);
  LoRa.setSyncWord(LORA_SYNC);
  LoRa.enableCrc();
  LoRa.setTxPower(TX_POWER_DBM);
  Serial.println(F("READY indradhanu-lora-modem 1"));
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[linelen] = 0;
      if (overflow) Serial.println(F("!long"));
      else command(line, linelen);
      linelen = 0;
      overflow = false;
    } else if (linelen < (int)sizeof(line) - 1) {
      line[linelen++] = c;
    } else {
      overflow = true;
    }
  }

  int size = LoRa.parsePacket();
  if (size > 0) {
    Serial.print('R');
    Serial.print(LoRa.packetRssi());
    Serial.print(',');
    Serial.print(LoRa.packetSnr(), 1);
    Serial.print(',');
    while (LoRa.available()) printHexByte((uint8_t)LoRa.read());
    Serial.println();
  }
}
