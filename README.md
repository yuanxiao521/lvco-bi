# LvcoBI — AI-Powered Data Analysis Platform (Text-to-BI)

**Turn natural language into insightful data visualizations.**

LvcoBI is a full-stack BI SaaS platform that lets users interact with their data through natural language. It features a custom multi-agent orchestration engine, a metric semantic layer, and a canvas-based drag-and-drop visualization workspace — all powered by LLM agents.

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18.3-61DAFB)](https://react.dev/)
[![DuckDB](https://img.shields.io/badge/DuckDB-1.1-Yellow)](https://duckdb.org/)
[![License](https://img.shields.io/badge/License-MIT-red)](LICENSE)

---

## Architecture Overview

```mermaid
flowchart TB
    subgraph Frontend ["Frontend (React + TypeScript)"]
        AIChat["AIChat Page"]
        Canvas["FreeCanvas Page"]
    end

    subgraph API ["API Layer (FastAPI)"]
        SSE["SSE Endpoints<br/>/chat/stream /canvas/chat"]
        Inject["Context Injection<br/>Metrics · Schema · Canvas Snapshot"]
    end

    subgraph Lead ["Supervisor (LeadAgent)"]
        Merge["Round 0: Merged Intent+Decision<br/>2 tasks → 1 LLM call"]
        Loop["Supervisor Loop<br/>max 3 rounds, code-level convergence gate"]
        Review["assess_subtask<br/>Code-level review, zero LLM cost"]
    end

    subgraph Executors ["Three Executors (Lightweight Graph Engine)"]
        CanvasExec["CanvasOrchestrator<br/>Step-by-step block placement<br/>Backend self-validation · Auto fill"]
        GenExec["AgentOrchestrator<br/>Planner skeleton → parallel exec<br/>Fail signature → tool switch"]
        ReactExec["ReactGraphAgent<br/>Reason → Tool loop<br/>Max 6 rounds · Circuit breaker"]
    end

    subgraph Tools ["Unified Tool Kernel (ToolExecutor)"]
        TL["Tool Lifecycle<br/>Whitelist → Memo → Execute → Check → Emit"]
        WL["3-Layer Whitelist<br/>Entry → Planner → Executor"]
        FS["Fail Signature Tracking<br/>2×: force tool switch<br/>3×: skip step"]
    end

    subgraph Data ["Data Layer"]
        Guards["4-Layer SQL Guards<br/>AST whitelist · Table ownership<br/>LIMIT+timeout · fail-closed"]
        Metrics["Metric Semantic Layer<br/>4 governance tables"]
        DuckDB["DuckDB Query Engine<br/>Columnar OLAP · Zero ops"]
        Cache["Redis Cache<br/>config_hash → millisecond"]
    end

    AIChat --> SSE
    Canvas --> SSE
    SSE --> Inject
    Inject --> Lead
    
    Lead -->|"deterministic routing<br/>entry + complexity"| Executors
    Review -.->|"guidance → redo"| Lead
    
    CanvasExec --> Tools
    GenExec --> Tools
    ReactExec --> Tools
    
    Tools --> Guards
    Tools --> Metrics
    Guards --> DuckDB
    Metrics --> DuckDB
    DuckDB --> Cache
```

### Key Design Points

| Point | What | Why |
|-------|------|-----|
| **A** | Merged intent+decision (Round 0) | One LLM call, 40% token savings |
| **B** | Executor tool whitelist narrowed to placement-only | Prevents "query but don't chart" |
| **C** | fail_signature tracking | Same signature 2× fail → force switch; 3× → skip |
| **D** | SQL 4-layer fail-closed | Parse failure → reject, never downgrade |

---

## Rich Architecture SVG

<div align="center">
  <img src="https://raw.githubusercontent.com/yuanxiao521/lvco-bi/main/docs/architecture.svg" alt="LvcoBI Architecture" width="720">
</div>

> **Note**: If the SVG isn't rendering yet, push this README first — the `docs/architecture.svg` will be generated on the next build.

---

## Project Flow (Canvas Conversation Example)

```
User: "Analyze sales by category, brand, and season"
   │
   ▼
① Frontend (AIChat/FreeCanvas)
   │ POST /api/v1/ai/canvas/chat (SSE stream)
   │ Zustand store: stream survives route switches
   ▼
② API Layer
   │ Inject: canvas snapshot + governed metrics list
   │ Session: 1:1 binding per canvas
   ▼
③ LeadAgent (Supervisor)
   │ Round 0: merged intent+decision → intent=analysis, action=call_analysis
   │ Code routing: entry=canvas → CanvasOrchestrator (zero LLM cost)
   ▼
④ CanvasOrchestrator
   │ plan → execute_steps → finish
   │ Planner generates skeleton (3 chart steps + narrative)
   │ Mini-ReAct loop: LLM → ToolExecutor → goal validation
   │ Auto chart filling (if <2 chart types, auto-fill one)
   │ Auto layout arrange (deterministic, not LLM-triggered)
   ▼
⑤ ToolExecutor
   │ Whitelist check → idempotent memo → execute → error check → emit
   │ add_chart_block: back-end self-validation via execute_chart_query
   ▼
⑥ Review & Memory
   │ assess_subtask: 4-dimension code review (zero LLM)
   │ Pass → STOP; Fail → guidance → redo
   │ _maybe_summarize: early rounds → LLM summary → ai_memories upsert
   ▼
   SSE: intent → decision → progress → tool_call → canvas_action → done
   Frontend renders canvas blocks in real-time
```

---

## Multi-Agent Orchestration

| Component | Role | Mechanism |
|-----------|------|-----------|
| **Lead Agent** | Supervisor state machine | Merged Round 0 + convergence gate + subtask review |
| **Canvas Executor** | Step-by-step canvas charting | Tool whitelist narrowed to placement-only; self-validating add_chart_block |
| **General Executor** | Multi-step complex analysis | Planner skeleton → topological parallel execution → fail-signature tracking |
| **React Executor** | Lightweight Q&A | Reason→Tool loop; max 6 rounds; 5-consecutive-fail circuit breaker |

### Three Query Paths

| Path | Tool | When | Security |
|------|------|------|----------|
| **Governed Metric** | `metric_key` | Business metrics with defined caliber | Formula AST whitelist; auto-upgrade to governed caliber |
| **Structured Query** | `query_engine` | Flexible aggregation/grouping/filtering | Parameterized SQL, injection-proof |
| **Raw SQL** | `query_sql` | Complex analytics (window functions, CTEs) | 8-rule AST validation + table ownership + fail-closed |

### Evaluation Results

| Suite | Tests | Pass Rate |
|-------|-------|-----------|
| Agent E2E (Semantic Eval) | 20 dual-source canvas | **90.8%** (14/20 full pass) |
| LLM Full-Chain | 21/21 | 100% |
| Metric Semantic Layer | 25/25 | 100% |
| SQL Safety (AST guard) | 21/21 | 100% |
| Output Efficiency Baseline | 25 questions (historic) | 48% → **96%** |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3.12 • FastAPI • SQLAlchemy • Alembic • PostgreSQL |
| **Query Engine** | DuckDB (columnar OLAP, embedded, zero ops) |
| **Cache** | Redis |
| **Frontend** | React 18 • TypeScript • Vite • ECharts 5 • Zustand |
| **AI/Agent** | OpenAI-compatible LLM • Langfuse (observability) • Custom graph engine |
| **Security** | SQLGlot AST-level whitelist • Parameterized queries • RBAC |
| **Deployment** | Docker • Docker Compose • Nginx |

---

## Quick Start

```bash
# Prerequisites: Python 3.12+, Node.js 18+, PostgreSQL 15+, Redis 7+

# Backend
cd backend
cp .env.example .env    # configure DB connection, LLM API key
uv venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
uv pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload

# Frontend (separate terminal)
cd frontend
npm install
npm run dev

# Or Docker (full stack)
docker compose up -d
```

---

## Testing

```bash
cd backend
pytest                          # unit + integration
python -m tests.agent_evals.semantic_run_eval  # agent evaluation
```

---

## Project Structure

```
lvco-bi/
├── backend/
│   ├── app/
│   │   ├── api/              # REST endpoints (14 modules)
│   │   ├── core/             # Database, security, SSE, middleware
│   │   ├── models/           # SQLAlchemy models (20+)
│   │   ├── repositories/     # Data access (UoW pattern)
│   │   ├── services/
│   │   │   ├── agents/       # Multi-agent orchestration engine
│   │   │   │   ├── lead/     # Supervisor (LeadAgent)
│   │   │   │   ├── canvas_orchestrator.py
│   │   │   │   ├── agent_orchestrator.py
│   │   │   │   ├── react_agent.py
│   │   │   │   ├── planner_agent.py
│   │   │   │   └── tool_executor.py
│   │   │   ├── metric_service.py  # Metric semantic layer
│   │   │   ├── query_engine.py    # Structured query engine
│   │   │   ├── sql_guard_ast.py   # AST-based SQL protection
│   │   │   ├── observability.py   # Langfuse integration
│   │   │   └── ...
│   │   └── config.py
│   ├── prompts/              # 18 LLM system prompts (YAML)
│   ├── mock_data/            # 6 sample datasets
│   └── tests/                # 50+ test files
├── frontend/
│   └── src/
│       ├── pages/            # 14 page modules
│       ├── components/       # UI components + charts
│       └── stores/           # Zustand state management
└── docker-compose.yml
```

---

## License

MIT