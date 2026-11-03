# The VLM laptop — what to do

The 405 was never the model's fault. `VLM_URL` was pointed at a URL that only
answers GET, so every photo POST bounced off it while Qwen2.5-VL sat there
perfectly willing. The backend now probes for the right path by itself, so most
of this is verification rather than work.

---

## 1. Find out what is actually serving the model

On the laptop, run whichever of these matches what you started:

```bash
# vLLM, llama.cpp server, SGLang, LM Studio — all OpenAI-compatible
curl -s http://localhost:8000/v1/models     # vLLM / SGLang default
curl -s http://localhost:8080/v1/models     # llama.cpp server default
curl -s http://localhost:1234/v1/models     # LM Studio default
curl -s http://localhost:11434/api/tags     # Ollama
```

One of them will answer with JSON. **Write down the port, and write down the
exact model `id` it reports** — you need both.

If none answer, the server is not running. Start it (see §5).

---

## 2. Make it listen on all interfaces, not just localhost

This is the single most common reason a tunnel returns 404 or 502: the server is
bound to `127.0.0.1`, so the tunnel process cannot reach it either.

| Server | Flag |
|---|---|
| vLLM | `--host 0.0.0.0` |
| llama.cpp | `--host 0.0.0.0` |
| SGLang | `--host 0.0.0.0` |
| Ollama | set env `OLLAMA_HOST=0.0.0.0:11434` before starting |
| LM Studio | Developer tab → enable **Serve on Local Network** |

Restart the server after changing it.

---

## 3. Point the tunnel at that exact port

```bash
cloudflared tunnel --url http://localhost:8000
```

Use the port you found in §1. Cloudflare prints a URL like
`https://something-random.trycloudflare.com`. **That root URL is all you need.**

Then check it from *outside* the laptop — from your phone on mobile data, or
just from the browser on another machine:

```bash
curl -s https://something-random.trycloudflare.com/v1/models
```

If that returns the model list, you are done on the laptop side.

> A quick-tunnel URL changes every time you restart `cloudflared`. If you restart
> it, you must update `VLM_URL` on Render and redeploy. For the demo, start the
> tunnel once and leave it running.

---

## 4. Set two environment variables on Render

In the Render dashboard → your backend service → **Environment**:

| Key | Value |
|---|---|
| `VLM_URL` | `https://something-random.trycloudflare.com` — the **root**, no path |
| `VLM_MODEL` | the exact `id` string from §1, e.g. `Qwen/Qwen2.5-VL-7B-Instruct` |

Leave `VLM_API_KEY` empty unless your server requires one.

Then **Manual Deploy → Deploy latest commit**.

The backend now tries `/v1/chat/completions` first, then `/api/chat`, then the
root, treats 404/405/501 as "wrong door" rather than "service down", and
remembers whichever one answered. You will see one of these lines in the Render
log the first time a photo arrives:

```
vlm_endpoint_learned  url=https://….trycloudflare.com/v1/chat/completions  shape=openai
```

That line means it is working. If instead you see `vlm_wrong_endpoint` for every
candidate followed by `vlm_unavailable`, the tunnel is not reaching the server —
go back to §2.

---

## 5. If the server is not running at all

For a 7B Qwen2.5-VL on a laptop GPU, vLLM is the least fiddly:

```bash
pip install "vllm>=0.6.3"
vllm serve Qwen/Qwen2.5-VL-7B-Instruct \
  --host 0.0.0.0 --port 8000 \
  --max-model-len 8192 \
  --limit-mm-per-prompt image=1
```

On a machine without enough VRAM, use the 3B instead
(`Qwen/Qwen2.5-VL-3B-Instruct`) — it is noticeably weaker at reading water depth
against a reference object, but it answers the contract and it runs.

---

## 6. Prove it end to end

With the tunnel up and Render redeployed, from any browser console:

```js
await fetch("https://disruptionops.onrender.com/api/v1/citizen/vision-contract")
  .then(r => r.json())
```

That returns the JSON shape the model is asked for. Then attach a photo to a
report in the citizen app — the receipt will show what the photo contributed and
whether it agreed with what was typed.

---

## What happens if the laptop is closed mid-demo

Nothing breaks. `vision_client.analyse` never raises: a report with an
unreachable vision service is a report that keeps its "a photo was attached"
credit and loses only the "and the photo agreed" bonus. The photo path degrades,
the reporting path does not.

This is worth saying out loud to the judges, because it is the honest version of
a demo dependency — the model is an input to a score, not a link in the chain.
