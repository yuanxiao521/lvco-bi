# 10 · LLM 客户端与 Prompt 管理

> 涉及文件：`services/llm_client.py`、`services/prompt_registry.py`、`services/ai_prompts.py`、`app/config.py`（模型路由）、`prompts/*.yaml`（9 个）。

## LLMClient（`services/llm_client.py`）

**设计要点**：不依赖 openai SDK，用 httpx 裸调 OpenAI 兼容 `POST {base_url}/chat/completions`，因此一套代码兼容 DeepSeek / 通义 / OpenAI 等任意 provider（切换只改 `OPENAI_BASE_URL`/`OPENAI_API_KEY`/`OPENAI_MODEL`）。

### 核心方法

| 方法 | 说明 |
|------|------|
| `async complete(messages, *, temperature=0.3, max_tokens=None) -> str` | 非流式单次调用，返回 content |
| `async stream_chat(messages, *, temperature=0.3, max_tokens=None) -> AsyncIterator[str]` | 流式，逐 token 产出 delta.content |
| `async stream_chat_with_tools(messages, tools, *, temperature=0.5, max_tokens=None) -> AsyncIterator[dict]` | 流式 + function calling：按 index 累积 tool_calls 分片，`[DONE]` 后先按序吐 `{"type":"tool_call"}` 再 `{"type":"done"}`；文本增量发 `{"type":"text"}` |

### 异常与超时

- `AINotConfiguredError`：未配 API Key（路由层返回 503 `AI_NOT_CONFIGURED`）。
- `AIUpstreamError`：4xx/5xx、空 choices。
- 超时：`openai_timeout`（默认 30s）传给 httpx。

## 任务分级模型路由（`config.Settings.model_for_task`）

```python
def model_for_task(self, task_type: str) -> str:
    # simple/polish/clean/recommend → LLM_MODEL_SIMPLE
    # 其余（agent_stream/insights/sql/planner/chart 等）→ LLM_MODEL_COMPLEX
    # 未配置回退 openai_model
```

作用：简单任务（润色/清洗/推荐）用便宜快速模型，复杂任务（Agent/SQL/洞察）用强模型，控制成本。

## Prompt 注册中心（`services/prompt_registry.py`）

### YAML 存储（`backend/prompts/`，9 个模板）

| 文件 | 用途 |
|------|------|
| `chat_system.yaml` | 通用闲聊 |
| `chat_data_system.yaml` | 数据对话 |
| `agent_system.yaml` | 单 Agent ReAct 系统提示词 |
| `canvas_system.yaml` | 画布 AI 助手 |
| `recommend_system.yaml` | 图表推荐 |
| `clean_system.yaml` | 数据清洗建议 |
| `insights_system.yaml` | 洞察生成 |
| `insight_report_system.yaml` | 洞察报告（严格 JSON 输出） |
| `polish_system.yaml` | 文本润色 |

YAML 结构：`{name, version, system, template?}`，system 经 `textwrap.dedent` 去缩进；template 支持 `.format` 渲染（缺 key 回退 system）。

### 核心类

| 类/函数 | 说明 |
|---------|------|
| `PromptTemplate(name, version, system, template=None)` | `render(**kwargs)` 渲染 |
| `PromptRegistry`（单例 `get_instance()` / `reset()`） | `_load_all()` 全量加载；`get(name)` 缺失返回空模板(v0)；`list_prompts()`；**`reload()` 热更新** |
| `ai_prompts.py` | 兼容层：import 时把 `CHAT_SYSTEM`/`AGENT_SYSTEM` 等 9 个常量绑定到 Registry；YAML 缺失回退文件内硬编码 Fallback（与 YAML 互为镜像） |

### 使用注意

- 常量在 import 时求值，热更新后旧常量不自动刷新，新代码应改用 `get_prompt(name).system` 实时取。
- **多 Agent 三个子 Agent 的 prompt 是文件内硬编码的 system 字符串，未走 PromptRegistry**（`_build_plan_prompt`/`_build_sql_prompt`/`_build_chart_prompt`/`_build_report_prompt`）。
- YAML 的 `version` 字段目前仅元数据用途，无按版本切换逻辑。

## 消费方一览（`ai_service.py` 及各 Agent）

| 场景 | Prompt | 模型任务类型 |
|------|--------|-------------|
| `chat_stream` 闲聊 | CHAT_SYSTEM | simple |
| `recommend_charts` | RECOMMEND_SYSTEM + 用户偏好 hint | simple |
| `clean_suggest` | CLEAN_SYSTEM | simple |
| `generate_insights` | INSIGHTS_SYSTEM | complex |
| `polish_text` | POLISH_SYSTEM | simple |
| 单 Agent ReAct | AGENT_SYSTEM | complex |
| 洞察报告 | INSIGHT_REPORT_SYSTEM | complex |
| 多 Agent（Planner/SQL/Chart/Report） | 文件内硬编码 | complex |
