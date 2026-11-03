# BiChat × Indradhanu — progress and roadmap

Shared between two repositories; the same file lives in both:

- **kurukshetra_hackathon** — `src/bitchat-android` (BiChat, the Android mesh app) and
  `src/ai-surveillance` (camera + VLM pipeline).
- **DisruptionOps** — `backend` (command centre API) and `frontend/indradhanu`
  (admin dashboard, citizen PWA `/citizen`, crew PWA `/field`).

Update this file in **both** repos whenever an item moves.

## The product, in one paragraph

One phone app for citizens and field crews. **With internet** it is the full Indradhanu
experience, connected to the command centre: live map, guidance routes, dispatches,
reports with photos and voice. **Without internet** the same app falls back to BiChat:
Bluetooth/Wi-Fi mesh chat, SOS, the offline map with saved routes, and reports and
status updates that travel phone to phone until one has signal. Switching between them
keeps the person's place: signed-in identity, current route, selected incident, unsent
drafts. VLM camera alerts are for government only and never reach citizens.

---

## Done

| Date | Repo | What |
|---|---|---|
| 2026-10-03 | kurukshetra | Full-screen emergency map in BiChat (`ui/map/EmergencyMap*.kt`): dark map, clustering, SOS pulse, draggable sheet, filters, "View on map" from chat, open chat from map. |
| 2026-10-03 | kurukshetra | Map navigation uses `RouteCache` saved routes (green, works offline), fetches when online, honest dashed direct line otherwise. |
| 2026-10-03 | kurukshetra | Build fixes after merge: duplicate `parseGeoTag`, `Role.COMMAND` in map `when`s; Android Studio sync past dependency verification (trust `-sources`/`-javadoc` only). |
| 2026-10-03 | kurukshetra | Removed `_claude_tmp/` from the repo and ignored it. |
| 2026-10-03 | DisruptionOps | Citizen and crew PWAs map-first: full-screen map, bottom sheet (phone), floating card (tablet), map + panel (desktop), nav panel, filters, connectivity pill, Mapbox Standard night basemap, badge markers shared with Android. |
| 2026-10-03 | DisruptionOps | Navigation from the command centre is visible: citizen gets `/citizen/guide` routes (auto-started by an advisory with a safe location); crews get their unit's dispatched route, steps and ETA from `/field/state`, with hazards ahead and re-route notices. |
| 2026-10-03 | both | VLM/camera alerts are government-only (item 1). |
| 2026-10-03 | both | Sign-in in BiChat with the PWA accounts (item 2), one app online/offline (item 3), native report and crew status over the mesh (item 4), Apple-style redesign foundation (item 5). |
| 2026-10-03 | kurukshetra | Map shows command-centre road closures and alerts with their radius, and the crew's dispatch as "Your task" (item 6). Remaining screens restyled; emoji icons removed (item 5). |
| earlier | kurukshetra | `IndradhanuGateway`: phone ↔ command centre bridge (push mesh SOS/reports, pull outbox alerts/dispatches/road blocks onto the mesh as role COMMAND, cache centres + routes). See `src/bitchat-android/docs/COMMAND_CENTRE_LINK.md`. |

Verified only in this order: PWA typecheck/build/lint (no new errors); backend tests;
new Android files and the map compiled against a stand-in classpath (map packet parsing
also run). Edits inside existing Android screens (ChatScreen, ChatHeader, AboutSheet,
MessageComponents, ChatViewModel, MainActivity, InputComponents, ShelterSheet,
MeshPeerListSheet, LocationChannelsSheet, PermissionExplanationScreen) were reviewed by
hand, not compiled. **The Android app has not yet been built on a device** —
do that first on the next session (`cd src/bitchat-android && ./gradlew :app:assembleDebug`).

---

## To do (in order)

### 1. VLM alerts → government only  ·  status: done (verify on devices)

- [x] Android: every broadcast VLM brief carries an IDX1 `S` packet
      (`VlmMessageHandler.asSensorPacket`, unsigned `-`, kind from the text, last GPS fix).
      It still travels the mesh so any gateway phone forwards it to the command centre.
- [x] Android: `services/AudiencePolicy.kt` hides `S` packets and `vlm-` messages from the
      public timeline, channels, map and haptics unless the local role is GOV or COMMAND.
- [x] Backend: sensor reports are keyed `device:sensor:<node>`; `/citizen/state` and
      `/field/state` drop incidents only cameras reported (`CAMERA_ONLY` in
      `api/v1/personas.py`) until an operator moves them past `reported` or a person
      reports the same thing. The dashboard is unchanged and sees everything.
      `/citizen/guide` still routes around them (safety, not display).
- [x] PWA: no change needed; both apps read incidents only from those two endpoints.
- [ ] On devices: a Civilian phone next to a Gov phone; send a VLM brief; only the Gov
      phone shows it, and it appears on the dashboard.

### 2. Sign-in in BiChat (citizen and field crew)  ·  status: done (verify on devices)

- [x] `account/Account.kt` + `account/SignInScreen.kt`: email + password against the same
      Supabase project as the PWA. The phone only needs the command centre address; the
      Supabase URL, public key and PWA address come from the new public
      `GET /api/v1/auth/client-config` (DisruptionOps `api/v1/auth.py`).
- [x] `/auth/me` decides the role. Mapping (`Session.meshRole`): citizen → Civilian,
      field operator → Fire / Ambulance / Gov by the agency name, staff → Gov. Never COMMAND.
- [x] Session kept in EncryptedSharedPreferences; works offline, marked "last confirmed …"
      in Settings → Account. Token refresh on demand; revoked refresh token signs out.
- [x] Citizens can "Continue without an account".
- [ ] On devices: sign in as `fire@pune.indradhanu.local` and check the mesh role is Fire.

### 3. One app: online = Indradhanu, offline = BiChat  ·  status: done (verify on devices)

- [x] `shell/AppShell.kt`: sign-in first, then the PWA (`/citizen` or `/field` by role) in a
      WebView when the network is validated and the app URL is known; the native mesh app
      otherwise, or when the page fails to load (held for a minute, then retried).
- [x] Manual switch both ways: "Use mesh" next to the connection status in the PWA (only
      inside the app), "You're online · Open" banner on the mesh home. Choosing the mesh by
      hand pins it until the person goes back online.
- [x] `window.BiChatNative` (`shell/BiChatBridge.kt`, PWA `src/lib/native.ts`), answers only
      the app's own origin: the PWA adopts the phone's session (no second sign-in), saves
      route / destination / draft / position / crew unit; mesh sends and inbox go through it
      instead of the loopback API.
- [x] Handoff (`shell/Handoff.kt`): mesh side reopens the map guiding to the PWA's
      destination and restores the draft; PWA side re-asks the same guidance intent and
      restores the report draft.
- [x] Reports queued in the PWA's service-worker outbox stay there and send when online
      (no duplicate over the mesh).
- [ ] Photo/voice inside the WebView: file chooser and mic permission are wired; test.
- [ ] Set `PUBLIC_APP_URL` on the API (or rely on the first non-localhost CORS origin).

### 4. Offline parity (what works online works on the mesh, to some extent)

| Online (PWA) | Offline (BiChat) | Status |
|---|---|---|
| See incidents, alerts, road blocks | Command broadcasts via gateway; closures (`B`) and alerts (`A`) drawn as areas on the map | done |
| Guidance to shelter/hospital/food/water | Saved routes (`RouteCache`) + the PWA's destination carried over | done |
| Report a hazard (text/photo/voice) | Report sheet → IDX1 `R` (category + words + GPS) | done (text only) |
| Crew status (accepted, on site, done, road blocked) | Report sheet "My task" → IDX1 `F` with the unit id from the PWA | done |
| Crew dispatch + route | Dispatch `D` for this unit → "Your task" on the map and the guidance target; cancel `C` removes it | done |
| SOS | SOS sheet (quick reasons + note) → SOS broadcast + map | done |

### 5. Apple-style redesign of all of BiChat  ·  status: done (verify on devices)

Design direction (from the ui-ux-pro-max review): calm, trustworthy, system-like;
no emoji icons; no purple/pink AI gradients; red reserved for emergencies.

- [x] Tokens: Inter (bundled, OFL in `docs/third-party`), iOS system colours light/dark,
      iOS type scale on Material roles, spring motion, grouped-list palette, bubble colours.
- [x] Components (`ui/design/AppleComponents.kt`): inset groups, rows, toggle rows, field
      rows, switch, buttons, fields, segmented control, status pill, large title.
- [x] Home: avatar + "Nearby · N people in range" header, quick actions (SOS, Report, Map,
      Places), online/offline banner, capsule role filters.
- [x] Chat: iMessage-style bubbles; composer with send-with-location next to a filled send
      button (fixes the old SOS/location buttons that could never send).
- [x] Sheets: grabber, grouped background. Settings: Account, "Name on the mesh".
- [x] Sign-in, Report, SOS, Places (was Shelters), command centre link, camera (VLM) settings.
- [x] Onboarding: one setup-assistant layout (`onboarding/OnboardingStep.kt`) for permissions,
      Bluetooth, location, battery, background location, start-up and error.
- [x] People and channel lists, conversation header: readable sizes, primary-colour names.
- [x] No emoji used as icons: notification previews use words; the unused emoji-marker
      offline map sheet is removed.
- [ ] Not restyled (rarely seen): debug settings, verification sheets, location notes.

### 6. Command-centre navigation visibility  ·  status: done

- [x] Crew PWA shows its unit's dispatched route.
- [x] Citizen PWA shows the guide route and auto-routes on an advisory.
- [x] BiChat native map draws command-centre road blocks (`B` packets) as closures (dashed
      yellow area), warns when the way passes through one, and uses the crew's dispatch (`D`)
      as its guidance target. Needs the crew's unit id, which the field PWA hands over.

---

## Known constraints

- The Android SDK cannot be downloaded in the cloud coding environment, so Android
  changes are compile-checked against stand-ins only. Build on a real machine.
- Mapbox and OSM tile hosts are not reachable from that environment either; PWA map
  screenshots there use a plain dark stand-in basemap.
- All phones must run the same BiChat build (role-tagged packets).
