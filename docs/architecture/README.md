# Architecture diagrams

Graphviz sources are the truth; SVG/PNG are renders (`dot -Tsvg X.dot -o X.svg`, `dot -Tpng -Gdpi=110 X.dot -o X.png`).

| File | What it shows |
|---|---|
| `01-agentic-orchestration` | The whole agentic system in seven bands: signals in → intake agents → durable world state (Postgres) → orchestration spine (event router, emergency stop, policy gate, CP-SAT) → LangGraph cycle with its **shared run state** boundary → **private per-agent memory** namespaces and the ledger → LLM agents (Commander, Copilot). |
| `02-langgraph-agent-graph` | The compiled LangGraph graph from `backend/app/agents/graph.py`: every node with the state keys it reads and writes, its memory namespace, the `Send` fan-outs and reducers, the retry loop, the `interrupt()` approval, and every route to END. |
| `03-system-architecture` | Deployed system: edge (VLM camera node, bitchat phones, gateway phone, PWAs), FastAPI on Render, Supabase, the Vercel console, and external services. |

Earlier sets are in `_superseded/`.
