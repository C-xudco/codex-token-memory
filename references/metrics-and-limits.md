# 数据来源与边界

核实日期：2026-10-06；字段可能随版本变化。

本机实际观察到 `sessions/YYYY/MM/DD/rollout-*.jsonl` 的 `event_msg` → `payload.type=token_count` → `payload.info`：

|字段|含义|
|---|---|
|`total_token_usage`|已记录累计快照|
|`last_token_usage`|最近一次模型请求，未必是完整用户轮次|
|`input_tokens`|输入总量，含缓存输入|
|`cached_input_tokens`|缓存读取|
|`cache_write_input_tokens`|缓存写入，仅提供时可得|
|`output_tokens`|输出总量，含推理输出|
|`reasoning_output_tokens`|推理输出子集|
|`total_tokens`|记录中总量|
|`model_context_window`|模型上下文容量，不代表占用|

脚本增量读取完整 JSON 行，忽略非用量正文；重复累计快照不会重复计数。日志截短或替换时重读。缺失字段为未知。失败调用、工具调用、其他线程、子代理等不能推断为完整计入此线程数据。

App Server 有 `thread/tokenUsage/updated` 通知；单独启动新 app-server 不代表连接上桌面版已有进程。本技能只读已有日志，不启动新的模型会话。

Responses API 的 `usage.input_tokens_details.cached_tokens`、`cache_write_tokens` 与 `output_tokens_details.reasoning_tokens` 必须以实际响应为准。本技能不拦截 API 或 ChatGPT 网页请求。

缓存是 KV 状态，不是语义记忆。当前官方不提供手动清除；cache key 的改变也不能证明旧缓存被删除。保留时间、参数和价格依模型及服务而定，不硬编码。

- [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)：缓存读写、命中率和不能手动清除。
- [Codex App Server](https://learn.chatgpt.com/docs/app-server)：线程 tokenUsage 通知。
- [Response usage schema](https://developers.openai.com/api/reference/resources/responses/methods/retrieve)：用量细分。

查阅新 API 功能或价格时重新核实。此技能不需要 API key，不发起模型请求。
