# Olist 前端对话测试与修复复测报告

> **Historical test evidence:** This report records the retired fixed OList workflow. Its scripts, Pack runtime and generated fixtures are no longer current product entrypoints; native Agent coverage lives in `tests/integration/test_analysis_agent_trajectory.py` and `test_analysis_agent_security.py`.

## 0. 修复复测摘要

复测日期：2026-07-30<br>
复测数据源：`Olist Brazilian E-commerce`（PostgreSQL，数据源 ID `olist-postgres`）<br>
复测方式：启动修改后的后端和前端，在应用内浏览器中登录原隔离租户，使用 DeepSeek `deepseek-v4-flash` 从页面实际发送问题；结果同时与 Olist PostgreSQL 基准核对。

本报告原先列出的 4 个主要问题均已完成修复并通过复测：

| 复测 ID | 对应缺陷 | 操作与期望 | 实际结果 | 判定 |
| --- | --- | --- | --- | --- |
| R01 | F01 PostgreSQL 执行 | PostgreSQL `execute` 查询 Olist 订单总数 | 成功返回 1 行 `99,441`，与数据库基准一致，不再触发 credential mismatch | **通过** |
| R02 | F01 PostgreSQL 预览 | PostgreSQL `preview` 查询最早 2 个订单编号 | 成功返回 2 行；不再出现 `INTERNAL_ERROR` | **通过** |
| R03 | F01 PostgreSQL 规划 | PostgreSQL `plan` 按订单状态统计并降序 | 返回“已生成只读查询计划，尚未执行”，未执行数据库查询 | **通过** |
| R04 | F02 不可回答指标 | “退款率是多少？”应澄清口径，不得替换成状态分布 | 返回退款率定义、退款标识和分组口径的澄清问题；0 个结果表格、无替代 SQL | **通过** |
| R05 | F03 连续追问 | 首问“按客户州统计订单数 Top 5”，追问“只看 SP 州” | 首问返回 SP 41,746、RJ 12,852、MG 11,635、RS 5,466、PR 5,045；追问返回唯一一行 `SP / 41,746` | **通过** |
| R06 | F03 计划继承证据 | 追问应保留聚合、分组和排序，只新增 SP 过滤 | SQL 保留 `COUNT(order_id)`、客户表关联、`GROUP BY customer_state`、按计数降序，并新增 `WHERE customer_state = $1` | **通过** |
| R07 | F04 刷新恢复 | 刷新后应恢复结果、图表、SQL 和证据入口 | 两轮消息的 2 个表格、2 个图表、2 个证据入口全部恢复；追问 SQL 可再次打开并与刷新前一致 | **通过** |

修复后的关键结论：

- PostgreSQL 的 `plan`、`preview`、`execute` 三种模式均已跑通；
- 不可回答指标不再被静默替换成相关但错误的查询；
- 追问通过结构化计划增量继承上一轮未修改的聚合语义；
- 历史消息保存安全终态响应，刷新后仍可复核结果和 SQL；
- F05—F07 等 P2 问题不在本轮 4 个主要问题的修改范围内，仍建议后续处理。

## 1. 修复前基线结论

测试日期：2026-07-30<br>
测试入口：`http://127.0.0.1:5173/`<br>
模型配置：DeepSeek `deepseek-v4-flash`<br>
测试方式：在应用内浏览器中登录隔离测试租户、选择并绑定 Olist 数据源、从前端逐条发起对话；同时直接查询本机 Olist PostgreSQL 计算标准答案。

以下结论记录修复前的测试基线；最新状态以“0. 修复复测摘要”为准。修复前版本不建议作为“可直接连接 PostgreSQL 并进行连续可信分析”的版本发布。

- 文件快照链路的查询能力整体可用。24 个即时 UI 场景中，22 个符合预期，2 个语义错误；另有 1 个刷新持久化场景失败。
- 21 个实际对话执行场景中，19 个正确或可接受，2 个错误，执行语义通过率为 90.5%。
- 多表查询覆盖了订单、客户、订单项、商品、卖家、支付、评价、品类翻译，最深为 4 表关联，正确结果均与 PostgreSQL 基准一致。
- PostgreSQL 数据源能够注册、发现目录、创建 9 表绑定，但所有执行和预览查询均因凭据引用不一致而安全终止，属于阻断问题。
- 不可回答指标和同会话追问存在“看似成功但语义错误”的风险，比显式失败更需要优先处理。

综合统计如下：

| 范围 | 通过 | 失败 | 备注 |
| --- | ---: | ---: | --- |
| 文件快照即时 UI 场景 | 22 | 2 | 24 个场景，失败为退款率和上下文追问 |
| 页面刷新持久化 | 0 | 1 | 表格、SQL、图表、错误和证据入口丢失 |
| PostgreSQL 执行链路 | 0 | 1 | 注册、目录、绑定成功，执行失败 |
| 合计 | 22 | 4 | 26 个测试场景 |

## 2. 数据集与测试环境

### 2.1 原始 Olist PostgreSQL

直接读取的 9 张 Olist 表共 1,550,922 行：

| 表 | 行数 |
| --- | ---: |
| `olist_orders_dataset` | 99,441 |
| `olist_order_items_dataset` | 112,650 |
| `olist_customers_dataset` | 99,441 |
| `olist_sellers_dataset` | 3,095 |
| `olist_products_dataset` | 32,951 |
| `olist_order_payments_dataset` | 103,886 |
| `olist_order_reviews_dataset` | 99,224 |
| `olist_geolocation_dataset` | 1,000,163 |
| `product_category_name_translation` | 71 |

### 2.2 前端可执行数据源

由于 PostgreSQL 执行链路被缺陷阻断，从同一 PostgreSQL 原样导出除 geolocation 外的 8 张核心业务表，作为多文件 CSV 只读快照继续测试：

- 数据源名称：`Olist Full Core CSV`
- 数据源 ID：`olist-csv-full`
- 总行数：550,759
- 主表：`public.olist_orders_dataset`
- 映射字段：47 个
- 关联：7 条
- 覆盖全部订单、客户、订单项、商品、卖家、支付、评价和品类翻译记录，没有抽样

绑定关系：

1. 订单 → 客户：`customer_id`
2. 订单 → 订单项：`order_id`
3. 订单项 → 商品：`product_id`
4. 订单项 → 卖家：`seller_id`
5. 订单 → 支付：`order_id`
6. 订单 → 评价：`order_id`
7. 商品 → 英文品类翻译：`product_category_name`

## 3. 覆盖范围

本轮覆盖：

- 单表计数、分组、排序、最小值、最大值；
- 2、3、4 表关联；
- 金额求和、平均值、多聚合列；
- 月份半开区间、空值、零结果、未知枚举；
- 模糊问法、GMV 业务术语、不可回答指标；
- 参数化和注入样式输入；
- 1,000 行上限、20 行预览、空白输入；
- `plan`、`preview`、`execute` 三种模式；
- 同会话追问；
- 页面刷新后的历史会话复核；
- PostgreSQL 注册、目录、绑定和执行。

## 4. 详细用例结果

### 4.1 基础、边界与关联查询

| ID | 问题 / 操作 | PostgreSQL 标准答案 | Agent / UI 结果 | 判定 |
| --- | --- | --- | --- | --- |
| C01 | Olist 一共有多少个订单 | 99,441 | 99,441；使用 `COUNT(DISTINCT order_id)` | 通过 |
| C02 | 按订单状态统计并降序 | delivered 96,478；shipped 1,107；canceled 625；unavailable 609；invoiced 314；processing 301；created 5；approved 2 | 数值和顺序全部一致 | 通过 |
| C03 | 实际送达时间为空的订单数 | 2,965 | 2,965；使用 `IS NULL` | 通过 |
| C04 | 2018 年 8 月订单数 | 6,512 | 6,512；使用 `>= 2018-08-01` 且 `< 2018-09-01` | 通过 |
| C05 | 客户州为 ZZ 的订单数 | 0 | 0；订单与客户 2 表关联 | 通过 |
| C06 | 客户州订单数 Top 5 | SP 41,746；RJ 12,852；MG 11,635；RS 5,466；PR 5,045 | 全部一致 | 通过 |
| C07 | 卖家州商品金额 Top 5 | SP 8,753,396.21；PR 1,261,887.21；MG 1,011,564.74；RJ 843,984.22；SC 632,426.07 | 排名和数值一致；展示有浮点尾差 | 通过，格式警告 |
| C08 | 支付方式金额降序 | credit_card 12,542,084.19；boleto 2,869,361.27；voucher 379,436.87；debit_card 217,989.79；not_defined 0 | 全部一致；展示有浮点尾差 | 通过，格式警告 |
| C09 | 英文品类商品金额 Top 5 | health_beauty 1,258,681.34；watches_gifts 1,205,005.68；bed_bath_table 1,036,988.68；sports_leisure 988,048.97；computers_accessories 911,954.32 | 4 表关联、排名和表格值全部一致 | 通过 |
| C10 | 客户州平均评分 Top 5 | AP 4.1940；AM 4.1837；PR 4.1800；SP 4.1740；MG 4.1362 | 排名和原始均值一致；未控制小数位 | 通过，格式警告 |
| C11 | 已送达订单商品金额和运费总和 | 商品 13,221,498.11；运费 2,198,275.64 | 两个聚合值一致；展示有浮点尾差 | 通过，格式警告 |
| C12 | 最早和最晚下单时间 | 2016-09-04 21:15:19；2018-10-17 17:30:18 | SQL 结果正确；表格只显示到分钟 | 通过，展示警告 |
| C13 | 评价正文为空的评价数 | 58,247 | 58,247 | 通过 |

### 4.2 模糊语义、安全与限制

| ID | 问题 / 操作 | 期望 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| C14 | “哪类货卖得最火？给我前三。” | 需要选择合理口径，最好说明假设 | 采用销量口径，返回 cama_mesa_banho 11,115、beleza_saude 9,670、esporte_lazer 8,641 | 可接受；建议披露口径 |
| C15 | “GMV 最高的英文品类前 3” | 按商品价格求和 | health_beauty、watches_gifts、bed_bath_table，数值与基准一致 | 通过 |
| C16 | 请求最早 2,000 个订单 | 强制受 1,000 行上限约束 | 返回 1,000 行，前端每页显示 20 行 | 通过 |
| C17 | 状态为 returned 的订单数 | 0 | 0 | 通过 |
| C18 | 订单 ID 为 `x' OR 1=1 --` | 作为普通值参数化，不得拼接 SQL | SQL 使用 `$1` 参数，结果 0 | 通过 |
| C19 | “退款率是多少？” | 数据集没有退款字段或退款定义，应澄清或拒答 | 错误返回订单状态分布，并宣称 delivered 最高；没有计算比率 | **失败** |
| C21 | 仅输入空格 | 禁止发送 | 发送按钮保持禁用 | 通过 |

### 4.3 连续对话、模式与持久化

| ID | 场景 | 期望 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| C20a | 首问：按客户州统计订单数 Top 5 | 正确聚合 | 正确返回 SP、RJ、MG、RS、PR | 通过 |
| C20b | 追问：“只看 SP 州。” | 保留上一轮“按州统计订单数”的聚合语义并增加 SP 过滤，预期 1 行 `SP / 41,746` | 只继承 SP 过滤，丢失聚合意图，返回 100 行订单明细 | **失败** |
| C22 | 规划模式：按支付方式汇总金额 | 生成 SQL，不执行 | 返回“已生成只读查询计划，尚未执行”，0 行 | 通过 |
| C23 | 预览模式：SP 客户订单明细 | 最多预览 20 行 | 返回最早的 20 行，顺序正确 | 通过 |
| C24 | 刷新页面后复核 C23 | 保留表格、SQL、证据入口和运行状态 | 只剩“查询完成，共返回 20 行”文本；表格数 0，证据入口数 0 | **失败** |

### 4.4 PostgreSQL 原生数据源

| ID | 场景 | 实际 | 判定 |
| --- | --- | --- | --- |
| P01 | 注册 PostgreSQL → 发现 17 张可见表 → 激活 9 张 Olist 表绑定 → 执行最简单订单计数 | 注册、目录、绑定均成功；执行返回 `INTERNAL_ERROR` 和 `selected datasource query could not be executed safely` | **阻断失败** |

## 5. 主要缺陷

### F01 — PostgreSQL 数据源无法执行查询

严重度：P0 / 阻断

现象：

- PostgreSQL 数据源注册成功；
- 目录发现成功；
- 9 张 Olist 表、52 个字段、8 条关系绑定并激活成功；
- 任意 `execute` 或 `preview` 在执行阶段安全失败。

根因证据：

- `DataSourceQueryService` 创建 `CredentialLease` 时使用 `source.location_ref or source.source_id`；
- PostgreSQL Connector 注册时要求的 `connection_ref` 是 `source.credential_ref or source.source_id`；
- PostgreSQL 数据源的 `location_ref` 为空、`credential_ref` 为 `secret://olist/local/database`，因此 Lease 使用 `olist-postgres`，Connector 期待 `secret://olist/local/database`，触发 credential mismatch；
- 随后的广义 `except Exception` 将真实连接器错误折叠成统一 `INTERNAL_ERROR`。

相关代码：

- `src/api/dataset_query_service.py:646-676`
- `src/api/datasource_service.py:762-778`

建议：

1. 由数据源服务或 Connector 暴露规范化的 `connection_ref`，不要在查询服务中再次推导；
2. 至少对 PostgreSQL 使用 `source.credential_ref`，文件数据源使用 `source.location_ref`；
3. 增加真实 Postgres 注册后执行和预览的集成测试；
4. 对内部 `ConnectorErrorCode` 做安全映射并写入服务端结构化日志，避免所有问题都变成不可诊断的 `INTERNAL_ERROR`。

### F02 — 不可回答指标被错误替换成相关但不同的查询

严重度：P1 / 高

问题“退款率是多少？”需要至少有退款事件字段和分母定义。当前 Planner 没有澄清或拒答，而是生成：

```sql
SELECT order_status, COUNT(order_id)
FROM olist_orders_dataset
GROUP BY order_status
LIMIT $1
```

这不是退款率，也没有任何比率计算。结果界面仍标记“查询已完成”和“已验证”，容易形成可信度错觉。

建议：

- 在 Planner 输出中增加 `unsupported` / `needs_clarification` 结果；
- 对“率、占比、同比、环比”等需要派生计算的意图做能力校验；
- 校验“问题要求的输出形态”与计划是否一致，例如“率”不能只返回原始分组计数；
- 当缺少定义时，明确回答缺失字段和需要用户确认的口径。

### F03 — 同会话追问丢失上一轮聚合意图

严重度：P1 / 高

第二轮“只看 SP 州”正确识别了 `SP` 过滤，但丢失上一轮“按客户州统计订单数”的聚合、排序和限制，退化为订单明细查询。

代码层面，用户数据源 Planner 请求只包含当前 `question`、逻辑目录和计划 Schema，没有上一轮问题或计划，见 `src/api/dataset_query_service.py:172-178`。

建议：

- 在构建计划前读取同会话最近一轮用户问题和已验证计划；
- 生成并展示 `contextualized_question`；
- 追问计划应显式保留或替换上一轮的 select、aggregation、group、filter、order 和 limit；
- 增加“只看 X”“改成前 10”“再按月份拆分”“换成金额口径”等回归用例。

### F04 — 刷新后历史会话失去可复核证据

严重度：P1 / 高

即时响应包含 rows、SQL、chart、error、version pins 和完整 trace；持久化的 `ConversationMessageMetadata` 只保留 message type、answer、ok、error code、row count 和 trace。刷新后：

- 成功结果的表格和图表消失；
- SQL、逻辑计划、版本锁定和证据入口消失；
- 失败响应缺少完整 error 对象，前端可能把失败卡片显示成“查询已完成”；
- 历史会话无法重新核对。

相关代码：

- `src/data_agent/runtime/models.py:182-197`
- `src/data_agent/runtime/upload_runtime.py:293-322`
- `frontend/src/viewModel.ts:29-54`

建议：

- 持久化安全裁剪后的完整终态响应，至少包含 rows、logical plan、SQL、chart、error、version pins 和 trace；
- 如果结果体积需要限制，持久化结果快照引用和摘要，并让前端按引用读取；
- 增加“成功刷新”“失败刷新”“图表刷新”“1,000 行分页刷新”的前端集成测试。

### F05 — 金额和均值暴露浮点尾差

严重度：P2 / 中

例如：

- `8753396.210007336`
- `12542084.189999327`
- `4.1940298507462686`

CSV 导入把金额推断为 `DOUBLE`，Agent 摘要直接使用原始数值；部分表格列名也没有被金额格式化规则识别。

建议：

- Olist 金额字段在导入或绑定层使用 DECIMAL；
- 答案生成按字段语义格式化，金额固定两位、评分保留合理小数位；
- 前端金额识别覆盖 `total_revenue`、`total_payment`、`gmv`、`total_price`、`total_freight` 等聚合别名。

### F06 — 数据源目录暴露同库的内部控制表

严重度：P2 / 中

注册 Olist PostgreSQL 后，目录除 9 张 Olist 表外还显示 `data_agent_*`、`metrics_registry`、`semantic_index` 等内部表，共 17 张。虽然本次绑定只选择 Olist 表，但用户仍可在 UI 中浏览并加入内部表。

建议：

- PostgreSQL 数据源支持 schema / relation allowlist；
- 生产部署用最小权限的只读业务库账号；
- 默认过滤平台内部控制表，除非管理员显式允许。

### F07 — 激活绑定的编辑表单没有回填真实逻辑字段

严重度：P2 / 中

绑定详情显示当前激活版本为 `dataset.olist_commerce`，但业务域输入框显示默认的 `dataset.olist-postgres`，逻辑字段输入框也显示自动生成的 `dataset.Olist...`，而非已激活绑定中的 `order.*`、`customer.*` 等映射。若用户直接创建新版本，可能意外覆盖语义命名。

建议：

- 选中激活绑定时，从 binding 回填 `domainId` 和每个 `logical_ref`；
- 创建新版本前显示字段差异预览；
- 增加“加载已有绑定并原样保存”的无损回归测试。

## 6. 关键 SQL 证据

4 表品类金额：

```sql
SELECT
  translation.product_category_name_english,
  SUM(items.price) AS total_amount
FROM olist_orders_dataset AS orders
JOIN olist_order_items_dataset AS items
  ON orders.order_id = items.order_id
JOIN olist_products_dataset AS products
  ON items.product_id = products.product_id
LEFT JOIN product_category_name_translation AS translation
  ON products.product_category_name = translation.product_category_name
GROUP BY translation.product_category_name_english
ORDER BY total_amount DESC
LIMIT 5;
```

注入样式输入的生成 SQL保持参数化：

```sql
SELECT COUNT(orders.order_id) AS order_count
FROM olist_orders_dataset AS orders
WHERE orders.order_id = $1
LIMIT $2;
```

追问错误生成的明细 SQL：

```sql
SELECT
  orders.order_id,
  customers.customer_id,
  orders.order_purchase_timestamp
FROM olist_orders_dataset AS orders
JOIN olist_customers_dataset AS customers
  ON orders.customer_id = customers.customer_id
WHERE customers.customer_state = $1
LIMIT $2;
```

它保留了 `SP` 筛选，但没有保留上一轮的 `COUNT(order_id)`、`GROUP BY state` 和 Top 5 语义。

## 7. 发布建议

发布前至少完成：

1. 修复 F01，并用真实 PostgreSQL 数据源跑通 `plan`、`preview`、`execute`；
2. 为不可回答指标增加澄清/拒答能力，禁止用相关查询替代原问题；
3. 为同会话追问引入可验证的上下文计划继承；
4. 持久化完整、安全、可重放的结果证据，确保刷新后仍能复核；
5. 增加金额精度处理和真实 Postgres/连续对话/刷新恢复的自动化门禁。

修复后建议复跑本报告全部 26 个场景，并要求：

- PostgreSQL 执行链路 100% 通过；
- 精确问题结果与基准完全一致；
- 不可回答问题明确澄清或拒答；
- 追问保持上一轮未被修改的语义；
- 刷新后结果、错误和证据完整可复核。

## 8. 修改实现与自动化验证

### 8.1 四项修改

1. PostgreSQL 执行权威统一
   - 新增 `DataSourceExecutionContext`，在数据源服务中一次性解析并返回 source、snapshot、binding、connector 和规范化 `connection_ref`；
   - PostgreSQL 使用 `credential_ref`，文件数据源使用 `location_ref`，查询服务不再自行推导；
   - `ConnectorErrorCode` 被映射为稳定、安全的 API 错误码和可重试状态。

2. 不可回答与问题—计划一致性
   - 数据集计划新增 `ready`、`needs_clarification`、`unsupported` 状态；
   - 规划提示明确禁止用相关指标替代用户要求的指标；
   - 对退款、退货、率、占比和比例意图增加确定性能力校验；缺少字段或派生指标定义时只返回澄清，不编译、不执行 SQL。

3. 结构化追问继承
   - 历史消息保存上轮 `dataset_query_plan`；
   - 追问规划使用 `patch` / `replace` 两种明确模式；
   - `patch` 默认继承 select、aggregation、group、filter、order 和 limit，只应用显式增量；独立新问题使用 `replace`，避免错误继承。

4. 完整终态响应持久化
   - 历史消息元数据新增 logical plan、dataset query plan、SQL、rows、chart、完整 error、pending memory updates 和 version pins；
   - 前端历史消息直接恢复同一终态结构，不再把刷新后的消息降级为纯文本；
   - 公共 OpenAPI 和前端 AgentResponse 校验 Schema 已同步刷新。

### 8.2 自动化验证结果

| 验证项 | 结果 |
| --- | --- |
| 后端完整测试 | `442 passed`，另有 `609 subtests passed` |
| 前端 Vitest | `12` 个测试文件、`49 passed` |
| 前端 TypeScript + Vite 生产构建 | 通过，`1590 modules transformed` |
| PostgreSQL 执行级回归 | 通过；测试 Connector 收到规范化 credential authority 并成功返回查询结果 |
| 不可回答指标回归 | 通过；退款率不会生成替代 SQL |
| 追问 patch / 独立问题 replace | 通过；分别验证语义继承和上下文重置 |
| 重启后消息恢复 | 通过；rows、chart、SQL、dataset plan 均从持久化存储恢复 |

唯一测试告警为 Starlette 对旧 `httpx` 兼容层的弃用提示，与本轮功能无关。
