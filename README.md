# Memory

通过 MCP 在多个 Agent 客户端之间共享记忆。使用 tag 隔离数据，保存 raw 与 extracted Memory，异步更新文本 UserProfile 和实体关系。

客户端 A 写入的记忆，可以被连接同一服务、使用相同 tag 的客户端 B 读取。服务只接收 Agent 主动传入的内容；MCP 本身不会自动采集完整会话。

## 快速启动

需要 Docker Compose，以及一个聊天模型和一个 Embedding 模型的服务地址。

```sh
cp .env.example .env
# 编辑 .env 中的模型地址、名称、密钥和 Embedding 维度
docker compose up --build -d
curl http://localhost:8765/health
```

MCP 地址：`http://localhost:8765/mcp`，传输方式为 Streamable HTTP。将各客户端连接到这个地址，并在调用时使用相同的 tag。各客户端配置文件的具体格式以其自身文档为准。

Compose 启动 PostgreSQL、一次性的迁移任务、MCP 服务和独立 Worker。默认仅发布本机回环端口。此版本用于个人本机/可信环境，tag 是数据分区，不承担身份认证；跨设备或公开部署需要另配认证与 HTTPS。

## 模型配置

| 配置 | 用途 |
| --- | --- |
| `MEMORY_LLM_BASE_URL / API_KEY / MODEL` | `chat/completions`，用于提炼、消歧、Profile 文本编辑 |
| `MEMORY_LLM_JSON_SCHEMA` | 默认 `true`；不支持 JSON Schema 的接口可设 `false`，改用 JSON 模式并做本地校验 |
| `MEMORY_EMBEDDING_BASE_URL / API_KEY / MODEL` | `embeddings`，与聊天模型独立 |
| `MEMORY_EMBEDDING_DIMENSIONS` | 模型实际返回的维度，默认 1024；必须正确设置 |
| `MEMORY_EMBEDDING_SEND_DIMENSIONS` | 是否把维度参数传给接口，默认关闭 |
| `MEMORY_EMBEDDING_DOCUMENT_PREFIX / QUERY_PREFIX` | 支持需要文档/查询指令前缀的模型 |
| `MEMORY_RERANKER_BASE_URL / API_KEY / MODEL` | 可选；调用 `{base_url}/rerank` |

`API_KEY` 可为空，以支持本地服务。Compose 访问宿主机上的模型服务时使用 `host.docker.internal`；直接运行 Python 时通常使用 `localhost`。配置中的 API key 属于 Memory 后台，与客户端使用的模型或订阅无关。

Reranker 适配器接收 `results: [{index, relevance_score}]`，请求完整候选排序。未配置、超时或返回无效数据时，search 返回完整的 RRF 排序，并标注 `rerank_applied: false` 与错误码。

一个数据库固定一套 Embedding 配置（接口、模型、维度、前缀）。修改后会报告 `EMBEDDING_CONFIG_MISMATCH`，防止混用不兼容向量；第一版未提供自动重建索引命令。提炼模型可以单独更换。

## 五个 MCP 工具

| 工具 | 主要参数 | 行为 |
| --- | --- | --- |
| `add` | `tag, content, metadata?, idempotency_key?` | 原文和任务原子落库，立即返回 raw ID 和任务状态 |
| `search` | `tag, query, top_k=10, rerank=false` | 召回 extracted Memory，返回内容、raw IDs、排序分数 |
| `get_memory` | `tag, id` | 读取 raw 或 extracted；raw 包含处理状态和 extracted IDs |
| `get_user_profile` | `tag` | 返回文本、版本和时间戳；不存在时为版本 0 |
| `edit_user_profile` | `tag, expected_version, content` | 全文替换，原子检查版本；不会在接口内调用 LLM |

详细输入输出见 [接口约定](docs/api.md)。工具通过 `structuredContent` 和 JSON 文本返回相同数据，错误设置 `isError: true`。

推荐的客户端使用指引：

> 使用已配置的 tag。需要历史决策时先 search，需要偏好时读取 UserProfile；形成有价值的决策或发现后调用 add，保留说话人和必要上下文。add 返回后继续工作，无需每次轮询。仅在需要核对原文时通过 raw_ids 调用 get_memory。修改 Profile 时携带读取到的版本，遇到冲突后重新合并修改。

## 后台处理与检索

`add` 成功表示 raw 和待处理任务已经持久化，不代表已经能检索到提炼结果。Worker 保存提炼检查点，生成向量、消歧实体、准备 Profile 修改，然后在一个事务中写入结果并标记 completed。允许一次处理产生零条 Memory。

任务使用租约和所有权令牌，支持 Worker 重启、失败退避与过期回收。同一 tag 的待处理任务按写入顺序处理；不同 tag 可以由多个 Worker 并行处理。Profile 显式编辑与后台更新作用于同一文本；遇到并发修改时，后台仅重放仍可匹配的文本修改，保留被用户改过的区域。

失败任务不会删除原文。检查和手动重试：

```sh
docker compose logs worker
docker compose run --rm worker memory retry --tag demo --raw-id raw_...
```

两路检索：

1. Memory 向量与关键词各自产生候选，先通过 RRF 合并。
2. 实体向量命中节点，沿实体与 Memory 的关联产生独立候选。
3. 两路再次按 Memory ID 做 RRF，可选 reranker 后返回 top_k。

第一版使用 tag 过滤后的精确向量检索，便于建立正确性与性能基线；未默认建立 HNSW。中文在应用层分词，关键词排序使用 PostgreSQL `ts_rank_cd`。关系失效保留历史证据；当前不会因关系 inactive 自动隐藏相关 Memory，也未开启多跳图扩展。

## 本地开发

主开发版本 Python 3.13，包声明支持 Python 3.12+。使用 uv 和提交的锁文件：

```sh
uv sync --locked
docker compose up -d db
# 本地运行时把 .env 中模型服务地址改为本机可访问的地址
uv run memory migrate
uv run memory serve
# 在另一个终端中
uv run memory worker
```

质量检查：

```sh
uv run ruff check .
uv run ruff format --check .
uv run lint-imports
uv run deptry src
uv run pytest -q
uv build
```

没有测试数据库时，pytest 会明确跳过 PostgreSQL 集成测试。运行完整测试需要专用、可清空的测试库，库名必须以 `_test` 结尾：

```sh
docker compose exec db createdb -U memory memory_test
MEMORY_TEST_DATABASE_URL=postgresql+psycopg://memory:memory@localhost:5432/memory_test uv run pytest -q
```

测试自动迁移并清空这个测试库，覆盖跨客户端 MCP 调用、tag 隔离、幂等写入、两路召回、任务恢复、Profile 并发和关系变更。模型调用使用可控替身；真实模型的提炼质量、成本和召回效果需要另做 benchmark。

包边界、依赖升级和发布检查见 [贡献指南](CONTRIBUTING.md) 与 [架构说明](docs/architecture.md)。当前未发布到包仓库。
