"""Resident Q&A surge against the LLM gateway with a 400 ms fake model: how many
requests reach the model. Run from backend/: python scripts/bench_llm_cache.py"""
import asyncio, sys, time, random
sys.path.insert(0,"tests"); sys.path.insert(0,".")
import _stubs; _stubs.install()
from app.agents import llm, llm_cost
from app.agents.llm import ProviderResult
class P:
    engine="gemini"; calls=0
    async def complete(self, system, prompt, *, max_tokens=600, tier="large"):
        P.calls+=1; await asyncio.sleep(0.4)   # a realistic model latency
        return ProviderResult(text="Shelter A is open, 1.2 km north.", model="gemini-2.5-flash-lite", tokens_in=380, tokens_out=40)
llm._chain=[P()]
QUESTIONS=["is shelter open","where is nearest shelter","is water safe to drink","which road is closed","where do i get food"]
async def main():
    t=time.perf_counter()
    async def one(i):
        ward=f"W{random.randint(1,30)}"
        q=random.choice(QUESTIONS)
        await asyncio.sleep(i * 0.0005)   # 10,000 questions arriving over 5 s
        return await llm.complete("sys", f"{q} in {ward}", fallback="fb", task="citizen_ask", cache_key=(ward,q))
    out=await asyncio.gather(*[one(i) for i in range(10000)])
    dt=time.perf_counter()-t
    u=llm_cost.usage()["tasks"]["citizen_ask"]
    print(f"10000 resident questions in {dt:.2f}s -> model calls {P.calls} ({P.calls/100:.1f}% of requests), tokens {u['tokensIn']+u['tokensOut']}, est cost ${u['costUsd']}, served from cache {u['servedFromCache']}, deterministic {u['deterministic']}")
    print("naive (one call each): tokens", 10000*420, " est cost $", round(10000*(380*0.10+40*0.40)/1e6,4))
asyncio.run(main())
