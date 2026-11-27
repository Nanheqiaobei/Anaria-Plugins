# astrbot_plugin_gemini_tts

基于 **AIStudio2API / Gemini 3.8 Flash TTS** 的 AstrBot 语音合成插件。

## 功能特性

- **完整配置项（WebUI 可视化填写）**：
  - `api_base`：接口地址（默认 `https://generativelanguage.googleapis.com`，同机部署可改 `http://127.0.0.1:2048`）
  - `api_key`：调用密钥（支持 `secret` 遮罩填写，自动按端点切换 `Authorization: Bearer` 与 `x-goog-api-key`）
  - `model`：内置 `gemini-3.8-flash-tts`、`gemini-3.8-flash-lite-tts` 等 5 个 TTS 模型下拉选择
  - `voice`：**完整保留 70 个音色下拉选择**（40 个新一代自然音色 + 30 个经典天体名音色），并支持 `custom_voice` 自定义覆盖
  - 支持四种调用模式：`openai`（`/v1/audio/speech`）、`gemini_native`（`/v1beta/models/{model}:generateContent` 自动为裸 PCM 补全 RIFF WAV 头）、`gemini_stream`（SSE 流式分块拼接）、`interactions`（`/v1/interactions` 返回 WAV 与 token 用量）
- **单人朗读与参数覆盖**：`/tts [-v 音色] [-s 语速] [-i "风格"] [-m 模型] [--mode 模式] 文本`
- **多说话人对话合成**：`/对话tts`，支持 `角色[音色|风格]: 台词` 逐句控制
- **音色/模式管理**：`/tts音色`、`/tts音色 在线`、`/tts切换音色`、`/tts模式`、`/tts自动`、`/tts模型 [在线]`
- **AI 工具与自动转语音**：支持大模型主动调用 `gemini_tts_speak`，以及可选开启 `auto_tts_enable` 自动将 AI 回复转为语音。

## 安装方式

将 `astrbot_plugin_gemini_tts.zip` 上传至 AstrBot WebUI「插件 -> 安装插件 -> 上传压缩包」，安装并重启/重载后，前往插件配置页面填入 `api_key`、选择 `voice` 音色即可使用。
