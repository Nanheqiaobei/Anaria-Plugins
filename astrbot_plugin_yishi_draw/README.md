# 安亦诗生图（astrbot_plugin_yishi_draw）

对接 OpenAI 兼容的 `gpt-image` 系列生图接口，给 AstrBot 加上文生图、参考图改图、GIF 动图和 LLM 自主生图。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/绘 提示词` | 文生图 |
| `/改绘 提示词` | 带图或引用图 → 图生图 / 多图融合 |
| `/动图 提示词` | GIF 动图；带图或引用图就是让原图动起来 |
| `/绘模型 [在线]` | 看默认模型；加「在线」会拉接口的模型列表 |
| `/绘风格` | 看风格预设键名 |
| `/绘历史 [n]` | 看本会话最近生成记录 |
| `/绘重发 序号` | 重发历史图 |
| `/绘删 序号` | 删记录和本地文件 |
| `/绘帮助` | 用法 |

别名：`/绘图`、`/ydraw`、`/亦绘`；`/改画`、`/yedit`；`/gif`、`/动画`、`/ygif`。

## 参数

```
-m  模型         gpt-image-2.6 / gpt-image-2.5 / gpt-image-2 ...
-s  画幅         1024x1024 方 / 1536x1024 横 / 1024x1536 竖 / auto
-q  质量         low / medium / high / auto
-n  张数         受 max_n 限制
-st 风格         realistic anime watercolor oil pixel sketch 3d cyberpunk ink flat cinematic cute
-neg 排除        不希望出现的元素
-f              开启参考图模式（/绘 用，等价于 /改绘）
-out save       只存不发，回复本地路径
-url            额外回复图片直链
-pre 前缀        文件名前缀
```

动图专用：

```
-fr 帧数         2-16，默认 8
-ms 每帧毫秒     20-2000，默认 120
-once           只播一次，不加是无限循环
```

参数可以用 `-s 竖`、`--size=1536x1024`、`-s 横版` 这几种写法，中文别名也认。

## 输入口

1. 提示词（正文）
2. 当前消息附带的图片
3. 引用消息里的图片（`/改绘` 或 `/绘 -f`）
4. 多张参考图一起当输入做融合
5. 风格预设追加词
6. 负向提示（拼成「画面中避免出现：…」）
7. 画幅、质量、张数、模型

## 输出口

1. 直接发到当前会话（默认）
2. 本地归档：`data/plugin_data/astrbot_plugin_yishi_draw/images/年-月/`
3. `-out save` 只回路径
4. `-url` 回上游直链
5. 历史记录：`data/plugin_data/astrbot_plugin_yishi_draw/history.json`，可按序号重发

## LLM 工具

工具名 `yishi_draw`，参数：

- `prompt`：画面描述
- `style`：风格键
- `size`：画幅
- `quality`：质量
- `n`：张数
- `use_reference`：是否拿消息里的图当参考
- `negative`：排除项

工具自己发图，返回值只带一句结果说明，避免我重复描述。

动图工具 `yishi_draw_gif`，参数：

- `prompt`：画面 + 动作描述
- `style`、`quality`：同生图
- `frames`：帧数 2-16
- `frame_ms`：每帧毫秒 20-2000
- `use_reference`：拿消息里的图动起来
- `loop`：是否无限循环
- `negative`：排除项

## 配置要点

- `api_base` / `api_key`：接口地址与密钥，base 带不带 `/v1` 都行
- `default_model`、`default_size`、`default_quality`：默认档位
- `max_n`：单次张数上限（默认 4，最大 10）
- `request_timeout`：默认 300 秒，high 质量大图会比较慢
- `access_mode`：`everyone` / `admin` / `master`，默认 admin
- `allow_users`：白名单，同博士一样不受限
- `cooldown_seconds`：普通用户冷却，博士与管理员不受限
- `enable_llm_tool`：是否允许我自主调用
- `llm_tool_respect_access`：我调用时是否也走权限校验
- `enable_gif`、`gif_frames`、`gif_frame_ms`、`gif_quality`、`gif_max_frames`：动图开关、默认帧数、帧间隔、质量、上限
- `keep_days`：本地图保留天数，0 不清理

## 已知行为

- 该接口只回图片 URL，不回 base64，所以插件会下载到本地再发送，链接失效也不影响已保存的图。
- `/v1/images/edits` 走 multipart，字段名 `image[]`，多图已验证可用。
- 同一提示词下 high 质量明显更慢（实测百秒量级），聊天里默认 medium。
- 风格预设是往提示词前面追加描述词，不改变模型本身。
- GIF 只有 `gpt-image-2.6` 系列支持，参数是 `output_format: "gif"`，配合 `frame_count` 与 `frame_duration_ms`。
- 帧参数只在 edits 端点生效；generations 端点会忽略它们，固定回 9 帧 200ms。插件收到图后按目标帧数和帧间隔本地重编码，保证 `/动图 -fr 6 -ms 80` 是准的。
- GIF 输出尺寸由上游压到 512 以内（实测 435x389、516x512），请求 1024 或 1536 也不会变大；耗时 40-120 秒，比静态图慢不少。
