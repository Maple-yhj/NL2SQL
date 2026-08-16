# Data Agent：可治理的数据分析代理

Data Agent（仓库名为 NL2SQL）是一个面向用户自有数据的数据分析代理。它不只把自然语言翻译成 SQL，还会围绕数据目录、语义定义、表关系、查询权限、执行预算、证据和版本信息组织完整的分析流程。

项目由 FastAPI 后端、LangGraph 分析运行时和 React 前端组成，当前支持 CSV、XLSX、SQLite 与 PostgreSQL 数据源。

> 当前版本为 `0.1.0`，仍处于积极开发阶段。接口、状态存储结构和语义绑定模型可能继续演进，暂不建议未经评估直接用于关键生产业务。

## 核心能力

- **多种数据源**：上传 CSV、XLSX、SQLite 文件，或通过接口注册 PostgreSQL 数据库。
- **不可变数据快照**：为数据目录生成版本号和结构指纹；CSV 与 XLSX 会导入本地 DuckDB 快照，SQLite 文件会生成受控副本。
- **语义建模**：定义逻辑字段、指标、主表和跨表关系，并以版本化绑定控制实际分析范围。
- **关系图工作流**：生成关系图草稿，支持模型推荐、人工编辑、校验、路径预览和激活。
- **多阶段分析代理**：按照计划、预览、执行三种模式工作，可进行规划、查询编译、受控执行、结果评估与答案合成。
- **证据化结果**：响应中可包含逻辑计划、数据集查询计划、SQL、结果行、图表、分析步骤、制品、证据和版本锁定信息。
- **可恢复运行**：通过服务端事件流返回运行进度，支持等待人工澄清或审批、恢复、取消和事件回放。
- **租户隔离与鉴权**：使用 PostgreSQL 保存用户和刷新令牌，使用 JWT 保护业务接口。
- **模型提供方适配**：支持 OpenAI、通义千问、Google Gemini、Anthropic Claude、智谱 GLM、DeepSeek 以及兼容 OpenAI 协议的服务。

## 工作流程

```mermaid
flowchart LR
    A["登录并创建会话"] --> B["上传或注册数据源"]
    B --> C["生成目录与版本化快照"]
    C --> D["编辑关系图和语义定义"]
    D --> E["激活语义绑定"]
    E --> F["提交自然语言问题"]
    F --> G["规划并调用受控数据工具"]
    G --> H["校验证据并生成答案"]
    H --> I["返回结果、图表、证据和版本信息"]
```

分析请求会锁定数据源版本、语义绑定版本和结构指纹。查询只能访问绑定允许的关系与字段，并受到调用次数、执行时间和结果行数等预算限制。

## 技术组成

| 层次 | 当前实现 |
| --- | --- |
| 网页端 | React 19、TypeScript、Vite 7 |
| 接口层 | FastAPI、Pydantic、JWT、服务端事件流 |
| 代理编排 | LangGraph、LangChain |
| 查询规划 | 类型化数据集查询计划、SQLGlot |
| 数据连接 | DuckDB、SQLite、PostgreSQL |
| 本地状态 | SQLite 控制库、文件制品和数据快照 |
| 鉴权状态 | PostgreSQL |
| 测试 | Pytest、Vitest |

## 运行要求

使用 Docker 部署时只需要：

- Docker Engine 或 Docker Desktop
- 支持 `docker compose` 命令的 Docker Compose v2
- 至少一个受支持模型提供方的接口凭据

本地开发需要：

- Python `3.12` 或更高版本
- Node.js `20.19.x`，或 `22.12.0` 及更高版本
- npm
- PostgreSQL，用于用户鉴权和刷新令牌
- 至少一个受支持模型提供方的接口凭据

## Docker 一键部署

Compose 方案会启动以下服务：

- PostgreSQL：保存用户、密码哈希和刷新令牌，仅在容器网络内可访问。
- FastAPI 后端：自动初始化鉴权表、创建首个管理员，并将运行状态保存到命名卷。
- Nginx 网页端：提供前端静态文件，并反向代理 API、接口文档和服务端事件流。

### 1. 创建部署配置

```bash
cp .env.docker.example .env.docker
```

生成数据库密码和 JWT 密钥：

```bash
openssl rand -hex 32
openssl rand -hex 48
```

编辑 `.env.docker`，至少填写以下变量：

```dotenv
POSTGRES_PASSWORD=<数据库密码>
BOOTSTRAP_ADMIN_PASSWORD=<初始管理员密码>
JWT_SECRET_KEY=<JWT 随机密钥>

LLM_PROVIDER=openai
DEFAULT_MODEL_NAME=<模型名称>
LLM_API_KEY=<模型接口密钥>
```

`LLM_API_KEY` 是容器部署使用的通用凭据变量，适用于项目支持的全部模型提供方。使用兼容 OpenAI 协议的自定义服务时，还必须设置 `LLM_BASE_URL`。

### 2. 一键启动

Linux 或 macOS 可以执行：

```bash
./scripts/docker-deploy.sh
```

也可以在任意支持 Docker Compose 的系统中直接执行：

```bash
docker compose --env-file .env.docker \
  up --build --detach --wait --wait-timeout 300
```

启动完成后可访问：

- 网页端：<http://127.0.0.1:8080>
- 接口文档：<http://127.0.0.1:8080/docs>
- 后端直连地址：<http://127.0.0.1:8000>

使用 `.env.docker` 中的 `BOOTSTRAP_ADMIN_TENANT_ID`、`BOOTSTRAP_ADMIN_USERNAME` 和 `BOOTSTRAP_ADMIN_PASSWORD` 登录。初始管理员只会在账号不存在时创建；容器重启不会覆盖其密码和角色。

### 3. 常用运维命令

```bash
# 查看服务状态
docker compose --env-file .env.docker ps

# 跟踪日志
docker compose --env-file .env.docker logs --follow

# 拉取代码更新后重新构建
docker compose --env-file .env.docker \
  up --build --detach --wait --wait-timeout 300

# 停止并删除容器，保留数据卷
docker compose --env-file .env.docker down
```

PostgreSQL 数据和应用状态分别保存在两个 Docker 命名卷中。不要在需要保留数据时执行 `docker compose down --volumes`；该命令会永久删除鉴权数据、会话、数据快照、运行事件和分析制品。

默认网页端监听所有网络接口的 `8080` 端口，后端 `8000` 端口仅绑定到本机。可以通过 `.env.docker` 中的 `WEB_BIND_ADDRESS`、`WEB_PORT`、`API_BIND_ADDRESS` 和 `API_PORT` 调整。公网部署还应在外层配置 HTTPS、访问控制、密钥管理、备份和监控。

如需向容器传递 PostgreSQL 分析数据源凭据，直接在 `.env.docker` 中添加对应的 `DATA_SOURCE_SECRET_*` 变量即可。

## 本地开发

### 1. 获取代码

```bash
git clone https://github.com/Maple-yhj/NL2SQL.git
cd NL2SQL
```

### 2. 安装后端依赖

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### 3. 安装前端依赖

```bash
cd frontend
npm ci
cd ..
```

### 4. 初始化鉴权数据库

下面以本地数据库 `data_agent_auth` 为例；请按照自己的 PostgreSQL 用户、密码、主机和端口修改连接地址。

```bash
createdb data_agent_auth
export AUTH_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/data_agent_auth'
psql "$AUTH_DATABASE_URL" -f db/auth.sql
```

### 5. 配置环境变量

项目使用仓库根目录下的 `.env`。先生成一个足够长的 JWT 密钥：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

然后创建 `.env`，以下示例使用 OpenAI；所有占位值都需要替换：

```dotenv
LLM_PROVIDER=openai
DEFAULT_MODEL_NAME=<模型名称>
OPENAI_API_KEY=<模型接口密钥>

AUTH_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/data_agent_auth
JWT_SECRET_KEY=<上一步生成的随机密钥>

DATA_AGENT_STATE_DIR=var/data-agent
```

### 6. 创建登录用户

```bash
python scripts/create_auth_user.py \
  --tenant-id demo \
  --user-id demo-user \
  --username demo \
  --password '<请替换为登录密码>' \
  --roles user
```

### 7. 启动后端

```bash
uvicorn api.app:app \
  --host 127.0.0.1 \
  --port 8000 \
  --reload \
  --env-file .env
```

健康检查地址为 <http://127.0.0.1:8000/health>，交互式接口文档位于 <http://127.0.0.1:8000/docs>。

### 8. 启动前端

另开一个终端：

```bash
cd frontend
npm run dev
```

访问 <http://127.0.0.1:5173>，使用前面创建的租户、用户名和密码登录。

### 9. 完成首次分析

1. 创建一个会话。
2. 打开数据源面板，上传 CSV、XLSX 或 SQLite 文件。
3. 检查自动生成的数据目录和关系图草稿。
4. 配置逻辑字段、指标和必要的表关系，然后激活语义绑定。
5. 返回会话，选择计划、预览或执行模式并提交问题。

未激活语义绑定时，运行时会拒绝执行分析。

## 配置说明

### 基础配置

| 变量 | 是否必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | 建议显式设置 | 无 | 模型提供方：`openai`、`qwen`、`google`、`anthropic`、`glm`、`deepseek` 或 `openai-compatible`。只有一个专用凭据时可以自动推断，但显式设置更清晰。 |
| `DEFAULT_MODEL_NAME` | 是 | 无 | 明确的模型名称或版本。 |
| `AUTH_DATABASE_URL` | 是 | 回退到 `DATABASE_URL` | 鉴权 PostgreSQL 连接地址。 |
| `JWT_SECRET_KEY` | 是 | 无 | JWT 签名密钥。 |
| `DATA_AGENT_STATE_DIR` | 否 | `var/data-agent` | 本地控制库、数据快照、运行事件、检查点和制品的根目录。 |

### 模型凭据

| 提供方 | 可用密钥变量 | 可用地址变量 |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` 或 `LLM_API_KEY` | `OPENAI_BASE_URL` 或 `LLM_BASE_URL` |
| 通义千问 | `DASHSCOPE_API_KEY`、`QWEN_API_KEY` 或 `LLM_API_KEY` | `QWEN_BASE_URL`、`DASHSCOPE_BASE_URL` 或 `LLM_BASE_URL` |
| Google Gemini | `GEMINI_API_KEY`、`GOOGLE_API_KEY` 或 `LLM_API_KEY` | `GEMINI_BASE_URL` 或 `LLM_BASE_URL` |
| Anthropic Claude | `ANTHROPIC_API_KEY` 或 `LLM_API_KEY` | `ANTHROPIC_BASE_URL` 或 `LLM_BASE_URL` |
| 智谱 GLM | `ZAI_API_KEY`、`GLM_API_KEY`、`ZHIPUAI_API_KEY` 或 `LLM_API_KEY` | `GLM_BASE_URL`、`ZAI_BASE_URL` 或 `LLM_BASE_URL` |
| DeepSeek | `DEEPSEEK_API_KEY` 或 `LLM_API_KEY` | `DEEPSEEK_BASE_URL` 或 `LLM_BASE_URL` |
| 兼容 OpenAI 协议的服务 | `LLM_API_KEY` | `LLM_BASE_URL`，必须设置 |

### JWT 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `JWT_ALGORITHM` | `HS256` | JWT 签名算法。 |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | 访问令牌有效分钟数。 |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | 刷新令牌有效天数。 |
| `JWT_ISSUER` | `nl2sql-api` | 令牌签发方。 |
| `JWT_AUDIENCE` | `nl2sql-client` | 令牌接收方。 |

### PostgreSQL 数据源凭据

注册 PostgreSQL 数据源时，`credential_ref` 必须采用 `secret://` 格式。运行时会把引用转换为环境变量名。例如：

```text
secret://warehouse/main  ->  DATA_SOURCE_SECRET_WAREHOUSE_MAIN
```

对应变量的值应为实际的数据源连接地址：

```dotenv
DATA_SOURCE_SECRET_WAREHOUSE_MAIN=postgresql://只读用户:密码@数据库主机:5432/业务数据库
```

建议为分析数据源使用只读数据库账号，并只授予必要的模式和数据表权限。网页端目前只提供文件上传入口，PostgreSQL 数据源需要通过 `POST /api/data-sources/postgres` 注册。

## 接口概览

除健康检查、登录、刷新和退出接口外，业务接口都需要 `Authorization: Bearer <访问令牌>`。

| 范围 | 主要路径 | 用途 |
| --- | --- | --- |
| 健康检查 | `GET /health` | 检查服务是否可用。 |
| 鉴权 | `/api/auth/*` | 登录、刷新令牌、读取当前用户和退出。 |
| 数据源 | `/api/data-sources/*` | 上传、注册、列出、删除数据源并读取目录。 |
| 关系与语义 | `/api/data-sources/{source_id}/relationship-*`、`/bindings` | 编辑关系图、校验路径和管理语义绑定。 |
| 分析 | `POST /api/nl2sql`、`POST /api/nl2sql/stream` | 发起同步或流式分析。 |
| 运行控制 | `/api/runs/*` | 恢复、取消运行和读取事件。 |
| 会话 | `/api/conversations/*` | 创建、查询、更新会话并发送消息。 |
| 记忆提案 | `/api/memory/proposals/*` | 查询和审批记忆提案。 |

完整请求与响应结构以运行中的 `/docs` 和 `/openapi.json` 为准。仓库中的 `docs/apifox-openapi.json` 是导出产物，修改接口后应重新生成并校验。

### 登录示例

```bash
curl --request POST 'http://127.0.0.1:8000/api/auth/login' \
  --header 'Content-Type: application/json' \
  --data '{
    "tenant_id": "demo",
    "username": "demo",
    "password": "<登录密码>"
  }'
```

## 命令行与 LangGraph 调试

安装项目后会提供 `data-agent` 命令：

```bash
data-agent ask '按月份汇总销售额' \
  --source-id <数据源编号> \
  --source-version <数据源版本> \
  --binding-id <绑定编号> \
  --binding-version <绑定版本> \
  --mode execute
```

命令行不会替你创建数据源或语义绑定，必须复用已经存在于 `DATA_AGENT_STATE_DIR` 中的有效版本信息。

仓库根目录的 `langgraph.json` 暴露了一个离线计划模式示例图。安装开发依赖后可执行：

```bash
langgraph dev
```

该示例只用于观察代理拓扑，不会连接真实数据源、调用模型或执行查询。真实分析请使用网页端、接口或命令行入口。

## 数据与安全边界

- 所有数据源、会话和运行状态都按租户及用户范围解析。
- 数据源查询通过已激活的语义绑定和允许关系集合进行约束。
- 数据分析使用有限预算；当前默认最多返回 `1000` 行，单次代理运行最长 `180` 秒。
- 单个上传文件的默认上限为 `64 MiB`。
- DuckDB 文件导入明确禁用社区扩展的自动加载和自动安装。
- PostgreSQL 数据源密钥不写入数据源注册表，只保存 `secret://` 引用。
- `DATA_AGENT_STATE_DIR` 包含会话、快照、检查点、事件和分析制品，不应提交到版本控制，也不应由不受信任的用户读取。
- `.env`、数据库连接地址、令牌和模型密钥都不应提交到仓库。

## 当前实现边界

- 项目不附带示例业务数据源；完成安装后仍需上传或注册自己的数据。
- 默认组合使用 SQLite 保存会话与运行控制状态，PostgreSQL 仅用于鉴权。
- 默认记忆管理器为无持久化实现，因此记忆提案接口不会形成跨进程的长期记忆。`db/data_agent_memory.sql` 和 PostgreSQL 记忆实现已存在，但尚未接入默认应用组合。
- 网页端尚未提供 PostgreSQL 数据源注册表单。
- Docker Compose 方案面向单机部署；生产环境仍需按照实际基础设施补充 HTTPS、集中式密钥管理、数据库备份和可观测性方案。

## 项目结构

```text
NL2SQL/
├── src/api/                    # FastAPI 应用、鉴权、路由和数据源服务
├── src/data_agent/
│   ├── analysis_agent/         # LangGraph 分析代理、规划、评估和合成
│   ├── dataset_query/          # 类型化查询计划、编译与执行
│   ├── datasources/            # 数据源注册表与文件快照
│   ├── memory/                 # 记忆契约及 PostgreSQL 实现
│   ├── relationships/          # 关系发现、校验、路由和编译
│   ├── runtime/                # 公共运行时契约、事件和组合入口
│   └── tools/                  # 受控工具、连接器与数据集工具提供方
├── frontend/                   # React 网页端
├── docker/                     # 前后端镜像、Nginx 和容器初始化入口
├── tests/                      # 单元、契约、接口和集成测试
├── db/                         # PostgreSQL 初始化脚本
├── scripts/                    # 用户创建、契约导出、迁移和仓库审计脚本
├── docs/                       # 架构记录、实现计划和测试报告
├── compose.yaml                # 一键部署编排
├── pyproject.toml              # Python 包与依赖配置
└── langgraph.json              # LangGraph 开发入口
```

## 测试与质量检查

运行后端测试：

```bash
pytest
```

运行前端测试和构建检查：

```bash
cd frontend
npm test
npm run build
```

运行仓库可达性审计：

```bash
python scripts/audit_repository_reachability.py
```

检查 Docker 启动器、Compose 配置和镜像构建：

```bash
pytest tests/test_docker_entrypoint.py
sh -n scripts/docker-deploy.sh
docker compose --env-file .env.docker config --quiet
docker compose --env-file .env.docker build
```

构建本地 Python 分发包：

```bash
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

部分 `tests/integration/` 用例需要本地数据库或相应外部服务；提交变更时至少应运行与修改范围相关的测试。

## 参与贡献

欢迎通过议题和拉取请求参与改进。建议遵循以下流程：

1. 先创建议题，说明问题、使用场景或设计目标。
2. 从自己的分支完成范围明确的修改。
3. 为行为变化补充或更新测试。
4. 运行后端测试、前端测试和构建检查。
5. 提交拉取请求，说明变更内容、验证方式、兼容性影响和仍存在的限制。

提交代码时请保持公共契约严格、错误信息安全、租户边界明确，并避免在日志、测试夹具或文档中写入真实凭据和业务数据。

## 问题反馈与安全报告

普通缺陷和功能建议可以通过 GitHub 议题提交。若发现可能泄露数据、绕过鉴权或扩大查询权限的安全问题，请不要在公开议题中披露利用细节；请先通过仓库维护者的 GitHub 联系方式进行私下沟通。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
