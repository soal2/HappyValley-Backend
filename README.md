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
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8083
```

打开 `http://127.0.0.1:8000/docs` 查看 OpenAPI，健康检查为 `GET /health`。默认 `MODE=demo`，所有 AI 结果均为明确标记的本地确定性演示结果，不会调用外部模型。不要在生产中使用示例密码或 `SECRET_KEY`。

真实 HTTP 模式设置 `MODE=production`，并分别提供：

```dotenv
AI_API_KEY=...
AI_MODEL=...
AI_BASE_URL=https://your-provider.example/v1
IMAGE_API_KEY=...
IMAGE_MODEL=...
IMAGE_BASE_URL=https://your-image-provider.example/v1
```

适配器使用 OpenAI-compatible 的 HTTP envelope，但供应商、地址、密钥、模型和超时均来自环境变量，没有代码默认值。

## REST 使用顺序

所有私有接口使用 `Authorization: Bearer <JWT>`。

| 方法与路径 | 用途 |
|---|---|
| `POST /api/auth/register` | 用户名/密码注册；用户名是不可修改的公开昵称 |
| `POST /api/auth/login` | 登录获取 JWT |
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
{"command_id":"client-unique-id","type":"submit_base","payload":{"text":"山谷里的一棵树"}}
```

阶段命令顺序为 `submit_base` → `submit_sharing` → `answer_follow_up` 或 `skip_follow_up`。另有 `crisis_response`（`payload.can_continue`）、`heartbeat` 和 `exit`。重复命令 ID 返回 `already_processed`，不会重复生成图片或明信片。

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
