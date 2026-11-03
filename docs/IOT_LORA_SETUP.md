# LoRa field sensors: wiring, flashing, bridge and analytics

Two Arduino Unos and two SX1278 (Ra-02) LoRa modules. The **field node** reads
gas, heat, sound, tapping, tilt and shock, and sends one line every 2 s over
LoRa. The **gateway** sits on the command-centre laptop's USB. `lora_bridge.py`
reads the gateway's serial output and posts it to the deployed API. The API
scores each reading, stores it, escalates real hazards into the incident
pipeline, and feeds **Admin → Sensor analytics**.

```
FIELD (power bank)                                COMMAND CENTRE (laptop USB)
┌──────────────────────────────┐                  ┌────────────────────────────┐
│ Arduino Uno #1               │                  │ Arduino Uno #2             │
│  MQ-2 ─ A0     mic ─ A3      │   LoRa 433 MHz   │  SX1278 (RX)               │
│  MQ-135 ─ A1   MPU-6050 ─ I2C│ ───────────────► │  16x2 I2C LCD (optional)   │
│  piezo disc ─ A2  tilt ─ D5  │   SF9 BW125      │        │ USB serial        │
│  DS18B20 ─ D4  SX1278 (TX)   │                  │        ▼                   │
└──────────────────────────────┘                  │  lora_bridge.py ── HTTPS ──┼──► Render API
                                                  └────────────────────────────┘      │
                                     Supabase ◄── sensor_readings / sensor_nodes ◄────┤
                                     Incidents ◄── S packet (fire / collapse) ◄───────┤
                                     /admin/analytics ◄── /analytics/* ◄──────────────┘
```

Code:

| What | Where |
|---|---|
| Field node sketch | `hardware/lora/field_node/field_node.ino` |
| Gateway sketch | `hardware/lora/gateway/gateway.ino` |
| Serial → API bridge | `hardware/lora/lora_bridge.py` |
| API (ingest, scoring, analytics) | `backend/app/iot/`, `backend/app/api/v1/iot.py` |
| Tables | `backend/migrations/027_iot_sensor_nodes.sql` |
| Console page | `frontend/indradhanu/src/routes/admin/SensorAnalytics.tsx` |

---

## 0. Current build: `node.ino` on BOTH Unos (supersedes field_node / gateway / modem)

`hardware/lora/node/node.ino` is one sketch for both boards. Each Uno is at once a
sensor node, the LoRa modem for `lora_mesh_link.py`, and a receiver.

* Set `#define NODE_ID "RN01"` on one board and `"RN02"` on the other. Everything else is identical.
* Every 5 s it reads whatever sensors are plugged in and **mimics any that are missing**
  (all missing = all mimicked). It prints `L<reading>` over USB and sends `N<reading>` over LoRa.
* The last field `sim` is a hex bitmask of mimicked channels
  (1 mq2, 2 mq135, 4 temp, 8 imu, 10 mic, 20 piezo, 40 tilt switch). The API stores it, adds a
  `simulated:<channels>` flag, and does **not** raise incidents from mimicked channels unless
  `IOT_ESCALATE_SIMULATED=1` is set on Render (those incidents are captioned `[SIMULATED sensor data]`).
* Sensor detection is re-run every 30 s, so sensors can be plugged in live.
* Serial commands: `E f` / `E c` / `E t` / `E 0` start a mimicked fire / collapse / trapped event
  for 60 s (or stop it); `I` prints which sensors are real.
* Libraries: LoRa (Sandeep Mistry), OneWire, DallasTemperature, LiquidCrystal I2C.

Pins: radio NSS D10, SCK D13, MOSI D11 (each through 10k), MISO D12, DIO0 D2, RST through 10k to D9 (`LORA_RST_PIN 9`).
Sensors: MQ-2 A0, MQ-135 A1, piezo A2 (+10k A2→GND), analog mic module A3, MPU-6050 SDA A4 / SCL A5,
DS18B20 D4 (+10k D4→5V), tilt switch D5→GND, LED D7 (+220Ω), optional I2C LCD.

Run on each laptop (`pip install pyserial`):

```
# field laptop (bitchat phone on the same Wi-Fi)
python hardware/lora/lora_mesh_link.py --phone <phone-ip> --name field-A --hmac <MESH_HMAC> --lat .. --lon .. --keyboard
# command-centre laptop (internet)
python hardware/lora/lora_mesh_link.py --api https://<render-api> --key <MESH_GATEWAY_KEY> --name hq-B \
       --hmac <MESH_HMAC> --loc RN01=lat,lon --loc RN02=lat,lon --keyboard
```

Readings from both nodes reach `/iot/observations` through whichever laptop has `--api`.
In the keyboard prompt: `event fire|collapse|trapped|stop`, `sensors`, `sos <text>`, `stats`.

## 1. Power: will USB or a power bank do?

The two MQ gas sensors carry heaters that run all the time and draw most of the
current.

| Field node part | Current (5 V) |
|---|---|
| Arduino Uno | ~45 mA |
| MQ-2 heater | ~150–160 mA |
| MQ-135 heater | ~150 mA |
| SX1278 at 14 dBm (only while sending, ~0.3 s every 2 s) | ~60 mA |
| MPU-6050, mic module, DS18B20, tilt switch | ~12 mA |
| **Total** | **~360 mA steady, ~430 mA peaks** |

- **Phone power bank → Uno USB port: best choice.** A 10 000 mAh bank runs the node for roughly 12–15 hours. Some banks switch off below ~100 mA, but this node draws well above that, so it stays on.
- **Laptop USB 3 port (blue): fine** (900 mA).
- **Laptop USB 2 port: borderline** (500 mA, the same as the Uno's own fuse). If the node resets when the heaters start, use the power bank or a USB 3 port. For a quick bench test you can also unplug one MQ sensor.
- **Don't use a 9 V battery.** It can't feed the gas heaters for long, and the Uno's regulator overheats.

| Gateway part | Current |
|---|---|
| Uno + SX1278 listening | ~60 mA |
| 16x2 I2C LCD with backlight | ~20–30 mA |

Laptop USB powers the gateway easily, LCD included. If the LCD stays blank,
the sketch carries on without it.

**The SX1278 runs on 3.3 V.** Power it from the Uno's **3.3V pin**, never 5V. The pin can deliver ~150 mA, which covers 14 dBm.
If you have them, put a 100 µF + 0.1 µF capacitor across the module's 3.3V/GND.

---

## 2. Field node wiring (Uno #1)

### SX1278 / Ra-02 → Uno

The Ra-02's logic is 3.3 V and the Uno's is 5 V. On the four lines the Uno
*drives*, use a resistor divider: **Uno pin → 1 kΩ → module pin**, then from the
module pin **2 kΩ → GND**. Two 1 kΩ resistors in series make 2 kΩ. The two
lines the module drives go straight across.

| SX1278 pin | Uno | Divider? |
|---|---|---|
| 3.3V | **3.3V** | — |
| GND | GND | — |
| NSS / CS | D10 | yes (1k / 2k) |
| SCK | D13 | yes |
| MOSI | D11 | yes |
| MISO | D12 | no (direct) |
| RST | D9 | yes |
| DIO0 | D2 | no (direct) |
| ANT | antenna | — |

```
 Uno D10 ──[1kΩ]──┬── NSS (SX1278)        same for D13→SCK, D11→MOSI, D9→RST
                  └──[2kΩ]── GND
```

> **Always fit the antenna before powering the module** (433 MHz spring
> antenna, or a straight 17.3 cm wire on ANT). Transmitting without one can
> damage the radio. Ra-02 pads are 2 mm pitch, so solder wires or use a
> breakout board rather than pushing it into a breadboard.

### Parts this build actually uses

Your kit: MQ-2, MQ-135, DS18B20 waterproof probe (red = VDD, black = GND,
yellow = DQ → D4, 10k R5 between yellow and red), tilt switch, LCD.
Not used: the **INMP441** mic (SD/WS/SCK/L-R pins) is an I2S digital mic that
an Uno cannot read (`HAS_MIC 0`; a KY-037/KY-038 analog sound sensor on A3
works with `HAS_MIC 1`), no piezo (`HAS_PIEZO 0`), no MPU-6050 (auto-detected,
sends empty fields). Without sound or piezo the human-presence score has
little evidence; gas, heat and tilt work normally.

### Resistors actually used: 9 × 10 kΩ, 2 × 220 Ω

10 kΩ = brown-black-orange-gold, 220 Ω = red-red-brown-gold.

| Tag | Value | Where | Job |
|---|---|---|---|
| R1–R3 | 10k | field node: in series on D10→NSS, D13→SCK, D11→MOSI | limit the Uno's 5 V into the 3.3 V radio |
| R4 | 10k | field node: D9→RST | radio reset (`LORA_RST_WIRED 1`) |
| R5 | 10k | DQ ↔ 5V | DS18B20 pull-up, only for a bare 3-leg sensor |
| R6 | 220 Ω | D7 → LED long leg; short leg → GND | status LED |
| R7–R10 | 10k | gateway: same as R1–R4 | same |
| R11 | 220 Ω | gateway D7 → LED | status LED |

No resistor: radio MISO/DIO0, piezo (internal pull-up on A2), tilt switch.
Never put a 220 Ω on a radio line. Series 10k is a demo-grade shortcut; a
4-channel logic level shifter is the proper part. SPI runs at 1 MHz for it.

### Sensors → Uno

| Sensor | Pin on sensor | Uno |
|---|---|---|
| **MQ-2** (smoke / LPG) | VCC · GND · **AO** (DO unused) | 5V · GND · **A0** |
| **MQ-135** (air quality / CO₂) | VCC · GND · **AO** | 5V · GND · **A1** |
| **Piezo disc** (the flat brass "plate" mic) | red (+) · black (−) | **A2** · GND, plus a **1 MΩ resistor between A2 and GND** |
| **Mic module** (KY-037 / KY-038 / MAX4466) | + · G · **AO** | 5V · GND · **A3** |
| **MPU-6050** (GY-521 gyro + accelerometer) | VCC · GND · SCL · SDA | 5V · GND · **A5** · **A4** |
| **DS18B20** (temperature) | VDD · GND · DQ | 5V · GND · **D4**, plus **4.7 kΩ between D4 and 5V** (modules on a small PCB already have it) |
| **Tilt switch** (SW-520D ball) | two legs | **D5** · GND (or module DO → D5, VCC → 5V) |
| Status LED (optional) | anode via 220 Ω | **D7** |
| PIR (optional, HC-SR501) | OUT | **D6** (set `HAS_PIR 1`) |

**If your temperature sensor is an LM35, not a DS18B20:** LM35 +Vs → 5V,
Vout → **A2**, GND → GND, and set `#define THERMAL_DS18B20 0`. The LM35 then
uses A2, so the **piezo moves to D3** (same 1 MΩ to GND). On D3 it still counts
knocks but no longer reports a level.

#### The piezo disc

The disc is a *contact* microphone. It hears what travels through solids:
tapping on a slab, a pipe or rubble, and rumbling in the structure. Tapping is
the classic signal from a trapped person, which makes this the most useful
human sensor in the kit.

- Fix it **flat against the surface** (hot glue, or tape pressed down hard). If it dangles in the air, it hears nothing.
- The 1 MΩ resistor drains the disc's charge so the reading returns to zero. Without it the pin floats.
- Optional protection: a 5.1 V Zener across it (cathode to A2), because a hard hit can make the disc produce high-voltage spikes.
- Tune `KNOCK_THRESHOLD` (default 60): raise it if it counts knocks in silence, lower it if light taps are missed.

#### The mic module

Turn its potentiometer until the module's LED is *just* off in a quiet room.
The node sends the sound level (peak-to-peak over 1.5 s). The API compares it
with that node's own quiet baseline.

#### Mounting for a demo

- Screw or tape the **MPU-6050 rigidly** to the thing you want to watch (board, wall, model building). Tilt is measured from the angle at install, so it doesn't have to be level.
- Put the **MQ sensors** where air reaches them, not inside a closed box.
- The **MQ heaters need ~2 minutes** to warm up. Until then the API marks readings `warming_up` and ignores gas.

### Full pin map (field)

```
            ┌──────── Arduino Uno #1 ────────┐
   MQ-2 AO ─┤A0                           D13├─[div]─ SX1278 SCK
 MQ-135 AO ─┤A1                           D12├─────── SX1278 MISO
 piezo(+)  ─┤A2 ─[1MΩ]─GND                D11├─[div]─ SX1278 MOSI
   mic AO  ─┤A3                           D10├─[div]─ SX1278 NSS
  MPU SDA  ─┤A4                            D9├─[div]─ SX1278 RST
  MPU SCL  ─┤A5                            D7├─[220Ω]─LED─GND (optional)
            │                              D6├─ PIR OUT (optional)
            │                              D5├─ tilt switch ─ GND
            │                              D4├─ DS18B20 DQ ─[4.7k]─ 5V
            │                              D2├─ SX1278 DIO0
  SX1278 3.3V ─┤3.3V                          │
  MQ×2, mic, MPU, DS18B20 VCC ─┤5V            │
  all grounds ─┤GND              USB ◄ power bank
            └────────────────────────────────┘
```

---

## 3. Gateway wiring (Uno #2)

The SX1278 is wired **exactly as on the field node** (same pins, same dividers, 3.3V, antenna).

| 16x2 LCD with I2C backpack | Uno |
|---|---|
| VCC | 5V |
| GND | GND |
| SDA | A4 |
| SCL | A5 |

The sketch finds the LCD at 0x27 or 0x3F on its own. If the screen lights up
but shows no text, turn the contrast potentiometer on the back of the backpack.
No LCD at all is fine.

LCD shows:
```
RN01 #184 -67dB       ← node, packet number, signal strength
MQ2 382 135 614       ← cycles every 2 s: gas / temp + tilt / mic + piezo + knocks
```

---

## 4. Flash the boards

1. Arduino IDE → Library Manager, install:
   - **LoRa** (Sandeep Mistry)
   - **OneWire** and **DallasTemperature** (field node, DS18B20 only)
   - **LiquidCrystal I2C** (Frank de Brabander) (gateway)
2. Use a USB cable that carries **data**; a charge-only cable shows no port. Select Board **Arduino Uno** and the right COM port.
3. Field node: open `field_node.ino` and set `NODE_ID` (each node unique: RN01, RN02…) and `THERMAL_DS18B20` (1 or 0). Upload.
   - Serial Monitor at **115200** should print `#NODE RN01 imu=1 temp=1`, then a packet line every 2 s.
   - `imu=0` means the MPU-6050 wiring is wrong; `temp=0` means the DS18B20 is wrong.
4. Gateway: upload `gateway.ino`. Serial Monitor at 115200 shows `GW|READY|433`, then `RX|RN01|…` lines.
   - **Close the Serial Monitor before starting the bridge**: only one program can hold the port.

Both sketches compile for the Uno with room to spare (field ~46 % flash / 33 % RAM, gateway ~36 % / 39 %).

`#ERR LoRa not found` means the module isn't answering. Check, in order: 3.3V, then GND, then NSS→D10 and RST→D9, then the dividers.

The radio settings (433 MHz, SF9, BW 125 kHz, sync word 0xA5, CRC on) must
match on both boards; they do by default.

---

## 5. One-time setup on the API

1. **Create the tables.** Run `backend/migrations/027_iot_sensor_nodes.sql` in the Supabase SQL editor (project *Indradhanu*). It only adds two new tables.
2. **Deploy the backend** (push and let Render redeploy). That adds:
   - `POST /api/v1/iot/observations`, authenticated with the existing `MESH_GATEWAY_KEY` (the same key the mesh gateways use)
   - `GET /api/v1/analytics/overview | spatial | timeseries` (staff login)
3. **Deploy the frontend.** The new page is under **Operations → Sensor analytics** (`/admin/analytics`).

---

## 6. Run the bridge on the command-centre laptop

```powershell
pip install pyserial requests
cd hardware\lora
python lora_bridge.py --api https://disruptionops.onrender.com --key <MESH_GATEWAY_KEY> `
    --loc RN01=18.5204,73.8567
```

- It finds the gateway Arduino's COM port on its own; pass `--port COM5` to choose one.
- `--loc NODE=lat,lon` puts the node on the map, since an Arduino has no GPS. Pass one per node. The API remembers it, so you only need it again when you move the node.
- The bridge doesn't think: it reads lines, splits them and posts them. If the internet drops, readings queue up, and survive Ctrl+C in `lora_bridge_backlog.jsonl`. They're sent with their original timestamps when the API is reachable again.
- Every batch prints what the API concluded:
  ```
  RN01: human 82%  structural 74%  environment 12%  -> ESCALATED ['collapse', 'trapped']
  ```

**No hardware yet?**

```
python lora_bridge.py --api … --key … --simulate
```

This creates three fake nodes, SIM-01..03, around Pune. After about 60 s,
SIM-03 develops a survivor-under-rubble scene: tapping, voices, lean, smoke.
Simulated nodes appear on the page marked *simulated*. They **do not file
incidents** unless the API runs with `IOT_ESCALATE_SIMULATED=1`.

---

## 7. What the API makes of a reading

Every channel is judged against **that node's own quiet baseline**, learned
from its first ~15 readings (gas only after warm-up). The baseline then follows
slow drift while things are calm, and never learns an emergency as "normal".
The scores are transparent evidence weights, not a trained model. Each reading
stores what each score was made of.

| Score | Built from |
|---|---|
| **Human presence** | sound above the room's level (0.55), **repeated tapping on the piezo** (0.75), warmth near body temperature (0.25), CO₂ rise on MQ-135 without smoke (0.2), PIR (0.6), combined as independent clues |
| **Structural** | lean since install (>1.5°, full at ~10°), tilt switch flipped, gyro shocks, sustained shaking (accelerometer and piezo rumble) |
| **Environmental** | MQ-2 / MQ-135 rise over baseline, temperature over 45 °C, or a fast rise |
| **Overall** | the worst hazard, lifted when a person is inside a dangerous zone; this is the heatmap's default |

**Escalation into the incident pipeline.** A hazard has to hold for two
readings in a row (about 4 s). It's then filed as a sensor **S packet**, the
same path the VLM cameras use, which gives it the same trust scoring,
de-duplication, incident and commander nudge.

| Condition | Filed as |
|---|---|
| gas/heat ≥ 70 % | **fire / smoke** |
| structural ≥ 70 % | **collapse** |
| human ≥ 65 % **and** structural or gas ≥ 40 % | **collapse**, captioned "possible person in a hazardous spot" |

Each kind cools down for 10 minutes per node. The node needs a location
(`--loc`) for this.

**The agent** gets the same data through a copilot tool,
**`get_sensor_field`**: every node's scores, what drove them, its raw
readings and its location. Ask the copilot *"is anyone trapped near RN01, and
is it safe to send a crew in?"*.

Honest limits:

- A DS18B20 or LM35 measures temperature *at the probe*. It's good for fire and heat, but weak evidence of a person. For real body-heat detection, add an MLX90614 or an AMG8833, or a PIR (`HAS_PIR`).
- Cheap MQ sensors drift with humidity and temperature. That's why everything is relative to the node's baseline.

---

## 8. Demo script

1. Power the field node and wait for the MQ heaters (~2 min, `warming_up` on the page).
2. Start the bridge. The node appears on the map, green.
3. **Tap the piezo disc** 3–4 times, pause, and repeat. Talk near the mic. *Human presence* rises, and the evidence bars show *Tapping on rubble* and *Voices*.
4. **Tilt the board** more than 10° and flip the tilt switch. *Structural* goes red, a marker appears on the structure chart, and with a person signal present the node **escalates**. A red line marks it on the risk timeline, and it shows under *Incidents & response*.
5. Gas: hold an **unlit** lighter near the MQ-2 for a second, away from any flame. *Gas / heat* rises.
6. Ask the copilot what the sensors say.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| No COM port in Arduino IDE | Use a data USB cable; install the CH340 driver for clone boards |
| `#ERR LoRa not found` | 3.3V (not 5V), NSS→D10, RST→D9, dividers |
| Gateway hears nothing | antenna on both, same `LORA_FREQ`/`LORA_SYNC`, field node actually sending (its Serial Monitor) |
| `#BAD packet` lines | another 433 MHz device nearby; harmless, they are dropped |
| Node resets every few seconds | power: move to a power bank / USB 3, or unplug one MQ |
| `imu=0` | MPU-6050 SDA→A4, SCL→A5, VCC 5V |
| Knocks counted in silence | raise `KNOCK_THRESHOLD`; check the 1 MΩ resistor |
| Bridge: `403` | `--key` must equal the API's `MESH_GATEWAY_KEY` |
| Bridge: port busy | close Arduino IDE's Serial Monitor |
| Node on the page but not on the map | run the bridge once with `--loc NODE=lat,lon` |
