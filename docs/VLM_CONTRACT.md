# Vision service contract

Build your service to this and nothing on our side changes. `VLM_URL` in
`backend/.env` points at it; `VLM_API_KEY` and `VLM_MODEL` are optional.

## 1. What we POST to your endpoint

```http
POST <VLM_URL>
Authorization: Bearer <VLM_API_KEY>      # only if VLM_API_KEY is set
Content-Type: application/json
```

```json
{
  "prompt": "<the full prompt text, sent every time — see section 3>",
  "model": "qwen2.5-vl-3b-instruct",
  "response_format": "json",
  "max_tokens": 512,
  "temperature": 0.1,
  "image_url": "https://.../photo.jpg",
  "image_base64": null
}
```

Exactly one of `image_url` / `image_base64` is non-null. Prefer handling
`image_url` — we would rather not push a megabyte of base64 through our process
to reach your GPU. `image_base64` is raw base64, no `data:` prefix.

The `prompt` is sent on every call on purpose: we can change what we ask the
model without you redeploying.

## 2. What you return

Any of these shapes work — we unwrap all of them, so use whatever your serving
stack produces naturally:

```json
{ "analysis": { ...the object below... } }
{ "result":   { ...the object below... } }
{ "text": "```json\n{ ...the object below... }\n```" }
{ ...the object below, at the top level... }
```

Fenced code blocks and a leading "Here is the JSON:" are tolerated. HTTP 200
with valid JSON; any non-2xx is treated as the service being down, which costs
the report its corroboration bonus and nothing else.

### The object

```json
{
  "shows_hazard": true,
  "hazard_visible": ["water_on_road", "debris"],
  "water": {
    "present": true,
    "depth_band": "knee",
    "moving": true,
    "reference": "water reaches the middle of the car door"
  },
  "people": {
    "visible": 3,
    "in_water": 1,
    "apparently_trapped": false
  },
  "vehicles_visible": 2,
  "time_of_day": "day",
  "image_quality": "clear",
  "looks_staged_or_reused": false,
  "caption": "Water covering a two-lane road, a stalled car and people wading.",
  "confidence": 0.82
}
```

| Field | Type | Allowed values |
|---|---|---|
| `shows_hazard` | bool or `"unclear"` | — |
| `hazard_visible` | string[] | `water_on_road`, `standing_water`, `fallen_tree`, `damaged_structure`, `downed_power_line`, `blocked_drain`, `fire`, `debris`, `crowd`, `none` |
| `water.present` | bool | — |
| `water.depth_band` | string | `none`, `ankle`, `knee`, `waist`, `above_waist`, `unclear` |
| `water.moving` | bool or `"unclear"` | — |
| `water.reference` | string | free text, ≤120 chars, what the depth was judged against |
| `people.visible` | int | 0–500 |
| `people.in_water` | int | 0–500 |
| `people.apparently_trapped` | bool or `"unclear"` | — |
| `vehicles_visible` | int | 0–500 |
| `time_of_day` | string | `day`, `night`, `unclear` |
| `image_quality` | string | `clear`, `blurry`, `dark`, `obstructed` |
| `looks_staged_or_reused` | bool or `"unclear"` | — |
| `caption` | string | ≤200 chars, factual, no speculation |
| `confidence` | float | 0.0–1.0, the model's confidence in the whole answer |

**Validation is strict and silent.** A value outside the allowed set is
**dropped, not mapped to the nearest thing we recognise** — an invented hazard
name disappears rather than becoming `debris`. Extra keys are ignored. A
response that fails wholesale leaves the report exactly as it would have been
with no photo. Silent coercion is how an "unclear" becomes a dispatch.

`"unclear"` is a real answer everywhere it is allowed, and it is the right one.
A 3B model asked to be certain will be certain and wrong.

## 3. The prompt

Do not hardcode it — fetch it from `GET /api/v1/citizen/vision-contract`, which
returns `{ prompt, hazards, depthBands, imageQuality, categoriesCovered,
maxImageEdge, hostedAvailable, notes }`. It is the single source of truth and it
is served so it can change without a redeploy on either side.

Images are downscaled to a 1024 px longest edge before sending.

## 4. What we do with the answer

`water_on_road` + a `flooded_road` report is agreement, and trust goes up. A
confident, legible "no hazard at all" against a report that there is one is
disagreement: trust goes down and the report is held for a human, because that
is also what a fabricated report looks like. Anything unclear, blurry, or below
0.45 confidence counts as saying nothing, and the report is scored exactly as it
would have been without a photo — a system that penalises bad photos teaches
people to stop sending photos.

**The ceiling is the point.** A photo may raise or lower trust and nothing else.
It cannot set the category, set the severity, resolve an incident, or commit a
unit. At full agreement the evidence component reaches 0.95, which is still not
enough on its own to clear the auto-confirm floor.

## 5. Endpoints on our side

| Endpoint | Who calls it | When |
|---|---|---|
| `GET /api/v1/citizen/vision-contract` | your service, our app | to fetch the prompt and vocabularies |
| `POST /api/v1/citizen/vision/analyse` | our app | we call your service and return the assessment. Body: `{imageUrl \| imageBase64, category, reportId?}` |
| `POST /api/v1/citizen/vision` | the phone | the model ran on the device; post the JSON straight in. Body: `{analysis, model, ranOn: "device", category, reportId?}` |

Device and hosted results go through identical validation and have identical
ceilings. That is deliberate: where the model runs stays an engineering
decision, never a safety one.

## 6. Minimal FastAPI shim

```python
@app.post("/analyse")
async def analyse(body: dict):
    image = body.get("image_url") or body.get("image_base64")
    text = run_qwen_vl(prompt=body["prompt"], image=image,
                       max_tokens=body.get("max_tokens", 512),
                       temperature=body.get("temperature", 0.1))
    return {"text": text}          # we pull the JSON out of it
```
