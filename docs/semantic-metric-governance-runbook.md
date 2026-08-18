# 语义指标混合流程实施与发布手册

更新时间：2026-08-19

## 实现结果

系统已实现“领域包优先、受控 Web/LLM 兜底、确定性校验、人工审批、版本化发布”的语义指标流程。运行时只有已发布且被 pin 的 `MetricSet`，或经校验并明确开启的临时 `MetricOverlay`，可以参与 SQL 编译。领域包、网页内容和 LLM 输出都只能产生候选，不能自行成为组织级口径。

未知 GMV 的实际流程如下：

1. Agent 先在当前 effective metric catalog 中匹配规范名或同义词。
2. 未命中时，Commerce Domain Pack 识别 GMV/成交总额，并基于 active binding 生成可落地候选，例如 `SUM(price)`、`SUM(price + freight_value)`、`SUM(payment_value)`。
3. Agent 自动创建或复用同一用户、数据快照和 binding 下的治理草案，展示 proposal ID、候选和待确认项；不会在当前运行中静默激活。
4. 用户在“指标口径治理”面板选择候选，确认时间、状态、退款、币种和粒度。
5. 系统执行字段、类型、时间角色、关系路由、grain 与 fan-out 校验。
6. `semantic_admin`/`data_admin` 审批后，原子发布不可变 MetricSet 新版本并移动 active pointer。
7. 新查询 pin 新版本；已有 conversation 继续使用原 pin，不被静默迁移。

## 主要组件

- `semantic_metrics/ast.py`：受限、不可变、无原始 SQL 的指标 AST。
- `semantic_metrics/domain_packs.py`：版本化 Commerce/Finance 术语与模板；首个可执行模板为 Commerce GMV。
- `semantic_metrics/catalog.py`：governed、overlay、legacy 的确定性优先级与同义词冲突关闭策略。
- `semantic_metrics/compiler.py`：AST 到 SQLGlot 的参数化编译，支持复合 SUM、过滤和安全除法。
- `semantic_metrics/validator.py`：字段/类型/时间/grain/关系/fan-out 静态验证。
- `semantic_metrics/service.py`：草案、修订、验证、overlay、审批、发布、审计和 RBAC。
- `semantic_metrics/web_discovery.py`：可信 HTTPS 域名、SSRF 防护、截断、不可信内容隔离、严格 JSON 候选适配器。
- SQLite 控制面：additive migration ledger、proposal/report/metric-set/pointer/overlay/pin/audit 表。
- 前端 `MetricGovernancePanel`：候选比较、口径确认、验证结果和管理员发布。

## API 与权限

主要 API：

- `POST /api/data-sources/{source_id}/metric-proposals/discover`
- `GET|POST /api/data-sources/{source_id}/metric-proposals`
- `POST /api/metric-proposals/{proposal_id}/select`
- `PUT /api/metric-proposals/{proposal_id}/candidates/{candidate_id}`
- `POST /api/metric-proposals/{proposal_id}/validate`
- `POST /api/metric-proposals/{proposal_id}/overlays`
- `POST /api/metric-proposals/{proposal_id}/approve-and-activate`
- `GET /api/data-sources/{source_id}/metric-sets/active`

`analyst` 可提案和编辑自己的草案；`semantic_editor` 可验证；`semantic_admin`、`data_admin` 或企业管理员可批准和激活。绑定/关系图激活同样要求语义管理员角色，避免绕过治理 API。

## 功能开关

| 环境变量 | 默认值 | 作用 |
|---|---:|---|
| `SEMANTIC_METRICS_DOMAIN_DISCOVERY` | `true` | Agent 和 API 使用已安装领域包生成草案 |
| `SEMANTIC_METRICS_WEB_DISCOVERY` | `false` | 领域包无候选时启用注入的受控 Web 搜索/LLM 适配器 |
| `SEMANTIC_METRICS_PROVISIONAL_OVERLAYS` | `false` | 允许已验证指标显式绑定到 run，或用 CAS repin 到 conversation 临时 overlay |
| `SEMANTIC_METRICS_AUTO_PUBLISH_ALIAS` | `false` | 预留低风险别名自动发布策略；当前实现不会自动发布金额指标 |

无效布尔值会让服务启动失败，防止配置拼写错误造成意外放开。

## 发布顺序

1. 备份 `DATA_AGENT_STATE_DIR/control/datasources.sqlite3`。
2. 部署代码但保持 Web、overlay、auto-publish 为 `false`；启动时只执行 additive migration。
3. 运行 `python -m pytest -p no:cacheprovider tests/unit/semantic_metrics tests/test_api_semantic_metrics.py`。
4. 在测试租户接入 OList，确认“季度 GMV”只生成草案且不会执行替代 SQL。
5. 由管理员确认并发布一个 GMV MetricSet，验证新会话使用新 pin、旧会话保持旧 pin。
6. 观察 proposal 失败率、验证错误分布、alias 冲突和 fan-out 拒绝；再扩大租户范围。
7. 只有配置了可信域名搜索客户端、严格 JSON 模型适配器和出网策略后，才单独开启 Web discovery。
8. Overlay 需单独灰度；金额/收入/利润和多表公式仍不得自动发布。

## 回滚

应用回滚不需要删除新表：迁移是 additive，旧版本会忽略这些表。立即停止候选发现时，将四个开关全部设为 `false` 并重启。已发布 MetricSet 不应物理删除；把 active pointer 回切到上一不可变版本，保留 audit 和历史 conversation pin。若怀疑定义错误，先阻止新查询选择该版本，再将记录标记为 revoked；不要覆盖原内容或重写历史证据。

## 安全边界与限制

- Web 文本是不可信数据，不能携带数据库行、凭据或敏感 schema；仅允许最小化逻辑角色描述。
- 当前默认不启用真实 Web adapter；代码提供受控端口和安全策略，由部署方显式注入客户端。
- 同一次 Agent 运行不会在草案发布后自动 repin。用户发布后需重新发起查询，这是为了保证 checkpoint、证据和 SQL 使用同一权威版本。开启 overlay 后，可由用户显式绑定到指定 run，或把已验证口径用 revision/CAS 绑定到 conversation；若 run overlay 与已有 conversation pin 的基础权威不同，运行会 fail-closed。
- Payment 与 item 的多行直接 JOIN 可能乘法膨胀。验证器会拒绝不安全的跨表聚合；应先按订单粒度预聚合或建立明确安全路径。
- 历史 V1 单字段指标继续通过 `LegacyMetricAdapter` 只读兼容；新复合指标使用 V2 AST。

## 验收门槛

- 后端全量 pytest 在 Windows/Linux 通过。
- 前端单元测试和生产构建通过。
- OpenAPI、前端 AgentResponse schema/fixture 与后端生成结果完全一致。
- 离线语义评测覆盖英文/中文 GMV、边界词、跨域隔离和普通金额请求。
- RBAC、租户隔离、authority stale、SSRF、网页提示注入、fan-out 与旧 pin 回放均有回归测试。
