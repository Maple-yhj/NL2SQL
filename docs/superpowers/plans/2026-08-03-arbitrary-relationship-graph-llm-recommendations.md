# Arbitrary Relationship Graph and LLM Recommendation Implementation Plan

> **For the next implementation session:** Execute this plan task-by-task and keep the checkboxes current. The repository already contains substantial uncommitted user changes. Treat the working tree as the baseline, inspect overlapping diffs before every edit, and do not reset, discard, or rewrite unrelated changes.

**Goal:** 将当前以主表为根、依赖加入顺序的多表关联树，升级为支持环、平行边、自关联、复合键和多对多桥接的通用语义关系图；数据源目录发布后自动由 LLM 生成字段关联建议，并通过本地数据验证与人工编辑后激活；查询阶段由确定性路由器选择安全、可解释的 JOIN 子图，禁止模型直接生成或选择物理 SQL。

**Architecture:** 配置层保存任意有向多重图，节点表达“物理表在业务中的角色”，边表达复合关联条件、方向、保留侧、基数、来源和验证结果。导入流程先发现数据库约束并进行受限字段剖析，再让 LLM 对经过预筛选的候选字段对做语义推荐，最后由本地只读验证器校准评分。用户在图编辑草稿中接受、拒绝或修改建议，验证通过后生成不可变、快照固定的 v2 语义绑定。查询 Planner 仍只使用逻辑字段；确定性 Graph Route Resolver 从激活图中解析路径，Fan-out Guard 校验粒度和基数，Compiler 只编译该次查询需要的无环 JOIN 执行树。

**Tech Stack:** Python 3.12、Pydantic v2、FastAPI、SQLite 控制面、DuckDB/SQLite/PostgreSQL 只读连接器、sqlglot、现有 `ModelClient` 协议、React 19、TypeScript、Vite、Vitest、pytest/unittest。

---

## 1. Product and Safety Decisions

以下决策属于本计划的实现前提，后续会话不要在没有新产品要求时重新退回树形模型：

- 配置模型从第一版开始就是通用图，不再用“表加入顺序”表达拓扑。
- 图允许环、同一对节点的多条边、同一物理表的多个角色节点以及复合字段条件。
- 图可以包含多个不连通分量；单次查询引用的节点必须位于同一个可达分量，否则安全失败。
- 配置图可以有环，但单次 SQL 编译必须选择一棵确定的无环 JOIN 执行树。
- `INNER` 关系可双向参与路径搜索；`LEFT` 关系只能从声明的保留侧向非保留侧扩展，除非用户另建反向关系。
- LLM 负责生成语义关联假设和解释，不负责执行 SQL，不负责绕过验证，不直接决定激活状态。
- 原始行、凭据和连接字符串默认不得发送给 LLM；默认只发送目录元数据、约束信息和归一化统计摘要。
- LLM 推荐任务失败不得阻塞数据源导入；用户必须仍可手工建立图。
- 高置信度建议可以在界面预选，但所有图版本仍需用户确认并显式激活。
- 多对多、类型转换、未知基数、低匹配率关系不得自动预选。
- 多路径同等可信时不得使用任意排序静默选路，必须返回路径歧义错误或使用用户配置的首选路径。
- 当前 active binding 保持不可变；图编辑只发生在 revision 化草稿中。
- v1 绑定不原地改写；将其转换为 v2 草稿，验证并激活后再退役旧版本。

## 2. Current Baseline

当前实现需要被替换或适配的关键点：

- `frontend/src/DataSourcePanel.tsx`
  - 以 `selectedRelations` 顺序和 `relationships[index]` 表达关联树。
  - 新增表默认连接主表。
  - 默认推荐仅查找第一个同名字段，否则选择两侧第一列。
  - 删除中间表或更换主表会重建关系。
- `src/data_agent/datasources/models.py`
  - `SemanticRelationship` 只有单字段相等条件。
  - `SemanticBindingRecord` 强制关系按拓扑顺序扩展，拒绝环和重复节点。
- `src/data_agent/tools/schemas.py`
  - `CatalogRelation`/`CatalogColumn` 没有稳定 ID、主键、唯一键或外键元数据。
- `src/api/datasource_service.py`
  - 绑定创建直接接收 `primary_relation` 和树形 `relationships`。
- `src/data_agent/datasources/registry.py`
  - 只保存不可变绑定，没有独立的可编辑图草稿或推荐运行记录。
- `src/api/dataset_query_service.py`
  - `DatasetQueryCompiler` 使用 `right_relation -> relationship` 映射向主表回溯。
  - 无通用图路由、基数传播或 fan-out 安全检查。
- `frontend/src/types.ts`、`frontend/src/api.ts`
  - 只暴露 v1 树形关系契约。

在开始 Task 1 前运行并记录：

```bash
git status --short
git diff --stat
.venv/bin/python -m pytest tests/test_api_datasources.py tests/test_dataset_query_service.py tests/unit/datasources/test_registry.py -q -p no:cacheprovider
npm --prefix frontend run build
```

当前已知基线：上述后端聚焦测试最近一次为 24 项通过，前端生产构建通过。若新会话结果不同，先判断是否是工作区新增改动导致，不要重置。

## 3. Target Domain Model

### 3.1 Stable catalog identifiers

扩展目录类型：

```python
class CatalogColumn(ToolModel):
    column_id: StableCatalogId
    name: NonBlankText
    data_type: NonBlankText
    nullable: bool
    ordinal: int

class CatalogKey(ToolModel):
    key_id: StableCatalogId
    kind: Literal["primary", "unique"]
    column_ids: tuple[StableCatalogId, ...]

class CatalogForeignKey(ToolModel):
    foreign_key_id: StableCatalogId
    from_relation_id: StableCatalogId
    from_column_ids: tuple[StableCatalogId, ...]
    to_relation_id: StableCatalogId
    to_column_ids: tuple[StableCatalogId, ...]

class CatalogRelation(ToolModel):
    relation_id: StableCatalogId
    relation: NonBlankText
    columns: tuple[CatalogColumn, ...]
    keys: tuple[CatalogKey, ...] = ()
    foreign_keys: tuple[CatalogForeignKey, ...] = ()
```

ID 生成规则：

- 基于规范化的 schema/table/column 名称产生稳定摘要 ID。
- 相同物理名称跨快照保持稳定；改名应产生新 ID。
- schema fingerprint 继续固定完整目录内容，新增约束元数据必须进入 fingerprint。
- 旧快照加载时由兼容适配器补齐 ID，不能要求用户重新上传才能读取旧绑定。

### 3.2 Graph draft

新增 `src/data_agent/relationships/models.py`：

```python
class RelationshipGraphNode(RelationshipModel):
    node_id: StableIdentifier
    relation_id: StableCatalogId
    role_name: NonBlankText
    logical_entity: NonBlankText
    enabled: bool = True

class RelationshipCondition(RelationshipModel):
    from_column_id: StableCatalogId
    operator: Literal["eq"] = "eq"
    to_column_id: StableCatalogId

class RelationshipEdge(RelationshipModel):
    edge_id: StableIdentifier
    from_node_id: StableIdentifier
    to_node_id: StableIdentifier
    conditions: tuple[RelationshipCondition, ...]
    cardinality: Literal[
        "one_to_one",
        "one_to_many",
        "many_to_one",
        "many_to_many",
        "unknown",
    ]
    join_semantics: Literal["inner", "left"]
    preserve_node_id: StableIdentifier | None = None
    route_priority: int = 100
    enabled: bool = True
    provenance: RelationshipProvenance
    quality: RelationshipQuality | None = None

class RelationshipComponent(RelationshipModel):
    component_id: StableIdentifier
    anchor_node_id: StableIdentifier
    grain_column_ids: tuple[StableCatalogId, ...]
    anchor_scoped: bool = False

class RelationshipRouteRule(RelationshipModel):
    rule_id: StableIdentifier
    terminal_node_ids: tuple[StableIdentifier, ...]
    ordered_edge_ids: tuple[StableIdentifier, ...]

class RelationshipGraphDraft(RelationshipModel):
    graph_id: StableIdentifier
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    source_snapshot_version: int
    schema_fingerprint: NonBlankText
    revision: int
    status: Literal["discovering", "draft", "validating", "ready", "failed"]
    nodes: tuple[RelationshipGraphNode, ...]
    edges: tuple[RelationshipEdge, ...]
    components: tuple[RelationshipComponent, ...]
    route_rules: tuple[RelationshipRouteRule, ...] = ()
```

结构验证必须覆盖：

- 节点、边、组件、规则 ID 唯一。
- 边端点存在；允许 `from_node_id == to_node_id` 表达自关联。
- 复合条件非空，字段分别属于对应节点的物理 relation。
- 同一 edge 中不允许重复字段对。
- `LEFT` 必须声明 `preserve_node_id`，且只能是该边端点之一。
- `INNER` 不得声明保留侧。
- 同一物理 relation 可对应多个 node，但 role name 和 logical entity 必须唯一。
- 图允许环、平行边和不连通分量。
- route rule 的 edge 顺序必须构成可执行的简单路径或树。

### 3.3 Active v2 binding

不要让可编辑 draft 直接成为 active binding。新增不可变：

```python
class SemanticGraphFieldMapping(RelationshipModel):
    logical_ref: NonBlankText
    node_id: StableIdentifier
    column_id: StableCatalogId

class SemanticGraphBindingRecord(RelationshipModel):
    schema_version: Literal[2] = 2
    binding_id: StableIdentifier
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    source_snapshot_version: int
    schema_fingerprint: NonBlankText
    domain_id: StableIdentifier
    version: int
    status: SemanticBindingStatus
    graph: ActivatedRelationshipGraph
    mappings: tuple[SemanticGraphFieldMapping, ...]
    validation_report_digest: NonBlankText
    created_at: datetime
    updated_at: datetime
```

兼容策略：

- 当前 `SemanticBindingRecord` 作为 schema version 1 保留读取能力。
- 新建绑定只创建 schema version 2。
- API 响应使用带 `schema_version` discriminator 的 union。
- 对没有 discriminator 的历史 JSON，在 registry loader 中注入 `schema_version=1`。
- 新增 `normalize_binding_graph(binding, catalog)`，把 v1 树适配为只读图，避免执行路径出现两套编译器。

## 4. Recommendation and Validation Pipeline

### 4.1 Local profiler

新增 `src/data_agent/relationships/profiler.py`。

对每个字段保存有界摘要，不保存完整值：

- null count/rate
- approximate distinct count/rate
- bounded min/max for compatible scalar types
- type family
- optional hashed value sketch
- optional normalized-name tokens

对候选字段对计算：

- type compatibility
- normalized-name similarity
- overlap/match rate
- left/right orphan rate
- left/right uniqueness
- average and maximum fan-out
- estimated joined rows and expansion ratio

约束：

- 所有剖析查询必须使用现有只读连接器和独立预算。
- 每表/每字段/总候选数必须有硬上限。
- PostgreSQL 使用采样或近似统计，不能无界全表扫描。
- 文件快照可在受控 DuckDB 上完成精确或有界统计。
- 统计失败返回 unknown，不把失败解释为无关联。

### 4.2 Candidate prefilter

新增 `src/data_agent/relationships/candidates.py`。

候选来源按优先级：

1. 数据库声明的外键。
2. 主键/唯一键与类型兼容字段。
3. 规范化字段名匹配。
4. 值域重合与唯一率证据。
5. LLM 发现的语义别名，但必须通过字段 ID allowlist。

限制：

- 默认每个表对最多保留 5 个单字段候选。
- 复合键优先来自数据库约束；启发式复合键最多 2–3 列，并受严格组合预算控制。
- 明显不兼容类型不进入 LLM prompt。
- 记录候选被过滤的 reason code，便于调试。

### 4.3 LLM recommender

新增 `src/data_agent/relationships/recommender.py`。

复用 `data_agent.runtime.dependencies.ModelClient`，不要直接绑定具体 Provider SDK。

输入只包含：

- relation/node/column stable IDs
- 表名、字段名、数据类型、注释（存在时）
- PK/UK/FK 元数据
- 归一化统计摘要
- 已预筛选候选字段对
- 严格 JSON Schema

输出模型：

```python
class LlmRelationshipRecommendation(RelationshipModel):
    from_node_id: StableIdentifier
    to_node_id: StableIdentifier
    column_pairs: tuple[RelationshipCondition, ...]
    cardinality_hint: RelationshipCardinality
    semantic_type: Literal[
        "foreign_key",
        "same_entity",
        "bridge",
        "temporal_reference",
        "unknown",
    ]
    confidence: float
    reason_codes: tuple[NonBlankText, ...]
    explanation: NonBlankText
```

安全校验：

- 拒绝未知 ID、SQL、自由表达式和未声明 operator。
- 最多修复重试一次；再次失败标记推荐 run 失败。
- prompt、response 和错误日志均不得包含凭据或原始行。
- 模型返回 confidence 不能直接成为最终置信度。

大目录策略：

- 按候选表对分批调用。
- 每批输入大小受限。
- 最后执行一次只包含摘要的全局一致性 pass，用于识别桥表、角色别名和冲突建议。
- 缓存键包含 schema fingerprint、模型 ID/版本、prompt version 和 profiler version。

### 4.4 Deterministic calibration

新增 `src/data_agent/relationships/validator.py`。

验证报告包含：

```python
class RelationshipFinding(RelationshipModel):
    code: NonBlankText
    severity: Literal["info", "warning", "error"]
    edge_id: StableIdentifier | None
    message: NonBlankText

class RelationshipValidationReport(RelationshipModel):
    graph_id: StableIdentifier
    graph_revision: int
    schema_fingerprint: NonBlankText
    findings: tuple[RelationshipFinding, ...]
    edge_quality: tuple[RelationshipEdgeQuality, ...]
    route_ambiguities: tuple[RelationshipRouteAmbiguity, ...]
    activation_allowed: bool
    report_digest: NonBlankText
```

最终推荐等级由确定性证据校准：

- 高可信：数据库 FK 或语义建议与高匹配/合理基数同时成立。
- 推荐：语义和数据证据大体一致，需要人工确认。
- 低可信：仅名称相似、匹配率低或基数不稳定。
- 阻断：字段不存在、类型不兼容、条件无效或声明基数与数据严重冲突。

多对多、unknown cardinality、明显 expansion 风险至少 warning；若没有显式桥接/grain 规则则阻断激活或阻断相关查询。

## 5. Deterministic Graph Routing

新增 `src/data_agent/relationships/router.py`。

### 5.1 Inputs and outputs

```python
class GraphRouteRequest(RelationshipModel):
    required_node_ids: tuple[StableIdentifier, ...]
    required_logical_refs: tuple[NonBlankText, ...]

class ResolvedJoinStep(RelationshipModel):
    edge_id: StableIdentifier
    existing_node_id: StableIdentifier
    introduced_node_id: StableIdentifier
    traversal: Literal["forward", "reverse"]

class ResolvedJoinGraph(RelationshipModel):
    root_node_id: StableIdentifier
    required_node_ids: tuple[StableIdentifier, ...]
    included_node_ids: tuple[StableIdentifier, ...]
    steps: tuple[ResolvedJoinStep, ...]
    route_rule_id: StableIdentifier | None
    route_digest: NonBlankText
```

### 5.2 Resolution algorithm

1. 去重 required nodes。
2. 单节点请求直接返回零 JOIN route。
3. 验证所有节点存在、启用且在同一可达分量。
4. 若存在匹配的显式 route rule，严格验证并使用。
5. 否则构建可遍历边：
   - enabled 且无 error finding。
   - INNER 双向。
   - LEFT 仅允许从 preserve node 向另一端扩展。
6. 使用确定性代价模型寻找候选路径：
   - validation risk
   - cardinality/fan-out risk
   - user route priority
   - hop count
7. 从组件 anchor 或可安全保留粒度的 required node 出发，对目标节点计算受限最短路径并合并。
8. 移除无关分支并拓扑排序，生成无环 join steps。
9. 若多个路径具有相同语义成本且没有用户规则，返回 `GRAPH_AMBIGUOUS_PATH`。
10. 若不可达，返回 `GRAPH_NO_PATH`。

不要使用 edge ID 排序来掩盖语义歧义；edge ID 只用于稳定展示，不用于替用户作业务决定。

### 5.3 Cycle and parallel-edge behavior

- 图中的环不会被删除。
- 单次 route 不能重复引入同一 node role。
- 平行边按显式 route rule、验证质量和优先级选择。
- 若三角关系中的第三条边是额外一致性约束而非路径边，首版不自动拼入 WHERE/ON；用户应把它建模为同一复合 edge 或显式 route rule。
- 自关联必须使用不同 node role；物理 relation 相同但 SQL alias 不同。

## 6. Grain and Fan-out Safety

新增 `src/data_agent/relationships/grain.py`。

必须在 SQL 编译前完成：

- 从节点/组件 grain 和 edge cardinality 推导每个逻辑字段的原生粒度。
- 沿 resolved route 传播一对多和多对多扩张。
- 判断 aggregation 输入字段是否会在 JOIN 后重复。
- 能确定安全时生成 pre-aggregation requirement。
- 不能证明安全时返回 `GRAPH_UNSAFE_FANOUT`，不得让 LLM 添加 DISTINCT 猜测修复。

首版安全策略：

- detail query：允许明确的行扩张，但必须受 limit 和结果预算控制。
- aggregate query：任何 measure 穿过会扩张其原生粒度的路径都需要：
  - 已声明可安全预聚合；或
  - 对应 metric/grain rule；否则失败。
- many-to_many 默认不允许聚合，除非存在显式 bridge node 和 bridge grain。
- count_distinct 只有在用户问题和逻辑计划明确要求时才能使用，不能作为自动 fan-out 修复。

## 7. Persistence and Lifecycle

### 7.1 SQLite control-plane tables

修改 `src/data_agent/datasources/sqlite_registry.py`，新增：

```sql
CREATE TABLE relationship_graph_drafts (
    tenant_id TEXT NOT NULL,
    graph_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_snapshot_version INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (tenant_id, graph_id)
);

CREATE INDEX relationship_graph_drafts_source_idx
ON relationship_graph_drafts (tenant_id, source_id, source_snapshot_version);

CREATE TABLE relationship_recommendation_runs (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_snapshot_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id)
);
```

In-memory registry 必须实现相同协议，不能只实现 SQLite。

### 7.2 Optimistic concurrency

- PATCH draft 必须携带客户端读取的 revision。
- 保存时使用 compare-and-swap；冲突返回 `GRAPH_REVISION_CONFLICT`。
- 每次修改 revision + 1。
- recommendation rerun 产生 candidate revision，不覆盖 user-edited/rejected edges。

### 7.3 Activation transaction

激活必须在同一控制面写事务中完成：

1. 读取指定 graph revision。
2. 校验 source snapshot 和 schema fingerprint 未变化。
3. 重新执行结构校验；必要时复用未过期的数据验证报告。
4. 确认没有 error finding。
5. 生成不可变 `SemanticGraphBindingRecord`。
6. 保存并激活新 binding version。
7. 退役同 source/domain 的旧 active binding。
8. 返回新的 pins。

任一步失败不得留下“半激活”状态。

## 8. API Contract

新增或调整：

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/data-sources/{source_id}/relationship-graphs/draft` | 获取当前快照的图草稿 |
| POST | `/api/data-sources/{source_id}/relationship-recommendations` | 手动重跑推荐 |
| GET | `/api/data-sources/{source_id}/relationship-recommendations/{run_id}` | 查询推荐任务 |
| PATCH | `/api/data-sources/{source_id}/relationship-graphs/{graph_id}` | revision 化编辑图 |
| POST | `/api/data-sources/{source_id}/relationship-graphs/{graph_id}/validate` | 结构和数据验证 |
| POST | `/api/data-sources/{source_id}/relationship-graphs/{graph_id}/preview-route` | 预览节点集合的实际路径 |
| POST | `/api/data-sources/{source_id}/relationship-graphs/{graph_id}/activate` | 原子激活 v2 绑定 |

上传行为：

- 文件或数据库目录成功发布后立即创建 graph draft。
- 自动启动 recommendation run。
- 上传响应不等待 LLM 完成，返回 `relationship_discovery` 摘要：run ID、status、graph ID。
- 前端轮询或订阅已有 typed event 机制显示进度。
- 服务重启时遗留 running 任务标记为 retryable failed，用户可以重跑。

API 错误码至少增加：

- `GRAPH_REVISION_CONFLICT`
- `GRAPH_STALE_SNAPSHOT`
- `GRAPH_VALIDATION_FAILED`
- `GRAPH_NO_PATH`
- `GRAPH_AMBIGUOUS_PATH`
- `GRAPH_UNSAFE_FANOUT`
- `RELATIONSHIP_RECOMMENDATION_FAILED`

契约变化后重新生成：

```bash
.venv/bin/python scripts/export_apifox_openapi.py
.venv/bin/python scripts/export_frontend_agent_response_schema.py
```

## 9. Frontend Experience

### 9.1 New modules

建议拆分：

```text
frontend/src/relationships/
  RelationshipGraphEditor.tsx
  RelationshipGraphCanvas.tsx
  RelationshipTableView.tsx
  RelationshipEdgeInspector.tsx
  RelationshipNodeInspector.tsx
  RelationshipValidationPanel.tsx
  RelationshipRoutePreview.tsx
  relationshipGraphState.ts
  relationshipGraphTypes.ts
```

`DataSourcePanel.tsx` 只负责导入、目录、草稿生命周期和激活编排，不再承载全部图编辑逻辑。

### 9.2 Workflow

将当前四步调整为：

1. 导入或选择数据源。
2. 检查目录和字段。
3. AI 发现关联。
4. 复核关系图与逻辑字段。
5. 验证并激活。

### 9.3 Layout

- 左侧：表/角色/字段列表；搜索；筛选未连接、低可信、高风险。
- 中间：图视图；节点显示表角色和字段数量，边显示字段条件、基数和状态。
- 右侧：选中节点/边的属性编辑器。
- 底部或抽屉：验证报告、路径预览、版本差异和激活操作。
- 必须提供与画布等价的可访问表格视图，保证键盘和窄屏可完整编辑。

画布依赖决策：

- 执行时先评估成熟 React node-edge library 的许可证、React 19 兼容性、bundle 增量和键盘支持。
- 若引入依赖，必须更新 `frontend/package.json` 和 lockfile，并增加构建门禁。
- 不得为了避免依赖而交付无法处理环、平行边或自关联的伪图编辑器。

### 9.4 Editing behavior

- 用户可以新增/删除角色节点和边。
- 边支持多个字段条件。
- AI/FK/user/migration 来源可见。
- 用户接受、拒绝、编辑建议均进入本地 draft state 并保存 revision。
- 用户编辑后的 edge 标记为 `user_edited`；rerun AI 不得覆盖。
- 删除节点时必须明确列出受影响边，禁止静默重连。
- 修改保留侧、基数或条件后，旧 validation 立即失效。
- undo/redo 仅作用于未保存本地操作；保存后依赖 revision history。

### 9.5 Recommendation presentation

建议按以下层级显示：

- 高可信：默认展开，可批量接受。
- 推荐：逐条确认。
- 低可信：折叠，不预选。
- 阻断：不能接受，显示明确原因。

每条边显示：

- 字段条件
- 基数
- join semantics/preserve side
- LLM explanation
- 数据库约束证据
- match/orphan/uniqueness/fan-out 指标
- 用户是否已修改

### 9.6 Route preview

用户选择两个或多个 node 后调用 preview API，展示：

- root 和实际 join order
- 使用的 edge ID、字段条件和方向
- 备用路径
- 路径歧义
- 预计 expansion
- 只读示例 SQL/逻辑 JOIN 计划

若有歧义，允许将当前路径保存为 route rule。

## 10. Query Planner and Compiler Integration

### 10.1 Logical planner

修改 `DatasetLogicalPlanner` 的 logical catalog：

```json
{
  "ref": "dataset.Order.amount",
  "type": "decimal",
  "nodeId": "orders",
  "grain": ["orders.order_id"]
}
```

模型仍然：

- 只能返回 logical refs。
- 不能返回 physical relation、column、edge ID 或 SQL。
- 不负责在多路径中选边。

### 10.2 Compiler pipeline

将 `DatasetQueryCompiler.compile()` 拆分：

```text
logical refs
→ resolve graph nodes
→ GraphRouteResolver
→ Grain/FanOutGuard
→ BoundJoinPlan
→ sqlglot compiler
→ PreparedQuery
```

新增 `BoundJoinPlan`，至少包含：

- root node
- aliased physical relations
- ordered join steps
- composite ON conditions
- join type
- route digest
- grain/fan-out decision

SQL alias 必须按 node role 分配，而不是按 physical relation 分配，以支持 self-join。

### 10.3 Evidence

AgentResponse 或 trace 中增加安全的关系证据：

- graph/binding version
- route digest
- logical node roles
- edge IDs
- cardinality
- join semantics
- validation status
- fan-out decision

物理 SQL 仍只在现有 evidence 层按权限展示。修复历史回答恢复后 evidence summary 与消息卡片行数/图表状态不一致的问题，并增加回归测试。

## 11. Implementation Tasks

### Task 0 — Freeze and document the baseline

- [x] 读取 `README.md`、`PRODUCT.md`、当前 datasource/query 相关代码及本计划。
- [x] 运行基线测试和前端构建。
- [x] 记录 `git status --short` 和所有重叠文件的用户 diff。
- [x] 不修改或格式化无关文件。

完成门槛：后续失败可以明确区分为基线问题或本次改动。

### Task 1 — Extend catalog IDs and constraints

主要文件：

- `src/data_agent/tools/schemas.py`
- `src/data_agent/datasources/file_snapshot.py`
- `src/data_agent/datasources/sqlite_snapshot.py`
- `src/api/datasource_service.py`
- `src/data_agent/tools/connectors/postgres.py`
- `src/data_agent/tools/connectors/sqlite.py`
- `src/data_agent/tools/connectors/duckdb.py`

工作：

- [x] 先添加 Catalog ID/constraint 模型测试。
- [x] 实现稳定 relation/column/key/FK ID。
- [x] PostgreSQL 发现 PK/UK/FK。
- [x] SQLite 使用 PRAGMA 发现 PK/UK/FK。
- [x] CSV/XLSX 生成 IDs，但约束为空。
- [x] 旧 catalog JSON 兼容加载。
- [x] 更新 fingerprint 测试。

完成门槛：相同目录产生稳定 ID；约束变化引起 fingerprint 变化；旧快照仍可加载。

### Task 2 — Add arbitrary graph domain models

主要文件：

- Create `src/data_agent/relationships/__init__.py`
- Create `src/data_agent/relationships/models.py`
- Add `tests/unit/relationships/test_models.py`

工作：

- [x] 先写 cycle、parallel edge、self-join、composite key 测试。
- [x] 实现 graph/node/edge/component/route-rule 模型。
- [x] 实现结构验证。
- [x] 明确 LEFT traversal 约束。
- [x] 实现 stable graph/report digest。

完成门槛：合法任意图可建模，非法引用/条件被拒绝，模型冻结且序列化稳定。

### Task 3 — Add graph draft and recommendation persistence

主要文件：

- `src/data_agent/datasources/registry.py`
- `src/data_agent/datasources/sqlite_registry.py`
- Add `tests/unit/relationships/test_registry.py`

工作：

- [x] 扩展 registry Protocol。
- [x] 实现 in-memory 和 SQLite 一致行为。
- [x] 新增 draft/run 表和索引。
- [x] 实现 revision compare-and-swap。
- [x] 删除 datasource 时级联清理 graph drafts/runs。
- [x] 重启持久化回归测试。

完成门槛：并发旧 revision 写入失败；重启后草稿和推荐审计仍存在。

### Task 4 — Implement profiling and candidate generation

主要文件：

- Create `src/data_agent/relationships/profiler.py`
- Create `src/data_agent/relationships/candidates.py`
- Add `tests/unit/relationships/test_profiler.py`
- Add `tests/integration/test_relationship_profiling.py`

工作：

- [x] 定义 profile budgets 和 typed results。
- [x] 实现名称/类型/约束预筛选。
- [x] 实现有界 overlap、orphan、uniqueness、fan-out 统计。
- [x] 覆盖空表、全 NULL、高基数、类型不兼容和采样失败。
- [x] 确认 prompt input 不包含原始值。

完成门槛：真实 FK、同名非 FK 和错误同名字段能被确定性区分；所有查询受预算限制。

### Task 5 — Implement LLM recommendation

主要文件：

- Create `src/data_agent/relationships/recommender.py`
- Create `src/data_agent/relationships/prompts.py`
- Add `tests/unit/relationships/test_recommender.py`

工作：

- [x] 定义严格输出 Schema。
- [x] 使用现有 ModelClient。
- [x] 实现分批 prompt、一次修复和全局 reconciliation。
- [x] 拒绝 unknown IDs/SQL/free-form expressions。
- [x] 保存 model/prompt/profiler provenance。
- [x] 实现 schema fingerprint cache key。

完成门槛：fake model 测试覆盖有效、幻觉 ID、非法 SQL、超时、坏 JSON 和部分批次失败。

### Task 6 — Implement deterministic validation and scoring

主要文件：

- Create `src/data_agent/relationships/validator.py`
- Add `tests/unit/relationships/test_validator.py`

工作：

- [x] 合并 constraint、profiler、LLM、user provenance。
- [x] 生成 edge quality 和 typed findings。
- [x] 实现 activation blocking rules。
- [x] 实现 user-edited/rejected preservation。
- [x] 生成稳定 report digest。

完成门槛：LLM 高分不能覆盖确定性阻断；多对多/低匹配/基数冲突产生正确严重级别。

### Task 7 — Wire async import-time discovery APIs

主要文件：

- Create `src/api/relationship_service.py`
- `src/api/app.py`
- `src/api/routes.py`
- `src/api/schemas.py`
- `src/api/datasource_service.py`
- `frontend/src/api.ts`
- `frontend/src/types.ts`
- Add `tests/test_api_relationships.py`

工作：

- [x] 上传成功后创建 graph draft 并启动推荐 run。
- [x] 上传响应不等待 LLM。
- [x] 实现 run 状态和 retry。
- [x] 实现 GET/PATCH/validate/preview/activate API。
- [x] 所有操作按 tenant 隔离。
- [x] 实现 revision conflict 和 stale snapshot 错误。
- [x] 更新 OpenAPI 和前端类型。

完成门槛：LLM 不可用时导入成功、推荐失败可重试、手工草稿仍可编辑。

### Task 8 — Implement GraphRouteResolver

主要文件：

- Create `src/data_agent/relationships/router.py`
- Add `tests/unit/relationships/test_router.py`

测试矩阵：

- [x] 单节点零 JOIN。
- [x] 简单链。
- [x] 星型。
- [x] 菱形双路径歧义。
- [x] 显式 route rule 解歧义。
- [x] 三节点环。
- [x] 平行边。
- [x] LEFT 正向和非法反向。
- [x] 自关联角色。
- [x] 不连通分量。
- [x] disabled/error edge。

完成门槛：相同输入总是得到相同 route 或相同 typed error，不存在隐式随机选路。

### Task 9 — Implement grain and fan-out guard

主要文件：

- Create `src/data_agent/relationships/grain.py`
- Add `tests/unit/relationships/test_grain.py`

工作：

- [x] 实现 cardinality 传播。
- [x] 实现 measure native grain。
- [x] 检测 1:N/N:N 聚合膨胀。
- [x] 定义安全 pre-aggregation 输出契约。
- [x] 无法证明安全时 fail closed。

完成门槛：客户金额经订单一对多关联不会被静默重复；合法订单金额按客户分组仍可执行。

### Task 10 — Upgrade active binding and compiler

主要文件：

- `src/data_agent/datasources/models.py`
- `src/api/datasource_service.py`
- `src/data_agent/datasources/registry.py`
- `src/data_agent/datasources/sqlite_registry.py`
- `src/api/dataset_query_service.py`
- Add `tests/test_graph_dataset_query_service.py`

工作：

- [x] 增加 v1/v2 binding union 和 loader compatibility。
- [x] 实现 v1-to-resolved-graph adapter。
- [x] 编译器接入 router 和 fan-out guard。
- [x] SQL alias 改为 node-role based。
- [x] 支持 composite ON、自关联和多跳。
- [x] activation 原子化。

完成门槛：旧 v1 测试继续通过；v2 覆盖任意图；模型仍看不到物理标识符。

### Task 11 — Build the graph editing UI

主要文件：

- `frontend/src/DataSourcePanel.tsx`
- Create `frontend/src/relationships/*`
- `frontend/src/styles.css`
- `frontend/src/api.ts`
- `frontend/src/types.ts`
- Add pure state and contract tests under `frontend/src/relationships/*.test.ts`

工作：

- [x] 拆出 graph editor state reducer。
- [x] 实现 recommendation progress 和 retry。
- [x] 实现 node/edge/table views。
- [x] 实现 composite condition editor。
- [x] 实现 accept/reject/edit 和 provenance 状态。
- [x] 实现 validation panel。
- [x] 实现 route preview 和 preferred route 保存。
- [x] 删除节点/边前展示影响，不静默重连。
- [x] 实现窄屏和键盘等价操作。

完成门槛：用户可以在不编辑 JSON 的情况下完成环、平行边、复合键和自关联配置；低可信建议不会被误激活。

### Task 12 — Evidence, migration, and release gates

主要文件：

- `frontend/src/App.tsx`
- `frontend/src/evidenceRoute.ts`
- `src/data_agent/runtime/models.py`
- `src/api/dataset_query_service.py`
- Create migration utility under `scripts/`
- Update README and API docs

工作：

- [x] 在 trace/evidence 中显示 route digest、logical nodes、edge IDs、cardinality 和 fan-out decision。
- [x] 修复历史消息证据行数/图表状态恢复一致性。
- [x] 提供 v1 binding -> v2 draft preview/execute 工具。
- [x] 迁移只创建草稿，不自动激活。
- [x] 更新 README、OpenAPI、前端 schema。
- [x] 运行完整发布门禁。

完成门槛：旧 active binding 无停机可读；管理员验证并激活 v2 后旧版才退役；证据可说明本次实际关联路径。

## 12. Required Acceptance Scenarios

### Graph expressiveness

- [x] `customers → orders → items → products` 四表链。
- [x] `A-B-D` 与 `A-C-D` 菱形图，未设规则时返回歧义。
- [x] `A-B-C-A` 环可保存和激活，但查询只选择无环 route。
- [x] 两表存在 `customer_id` 和 `account_id` 两条平行边。
- [x] `employee.manager_id → employee.id` 自关联使用两个 node role 和两个 SQL alias。
- [x] `(tenant_id, order_id)` 复合键生成两个 ON 条件。
- [x] 订单与促销通过桥表的多对多关系。

### Recommendation behavior

- [x] 声明 FK 获得最高证据等级。
- [x] 同名但类型不兼容字段被过滤或阻断。
- [x] 同名但 overlap 极低字段不被高可信推荐。
- [x] 不同名但语义与值域均匹配字段可被 LLM 推荐。
- [x] LLM 幻觉字段 ID 被拒绝。
- [x] LLM 超时不影响导入。
- [x] rerun 不覆盖 user-edited/rejected edge。
- [x] 默认 prompt 不包含原始数据值。

### Query correctness

- [x] 单表问题不产生无关 JOIN。
- [x] 多表问题只加入 resolved route 节点。
- [x] LEFT 关系保留侧正确。
- [x] 聚合经过 1:N 后不会静默重复。
- [x] unknown/many-to-many 风险在缺少规则时失败。
- [x] 路径歧义、无路径和 stale graph 使用 typed error。
- [x] plan mode 不执行 profiler/query 数据访问。
- [x] preview/execute 使用相同 route digest。

### Governance

- [x] 所有 graph/run API tenant 隔离。
- [x] active graph 固定 source version、schema fingerprint、validation digest。
- [x] recommendation provenance 可审计。
- [x] 激活事务失败不会退役现有 active binding。
- [x] 删除 datasource 级联清理 drafts/runs/bindings/pins。

## 13. Verification Commands

每个 Task 先跑聚焦测试，阶段完成后至少运行：

```bash
.venv/bin/python -m pytest tests/unit/relationships -q -p no:cacheprovider
.venv/bin/python -m pytest tests/test_api_datasources.py tests/test_api_relationships.py tests/test_dataset_query_service.py tests/test_graph_dataset_query_service.py -q -p no:cacheprovider
.venv/bin/python -m pytest tests/unit/datasources tests/integration/test_relationship_profiling.py -q -p no:cacheprovider
npm --prefix frontend test
npm --prefix frontend run build
```

发布前运行仓库标准门禁：

```bash
.venv/bin/python -m pytest -p no:cacheprovider
.venv/bin/python -m pytest tests/e2e/test_olist_golden_runtime.py -q -p no:cacheprovider
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

根据公共契约变化检查：

```bash
.venv/bin/python scripts/export_apifox_openapi.py
.venv/bin/python scripts/export_frontend_agent_response_schema.py
git diff --exit-code -- docs/apifox-openapi.json frontend/src/generated/agent-response.schema.ts frontend/src/generated/agent-response.fixture.json
```

如果生成文件本来就有用户未提交改动，应先理解并合并，不能用生成命令覆盖后直接假定正确。

## 14. Completion Definition

本计划只有在以下条件全部满足时完成：

- 用户可以导入多表数据源并看到自动启动的 LLM 关联推荐任务。
- 推荐包含语义解释、确定性数据质量指标和来源审计。
- 用户可以通过界面配置环、平行边、自关联和复合键。
- 图草稿支持 revision 冲突保护，用户修改不会被 AI rerun 覆盖。
- 激活生成不可变 v2 binding，并固定 snapshot/fingerprint/report digest。
- 查询使用确定性 Graph Route Resolver，不让 LLM 选择物理 JOIN。
- 多路径歧义、无路径和 fan-out 风险安全失败。
- SQL 编译支持 node role alias 和 composite ON。
- Evidence 能说明本次使用的实际关系路径和安全决策。
- v1 active binding 可继续运行，并可迁移为待人工确认的 v2 草稿。
- 聚焦测试、完整后端测试、前端测试/构建、OpenAPI/schema freshness 和 wheel 构建全部通过。

## 15. Suggested Prompt for the Next Session

将以下内容作为新会话起始指令：

```text
请按照 docs/superpowers/plans/2026-08-03-arbitrary-relationship-graph-llm-recommendations.md 执行。

先完成 Task 0，检查当前 dirty worktree 并把现有未提交改动视为用户基线，禁止 reset 或覆盖无关修改。随后从 Task 1 开始按测试先行方式逐项实施；每完成一个 Task，更新计划中的 checkbox，运行该 Task 的聚焦测试，并报告实际修改文件和验证结果。不要把 LLM 用于运行时物理 JOIN 选路；运行时必须使用确定性 Graph Route Resolver。
```
