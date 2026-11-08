# MCP 工具约定

所有工具都要求 `tag`。成功时，下面的 JSON 同时出现在 `structuredContent` 和文本 content 中。业务错误使用 `isError: true` 与 `{"error":{"code":"...","message":"..."}}`；不返回异常堆栈、模型响应体或密钥。

## add

```json
{"tag":"demo","content":"用户：Atlas 使用 PostgreSQL 保存业务数据。","metadata":{"client":"client-a","session_id":"s1"},"idempotency_key":"s1:chunk1"}
```

```json
{"raw_id":"raw_...","processing_status":"pending"}
```

content 为 1–100000 字符。幂等键在 tag 内唯一，相同请求返回同一个 raw ID 和当时的任务状态；不同请求返回 `IDEMPOTENCY_CONFLICT`。状态为 pending / processing / completed / failed。completed 允许没有提炼结果。

## search

```json
{"tag":"demo","query":"Atlas 的数据库选型","top_k":10,"rerank":false}
```

```json
{"results":[{"id":"mem_...","content":"Atlas 使用 PostgreSQL 保存业务数据。","raw_ids":["raw_..."],"score":0.0327868852}],"meta":{"score_type":"rrf","rerank_applied":false}}
```

top_k 为 1–100；query 为 1–4000 字符。默认只返回 extracted，空结果为 `results: []`。score 只能在当前结果排序中理解。Rerank 成功时 score_type 为 reranker；失败时为 rrf，并可附 `meta.rerank_error`。

## get_memory

```json
{"tag":"demo","id":"raw_..."}
```

raw 返回 id、tag、type、content、空 raw_ids、metadata、created_at、updated_at、processing_status、extracted_ids。extracted 返回相同基础字段和对应 raw_ids，不包含任务字段或 score。来源关系从关联表计算。不自动触发提炼。

ID 不存在或属于其他 tag 时均返回 `MEMORY_NOT_FOUND`。

## get_user_profile

```json
{"tag":"demo"}
```

不存在时：

```json
{"tag":"demo","content":"","version":0,"created_at":null,"updated_at":null}
```

存在时返回相同字段，content 为文本，version >= 1，时间戳为带时区 ISO 8601。

## edit_user_profile

```json
{"tag":"demo","expected_version":0,"content":"## 偏好\n- 默认使用中文。"}
```

成功返回完整 Profile，版本递增。content 为最终全文，最大 100000 字符，可为空。服务端不调用 LLM。

冲突示例：

```json
{"error":{"code":"PROFILE_VERSION_CONFLICT","message":"Reapply the intended edit to the current profile before retrying.","expected_version":7,"current_version":8,"current_content":"## 偏好\n- 默认简洁回答。"}}
```

调用方需把修改意图重新应用到 current_content，再用 current_version 重试。不能只换版本号重发旧全文。
