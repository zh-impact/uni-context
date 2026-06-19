# uni-context 设计规格

- **状态**：Draft
- **日期**：2026-06-19
- **作者**：gege + Claude（brainstorming session）

## 1. 概述

### 1.1 目标

构建一个统一的上下文知识管理应用 `uni-context`，把目前散落在多处的三类知识收敛到一个可检索的本地库：

- **User scope**：用户自己的笔记、摘抄、链接收藏
- **Project scope**：项目文档、Agent 自动产生的 memory、Agent 对话历史
- **Global scope**：外部导入的文档、手册、书籍、教程

应用提供三种对外接口，**所有接口共用同一套核心服务**：

- **CLI**（一等公民）：人 / shell pipeline / 脚本调用，完整功能集
- **HTTP REST**：第三方系统、轻量集成
- **MCP server**：AI Agent 接入（Claude Code、Cursor 等）

### 1.2 非目标（MVP 不做）

- 多用户 / 团队协作 / 权限模型（Port 预留扩展点）
- Web UI / TUI（只暴露 CLI/HTTP/MCP）
- 自动实体抽取、知识图谱构建
- 监听文件系统变化的实时同步
- 守护进程模型（每次 CLI 调用按需启动）
- 内置 embedding 模型（依赖 Ollama / LMStudio 等外部服务）

### 1.3 已确认的关键决策

| # | 维度 | 决定 |
|---|---|---|
| 1 | Scope 优先级 | user / project / global 三者同等，统一 `ContextItem` 模型 |
| 2 | 部署形态 | 单用户 local-first |
| 3 | 语言 | Go |
| 4 | Embedding provider | 优先 Ollama / LMStudio（OpenAI 兼容协议），可插拔 |
| 5 | Agent 知识入库 | Pull-based Connector（Claude Code 优先） |
| 6 | 检索 | 混合检索（向量 + BM25/FTS5 + RRF 融合） |
| 7 | 内容类型 (MVP) | Markdown / 纯文本 + PDF + 网页抽取 |
| 8 | 进程模型 | 单二进制，按需调用（无 daemon） |
| 9 | SQLite 驱动 | cgo（mattn/go-sqlite3）+ sqlite-vec |
| 10 | 多 embedding 模型 | 支持，每模型一张 vec0 表 + 注册表管理 |
| 11 | HTTP 鉴权 | 简单 token，loopback 自动生成，非 loopback 强制 |
| 12 | MCP 破坏性操作 | 允许 `update_note`（限 note/excerpt），不允许 delete |

---

## 2. 架构

### 2.1 进程模型

单 Go 二进制 `unictx`，按命令子分两种运行模式：

- **一次性命令**（`note add`、`search`、`sync run` 等）：加载 core → 打开 SQLite → 执行 → 退出。CLI 冷启动 ~100ms 级，包含同步 embed 的命令最长 ~10s。
- **常驻命令**（`unictx serve`）：长驻进程，hold 住 DB 连接 + 常驻 embedding worker，对外暴露 HTTP REST + HTTP MCP。

不做 daemon 模式；一次性命令的性能已经够 CLI 场景。

### 2.2 分层架构（Hexagonal）

```
┌──────────────────────────────────────────────────────────┐
│ Interface Layer（薄适配器，无业务逻辑）                   │
│   cli (cobra)  │  httpapi (net/http)  │  mcp (复用 svc)   │
└─────────────────────────┬────────────────────────────────┘
                          │
┌─────────────────────────┼────────────────────────────────┐
│              Application Service Layer                   │
│   ingest / search / sync / relation / project / query    │
│   (用例编排，事务边界，不变式校验)                        │
└─────────────────────────┼────────────────────────────────┘
                          │
┌─────────────────────────┼────────────────────────────────┐
│                    Domain Core                           │
│   ContextItem / Scope / Kind / Source / Relation         │
│   Project / User / Agent / Conversation / 不变式         │
│   (纯领域模型，零外部依赖)                                │
└─────────────────────────┼────────────────────────────────┘
                          │
┌─────────────────────────┼────────────────────────────────┐
│              Infrastructure Adapters                     │
│   sqlite / sqlitevec / ollama / openai-compat /          │
│   fsstore / importer_{md,pdf,web} / connector_claudecode │
│   (Port 接口的具体实现)                                   │
└──────────────────────────────────────────────────────────┘
```

硬纪律：`domain` 包不 import 任何 `adapter` 或外部库（除标准库）。Service 只依赖 `port` 接口和 `domain` 类型。

### 2.3 Go 包结构

```
uni-context/
├── cmd/unictx/main.go              # 唯一入口
├── internal/
│   ├── domain/                     # 纯领域模型
│   │   ├── context.go              # ContextItem, Scope, Kind, Source
│   │   ├── relation.go
│   │   ├── project.go  user.go  agent.go  conversation.go
│   │   └── errors.go
│   ├── service/                    # 用例编排
│   │   ├── ingest.go
│   │   ├── search.go
│   │   ├── sync.go
│   │   ├── relation.go  project.go
│   │   └── query.go
│   ├── port/                       # 接口定义（核心扩展点）
│   │   ├── repository.go  embedder.go  vectorstore.go
│   │   ├── searcher.go  importer.go  connector.go  filestore.go
│   ├── adapter/
│   │   ├── sqlite/                 # repository + FTS5 (mattn/go-sqlite3)
│   │   ├── sqlitevec/              # 向量存储（sqlite-vec 扩展）
│   │   ├── ollama/                 # Ollama HTTP embedder
│   │   ├── openai_compat/          # LMStudio / OpenAI / 智谱 / Voyage
│   │   ├── onnx/                   # P1：纯本地兜底
│   │   ├── fsstore/                # 文件存储（按 hash 寻址）
│   │   ├── importer_markdown/
│   │   ├── importer_pdf/
│   │   ├── importer_web/           # readability 抓取
│   │   └── connector_claudecode/   # ~/.claude/projects/...
│   ├── cli/                        # cobra 命令
│   ├── httpapi/                    # net/http handler
│   ├── mcp/                        # MCP server（复用 service）
│   └── config/                     # ~/.config/unictx/config.yaml
├── pkg/                            # 对外公开（暂空，预留 SDK）
└── docs/superpowers/specs/
```

### 2.4 依赖注入

唯一组装点 `wireApp(cfg) → *App`：

```go
type App struct {
    DB         *sql.DB
    Repo       port.ContextRepo
    Vector     port.VectorStore
    FTS        port.Searcher
    Embedders  map[string]port.Embedder  // slug → embedder
    Importers  map[domain.Kind]port.Importer
    Connectors []port.Connector
    Services   Services                   // ingest/search/sync/...
}
```

CLI、HTTP、MCP 三个入口在 main.go 里调用同一个 wireApp，得到 *App 后分发到对应 handler。改 Port 实现只动一处。

### 2.5 MVP / P1 / P2 范围

| 层 | MVP (P0) | P1 | P2 |
|---|---|---|---|
| Embedder Adapter | ollama + openai_compat（含 LMStudio/OpenAI/智谱/Voyage） | onnx 本地 | — |
| Importer | markdown + pdf + web | docx / epub / code | — |
| Connector | claude-code（memory + 会话 JSONL） | cursor / 通用 webhook | — |
| Service | ingest / hybrid search / sync / query / relation 基础 | 关系图遍历、自动摘要、跨实体冲突检测 | — |
| Interface | CLI（全）+ HTTP（核心）+ MCP（9 工具） | MCP stdio 持续优化 | Web UI（可选） |
| 关系图 | schema + relation CRUD（`relation add/list/delete`） | graph traversal（BFS/DFS、最短路径） | 社区检测、可视化 |
| 鉴权 | HTTP token (loopback 自动生成) | 远程访问策略 | per-user 权限 |
| 备份 | `unictx backup` / `restore` | 定时备份 | 云同步 |

---

## 3. 数据模型

### 3.1 主表：`context_item`

```sql
CREATE TABLE context_item (
  -- 标识
  id              TEXT PRIMARY KEY,        -- UUID v7（时序可排序）
  scope           TEXT NOT NULL,           -- user | project | global
  kind            TEXT NOT NULL,           -- note | excerpt | link | doc
                                             -- | conversation_msg | memory | file
  source          TEXT NOT NULL,           -- manual | agent | sync | import | webhook

  -- 归属（global 时 owner/project 都 NULL）
  owner_user_id   TEXT,
  project_id      TEXT,
  agent_id        TEXT,
  conversation_id TEXT,
  parent_id       TEXT,

  -- 内容
  title           TEXT NOT NULL DEFAULT '',
  summary         TEXT NOT NULL DEFAULT '',
  content         TEXT NOT NULL DEFAULT '',     -- <=4KB 内联
  content_uri     TEXT,                          -- >4KB 外置到 filestore
  content_mime    TEXT,
  content_hash    TEXT,                          -- sha256(content)，去重/变更检测
  language        TEXT,

  -- 检索元数据
  tags            TEXT NOT NULL DEFAULT '[]',    -- JSON array
  source_meta     TEXT NOT NULL DEFAULT '{}',    -- 原始 URL / 抓取时间 / 置信度等
  visibility      TEXT NOT NULL DEFAULT 'private',
  confidence      REAL NOT NULL DEFAULT 1.0,

  -- 系统
  word_count      INTEGER NOT NULL DEFAULT 0,
  any_embedding   INTEGER NOT NULL DEFAULT 0,    -- 冗余：是否至少一个模型已 embed
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL,
  version         INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_item_scope_created ON context_item(scope, created_at DESC);
CREATE INDEX idx_item_project       ON context_item(project_id) WHERE project_id IS NOT NULL;
CREATE INDEX idx_item_kind          ON context_item(kind);
CREATE INDEX idx_item_conversation  ON context_item(conversation_id) WHERE conversation_id IS NOT NULL;
CREATE INDEX idx_item_owner         ON context_item(owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX idx_item_any_emb       ON context_item(any_embedding) WHERE any_embedding = 0;
CREATE INDEX idx_item_hash          ON context_item(content_hash) WHERE content_hash IS NOT NULL;
```

### 3.2 全文索引（FTS5 / BM25）

```sql
-- trigram tokenizer 对中英混合都好用，存储大但召回可靠
CREATE VIRTUAL TABLE context_fts USING fts5(
  title, summary, content,
  content='context_item', content_rowid='rowid',
  tokenize='trigram'
);

-- 配套触发器保持主表与 FTS 一致（INSERT/UPDATE/DELETE）
-- 详见 §5.2 一致性策略
```

### 3.3 向量索引（多模型支持）

每模型一张 vec0 表 + 一张注册表统一管理。

```sql
CREATE TABLE embedding_model (
  slug        TEXT PRIMARY KEY,        -- 'bge-m3' / 'openai-text-embed-3-small' 等
  name        TEXT NOT NULL,
  provider    TEXT NOT NULL,           -- ollama | openai-compat | onnx
  dimension   INTEGER NOT NULL,
  vec_table   TEXT NOT NULL,           -- 对应 vec0 表名
  is_default  INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
  status      TEXT NOT NULL DEFAULT 'active',  -- active | evaluating | deprecated
  config      TEXT NOT NULL DEFAULT '{}',      -- base_url, model name, batch size 等
  created_at  INTEGER NOT NULL
);

-- 每个模型一张 vec0 表，运行时按 embedding_model 注册生成 DDL
CREATE VIRTUAL TABLE vec_bge_m3_1024 USING vec0(
  item_id TEXT PRIMARY KEY,
  embedding FLOAT[1024]
);

-- Item × 模型 N:N
CREATE TABLE context_embedding (
  item_id     TEXT NOT NULL REFERENCES context_item(id) ON DELETE CASCADE,
  model_slug  TEXT NOT NULL REFERENCES embedding_model(slug),
  embedded_at INTEGER NOT NULL,
  status      TEXT NOT NULL,           -- done | failed
  error       TEXT,
  PRIMARY KEY (item_id, model_slug)
);
CREATE INDEX idx_emb_model ON context_embedding(model_slug);
```

**约束**：同一时刻只有一个 `is_default=1`（应用层强制）。

### 3.4 关系图（schema + CRUD P0，graph traversal P1）

```sql
CREATE TABLE context_relation (
  id            TEXT PRIMARY KEY,
  from_id       TEXT NOT NULL REFERENCES context_item(id) ON DELETE CASCADE,
  to_id         TEXT NOT NULL REFERENCES context_item(id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL,    -- references | extends | contradicts | summarizes | related
  weight        REAL NOT NULL DEFAULT 1.0,
  created_by    TEXT,             -- user | agent:<id>
  created_at    INTEGER NOT NULL,
  UNIQUE(from_id, to_id, relation_type)
);
```

### 3.5 辅助实体

```sql
CREATE TABLE project (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  path TEXT, description TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);

CREATE TABLE agent (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
  config TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);

CREATE TABLE conversation (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agent(id),
  project_id TEXT REFERENCES project(id),
  external_ref TEXT,
  started_at INTEGER NOT NULL, ended_at INTEGER,
  summary TEXT,
  UNIQUE(agent_id, external_ref)
);
```

### 3.6 同步与队列

```sql
-- Pull connector 幂等同步
CREATE TABLE sync_state (
  connector     TEXT NOT NULL,    -- 'claude-code' / 'cursor' / ...
  external_ref  TEXT NOT NULL,    -- 文件路径 / session id
  last_synced_at INTEGER NOT NULL,
  last_hash     TEXT,
  last_error    TEXT,
  PRIMARY KEY (connector, external_ref)
);

-- embedding 任务队列（SQLite 当队列）
CREATE TABLE embed_queue (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id       TEXT NOT NULL,
  model_slug    TEXT NOT NULL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  next_try_at   INTEGER NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending', -- pending | running | done | dead
  last_error    TEXT,
  created_at    INTEGER NOT NULL,
  UNIQUE(item_id, model_slug)
);
CREATE INDEX idx_queue_due ON embed_queue(status, next_try_at)
  WHERE status = 'pending';
```

**两种 status 字段的区别**：
- `embed_queue.status`：任务**生命周期**（pending → running → done/dead）。retryable 失败回到 `pending`（`next_try_at` 设到未来）；耗尽重试次数才是 `dead`。
- `context_embedding.status`：item×model 的**最终结果**（done | failed）。只有耗尽重试或不可重试错误才写一条 `failed` 记录。

### 3.7 Schema 版本管理

```sql
CREATE TABLE schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- key='schema_version', value='1'
```

启动时按序跑 `internal/adapter/sqlite/migrations/NNNN_*.sql`。

### 3.8 存储职责划分

| 数据 | 存储 | 备注 |
|---|---|---|
| ContextItem 元数据 / 关系 / project / agent / conversation / sync_state / embed_queue / embedding_model / context_embedding | SQLite 主库 | 结构化、可 join |
| BM25 索引 | SQLite FTS5 虚拟表 | 触发器跟主表同步 |
| 向量 | SQLite vec0 虚拟表（每模型一张） | 单文件零运维 |
| 大于 4KB 的原文（PDF、HTML、长对话 JSONL） | filestore 本地目录，按 hash 寻址 | content_uri 引用，引用计数去重 |
| 配置 | `~/.config/unictx/config.yaml` | 不入库 |
| 鉴权 token | `~/.config/unictx/auth.json` (chmod 0600) | 不入库 |
| 应用数据目录 | `~/.local/share/unictx/` (XDG) | 含 sqlite 文件 + filestore + 备份 |

### 3.9 SQLite 默认配置

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
PRAGMA temp_store=MEMORY;
```

### 3.10 关键设计决定

1. **Conversation 粒度 = 一条消息一个 ContextItem**：检索精确到答；同 conversation 共享 `conversation_id`，需要上下文再 join。
2. **内容大小阈值 4KB**：超出外置到 filestore，避免 FTS 索引和主表扫描被大对象拖慢。
3. **scope 由 (owner, project) 推导 + 显式存储**：冗余但查询直观。
4. **FTS 用 trigram 而非 unicode61**：中文按字分 token 召回差；trigram 中英混合都好用，存储大可接受。P1 可考虑 jieba。
5. **删除走 ON DELETE CASCADE**：关系、context_embedding、conversation 关联自动清理；vec 表通过 service 层显式清理。

---

## 4. 接口设计

### 4.1 CLI 命令树

```
unictx
├── user                          # 个人 scope
│   ├── note    add|list|get|update|delete
│   ├── excerpt add|list|get|update|delete
│   └── link    add|list|get|delete          # add 默认抓取 + 摘要
├── project                       # 项目 scope
│   ├── create|list|rename|delete <name>
│   ├── doc       import <name> <file|dir|url>   # 自动识别 md/pdf/web
│   │             list|get|delete
│   ├── memory    list <name>
│   └── conversation list <name>|show <id>
├── global
│   ├── import <file|dir|url>
│   └── list|get|delete
├── search "<query>"
│   options: --scope user,project:myapp,global
│            --kind note,doc,memory,...
│            --tag t1,t2          (AND)
│            --model bge-m3
│            --mode hybrid|vector|fts   (默认 hybrid)
│            --compare m1,m2
│            --limit 20 --json
├── model
│   ├── register <slug> --provider ollama|openai-compat|onnx
│   │                     --base-url ... --model ... --dim N [--default]
│   ├── list|default <slug>
│   ├── backfill <slug> [--scope ...]
│   ├── deprecate <slug>
│   └── remove <slug> [--purge]
├── sync
│   ├── run [--connector claude-code] [--project <name>]
│   ├── status
│   └── connector list|add <type> --config ...
├── relation add|list|delete      # P1（schema 已就绪）
├── serve [--http :7234] [--mcp /mcp] [--bind 127.0.0.1] [--cors]
├── embed process|status          # 手动 drain / 看队列
├── auth show|rotate|token <value>
├── backup [--output <path>|--stdout]
├── restore <path>
├── doctor [--consistency] [--repair] [--gc-filestore] [--rebuild-vec <slug>] [--rebuild-fts]
├── config get|set|path
└── completion bash|zsh|fish
```

**CLI 约定**：
- 默认人类可读（表格 + 颜色）；`--json` 输出结构化
- stdin 接管：`-` 表示从管道读 content（如 `cat note.md | unictx user note add -`）
- 退出码：`0` ok / `1` 一般错 / `2` 参数错 / `3` DB 错 / `4` 外部依赖（Ollama 等）不可达
- stderr = 日志/错误，stdout = 数据（便于 `| jq`）
- 默认日志级别 `info`，`--verbose` 开 `debug`，`--quiet` 只 `error`

### 4.2 HTTP 端点

RESTful，前缀 `/api/v1`。

```
# Items（核心 CRUD，覆盖所有 scope）
POST   /items                      body: {scope, kind, source, project_id?, title, content|content_uri, tags[], source_meta{}}
GET    /items?scope=&project=&kind=&tag=&cursor=&limit=
GET    /items/{id}
PATCH  /items/{id}
DELETE /items/{id}

# Search
POST   /search                     body: {query, scope[], project_id?, kind[], tags[], model?, mode, limit, compare?[]}
GET    /search?q=...&limit=20

# Bulk ingest
POST   /ingest                     body: {items: [...]}   # ≤100/批，异步

# Project / Model / Sync / Relation
GET|POST          /projects
GET|DELETE        /projects/{name}
GET|POST          /models
PATCH|DELETE      /models/{slug}?purge=false
POST              /models/{slug}/backfill
POST              /sync/run
GET               /sync/status
POST              /relations
GET               /relations?from=|to=|around=
GET               /health                   # 不需鉴权
```

**约定**：
- cursor 分页（基于 `created_at + id`），不用 offset
- 错误响应统一 `{error: {code, message, details?}}`
- Content-Type 全 JSON
- MCP server 挂在 `/mcp`，与 REST 共用同一 server、同一 token

**鉴权**：

| 端点 | 是否鉴权 |
|---|---|
| `GET /api/v1/health` | ❌ |
| 其他所有 `/api/v1/*` | ✅ `Authorization: Bearer <token>` |
| `/mcp`（HTTP MCP） | ✅ 共用同一 token |

**Bind 策略**：
- 默认 `127.0.0.1:7234`
- `--bind 0.0.0.0` 或非 loopback 时，token 为硬性要求；无 token 启动失败（exit 4）
- loopback 时也启用 token，但首次自动生成不阻塞

**Token 生命周期**：
- `unictx serve` 首次启动时无 token 则自动生成，存 `~/.config/unictx/auth.json` (chmod 0600)
- `unictx auth show` / `rotate` / `token <value>` 管理
- stdio MCP（`unictx mcp stdio`）：进程边界已隔离，无需 token

### 4.3 MCP 工具集（9 个）

粗粒度、参数 ≤ 6、默认值对 Agent 友好。

| 工具 | 参数 | 用途 |
|---|---|---|
| `search_context` | `query, scope?, project?, kind?, limit?` | 混合检索 |
| `add_note` | `content, title?, tags?, scope?, project?` | 新增 note |
| `add_memory` | `content, title?, project, tags?` | Agent 写 memory 到指定 project |
| `update_note` | `item_id, content?, title?, tags?` | Agent 自我修正（限 kind ∈ {note, excerpt} 且 source=manual） |
| `get_context` | `item_id` | 完整内容（解析 content_uri） |
| `list_recent` | `scope?, project?, kind?, since?, limit?` | 时间倒序最近条目 |
| `list_projects` | — | 列 project，帮 Agent 选 scope |
| `add_relation` | `from_id, to_id, relation_type, weight?` | 关系（P1，接口先开） |
| `health` | — | 服务状态、可用模型 |

**故意不给 Agent**：`delete` / `model register` / `sync run` / `auth` 等管理性操作。破坏性最小的 `update_note` 也加了 kind/source 校验。

`search_context` 返回示例：

```json
{
  "results": [
    {
      "id": "01J...",
      "title": "部署流程",
      "kind": "note",
      "scope": "project",
      "project": "uni-context",
      "snippet": "...混合检索 + RRF...",
      "score": 0.87,
      "matched_by": ["vector", "fts"],
      "tags": ["deploy", "go"]
    }
  ],
  "total": 42,
  "model": "bge-m3"
}
```

### 4.4 接口职责边界

| 接口 | 主要使用者 | 范围 | 持久性 |
|---|---|---|---|
| CLI | 人、shell、脚本 | 全功能 + 管理 | 进程外（每次新启） |
| HTTP | 第三方系统、轻量集成 | 全功能（除管理类） | 长驻 `unictx serve` |
| MCP | AI Agent | 只读 + 增量写 | 同 HTTP，复用 server |

---

## 5. 数据流

### 5.1 写入流（统一 ingest 管线）

所有写入入口走同一条 `service.Ingest` 调用，差异在预处理：

```
Source (CLI / HTTP / MCP / Connector / Importer)
  │
  ▼
service.Ingest(input)
  ├── 1. Validate (domain 不变式)
  ├── 2. Normalize (mime/size/lang)
  ├── 3. Compute content_hash
  ├── 4. If content > 4KB: write to filestore → content_uri
  ├── 5. Auto-summarize (if title empty & long content)
  ├── 6. INSERT context_item (any_embedding=0) [事务]
  ├── 7. FTS index via trigger [事务内]
  ├── 8. Enqueue embed tasks (per active model)
  └── 9. (Sync mode) Wait for embedding completion
       (Async mode) Return immediately
```

**同步 vs 异步（按入口）**：

| 入口 | 默认 | 可配置 |
|---|---|---|
| CLI `note add` 等 | 同步等待（超时 10s） | `--async` |
| HTTP `POST /items` | 异步 | `?wait=true` |
| MCP `add_note` | 异步 | — |
| Connector sync | 异步（批量） | — |
| Bulk ingest `/ingest` | 异步 | — |

### 5.2 检索流（混合检索 + RRF）

```
search(query, filters, mode, model, limit, compare?)
  │
  ▼
Step 1: Query embedding
   vec_q = embedder.Embed(query, model)
   失败：默认报错；mode=fts-only 跳过
  │
  ▼
Step 2: 并行多路召回（over-fetch k*3）
   Vector: SELECT FROM vec_<model> ORDER BY distance LIMIT k*3
   FTS:    SELECT FROM context_fts MATCH ? ORDER BY bm25 LIMIT k*3
   多模型对比：vector 跑 N 次，FTS 共用一次
  │
  ▼
Step 3: 过滤（SQL WHERE 下推）
   scope / project_id / kind / tags
   过滤后 < limit：以 k*5 重召回一次
  │
  ▼
Step 4: RRF 融合
   score(d) = Σ 1/(rank_i + 60)
   双路命中加分；对比模式下各模型独立计分
  │
  ▼
Step 5: Top-k + Hydrate
   snippet（FTS highlight / vector 字符位置启发式）
   matched_by: ["vector"] / ["fts"] / ["both"]
  │
  ▼
{results: [...], total, model, mode}
```

**关键决策**：
- 过滤在 SQL WHERE 下推，避免在 Go 内存里过滤
- over-fetch 系数 3，不足时降级 k*5 重召回一次
- `--mode fts-only`：embedder 不可用时的降级路径，永远可用

### 5.3 同步流（Claude Code connector）

```
sync run --connector claude-code [--project <name>]
  │
  ▼
Step 1: 发现源
   扫描 ~/.claude/projects/<slug>/
     ├── memory/*.md   → kind: memory
     └── *.jsonl       → kind: conversation_msg (per message)
   slug → 解码 → 原始 cwd → 匹配 project.path → project_id
   无匹配：自动创建 project (name = basename)
  │
  ▼
Step 2: 增量检测
   for each file:
     h = sha256(file)
     prev = sync_state[connector][path]
     if h == prev.last_hash: skip
     else: 进入解析
  │
  ▼
Step 3: 解析 + Upsert
   memory/*.md → 一个 ContextItem (kind=memory, source=agent)
                 file mtime → updated_at
   *.jsonl → 逐行解析 message
              每条 → 一个 ContextItem (kind=conversation_msg,
                                       conversation_id=..., parent_id=prev)
              message uuid 去重：UPDATE 已存在 / INSERT 新增
              Tool 结果/系统消息：截断 4KB，原文存 filestore
  │
  ▼
Step 4: 更新 sync_state
   last_synced_at = now, last_hash = h
  │
  ▼
{files_scanned, new, updated, unchanged, errors}
```

**故意不做（MVP）**：
- 不删除（源文件没了不自动删库；P1 加 `--prune`）
- 不监听文件变化（手动 `sync run`；P1 加 `sync watch` 后台轮询）

### 5.4 后台流（Embedding 队列）

SQLite 当队列，无需外部 MQ。

**Worker 生命周期**：

| 入口 | 行为 |
|---|---|
| `unictx serve` | 启动 N 个常驻 worker（默认 1），轮询 |
| 一次性 CLI 命令 | 启动临时 worker，drain pending 后退出（超时 10s） |
| `unictx embed process` | 手动一次 drain |
| `unictx embed status` | 看队列深度、失败率 |

**Worker 循环**：

```
loop:
  task = SELECT ... WHERE status='pending' AND next_try_at <= now
         ORDER BY next_try_at LIMIT 1
  if no task: sleep 500ms; continue

  UPDATE task SET status='running'

  content = load_content(task.item_id)         // 解析 content_uri
  content = truncate(content, model.max_tokens)

  try:
    vec = embedder.Embed(content, model)
    BEGIN TX
      INSERT INTO vec_<model> (item_id, embedding) VALUES (?,?)
      INSERT INTO context_embedding (item_id, model_slug, status='done')
      UPDATE context_item SET any_embedding=1
      UPDATE task SET status='done'
    COMMIT
  except RetriableError (网络/429/超时):
    attempts += 1
    if attempts < max_attempts:
      backoff = exp(attempts) + jitter         // 1s, 5s, 30s
      UPDATE task SET status='pending',
                     next_try_at=now+backoff,
                     attempts=attempts, last_error=err
    else:
      UPDATE task SET status='dead', last_error=err
      INSERT context_embedding (status='failed', error=err)
  except NonRetriableError (内容过长/模型不存在):
    UPDATE task SET status='dead', last_error=err
```

**速率限制**：
- Ollama / LMStudio：默认 QPS=10、并发=1（避免本地推理打满 CPU）
- 收到 429 时尊重 `Retry-After` header
- 配置可调：`embedder.workers: 2`、`embedder.qps: 20`

**Backfill**：

```sql
-- unictx model backfill bge-m3 [--scope ...]
INSERT INTO embed_queue (item_id, model_slug, next_try_at)
SELECT id, 'bge-m3', now FROM context_item ci
WHERE NOT EXISTS (
  SELECT 1 FROM context_embedding ce
  WHERE ce.item_id = ci.id AND ce.model_slug = 'bge-m3'
)
AND <scope filter>;
```

Worker 自然消化；CLI 显示进度：`bge-m3: 0/2341 (0%), ETA 18m`。

**僵尸任务回收**：worker 启动时执行
```sql
UPDATE embed_queue SET status='pending', next_try_at=now
WHERE status='running' AND next_try_at < now - 5min;
```

---

## 6. 错误与一致性策略

### 6.1 事务边界

| 操作 | 原子性 | 实现 |
|---|---|---|
| INSERT / UPDATE / DELETE context_item | **强**：主表 + FTS + cascade | 单事务，触发器内执行 |
| INSERT context_relation | **强**：双向校验 + 写入 | 单事务 |
| INSERT vec + context_embedding | **中**：两者一致，跟主表解耦 | embedding worker 单事务 |
| Connector 单文件 → 多 item upsert | **中**：单文件事务 | 每文件一事务 |
| Bulk ingest | **松**：每条独立 | 单条失败不阻塞其他 |
| Backfill 入队 | **松**：UNIQUE 约束保幂等 | `INSERT OR IGNORE` |

### 6.2 三方一致性（item / FTS / vec）

```
主表存在 ──→ FTS 必有              (强约束，触发器保证)
主表存在 ──→ vec 可有可无           (正常，async 未完成)
vec 存在   ──→ 主表必须存在          (强约束，删除时清理)
context_embedding 记录 ←→ vec 表   (一致)
```

**保障机制**：
1. FTS 通过触发器跟主表事务同步
2. `context_item` AFTER DELETE 触发器同步删 vec + context_embedding（若 sqlite-vec 虚拟表触发器不支持跨表操作，则改在 service 层显式删，二选一在实现时验证）
3. 僵尸任务回收（见 §5.4）
4. `unictx doctor --consistency`：扫描并报告孤儿；`--repair` 自动修

### 6.3 文件存储一致性

`content_uri` 按 `content_hash` 寻址去重：

```
~/.local/share/unictx/filestore/
  ab/abc123def...        # 前两位分桶
  ab/abc123def....meta   # {size, mime, ref_count}
```

**引用计数**：同 hash 多 item 共享；删 item 时 `ref_count -= 1`，归零才删文件。

**孤儿 GC**：`unictx doctor --gc-filestore` 显式调用，扫描 filestore 反查 `content_hash`，无引用则删。MVP 不做自动 GC。

### 6.4 Schema 迁移

嵌入式版本化 SQL，启动时自动迁移：

```
internal/adapter/sqlite/migrations/
  0001_init.sql
  0002_add_relation_table.sql
  0003_add_embed_queue.sql
  ...
```

启动流程：
```
1. Open DB
2. 检查 schema_meta.schema_version
3. 按序跑 migrations/NNNN_*.sql
4. UPDATE schema_meta
5. 失败：ROLLBACK，拒绝启动，报清楚是哪个 migration
```

**Embedding 模型切换不算 schema 迁移**：新模型注册时 runtime DDL 创建 vec0 表；删模型按 `--purge` 显式触发 DROP TABLE + DELETE FROM context_embedding。

### 6.5 备份与恢复

Local-first 承诺：用户随时能拿走自己的数据。

**`unictx backup`**：

```bash
unictx backup                              # → ~/.local/share/unictx/backups/<ts>.tar
unictx backup --output /path/to.tar
unictx backup --stdout | gzip > remote.gz
```

流程：
1. `PRAGMA wal_checkpoint(TRUNCATE)` 把 WAL 写回主文件
2. `VACUUM INTO '<tmp>.db'` 一致性快照
3. tar 打包：snapshot.db + filestore/ + config.yaml + auth.json
4. sha256 校验和写入 backup.sha256

**`unictx restore`**：

```bash
unictx restore /path/to/backup.tar
```

流程：
1. 检查 `serve` 是否在跑（拒绝热还原）
2. 解压到临时目录
3. 校验 sha256
4. 替换 `~/.local/share/unictx/{sqlite, filestore}`
5. 不动 config.yaml / auth.json（用户决定是否覆盖）

**关键约束**：备份文件自包含、可读（标准 tar + 标准 sqlite）。即使本应用未来不维护，用户能用任何 SQLite 客户端读出数据。filestore 文件名即 hash，独立可读。

### 6.6 同步冲突处理

MVP 策略：**用户手动编辑优先，sync 仅补缺**。

| 情况 | 行为 |
|---|---|
| Connector 想写，DB 中无（按 external_ref） | INSERT |
| Connector 想更新，DB 有且 source=agent | UPDATE |
| Connector 想更新，DB 有且 source=manual | **跳过**，记 `sync_state.last_error='user_edited'` |
| 用户手动 add，已存在同 hash | 返回已存在 item_id，不重复 |

不做双向合并、不做 CRDT。P1 再考虑 `--force-overwrite`。

### 6.7 错误码与日志

**Domain 错误类型**：

```go
// internal/domain/errors.go
var (
    ErrNotFound           = errors.New("not found")
    ErrValidation         = errors.New("validation")
    ErrConflict           = errors.New("conflict")
    ErrExternalDependency = errors.New("external dependency")
    ErrSchemaIncompatible = errors.New("schema incompatible")
)
```

**映射**：

| 错误 | CLI exit | HTTP status |
|---|---|---|
| `ErrNotFound` | 1 | 404 |
| `ErrValidation` | 2 | 400 |
| `ErrConflict` | 1 | 409 |
| `ErrExternalDependency` | 4 | 502 |
| `ErrSchemaIncompatible` | 3 | 500 |
| 其他 | 1 | 500 |

**日志**：默认 stderr，结构化 JSON（`slog`），级别 `info`。不写文件日志（交系统 journald / launchd）。

### 6.8 容量与运维

| 关注 | 策略 |
|---|---|
| SQLite 单库上限 | 单用户 < 10GB 没问题；> 50GB 考虑分库 |
| sqlite-vec 索引膨胀 | `doctor --rebuild-vec <model>` 重建 |
| FTS 索引膨胀 | `doctor --rebuild-fts` rebuild |
| WAL 文件膨胀 | 每次 backup 时 `wal_checkpoint(TRUNCATE)`；定期 `PRAGMA optimize` |
| `serve` 长跑 | 每周自动 checkpoint（service 启动时检查上次时间） |

---

## 7. 测试策略

### 7.1 测试金字塔

```
            △
           / \
          / E2E\           ~50  cli/http/mcp 黑盒
         /─────\
        /  集成  \         ~200 service × 真 adapter
       /─────────\
      /  adapter   \       ~150 每 adapter 独立
     /─────────────\
    /    service    \      ~300 service + mock port
   /─────────────────\
  /      domain      \     ~200 纯函数 + 不变式
 /─────────────────────\
```

### 7.2 工具选型

| 用途 | 工具 |
|---|---|
| 测试框架 | Go 标准 `testing` + `testify/assert` & `require` |
| Mock | 手写 fake 实现 Port 接口 |
| HTTP 测试 | `net/http/httptest` |
| SQLite 测试 | 内存库（小用例）+ 临时文件（大用例） |
| Ollama/LMStudio mock | `httptest.NewServer` 模拟 `/api/embeddings` |
| Fixture 数据 | `testdata/` + `go:embed` |
| 快照测试 | 自写 golden + diff |
| 性能基准 | Go 标准 `Benchmark` |
| 属性测试 | `leanovate/gopter`（按需） |

### 7.3 各层重点

- **Domain**：scope/kind/source 组合不变式 100% 覆盖；纯函数。
- **Service**：mock port，覆盖正常/空输入/外部失败/并发/事务回滚。
- **Adapter**：真 SQLite（cgo）；外部 HTTP 用 `httptest` 起 mock。
- **集成**：跨 service 流程跑通（ingest→queue→embed→search；sync→ingest→search；backup→restore→search）。
- **E2E**：从外部用户视角，`--json` 输出做断言。

### 7.4 测试数据（`testdata/`）

```
testdata/
├── markdown/
│   ├── simple.md
│   ├── with_frontmatter.md
│   ├── cjk_mixed.md
│   └── very_long.md              # 10MB 测大文件
├── pdf/
│   ├── one_page.pdf
│   ├── multi_page.pdf
│   ├── scanned.pdf               # 无文本层（优雅失败）
│   └── encrypted.pdf             # 明确报错
├── html/
│   ├── article.html
│   ├── paywalled.html
│   └── javascript_heavy.html     # P1 才支持
├── claude_code/
│   ├── memory/MEMORY.md
│   └── session.jsonl             # 自造或脱敏
└── queries/
    ├── zh.json / en.json / mixed.json / edge_cases.json
```

**法律**：所有 `testdata/claude_code/` 必须自造或脱敏，不得拷贝真实用户隐私。

### 7.5 数据驱动场景

- **多模型对比**：同 query 跑 N 个模型，断言两路都有结果
- **CJK tokenization**：trigram 对中英混合的边界用例
- **Importer 边界**：每 importer 至少测正常/空/超长/二进制污染/编码/损坏

### 7.6 性能基准（CI 卡门槛）

放在 `internal/bench/`：

| 场景 | 目标 |
|---|---|
| 单条 ingest（含同步 embed） | < 200ms |
| 100 条 bulk ingest | < 5s |
| 1 万条库 hybrid search | < 50ms (p95) |
| 1 万条库 FTS search | < 10ms (p95) |
| Claude Code 1000 条会话同步 | < 3s |
| `unictx serve` 启动 | < 500ms |

### 7.7 CI 矩阵

```yaml
jobs:
  test-short:        # PR 必跑 < 2min
    - go test ./internal/domain/... ./internal/service/...
    - go test -race ./...
  test-integration:  # PR 必跑 < 5min
    - go test -tags=integration ./internal/adapter/...
  test-e2e:          # nightly < 10min
    - go test -tags=e2e ./internal/cli/... ./internal/httpapi/... ./internal/mcp/...
  test-ollama-real:  # nightly，docker compose
    services: { ollama: image: ollama/ollama }
    - go test -tags=realdeps ./internal/adapter/ollama/...
  bench:             # nightly
    - go test -bench=. -benchmem ./internal/bench/...
  lint:
    - golangci-lint run
    - go vet ./...
```

所有测试默认 `-race`。

### 7.8 手动验证清单（release 前）

```
[ ] 全新机器上 brew install / go install 能正常用
[ ] Ollama 未启动时 unictx doctor 给清晰提示
[ ] Claude Code session 文件结构变化时 sync 有清晰报错（不 panic）
[ ] 备份文件能在另一台机器 restore 成功
[ ] 中文 / 英文 / 混合 query 主观检索质量
[ ] MCP 在 Claude Code 实际跑通（写 MEMORY.md，让 Claude 调 search_context）
[ ] 大文件（>100MB PDF）不卡死
[ ] serve 跑 24h 后内存稳定（不泄漏）
```

---

## 8. 未来工作（P1+）

- **关系图遍历**：基于 `context_relation`，提供 graph traversal API（BFS/DFS、最短路径、社区检测）
- **自动摘要 / 实体抽取**：长文档入库时自动摘要，提取实体写入 `tags` 或独立实体表
- **跨实体冲突检测**：标 `relation_type=contradicts` 时提醒用户复核
- **ONNX 本地 embedding**：作为无服务器场景的兜底（P1）
- **docx / epub / code importer**：扩展内容类型覆盖（P1）
- **Cursor / 通用 webhook connector**：扩展 Agent 数据来源（P1）
- **`sync watch`**：后台监听 `~/.claude/projects/` 变化，实时增量同步（P1）
- **Web UI**：本地 web 前端（P2，可选）
- **多用户 / 团队**：Port 接口已预留，加 PG schema 迁移和 auth（P2）
- **云备份同步**：rsync / S3 / WebDAV target（P2）

---

## 9. 未决问题（实现时再定）

1. **FTS 触发器跨虚拟表写 vec 表**：sqlite-vec 的 vec0 虚拟表能否在主表 AFTER DELETE 触发器里清理？需在实现时验证；不行则改在 service 层。
2. **PDF 解析库**：选 `pdfcpu`（纯 Go，文本提取较弱）还是包装 `pdftotext`（系统依赖）？倾向 `pdftotext` 兜底 + `pdfcpu` 处理元数据。
3. **Web 抓取的 JS 渲染**：MVP 用 `go-readability` 静态抓取；遇到 SPA 站点标记 `source_meta.fetch_incomplete=true`。P1 引入 playwright。
4. **Embedding 任务的事务粒度**：当前设计为单任务单事务（INSERT vec + context_embedding + UPDATE context_item.any_embedding）。如果 ONNX 路径将来支持批处理，是否切到批事务？
5. **MCP server 协议**：MCP over stdio 跟 HTTP MCP 是否共用一套 handler？需要 MCP Go SDK 选型后定。

---

## 10. 参考资料

- sqlite-vec: https://github.com/asg017/sqlite-vec
- mattn/go-sqlite3: https://github.com/mattn/go-sqlite3
- FTS5 文档: https://www.sqlite.org/fts5.html
- Ollama API: https://github.com/ollama/ollama/blob/main/docs/api.md
- Model Context Protocol: https://modelcontextprotocol.io
- Claude Code memory 布局: https://docs.claude.com/en/docs/claude-code/memory
