# NL2SQL Data Assistant

一个面向企业 BI 和运营分析场景的自然语言问数项目。用户登录后可以在前端聊天界面中输入业务问题，系统自动生成 SQL、查询数据，并返回自然语言解释或明细表格。

## 1. 环境要求

推荐环境：

- Windows + PowerShell
- Conda 环境：`agents-env`
- Python 3.12+
- Node.js 20+
- PostgreSQL

安装 Python 依赖：

```powershell
conda activate agents-env
pip install -e ".[dev]"
```

安装前端依赖：

```powershell
cd frontend
npm install
cd ..
```

## 2. 环境变量配置

复制示例配置：

```powershell
Copy-Item .env.example .env
```

关键配置如下：

```env
# LLM
LLM_PROVIDER=deepseek
DEFAULT_MODEL_NAME=deepseek-v4-flash
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com

# 如果使用 Google Gemini，可改为：
# LLM_PROVIDER=google
# DEFAULT_MODEL_NAME=gemini-2.5-flash
# GEMINI_API_KEY=

# Embedding
EMBEDDING_MODEL=models/gemini-embedding-001
EMBEDDING_DIM=768

# 业务查询数据库：用于 schema 检索、SQL 校验和 SQL 执行
DATABASE_URL=postgresql://user:password@localhost:5432/nl2sql_data

# 会话记忆数据库：用于会话列表、历史问答、用户记忆
MEMORY_DATABASE_URL=postgresql://user:password@localhost:5432/nl2sql_memory

# 认证数据库：为空时回退到 MEMORY_DATABASE_URL
AUTH_DATABASE_URL=

# JWT
JWT_SECRET_KEY=replace-with-a-long-random-secret
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
JWT_ISSUER=nl2sql-api
JWT_AUDIENCE=nl2sql-client
```

说明：

- `DATABASE_URL` 是被查询的业务数据库。
- `MEMORY_DATABASE_URL` 保存聊天会话和历史问答。
- `AUTH_DATABASE_URL` 保存登录用户和 refresh token；如果为空，会使用 `MEMORY_DATABASE_URL`。
- `JWT_SECRET_KEY` 必须配置，否则登录相关接口无法正常工作。

## 3. 数据库初始化

初始化会话记忆表：

```powershell
psql "$env:MEMORY_DATABASE_URL" -f db/conversation_memory.sql
```

初始化认证表。如果单独配置了 `AUTH_DATABASE_URL`，使用认证库；否则使用记忆库：

```powershell
$authDsn = if ($env:AUTH_DATABASE_URL) { $env:AUTH_DATABASE_URL } else { $env:MEMORY_DATABASE_URL }
psql $authDsn -f db/auth.sql
```

创建或更新登录账号：

```powershell
python scripts/create_auth_user.py `
  --tenant-id demo `
  --user-id user-1 `
  --username yehj `
  --password "0708" `
  --roles user
```

## 4. 启动后端

在项目根目录执行：

```powershell
conda activate agents-env
uvicorn api.app:app --host 127.0.0.1 --port 8000
```

开发时需要自动重载可使用：

```powershell
uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
```

健康检查：

```powershell
curl http://127.0.0.1:8000/health
```

后端 API 文档：

```text
http://127.0.0.1:8000/docs
```

## 5. 启动前端

前端固定使用 `5173` 端口。启动脚本会先释放被占用的 `5173`，然后以 strict port 模式启动 Vite。

```powershell
cd frontend
npm run dev
```

前端地址：

```text
http://127.0.0.1:5173/
```

Vite 已配置代理：

- `/api` -> `http://127.0.0.1:8000`
- `/health` -> `http://127.0.0.1:8000`

因此前端和后端同时启动后，浏览器访问 `5173` 即可使用完整项目。

## 6. 登录使用

如果按上面的示例创建账号，可使用：

```text
tenant_id: demo
username: yehj
password: 0708
```

登录后可以：

- 新建聊天
- 提问业务指标
- 查询订单等明细数据
- 打开历史会话
- 重命名或删除会话
- 查看表格分页结果

## 7. 常用 API 调试

登录：

```powershell
curl -X POST http://127.0.0.1:8000/api/auth/login `
  -H "Content-Type: application/json" `
  -d "{\"tenant_id\":\"demo\",\"username\":\"yehj\",\"password\":\"0708\"}"
```

读取当前用户：

```powershell
curl http://127.0.0.1:8000/api/auth/me `
  -H "Authorization: Bearer <access_token>"
```

创建会话：

```powershell
curl -X POST http://127.0.0.1:8000/api/conversations `
  -H "Authorization: Bearer <access_token>" `
  -H "Content-Type: application/json" `
  -d "{\"title\":\"GMV 分析\"}"
```

发送会话消息：

```powershell
curl -X POST http://127.0.0.1:8000/api/conversations/<conversation_id>/messages `
  -H "Authorization: Bearer <access_token>" `
  -H "Content-Type: application/json" `
  -d "{\"question\":\"华东地区最新的20条订单记录\",\"execute\":true}"
```

## 8. 测试与构建

后端测试：

```powershell
conda run -n agents-env python -m unittest discover -s tests -v
```

前端测试：

```powershell
cd frontend
npm test
```

前端生产构建：

```powershell
cd frontend
npm run build
```

构建产物会生成在：

```text
frontend/dist
```

## 9. Apifox / OpenAPI

当前 OpenAPI 文件：

```text
docs/apifox-openapi.json
```

路由或 schema 变更后可重新导出：

```powershell
conda run -n agents-env python scripts/export_apifox_openapi.py
```

## 10. 目录说明

```text
api/        FastAPI 应用、认证、路由和请求响应模型
core/       环境加载、LLM 和 Embedding 适配
graph/      NL2SQL agent、会话记忆、消息分类和解释逻辑
rag/        指标和 schema 检索
db/         PostgreSQL 初始化脚本
frontend/   React + Vite 前端
scripts/    初始化、导出和辅助脚本
tests/      后端测试
```

更多业务功能说明见 `summary.md`。
