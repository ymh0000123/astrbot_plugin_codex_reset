# astrbot_plugin_codex_reset

订阅并推送 OpenAI Codex 用量重置动态的 AstrBot 插件。

数据来自 [codex-resets.com](https://codex-resets.com) 公开 API（免费、无需密钥）。
按其要求，插件在展示数据时会附上来源致谢：`Data from https://codex-resets.com`。
注意：Watch（预警）数据是 AI 分类预测，不是 OpenAI 官方承诺。

## 功能

1. **定时推送**：后台按配置间隔轮询 `/api/v1/status`，检测到以下变化时向已订阅会话主动推送：
   - 新重置发生（latest_reset 变化）
   - 宣布重置排期（scheduled_reset 出现/变化）
   - 出现重置预警（active_watch 出现/更新）
   - 已推送记录持久化在 `data/plugin_data/astrbot_plugin_codex_reset/state.json`，重启不重推；插件首次启动只记录基线不推送。
2. **指令**：
   - `/codex` —— 查询当前重置状态
   - `/codex 订阅` —— 订阅当前会话的推送（自动写入配置）
   - `/codex 退订` —— 取消当前会话的推送
3. **AI 工具**（LLM function calling，对话中由模型自动调用）：
   - `codex_reset_status` —— 查询实时状态（支持"简要/详细"两种风格）
   - `codex_reset_list` —— 按时间倒序列出最近 N 条重置公告

## 安装

将本目录放入 AstrBot 的 `data/plugins/` 下（或通过 WebUI 从仓库安装），重载插件即可。
依赖仅 `aiohttp`（见 `requirements.txt`，AstrBot 会自动安装）。

## 配置（WebUI 插件配置页）

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| push_targets | list | [] | 推送目标会话（unified_msg_origin，如 `aiocqhttp:GroupMessage:123456`）；推荐直接在群里发 `/codex 订阅` |
| poll_interval | int | 300 | 轮询间隔（秒），最小 60，过小可能触发 API 限流 |
| notify_new_reset | bool | true | 是否推送"新重置发生" |
| notify_scheduled | bool | true | 是否推送"重置排期" |
| notify_watch | bool | true | 是否推送"重置预警" |

## API 说明

- `GET https://codex-resets.com/api/v1/status` —— 当前状态（最新重置/排期/预警/统计），支持 ETag 条件请求
- `GET https://codex-resets.com/api/v1/resets?limit=&cursor=&from=&to=&order=` —— 重置公告列表（游标分页）
- 限流：429 时返回 `Retry-After`；插件已做对应处理并记录日志
