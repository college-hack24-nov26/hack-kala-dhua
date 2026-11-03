# LLM failover — and what Bedrock actually needs from AWS

## What now happens

`app/agents/llm.py` is no longer one provider with one circuit breaker. It is an
ordered **chain**:

```
Gemini  →  Bedrock  →  deterministic rules
```

`LLM_PROVIDER` chooses the *head* of the chain rather than the only member of it.
Anything else with credentials follows automatically.

### Why a chain rather than a switch

The most likely failure is not an outage — it is a free-tier quota running out
halfway through a demo. Gemini then returns 429 for hours. The old design
degraded to deterministic rules for the rest of the session while a perfectly
good second provider sat configured and idle. That is the worst of both worlds.

### The rules, exactly

| Situation | What happens |
|---|---|
| Gemini returns 429 / quota / throttling | Rested for **15 minutes** immediately — no point spending two more attempts proving a quota is exhausted — and Bedrock answers the *same* request |
| Gemini has an ordinary error | Counted; rested for **60 s** only after **3** consecutive failures |
| A provider is resting | Skipped entirely, not waited on. No 8-second timeout tax per request |
| Cooldown expires | Tried again on the very next request, half-open. One success clears its history |
| Everything down | Deterministic rules. Never an exception, never a missing answer |

Circuits reopen **on a timer**, so a quota that resets at midnight is picked up
on its own. Nothing has to be called by hand.

### Seeing what happened

```
GET /api/v1/status/llm
```

```json
{
  "engine": "bedrock",
  "note": "xai.grok-4-6 responding — gemini cooling down",
  "providers": [
    {"engine": "gemini",  "preferred": true,  "available": false,
     "consecutiveFailures": 1, "restingForSeconds": 847},
    {"engine": "bedrock", "preferred": false, "available": true,
     "consecutiveFailures": 0, "restingForSeconds": 0}
  ]
}
```

Every failover also logs `llm_failover` with the chain it walked, and each
`Completion` carries a `degraded_reason` saying which provider actually answered.
A silent failover is how a team discovers in March that the primary has been dead
since January.

Eight behaviours were verified against a stubbed chain before this shipped:
failover on quota in a single request, skipping a resting provider, correct
`engine_note`, both-down falling to rules, a transient error *not* resting a
provider, automatic recovery without a manual reset, `provider_status`, and chain
ordering following `LLM_PROVIDER`.

---

## What you need from AWS — you guessed "probably nothing", and that is not right

Bedrock is not like a Gemini key. Five things, and two of them are easy to miss.

### 1. An AWS account with billing enabled

Bedrock inference is **pay-per-token and has no free tier.** Grok on Bedrock is
charged per input and output token. For a hackathon demo the spend is
cents — but the account must have a valid payment method or every call fails.

### 2. Model access, requested and granted — the step people miss

Bedrock does **not** let you call a model just because you have credentials. In
the Bedrock console → **Model access**, you must explicitly request access to the
xAI Grok model, in the region you intend to use. Some models grant instantly;
some take minutes. Until it is granted every call returns `AccessDeniedException`
with a message about model access, which reads like a credentials problem and is
not.

### 3. The right region — check this before anything else

**Your config defaults to `ap-south-1` (Mumbai), and Grok is very unlikely to be
available there.** From AWS's own announcements, Grok 4.3 arrived on Bedrock in
June 2026 and Grok 4.6 in August 2026 with **cross-region inference**; a
GovCloud (US-West) rollout followed separately. I was not able to load the
per-region availability table to confirm the exact list, so **check the Bedrock
console model catalogue yourself** and set `AWS_REGION` to a region that actually
lists the model. `us-east-1` and `us-west-2` are the usual starting points.

Symptom of getting this wrong: `ValidationException` — "the provided model
identifier is invalid" — even though the id is copied correctly.

### 4. The exact model id, which may be an inference profile

Grok 4.6 ships with **cross-region inference**, which means the thing you pass as
`modelId` is an *inference profile id* with a regional prefix (the `us.` /
`eu.` / `apac.` form), not the bare model name. Copy the id from the console
rather than typing what the marketing page calls it.

Set it as `BEDROCK_MODEL_ID`.

### 5. Credentials — either shape works

The code already handles both, which is why `BedrockProvider` sets
`AWS_BEARER_TOKEN_BEDROCK` for you:

- **A Bedrock API key** (the `ABSK…` form) — simplest. Bedrock console →
  API keys. Put it in `AWS_API_KEY_BEDROCK_FOR_XAI`.
- **Classic IAM keys** — an access key and secret for a user or role carrying
  `bedrock:InvokeModel` (and `bedrock:InvokeModelWithResponseStream` if you ever
  stream). Set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` and boto3 picks them
  up through the normal chain.

### Environment on Render

```
LLM_PROVIDER=gemini                       # head of the chain; Bedrock still follows
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.0-flash

BEDROCK_MODEL_ID=<exact id from the console, likely us.xai.grok-…>
AWS_REGION=<a region that lists the model — probably NOT ap-south-1>
AWS_API_KEY_BEDROCK_FOR_XAI=ABSK...
```

Leave `LLM_PROVIDER=gemini`. The chain does the rest.

### Verifying it before you need it

The honest test is to prove the failover works, not just that Bedrock responds.
Temporarily set `GEMINI_API_KEY` to a bad value, redeploy, and hit
`/api/v1/status/llm`: the first request should fail Gemini, rest it, and answer
from Bedrock, and the response should name `bedrock` as the engine. Then restore
the real key.

Do this **before** the demo, not during. A misconfigured `AWS_REGION` fails in a
way that looks like a wrong model id, and you do not want to be debugging that
distinction in front of judges.

---

## If you would rather not set up AWS at all

That is a defensible choice, and the system is built for it. With no Bedrock
configured the chain is simply `Gemini → deterministic rules`, and the rules are
not a degraded imitation — they operate on the same structured inputs and give
correct answers with no model at all.

Nothing the LLM does produces an operational number. Every figure comes from the
solver and the scorer. So the worst case of having no second provider is that
explanations get terser, not that coordination stops — which is worth saying out
loud to a judge, because it is the honest answer to "what happens when your model
is unavailable?"

---

Sources for the Bedrock/Grok availability claims:

- [Grok 4.3 from xAI now available in Amazon Bedrock — AWS](https://aws.amazon.com/about-aws/whats-new/2026/06/grok-amazon-bedrock/)
- [Amazon Bedrock now supports Grok 4.6 with Cross Region Inferencing — AWS](https://aws.amazon.com/about-aws/whats-new/2026/08/amazon-bedrock-grok-4-6/)
- [Grok 4.3 on Amazon Bedrock in AWS GovCloud (US-West) — AWS](https://aws.amazon.com/about-aws/whats-new/2026/07/grok-4-3-bedrock-govcloud/)
