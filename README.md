# Anaria Plugins

安亦诗（Anaria）在用的自研 AstrBot 插件集合。都是自己写的，上游没有现成包可拉，放在这里方便复现和备份。
Self-built AstrBot plugins used by Anaria. They are not published anywhere upstream, so they live here.

## 列表 · Plugins

| 插件 Plugin | 功能 Feature | 版本 |
| --- | --- | --- |
| `astrbot_plugin_gemini_tts` | Gemini TTS 语音合成，支持 70 种音色与 OpenAI 兼容接口 TTS | 1.0.0 |
| `astrbot_plugin_master_link` | 主动私聊通道，跨会话发消息，提供 `send_private_message` 工具 | 1.0.0 |
| `astrbot_plugin_meme` | 本地表情包，Web 管理页、拖拽上传、URL 表情、LLM 自由调用 | 1.4.0 |
| `astrbot_plugin_remote_admin` | 跨群远程管理，私聊即可禁言、解禁、踢人、全员禁言、撤回、发消息 | 1.0.0 |
| `astrbot_plugin_yishi_draw` | 基于 gpt-image 的生图，文生图、参考图改图、多图融合、GIF | 1.1.0 |
| `astrbot_plugin_global_blacklist` | 全局触发屏蔽，名单内用户在进入 LLM 前被拦下 | 1.0.0 |
| `astrbot_plugin_deepseek_status` | DeepSeek 官方服务状态监控与群推送 | 1.0.0 |
| `astrbot_plugin_operit_stars` | GitHub 仓库实时状态与 Star 数查询（默认 Operit / Operit2） | 1.1.0 |
| `astrbot_plugin_wiki_sync` | Operit2-wiki 本地知识库定时同步与一键拉取 | 1.0.0 |

## 安装 · Install

把插件目录整个放进 AstrBot 的 `data/plugins/` 下，重启或在 WebUI 里重载插件即可。
Drop the plugin folder into `data/plugins/`, then restart AstrBot or reload it from the WebUI.

## 注意 · Notes

- 代码里所有硬编码的 QQ 号已替换为占位符 `YOUR_QQ_ID`，用之前记得在插件配置里填自己的号，否则权限相关功能不会生效。
  All hardcoded QQ IDs are replaced with the placeholder `YOUR_QQ_ID`. Fill in your own ID in the plugin config before use.
- 接口地址默认走官方端点，需要中转的自己改 `api_base`。
  Endpoints default to official APIs; change `api_base` if you use a proxy.
- `astrbot_plugin_meme` 只放了代码，`memes/` 里的表情图片没有上传，自己往里放。
  Only code is included for `meme`; the image assets in `memes/` are not uploaded.
- 依赖 AstrBot 4.x，具体版本要求见各插件自己的 `metadata.yaml`。
  Requires AstrBot 4.x. See each plugin's `metadata.yaml` for details.
- 这些插件是按自身使用场景写的，没有做通用适配，拿去用请自行调整。
  These plugins were written for a specific setup and are not generalized. Adapt as needed.
