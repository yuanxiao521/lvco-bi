# LvcoBI — AI-Powered Data Analysis Platform (Text-to-BI)

**Turn natural language into insightful data visualizations.**

LvcoBI is a full-stack BI SaaS platform that lets users interact with their data through natural language. It features a custom multi-agent orchestration engine, a metric semantic layer, and a canvas-based drag-and-drop visualization workspace — all powered by LLM agents.

---

## Architecture

```
User Input → Lead Agent (Supervisor)
                ├── Canvas Executor → step-by-step chart building with self-validation
                ├── General Executor → Planner skeleton + topological parallel execution
                └── React Executor   → lightweight Q&A for quick responses
```

### Multi-Agent Orchestration

| Component | Role |
|-----------|------|
| **Lead Agent** | Supervisor state machine — deterministic routing, code-level convergence gate, subtask review loop (code judges pass/fail, Lead decides retry or stop) |
| **Canvas Executor** | Builds charts step-by-step — tool whitelist narrowed to placement-only tools, backend self-validates against empty charts, auto-fallback for missing charts |
| **General Executor** | Planner skeleton plan → executor makes real-time parameter decisions → LLM self-correct on failure → same-signature failure forces tool switch |
| **React Executor** | Lightweight Q&A with iteration cap and consecutive-failure circuit breaker |

### Key Design Highlights

- **Metric Semantic Layer**: AI queries prioritize governed metrics first, fall back to structured query engine, then raw SQL with 4-layer deep protection (keyword blacklist / AST whitelist / table ownership / LIMIT+timeout / fail-closed)
- **Canvas Layout Loop**: Inject canvas snapshot before & after execution + read-only query tools for real-time awareness; judge by success rate / numeric posterior / failures, redo with guidance if not met
- **Unified Tool Kernel**: 18 tools share one lifecycle (whitelist → idempotent memo → execution observation → error check → emit; failures not cached to preserve self-correction)
- **DuckDB Unified Query Layer**: Covers 4 data source types (CSV/Excel import + PG/MySQL ATTACH federation)
- **Observability**: Full-trace Langfuse integration across all LLM calls and agent steps
- **SSE Event Stream**: Frontend Zustand keeps stream alive across route switches

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python • FastAPI • SQLAlchemy • Alembic • PostgreSQL |
| Query Engine | DuckDB (multi-source federation: CSV/Excel/MySQL/PostgreSQL) |
| Cache | Redis |
| Frontend | React • TypeScript • Vite • ECharts • Zustand |
| AI/Agent | LLM (OpenAI-compatible) • Langfuse (observability) • LangGraph-inspired orchestration |
| Security | AST-level SQL whitelist • Parameterized queries • Role-based access control |
| Deployment | Docker • Docker Compose • Nginx |

---

## Features

- **Natural Language Querying**: Ask questions in plain Chinese/English, get charts and insights
- **Canvas Workspace**: Drag-and-drop chart layout with AI-assisted building
- **Multi-Source Data**: Connect CSV, Excel, MySQL, PostgreSQL — unified through DuckDB
- **Metric Governance**: Define and manage metric definitions with lineage tracking
- **Dashboard & Reports**: Create scheduled reports with auto-refresh
- **Role-Based Access**: Granular permission management
- **Real-Time Streaming**: SSE-based event stream for long-running agent tasks

---

## Quick Start

### Prerequisites
- Python 3.11+
- Node.js 18+
- PostgreSQL 15+
- Redis 7+

### Backend

```bash
cd backend
cp .env.example .env    # configure your environment
uv venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
uv pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Docker (full stack)

```bash
docker compose up -d
```

---

## Testing

```bash
# Run all tests (677+ SQL safety, 25+ e2e metric semantic, 21+ full-chain LLM)
cd backend
pytest

# Run agent evaluation suite
python -m tests.agent_evals.semantic_run_eval
```

---

## Project Structure

```
lvco-bi/
├── backend/
│   ├── app/
│   │   ├── api/              # REST endpoints
│   │   ├── core/             # DB, security, SSE, middleware
│   │   ├── models/           # SQLAlchemy models
│   │   ├── repositories/     # Data access layer (UoW pattern)
│   │   ├── services/
│   │   │   ├── agents/       # Multi-agent orchestration engine
│   │   │   ├── insight_engine/ # Auto-insight discovery
│   │   │   └── ...           # Business services
│   │   └── config.py
│   ├── prompts/              # LLM system prompts (YAML)
│   ├── mock_data/            # Sample datasets
│   └── tests/                # Unit, integration & eval tests
├── frontend/
│   └── src/
│       ├── pages/            # Page components
│       ├── components/       # UI components & charts
│       ├── stores/           # Zustand state management
│       └── api/              # API client
└── docker-compose.yml
```

---

## Evaluation Results

| Suite | Tests | Pass Rate |
|-------|-------|-----------|
| LLM Full-Chain | 21/21 | 100% |
| Metric Semantic Layer | 25/25 | 100% |
| SQL Safety (AST guard) | 677+ | 100% |
| SQL Generation Accuracy | 20 cases | 90.8% |

---

> Built as an independent project for learning modern AI agent application development.