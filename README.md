# HappyValley Backend（心晴谷 P0）

FastAPI + PostgreSQL 的模块化单体，支持 P0 固定的 `1 位真人 + 4 位 AI 团友 + 1 位 AI 带领者` 绘画文字接龙闭环。它是游戏化心理互助服务，不是医疗诊断或治疗系统。

## 快速启动

要求 Python 3.11+、PostgreSQL 16+。本地可用 Docker：

```bash
docker compose up -d postgres
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.seed_db
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 `http://127.0.0.1:8083/docs` 查看 OpenAPI，健康检查为 `GET /health`。默认 `MODE=production`，活动必须配置真实文本与图片模型；Fake runtime 只允许测试进程显式使用 `MODE=test`。TTS 支持 `elevenlabs` 与 `edge` provider，活动消息会在服务端预生成规范语音片段和实际/估算时长。不要在生产中使用示例密码或 `SECRET_KEY`。

真实 HTTP 模式设置 `MODE=production`，并分别提供：

```dotenv
AI_API_KEY=...
AI_MODEL=...
AI_BASE_URL=https://your-provider.example/v1
AI_NETWORK_MODE=auto
AI_PROXY_URL=http://127.0.0.1:7890
IMAGE_API_KEY=...
IMAGE_MODEL=...
IMAGE_BASE_URL=https://your-image-provider.example/v1
```

适配器使用 OpenAI-compatible 的 HTTP envelope，但供应商、地址、密钥、模型和超时均来自环境变量，没有代码默认值。
`AI_NETWORK_MODE=auto` 会优先使用 `AI_PROXY_URL`；未显式配置时读取系统 HTTPS
代理。代理请求失败后，同一次生成会以 `trust_env=False` 直连重试。也可使用
`direct` 强制直连，或使用 `proxy` 强制代理（此时必须配置 `AI_PROXY_URL`）。
如果代理软件使用 Fake-IP DNS，关闭代理时仍需确保系统 DNS 已恢复真实地址。

### ElevenLabs 人物语音

`POST /api/tts/synthesize` 使用服务端角色白名单调用 ElevenLabs Text to Speech API，返回 `audio/mpeg`。生产环境必须携带 JWT；API Key、模型、Voice ID 和角色声音参数不会下发到前端。

```dotenv
TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=在部署平台的后端 Secret 中配置
ELEVENLABS_TTS_MODEL=eleven_multilingual_v2
ELEVENLABS_TTS_BASE_URL=https://api.elevenlabs.io/v1
ELEVENLABS_TTS_TIMEOUT_SECONDS=20
ELEVENLABS_TTS_OUTPUT_FORMAT=mp3_44100_128
ELEVENLABS_VOICE_FACILITATOR_ID=
ELEVENLABS_VOICE_YOU_ID=
ELEVENLABS_VOICE_ZHU_ID=
ELEVENLABS_VOICE_XIAOLI_ID=
ELEVENLABS_VOICE_ZHANG_ID=
ELEVENLABS_VOICE_LI_ID=
TTS_ALLOW_ANONYMOUS_DEMO=false
```

Voice ID 可在 ElevenLabs 的 My Voices 中通过“More actions → Copy voice ID”获取。建议选择中文母语或中文口音自然的声音，并对六个角色分别试听。本地免登录试听必须显式设置 `TTS_ALLOW_ANONYMOUS_DEMO=true`；`MODE=production` 会拒绝该配置。不要把 ElevenLabs Key 提交到仓库、发到聊天中或放入任何 `VITE_*` 变量。

也可配置 `TTS_PROVIDER=edge` 使用现有 Edge TTS 角色音色。两种 provider 的 `/api/tts/synthesize` 都返回 `X-TTS-Duration-Ms`（能够从 MP3 读取时），供活动语音时长校准；TTS 失败只降级为文字阅读计时，不改变业务阶段。

## REST 使用顺序

所有私有接口使用 `Authorization: Bearer <JWT>`。

| 方法与路径 | 用途 |
|---|---|
| `POST /api/auth/register` | 用户名/密码注册；用户名是不可修改的公开昵称 |
| `POST /api/auth/login` | 登录获取 JWT |
| `POST /api/tts/synthesize` | 将人物气泡文本合成为 MP3；角色声音由后端控制 |
| `POST /api/profile/complete` | 保存 MBTI、性别、出生年、预设外观 |
| `POST /api/activities` | 提交 `{"mood":"..."}`；返回房间或 FIFO 位置 |
| `GET /api/queue/me` | 轮询位置/60 秒预留 |
| `POST /api/queue/confirm` | 确认预留并建房 |
| `GET /api/rooms/{id}` | 断线恢复的可信快照 |
| `POST /api/rooms/{id}/commands` | 幂等活动命令 |
| `GET /api/postcards` | 本人明信片列表 |
| `POST/DELETE /api/postcards/{id}/share` | 创建/撤销公开分享 |
| `GET /api/public/postcards/{token}` | 匿名公开视图，响应含 `X-Robots-Tag: noindex, nofollow` |

命令统一格式：

```json
{"command_id":"client-unique-id","type":"submit_painting_turn","payload":{"turn_index":2,"text":"山谷里的一棵树"}}
```

V2 主流程命令为 `submit_painting_turn` → `complete_artwork_reveal` → 每轮 `submit_reflection` 或 `skip_reflection`。另有 `retry_current_stage`、`retry_image`、`crisis_response`（`payload.can_continue`）、`heartbeat` 和 `exit`。`submit_base`、`submit_sharing` 与旧追问命令只保留兼容解析并对 V2 明确拒绝；重复命令 ID 返回 `already_processed`，不会重复生成消息、图片或明信片。

房间创建时生成包含一位真人和四位 AI 的 `speaking_order`，绘画接龙与四轮回顾始终复用该顺序。完整无重试活动执行 27 次独立文本生成与 1 次图片生成；画作必须经 `complete_artwork_reveal` 确认后才开始第一轮回顾。完成态保留 V2 消息和游标供刷新恢复，同时清除图片 Prompt 历史；中止态仍删除临时消息和媒体。

## 原生 WebSocket

连接：`ws://127.0.0.1:8000/api/rooms/{room_id}/ws?token={JWT}`。JWT 和房间所有权在接受连接前校验；同一房间的新连接用关闭码 4001 接管旧连接。

服务端事件：

- `snapshot`：连接/重连后的完整可信快照；
- `progress`：命令已进入应用内异步处理；
- `command_result`：包含命令状态和新快照；
- `error`：无效或当前阶段不接受的命令。

客户端发送与 REST 相同的命令 JSON。普通断线保留 5 分钟；超时后台终止并清理，不生成正式明信片。

## 种子、迁移和验证

`config/characters.yaml`、`config/painting_activity.yaml`、`config/crisis_keywords.yaml` 都带版本。`python -m scripts.seed_db` 会幂等同步人物；活动与危机配置在运行时只读加载。危机援助号码明确标记为待产品负责人核验，上线前必须替换或确认。

```bash
.venv/bin/python -m black --check app tests scripts alembic
.venv/bin/python -m ruff check app tests scripts alembic
.venv/bin/python -m mypy app
.venv/bin/alembic upgrade head
.venv/bin/alembic check
.venv/bin/python -m pytest -q
HAPPYVALLEY_BASE_URL=http://127.0.0.1:8000 .venv/bin/python -m scripts.demo_p0
git diff --check
```

实现取舍和精确清理范围见 [docs/IMPLEMENTATION_DECISIONS.md](docs/IMPLEMENTATION_DECISIONS.md)。

## P0 真实限制

- 只支持单应用实例、最多 6 个活跃房间；没有 Redis、Celery 或跨实例事件代理。
- 本地媒体依赖单机磁盘，不适合无持久磁盘的平台。
- 只做配置化危机关键词检测，不是完整内容治理或临床安全系统；候选热线尚未由产品负责人核验。
- 固定单真人活动，不包含真人匹配池、多人轮次、AI 接管、冥想、共享画板或跨活动记忆。
- HTTP 真实适配器已用模拟响应验证协议/重试，但自动化测试不会发起付费模型调用。
