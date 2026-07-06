# OList NL2SQL 剩余问题记录

更新时间：2026-07-06

测试入口：前端代理 `http://127.0.0.1:5173/api/...`

测试清单：`evals/olist_questions.jsonl`

本轮结果：48 条问题全部返回 `ok=true`，没有 API 层失败；前端多行明细结果可以正常以真实表格展示。

## 剩余问题总览

当前剩余问题集中在 SQL 语义质量和口径稳定性，不是接口可用性问题。

| 类型 | 用例 | 问题摘要 |
| --- | --- | --- |
| metric | `olist_metric_001`、`olist_metric_002`、`olist_metric_010` | GMV 查询偶发带入无关 `olist_order_reviews_dataset` join 或去重 CTE，可能影响指标口径或查询成本。 |
| metric | `olist_metric_005` | 每个卖家州平均商品价格缺少 `ORDER BY avg_item_price DESC`。 |
| detail | `olist_detail_005` | 差评明细只返回评论标题和文本，缺少清单期望的 `review_id`、`order_id`。 |
| join | `olist_join_006` | “每个订单状态”的支付汇总额外按 `payment_type` 分组，结果粒度过细。 |
| payment | `olist_payment_001`、`olist_payment_002`、`olist_payment_004` | payment-only 问题仍可能通过 `olist_order_items_dataset` 或 review 表过滤支付记录，存在口径偏差风险。 |
| review | `olist_review_001`、`olist_review_002` | review-only 查询仍 join `olist_order_items_dataset`；月度平均评分可能被订单商品数加权。 |
| logistics | `olist_logistics_001` | 2018 月度平均配送天数使用实际送达月份分组，清单期望按购买时间月份统计。 |
| logistics | `olist_logistics_004` | 按卖家州统计承运商交付耗时会带入无关 review 去重 CTE。 |
| geo | `olist_geo_003` | SP 州经纬度覆盖点数量返回邮编前缀分组 1000 行，而不是单个覆盖点数量汇总。 |
| follow_up | `olist_followup_001`、`olist_followup_003` | 多轮追问已保留核心上下文，但仍可能带入无关 review join。 |
| follow_up | `olist_followup_002` | “品类 GMV 排名 + 只看信用卡”被改写为单个最高品类，返回 `LIMIT 1`，与“排名”列表预期不完全一致。 |

## 详细问题

### 1. 无关表 join 仍会污染指标口径

多个 GMV、payment、review、logistics 查询会把题目未要求的表加入 SQL，例如：

- GMV 趋势带入 `olist_order_reviews_dataset`。
- payment-only 汇总先 join `olist_order_items_dataset`。
- review-only 分布 join `olist_order_items_dataset`。
- logistics 聚合带入 review 去重 CTE。

影响：

- 增加不必要的查询成本。
- INNER JOIN 场景可能过滤掉合法记录。
- review 与 order_items 一对多时可能导致评价均值被商品数加权。

建议：

- 在领域规则中补充 `forbidden_tables` 或更细粒度的“payment-only / review-only / logistics-only”查询规则。
- 对 metric/domain 规则增加“必要表最小集”校验：只有题目维度、过滤、租户隔离确实需要时才允许扩表。

### 2. `olist_metric_005` 缺少排序

问题：每个卖家州平均商品价格应按平均价格降序排列。

当前风险：

- SQL 能执行，但结果不是排名视角。

期望：

```sql
ORDER BY avg_item_price DESC
```

### 3. `olist_join_006` 结果粒度过细

问题：题目要求“每个订单状态下的支付金额合计和订单数量”，但 SQL 额外选择并分组 `payment_type`。

影响：

- 一种订单状态会拆成多个支付方式行。
- 不符合按 `order_status` 汇总的预期粒度。

期望：

```sql
SELECT o.order_status,
       SUM(p.payment_value) AS total_payment_amount,
       COUNT(DISTINCT p.order_id) AS order_count
FROM olist_orders_dataset AS o
JOIN olist_order_payments_dataset AS p
  ON o.order_id = p.order_id
GROUP BY o.order_status
```

### 4. `olist_geo_003` 聚合层级不符合题意

问题：题目要求“SP 州客户覆盖的经纬度点数量”，当前 SQL 按客户邮编前缀分组并返回 1000 行。

影响：

- 用户得到的是分组明细，不是覆盖点数量总览。

期望方向：

- 如果统计 geolocation 记录数量：返回单行 `COUNT(*)`。
- 如果统计覆盖点去重数量：返回单行 `COUNT(DISTINCT geolocation_zip_code_prefix)` 或按明确地理点去重。

### 5. `olist_logistics_001` 时间口径不一致

问题：题目要求“2018 年按月统计平均实际配送天数”，清单期望按 `order_purchase_timestamp` 过滤和分月；当前结果倾向使用 `order_delivered_customer_date` 作为月份。

期望方向：

```sql
DATE_TRUNC('MONTH', order_purchase_timestamp) AS month
```

并使用：

```sql
order_delivered_customer_date - order_purchase_timestamp
```

计算实际配送耗时。

### 6. `olist_detail_005` 字段不完整

问题：差评明细返回评论标题和评论文本，但缺少 `review_id`、`order_id`。

期望字段：

```sql
review_id,
order_id,
review_comment_title,
review_comment_message
```

### 7. `olist_followup_002` 排名上下文被收敛成 Top 1

问题：上一轮问题是“GMV 最高的商品品类”，追问“只看信用卡支付呢？”后，SQL 返回 `LIMIT 1`。

影响：

- 如果用户期望延续“排名”列表，当前只返回单个最高品类。

建议：

- 追问上下文化时保留“排名/列表”意图，不要只因“最高”收敛为 Top 1。
- 若上轮没有明确 `LIMIT 1`，默认保留列表型排名。

## 已修复并通过的重点场景

- `olist_detail_003` 已避免 `SELECT *`，明确返回订单、商品、卖家、价格、运费字段。
- `olist_detail_006` 已正确生成商品体积表达式并返回 25 行表格。
- `olist_metric_007` 已使用月度维度同时统计订单量和 GMV。
- `olist_metric_009` 已可走 review -> orders -> customers 路径统计客户州平均评分。
- `olist_payment_003` 已直接基于支付表按 `payment_value DESC` 返回前 20 条支付记录。
- seller 租户隔离场景已保留 `seller_id = 当前租户` 过滤。
- `olist_followup_001` 已保留“2018 年 + 月份 + 客户州”上下文。
- `olist_followup_003` 已保留 seller 租户范围和 2018 年月度 GMV 语义。
