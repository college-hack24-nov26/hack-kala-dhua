# Setup runbook: from Android Studio to a working offline demo

Follow in order. Each part ends with a check; do not move on until it passes.
Background: [`OFFLINE_MESH.md`](OFFLINE_MESH.md), [`CHANGELOG_2026-09-26.md`](CHANGELOG_2026-09-26.md).

You need: Android Studio (recent), 2–3 Android phones (Android 8+), a USB cable,
the laptop, and access to Render and Vercel.

---

## Part A: Build the bitchat APK

1. **Open the project.** Android Studio → *Open* → `D:\GITHUB\kurukshetra_hackathon\src\bitchat-android`
   (the teammate's real source: roles, shelters, SOS, offline map, VLM API, plus the command-centre link).
   Wait for the Gradle sync to finish (first time: 5–15 min; it downloads Gradle 9.5).
2. **Two JDKs.** The Gradle daemon needs Java **25** (Android Studio's bundled JBR is 25,
   so keep *Settings → Build Tools → Gradle → Gradle JDK* on it). The app compiles with Java **21**
   (`jvmToolchain(21)`), which must be installed separately: `winget install EclipseAdoptium.Temurin.21.JDK`,
   or in that Gradle JDK dropdown *Download JDK… → version 21* (then switch the dropdown back to 25).
   Close and reopen Android Studio so Gradle finds it.
3. **SDK.** The project compiles against API **37**. If sync says a platform is
   missing, click the link in the error, or *Tools → SDK Manager → SDK Platforms*,
   tick the latest (API 37), *Apply*.
4. **Check the changes are there.** In the Project view these must exist:
   `vlm/IndradhanuGateway.kt` (command-centre link), `ui/map/RouteCache.kt` (offline routes),
   `net/LastFix.kt`, and `app/src/main/jniLibs/*/libarti_android.so` (Tor; the repo's
   `.gitignore` used to drop every `*.so`, which is why the teammate's copy lacked them).
5. **Build (terminal, in `D:\GITHUB\kurukshetra_hackathon\src\bitchat-android`).** The repo pins every dependency
   (lockfiles) and checks every download's checksum. On a new Windows machine it is
   missing a few entries, so the first build records them. Run once:
   ```powershell
   .\gradlew.bat --stop
   .\gradlew.bat :app:dependencies :wear:dependencies resolveIdeRuntimeClasspathCopyLocks --write-locks --write-verification-metadata sha256
   .\gradlew.bat assembleDebug --max-workers=2 "-Pkotlin.compiler.execution.strategy=in-process" --write-verification-metadata sha256
   ```
   After that, `.\gradlew.bat assembleDebug --max-workers=2 "-Pkotlin.compiler.execution.strategy=in-process"` is enough.
   `--max-workers=2` and in-process Kotlin keep memory down (a laptop with 8–16 GB ran out otherwise).
   Use **debug**; release needs a signing key. `w:` lines are warnings, not errors.
   - If a Kotlin file fails to compile, copy the first `e:` line (file + line) and send it.
6. **Find the APK.** `app\build\outputs\apk\debug\` → `app-debug.apk`
   (there may be one per CPU type; `arm64-v8a` or `universal` is the one for phones).

**Check:** the APK file exists.

---

## Part B: Install on the phones

Do this on **every** phone (at least 2).

1. If the Play Store bitchat is installed, **uninstall it first** (same app id
   `com.bitchat.droid`, different signature, so install fails otherwise).
2. Phone: *Settings → About phone → tap Build number 7×* → *Developer options →
   USB debugging ON*.
3. Plug in, accept the "Allow USB debugging" prompt.
4. Android Studio: pick the phone in the device dropdown → ▶ Run. Or:
   `adb install -r app\build\outputs\apk\debug\app-debug.apk`
   (`adb` is in `%LOCALAPPDATA%\Android\Sdk\platform-tools`).
5. Open bitchat, allow **every** permission it asks for: Nearby devices /
   Bluetooth, Location, Notifications.
6. *Settings → Apps → bitchat → Battery → Unrestricted*, so the mesh keeps
   running with the screen off.
7. Set a nickname per phone (e.g. `gateway`, `citizen1`, `crew1`).
8. *About → Settings*: pick each phone's **role** (Civilian, Ambulance, Fire, Gov, Command).
   Command-centre alerts arrive on the main timeline tagged **Command**; the chips under
   the header (All · Command · Ambulance · Fire · Gov · Civilian) sort the timeline by who is speaking.

**Check:** with Bluetooth on, each phone shows the others as peers (people icon /
peer count) within a minute. **Every phone must run this same build.** A phone on the
Play Store bitchat or an older build cannot read role-tagged messages (the category
byte changes the signed packet), which looks like "the mesh does not connect".

---

## Part C: Turn on the local API (every phone)

1. In bitchat: open the **About / Settings** sheet → **Settings** tab → **VLM API**
   → turn **Enable** on. Leave the port at **8765**.
2. **Force-stop and reopen bitchat** (*Settings → Apps → bitchat → Force stop*):
   the API server only starts when the mesh service starts.
3. Test from the laptop, phone on USB:
   ```powershell
   adb forward tcp:8765 tcp:8765
   curl.exe http://127.0.0.1:8765/status
   curl.exe "http://127.0.0.1:8765/inbox?since=0"
   curl.exe http://127.0.0.1:8765/gateway
   ```

**Check:** `/status` shows `"api_enabled":true` and a `peers_count`;
`/inbox` answers `"status":"ok"` (if it says *Not found*, the phone is running an
old build, so go back to Part A); `/gateway` shows `"enabled":false`.

---

## Part D: Keys, backend, deploy

1. Generate two secrets on the laptop:
   ```powershell
   python -c "import secrets;print(secrets.token_urlsafe(32))"   # -> MESH_GATEWAY_KEY
   python -c "import secrets;print(secrets.token_urlsafe(32))"   # -> MESH_HMAC_KEY
   ```
2. Put both in `backend\.env` (local runs) **and** in Render → your service →
   *Environment* → add `MESH_GATEWAY_KEY`, `MESH_HMAC_KEY` → *Save*.
3. The database needs nothing: migrations 020, 021 and 022 are already applied.
4. Run the tests (from `backend`, inside your venv):
   `python -m unittest discover -s tests -v` → 30 tests OK.
5. Run locally once: `uvicorn app.main:app --reload --port 8000`. Open the
   console, start the demo, and make sure nothing errors in the terminal.
6. Commit and push DisruptionOps (review the diff first). Render and Vercel
   redeploy on push.
7. Check the deployed API:
   ```powershell
   $api = "https://disruptionops.onrender.com"
   curl.exe -H "X-Mesh-Gateway-Key: <MESH_GATEWAY_KEY>" "$api/api/v1/mesh/outbox?gateway_id=test"
   ```

**Check:** the last call returns `{"messages":[...]}` (an empty list is fine),
not 403.

---

## Part E: Make one phone the gateway

Pick the phone that **will have mobile data** during the demo. Any phone can be a
gateway; several can at once (the API deduplicates).

1. bitchat → *About* → **Command Centre**.
2. **Enable sync on reconnect** on; API `https://disruptionops.onrender.com`;
   gateway key = `MESH_GATEWAY_KEY`; city `pune`; **Listen to command centre** on.
3. Tap **Sync now**. Set the phone's role to **Command** (About → Settings) so the
   centres it gossips show as verified on other phones.

What it then does every 10 s while online: forwards IDX1 packets heard on the mesh
(`/mesh/inbound`), posts SOS / geotagged posts / VLM briefs / shelters
(`/mesh/civ-sync`, every 30 s), pulls alerts and dispatches (`/mesh/outbox`) and
broadcasts them tagged *Command*, and every 10 min caches shelters, relief centres,
hospitals, medical camps, food and water points (`/lifelines`, `/shelters`), gossips the
nearest 15 to the mesh, and saves walking routes to the 3 nearest.

Same settings over USB, if you prefer (`adb forward tcp:8765 tcp:8765`):

```powershell
$body = @{ enabled = $true; api_url = "https://disruptionops.onrender.com";
           gateway_key = "<MESH_GATEWAY_KEY>"; city_id = "pune"; listen = $true } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/gateway -Body $body -ContentType "application/json"
Invoke-RestMethod http://127.0.0.1:8765/gateway
```

**Check:** the Command Centre sheet shows `online true`, "Alerts pulled" and
"Centres cached" with a time, no "Last error". The *Shelters* count in the header goes up.

*No phone with data?* Use the laptop as the gateway instead (Part G, option 2).

---

## Part E2: Mesh + VLM check (after the APK is on two phones)

Laptop and both phones on the same Wi-Fi, VLM API enabled on both (force-stop and
reopen bitchat after enabling). From `kurukshetra_hackathon\src\ai-surveillance`:

```powershell
python tools\mesh_vlm_check.py --a <phone A IP> --b <phone B IP>
python tools\mesh_vlm_check.py --a <phone A IP> --b <phone B IP> --api https://disruptionops.onrender.com
python tools\mesh_vlm_check.py --a <phone A IP> --b <phone B IP> --vlm <VLM laptop IP>
```

The VLM runs on the teammate's GPU laptop (`python run_vlm_to_bitchat.py`). It
now also serves `GET /health` and `POST /analyze` on port **8780**, so any laptop
on the same Wi-Fi reaches it by IP; `--vlm` checks it, points it at phone A,
runs the model on a test image and sends image + brief into the mesh. Allow
Python through Windows Firewall (private networks) on that laptop. If the phone's
IP changed, the VLM laptop finds it itself (`bitchat.auto_discover: true`).

Checks: both phones answer and see a peer; a message crosses the mesh A → B and
B → A; an image + brief goes out the way the camera sends it; A's command-centre
link; the API's event router. Then the two manual checks it prints: the real VLM
(`python run_vlm_to_bitchat.py`, press SPACE) and phone B with Wi-Fi and data off.

**Check:** 0 failed. A WARN on the command-centre link is fine for a mesh-only test.

---

## Part F: First end-to-end test, without real phones (5 minutes)

From `backend`:

```powershell
python scripts\mesh_bridge.py sim --api https://disruptionops.onrender.com `
  --key <MESH_GATEWAY_KEY> --hmac <MESH_HMAC_KEY> `
  --report flooded_road 18.5204 73.8567 "Water knee deep near the signal"
```

**Check:** the first result says `"outcome": "report"` (or `linked`), the second
says `"duplicate"`, and the incident appears on the console map.

---

## Part G: Real phones

### Option 1: a citizen phone with no signal

1. On the **citizen** phone, *with internet*, open the citizen site in Chrome,
   *Add to Home screen*, and ask for a route once (this caches the app and your
   route).
2. Turn **mobile data and Wi-Fi off**; keep **Bluetooth on**. Keep bitchat running.
3. Open the installed app. A **Mesh mode** panel appears ("The server is out of
   reach"). The first time, Chrome asks to allow access to devices on your
   local network: **Allow**.
4. It should say *bitchat connected · N phones in range*. Type a report, tap
   **Send what I typed through the mesh**.
5. The **gateway** phone (Part E) hears it and forwards it within ~10 s of
   having data.

**Check:** the incident appears on the console; `curl.exe http://127.0.0.1:8765/gateway`
on the gateway shows `queued: 0` and a recent `last_forward_ok_ms`.

### Option 2: the laptop as gateway (no app rebuild needed)

Laptop and a bitchat phone on the same Wi-Fi (or USB with `adb forward`):

```powershell
python scripts\mesh_bridge.py --api https://disruptionops.onrender.com `
  --key <MESH_GATEWAY_KEY> --phone <phone-ip or 127.0.0.1>
```

It prints `[in ]` for every packet forwarded and `[out]` for every alert or
dispatch broadcast.

### Alerts coming back

1. On the console, approve an advisory in the affected ward (or run a scenario,
   below).
2. Within ~10 s the gateway broadcasts it. Every phone shows it on the main
   timeline (tap the **Command** chip to see only those), pinned on the **Map**, and in
   the web app's Mesh mode panel with its distance.

**Check:** alert text visible on the offline phone.

### Offline navigation to the nearest centre

1. With signal, open **Map** once and pan around your area (tiles are kept for 30 days),
   and let the gateway cache centres (Part E).
2. Turn data off. **Map → Nearest centre**: a green route (cached walking route if one was
   saved from near here, otherwise a straight line with a compass direction), distance and
   minutes. **Next** cycles to the next nearest; full or closed centres come last.

### SOS and VLM briefs reaching the console

- Any phone: type a message and tap **SOS** (adds GPS). Offline phones' SOS reaches the
  gateway over the mesh; the gateway posts it within 30 s of having data.
- VLM: `ai-surveillance` posts to `/send/image` and `/send/text` on a phone. The brief is
  broadcast tagged Fire / Ambulance / Gov from its words, and the gateway forwards it
  (fire, smoke and flood briefs as camera detections; the rest as reports). `/send/text`
  also takes optional `lat`, `lon` and `category`.

---

## Part H: The camera node (optional)

1. `D:\GITHUB\ai-surveillance\configs\pipeline.yaml`:
   - `bitchat.ip`: the Wi-Fi IP of a bitchat phone
   - `indradhanu.enabled: true`, `api`, `gateway_key`, `hmac_key`, `node_id`,
     and the camera's real `lat` / `lon`
   - keep `disaster_mode: true`
2. `python -m pipeline.main_loop`. The log prints `[indradhanu] bridge on …`
   and, per detection, `https 200 …` (online) or `mesh 200 …` (offline).

**Check:** a fire held up to the camera (a phone video works) becomes an
incident marked with the camera as its source.

---

## Part I: Show the rest

- **Scenarios** (console logged in as staff):
  `POST /api/v1/demo/scenarios/block_on_approach/run`, then
  `GET /api/v1/demo/scenarios/last` for PASS/FAIL. Names: `flood_cascade`,
  `block_on_approach`, `unit_breakdown`, `officer_redirect`, `officer_hold`,
  `camera_fire_mesh`, `mesh_blackout_sos`, `multi_hazard_surge`.
- **Cancel / do instead:** *Who is on what* → *Cancel…* on a unit, or ask the
  Copilot "cancel <unit> and hold it for 20 minutes".
- **Incident Commander:** Copilot page, left column → *Wake it* (needs
  `GEMINI_API_KEY` or Bedrock configured).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Gradle sync: "requires Java 17" | Part A step 2 |
| "Cannot find a Java installation … languageVersion=21" | JDK 21 missing: Part A step 2 |
| "Dependency verification failed … kotlin-stdlib-2.3.20 / aapt2-…-windows.jar" | Part A step 5 (`--write-verification-metadata sha256`) |
| "Locking strict mode: Configuration ':app:androidTestUtil' is locked but does not have lock state" | Part A step 5 (`--write-locks` line) |
| `OutOfMemoryError` / "Gradle build daemon disappeared" | Close Chrome; `--stop`; build with `--max-workers=2` and in-process Kotlin; let Windows manage the page file |
| Android Studio offers "updateDaemonJvm --jvm-version 21" | Don't: Gradle runs on 25; only the compile needs 21 |
| "Failed to find target android-37" | Part A step 3 |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | Uninstall the old bitchat (Part B step 1) |
| `/status` connection refused | VLM API off, or bitchat not restarted after enabling (Part C step 2) |
| `/inbox` → Not found | Old APK on the phone; reinstall the new build |
| Gateway `last_error: inbound HTTP 403` | Key mismatch, or `MESH_GATEWAY_KEY` not set on Render |
| `online: false` on the gateway | No validated internet on that phone (captive portal, no data) |
| Mesh panel: "bitchat not found" | VLM API off, or the local-network prompt was blocked: Chrome → site settings → *Local network access* → Allow |
| Phones do not see each other | Same build on every phone (Part B check); Bluetooth + Location on, all permissions granted, battery unrestricted, within ~10 m for the first test |
| Command Centre sheet: "Last error: HTTP 403" | Gateway key wrong, or `MESH_GATEWAY_KEY` not set on Render |
| "Nearest centre": "No centres cached yet" | The gateway has not reached `/lifelines` yet: Sync now while online, or add a shelter from *Shelters* |
| Map is grey offline | Tiles are only what you viewed while online; open the map over your area once with signal |
| Role chip shows nothing | Chips filter strictly by the sender's role; tap **All** |
| `/send/text` → 400 "Rate limited" | Normal: 5 s between sends. The gateway and bridge wait for it |
| API returns `"outcome": "refused"`, "outside every ward" | The lat/lon is outside the city's wards; use a Pune coordinate |
