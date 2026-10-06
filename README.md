# Codex Token Memory

监测 Codex 本地对话的真实 token 用量，并管理用户主动保存的长期记忆。监测面板在本机运行，无第三方依赖，不需要 API key，也不发起额外模型请求。

## 功能

- 查看累计与最近一次模型请求的输入、输出、总 tokens。
- 查看缓存读取、缓存写入、未命中输入和输入缓存命中率。
- 添加、修改、启用、停用、删除独立的长期记忆条目。
- 按全局、项目、对话范围筛选记忆，预览并下载所选内容。
- 每 2 秒读取新增日志，提供仅监听 `127.0.0.1` 的本地面板。

## 安装为 Codex 技能

将本仓库的内容放入 `$CODEX_HOME/skills/codex-token-memory`。未设置 `CODEX_HOME` 时使用 `~/.codex/skills/codex-token-memory`。确保 `SKILL.md` 直接位于该目录中。

也可以在 Codex 中提供本仓库链接，让技能安装器安装此技能。安装后调用：

> 使用 $codex-token-memory 监测当前对话并管理我的长期记忆。

## 独立运行

需要 Python 3.10 或更新版本，无需安装 Python 包。在仓库根目录执行：

```sh
python scripts/token_memory.py sessions
python scripts/token_memory.py snapshot --thread-id YOUR_THREAD_ID
python scripts/token_memory.py serve --thread-id YOUR_THREAD_ID --state-dir ./local-state
```

启动后输出本地 URL。用浏览器打开该 URL 即可监测与管理记忆。线程 ID 可从 `sessions` 结果选择；Codex 环境中也可使用 `CODEX_THREAD_ID` 或 `CODEX_SESSION_ID`。

Codex 数据目录默认是 `CODEX_HOME` 或 `~/.codex`。需要指定其他目录时，把 `--codex-home PATH` 放在子命令之前：

```sh
python scripts/token_memory.py --codex-home PATH snapshot --thread-id YOUR_THREAD_ID
```

关闭页面不会停止服务。使用面板底部“停止监测”按钮，或：

```sh
python scripts/token_memory.py stop --state-dir ./local-state
```

## 长期记忆

初始清单为空，只保存用户主动选择的内容。记忆存在所指定状态目录的 `memory.json` 中；重启时使用同一目录即可继续管理。

```sh
python scripts/token_memory.py memory add --state-dir ./local-state --title "写作偏好" --text "使用简洁中文" --scope global
python scripts/token_memory.py memory list --state-dir ./local-state
python scripts/token_memory.py memory export --state-dir ./local-state --thread-id YOUR_THREAD_ID --project YOUR_PROJECT_PATH
```

更多操作与参数见各子命令的 `--help`。跨项目共用记忆时，始终使用同一个固定状态目录。

记忆预览只包含启用且范围匹配的条目。本技能参与后续任务时，可读取这些条目作为上下文。它不会自动接管所有 Codex 对话或修改 Codex 原生记忆库。

## 统计口径与限制

- 统计来自本机 Codex `sessions` 下的 JSONL 用量事件，随日志写入更新，不是逐 token 生成统计。
- “最近一次”指模型请求，可能只是一个用户轮次中的内部调用。
- 缓存读取是输入的子集，推理输出是输出的子集，不重复相加。
- 累计输入包含反复发送的历史，不等于当前上下文长度。日志统计不是完整账单。
- 字段缺失时显示不可得；本地日志格式可能随 Codex 版本变化。
- 停用或删除独立记忆条目不会抹去已发送的历史、原生记忆或服务器缓存。
- 服务器 prompt cache 是 KV 状态，当前不提供手动清除接口。详见[官方缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching)。

字段映射及官方资料见 [references/metrics-and-limits.md](references/metrics-and-limits.md)。

## 验证

```sh
python tests/test_token_memory.py
python tests/test_panel_http.py
```

覆盖重复累计事件、增量读取与半行写入、日志截短、未知字段、记忆范围与持久化、HTTP 请求鉴权、跨来源拒绝、记忆操作及正常停止。

源码仓库不包含用户对话日志、记忆条目、运行鉴权信息或机器专用启动脚本。
