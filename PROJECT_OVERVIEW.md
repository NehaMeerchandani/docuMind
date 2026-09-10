# docuMind

A multi-tenant, company-scoped RAG (Retrieval-Augmented Generation) platform. Companies
upload their own documents (PDFs, webpages, raw text); the system chunks and embeds
them into a vector database; and company members chat with an LLM that can pull answers
from those documents through a dynamically configurable, tool-using agent workflow.

## What the project contains

Django project (`main`) with the following apps:

| App | Responsibility |
|---|---|
| `user` | Authentication (JWT access + refresh tokens), custom user model (email as username), company membership + roles |
| `company` | The tenant model. Every company-scoped record (documents, chunks, conversations, messages) is isolated per company |
| `document` | Document ingestion: upload/scrape a source, parse it, chunk it, embed it, store vectors in Qdrant. Two async execution paths: Celery and Kafka |
| `chat` | The chat UI/API: conversations, messages, the streaming chat endpoint, session summarization |
| `agents` | Dynamic multi-agent workflow system: `Agent` model (name + description + workflow graph JSON), a visual workflow editor in the admin, and a `WorkflowGraphBuilder` that compiles each agent's graph into a runnable LangGraph graph at request time |
| `base` | Shared base models (soft delete, auditable created_by/updated_by/timestamps, company-scoping mixin), shared pagination/permissions/exception handling |
| `basic` | Small cross-cutting utilities: password/URL validators (incl. SSRF-safe URL validation), crypto helpers, time utilities, role/error constants |

Supporting infrastructure (see `INFRA_SETUP_GUIDE.md` for exact commands):
- **PostgreSQL** — primary relational database
- **Redis** — Celery's broker/result backend
- **Qdrant** — vector database storing document chunk embeddings
- **Ollama** (`nomic-embed-text`) — local embedding model, no external API calls for embeddings
- **Kafka** (KRaft mode, single broker) + **Kafka UI** — the alternate async processing path and its inspection dashboard
- **Langfuse** — hosts the versioned system prompts each agent step uses, and receives full execution traces (per-node latency, tool calls, token usage) for every chat turn
- **OpenAI** (via `langchain-openai`) — the LLM provider for both the router and the chat agents

## Core features

### 1. Multi-tenant document management
- Upload raw text, or provide a URL to a webpage/PDF to scrape (SSRF-guarded URL
  validation on scrape targets)
- Parsing (`ParserService`) → chunking (`ChunkingService`) → embedding
  (`EmbeddingService`, via Ollama) → stored as vectors in Qdrant, one point per `Chunk`
  row, scoped by company
- Document status lifecycle: `pending → processing → completed / failed`, with the
  error message persisted on failure
- Soft delete throughout: deleting a document cascades to its chunks, and chunk
  deletion also removes the corresponding vectors from Qdrant

### 2. Two async processing paths for the same job
- **Celery**: `process_document_task` (Redis-backed) — the standard task-queue path
- **Kafka**: a producer publishes a processing event; a separate long-running consumer
  process (`manage.py consume_document_processing`) picks it up and runs the same
  `DocumentService.process()`
- Both paths converge on the same service logic — the queueing mechanism is
  swappable without touching the actual processing code
- Admin document list has "Process (Celery)" and "Process (Kafka)" buttons that fire
  AJAX requests and update the row's status in place, no full page reload

### 3. RAG chat
- Company members chat in a `Conversation`; every turn is persisted as a `Message`
  (`role`: user/assistant, `message_type`: text/tool/error/system/context/media)
- Chat responses are streamed token-by-token (SSE) to the client
- Session summarization on demand: asking for a summary generates and overwrites
  `Conversation.summary`; a later message containing the phrase "use summary" makes
  the LLM use that saved summary as context instead of the last N raw messages
  (summary and raw history are never combined in the same turn)
- Tool activity (which tool ran, or that none did) is surfaced back into the message
  history so the chat history tab reflects what actually happened

### 4. Dynamic multi-agent workflow system
- Historically the agent/prompt/tools were hardcoded in Python. This is now fully
  data-driven:
  - An `Agent` row stores a `name`, a router-facing `description`, and a
    `workflow_json` graph (nodes: `start` / `step` / `end`; edges with optional
    conditions)
  - On every incoming message, `RouterService` asks an LLM to pick the single best
    `Agent` for that specific message (per-message routing, not per-conversation)
  - `WorkflowGraphBuilder` compiles the chosen agent's `workflow_json` into a real
    LangGraph graph at request time: each `step` node becomes its own small
    agent+tools loop, bound only to that step's assigned tools, with its system
    prompt pulled live from Langfuse using the step's `name` as the prompt slug
  - Conditional edges (`always`, `tool_called:<id>`, `tool_not_called:<id>`) let a
    step branch to different next steps (or end) depending on what actually happened
    during that step's run
  - Tools are a fixed registry of real Python functions (`ToolRegistry`) — the UI can
    only select from tools that already exist in code, so adding a genuinely new tool
    always requires a code deploy, never a UI-only change
- A visual **Workflow Editor** in the Django admin (drag nodes, wire edges, assign
  tools, set edge conditions) reads/writes this same `workflow_json`, validated through
  the *exact* function the chat runtime uses (`WorkflowGraphBuilder.validate`) — so a
  workflow that fails to save could never have run in chat either
- Every chat turn's execution (which step ran, which tool fired, latency, token usage)
  is traced in Langfuse under that step's node name, so the graph's actual runtime
  behavior is visually inspectable per conversation turn

### 5. Admin panel
- Built on `django-unfold` for a modern UI on top of Django's built-in admin
- Field ordering standardized across models (audit fields grouped and ordered
  consistently: created_by/updated_by/timestamps/soft-delete state)
- Custom views layered on top of standard admin (workflow editor, AJAX document
  processing triggers) via each app's `ModelAdmin.get_urls()`

## Cross-cutting patterns worth knowing
- **Company scoping**: `CompanyBaseModel` (in `base/models.py`) is the base class for
  every tenant-owned model — enforces the `company` FK consistently
- **Soft delete**: `SoftDeleteManager`/`SoftDeleteQuerySet` give every such model a
  reversible delete (`is_deleted`/`deleted_at`) while `.all_objects` still exposes
  everything including soft-deleted rows for admin/cleanup use
- **Auditability**: `created_by`/`updated_by`/`created_at`/`updated_at` are tracked
  consistently via a shared base model
- **JWT auth**: custom `CustomUser` (email-based) with access + refresh tokens
  (`RefreshToken` model, 7-day lifetime, revocable)
