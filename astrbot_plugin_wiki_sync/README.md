# astrbot_plugin_wiki_sync

Operit2-wiki 本地知识库的定时同步与一键拉取插件。

## 功能

- 后台按 `interval_hours` 周期 `git fetch`，有更新就 `pull --ff-only` 并推送摘要
- 指令 `同步wiki`（别名：拉取wiki / 更新wiki / wiki同步）立即拉取
- 指令 `wiki状态`（别名：wiki检查）只检查不拉取
- LLM 工具 `sync_operit_wiki`，可带 `check_only` 参数

## 权限

手动指令与工具仅对 `admin_qqs` 名单内的 QQ 号生效，默认只有 YOUR_QQ_ID。

## 配置

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| repo_path | 本地仓库路径 | /AstrBot/data/安亦诗本地记忆库/Operit知识储存库/Operit2-wiki |
| branch | 跟踪分支 | main |
| interval_hours | 自动检查间隔，0 关闭 | 6 |
| notify_enabled | 更新后是否推送 | true |
| notify_session | 推送会话 | pre-config:FriendMessage:YOUR_QQ_ID |
| admin_qqs | 允许手动触发的 QQ 号 | ["YOUR_QQ_ID"] |

## 说明

- 仓库的 `origin` 走 gh-proxy 代理，容器内 git 版本 2.47.3
- 只做 fast-forward 拉取，遇到本地改动冲突会报错而不是强推
- 拉取摘要包含提交数、文件增删改数量、review 与 wiki 正文条目数、改动最多的目录
