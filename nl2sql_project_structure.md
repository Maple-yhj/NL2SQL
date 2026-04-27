# NL2SQL Agent — 项目目录结构

```
nl2sql-agent/
│
├── README.md                          # 项目说明、架构图、快速启动（P4）
├── pyproject.toml                     # 依赖管理（uv / poetry）
├── .env.example                       # 环境变量模板（不提交真实密钥）
├── .github/
│   └── workflows/
│       └── regression.yml             # GitHub Actions 回归测试（P4）
│
│
├── db/                                # 数据库初始化
│   ├── migrations/
│   │   ├── 001_create_business_tables.sql   # orders/products/users/refunds（P1）
│   │   ├── 002_create_metrics_registry.sql  # metrics registry 表（P1）
│   │   └── 003_add_vector_columns.sql       # pgvector embedding 列（P2）
│   └── seeds/
│       ├── seed_business_data.py      # 生成 5 万条模拟业务数据（P1）
│       └── seed_metrics_registry.py   # 录入 gmv/paid_orders 等指标（P1）
│
│
├── catalog/                           # Schema Catalog 模块（P1 核心）
│   ├── extractor.py                   # 抽取脚本：读 information_schema → JSON
│   ├── loader.py                      # load_schema_snippet()：按需裁剪为 prompt 片段
│   └── schema_catalog.json            # 生成产物（可 gitignore 或提交作快照）
│
│
├── core/                              # 各阶段共用的基础模块
│   ├── claude_client.py               # Claude 流式调用封装、token 统计（P1）
│   ├── db_pool.py                     # asyncpg 连接池单例
│   └── logger.py                      # 结构化日志（JSON Lines 格式）
│
│
├── engine/                            # P1：NL2SQL 基础引擎
│   ├── intent_parser.py               # 意图解析器（结构化输出 → JSON）
│   ├── sql_generator.py               # SQL 生成器（意图 + schema 片段 → SQL）
│   └── executor.py                    # 只读执行器（asyncpg，statement_timeout）
│
│
├── agent/                             # P2：ReAct Tool Use Agent
│   ├── tools/
│   │   ├── search_metrics.py          # 语义检索指标定义（pgvector）
│   │   ├── search_schema.py           # 语义检索表/字段（混合检索）
│   │   ├── get_table_sample.py        # 返回表样本数据
│   │   ├── validate_sql.py            # SQL 安全校验（sqlglot AST）
│   │   ├── execute_sql.py             # 只读事务执行 + EXPLAIN 记录
│   │   └── explain_result.py          # 结果中文业务解读
│   ├── react_loop.py                  # ReAct 主循环（Thought→Action→Observation）
│   └── embeddings.py                  # 生成并写入 pgvector embedding
│
│
├── graph/                             # P3：LangGraph 编排（按需添加）
│   ├── state.py                       # NL2SQLState 定义
│   ├── nodes.py                       # 各节点函数
│   ├── edges.py                       # 条件路由（safe/risky/blocked）
│   └── builder.py                     # StateGraph 构建与编译
│
│
├── security/                          # P3/P4：安全层
│   ├── tenant_guard.py                # 多租户隔离（tenant_id 注入与过滤）
│   └── injection_detector.py          # Prompt 注入检测（规则层 + LLM 层）
│
│
├── evals/                             # P4：评测体系
│   ├── dataset/
│   │   └── test_cases.json            # 100 条标注评测集（5 类问题）
│   ├── runner.py                      # 批量运行测试、计算各项指标
│   └── llm_judge.py                   # LLM-as-Judge SQL 质量评分
│
│
├── monitor/                           # P4：监控面板
│   └── dashboard.py                   # rich 面板：成功率/延迟/成本/慢查询
│
│
├── mcp/                               # P3：MCP Server 对外接口
│   └── server.py                      # FastAPI/MCP 接口暴露 Agent 能力
│
│
├── cli/                               # 命令行入口
│   └── nl2sql.py                      # python nl2sql.py "上个月华东 GMV 是多少"
│
│
└── tests/                             # 单元测试 + 核心回归测试（GitHub Actions 用）
    ├── test_schema_extractor.py
    ├── test_intent_parser.py
    ├── test_sql_validator.py
    └── core_regression/               # 20 条核心用例（CI 必跑）
        └── cases.json
```

---

## 各目录与项目阶段对照

| 目录 | 对应阶段 | 说明 |
|---|---|---|
| `db/` | P1 | 建表 + 种数据，一次性运行 |
| `catalog/` | P1 | 本步骤重点，schema 抽取与加载 |
| `core/` | P1 | 所有阶段复用的底层工具 |
| `engine/` | P1 | 基础 NL2SQL 三件套 |
| `agent/tools/` | P2 | 拆分为受控工具集 |
| `agent/react_loop.py` | P2 | ReAct 主链路串联 |
| `graph/` | P3 | LangGraph 替换手写循环 |
| `security/` | P3/P4 | 多租户隔离 + 注入防护 |
| `evals/` | P4 | 评测闭环 |
| `monitor/` | P4 | 运营监控 |

---

## 当前阶段（P1）重点文件

```
现在需要创建的文件：

catalog/
├── extractor.py     ← 你现在要写的
└── loader.py        ← 写完 extractor 后紧接着写

db/
├── migrations/001_create_business_tables.sql   ← 同步进行
└── seeds/seed_business_data.py                 ← 同步进行
```
