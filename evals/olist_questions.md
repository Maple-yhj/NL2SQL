# OList NL2SQL Question Test Set

Source dataset: Kaggle Brazilian E-Commerce Public Dataset by Olist, imported locally from `data/olist/brazilian-ecommerce.zip`.

Project notes:

- OList native tables do not contain `tenant_id`.
- The project maps non-admin `tenant_id` to OList `seller_id`.
- Registered OList metrics include `gmv`, `orders`, `avg_item_price`, and `avg_review_score`.
- Project GMV is `SUM(olist_order_items_dataset.price + olist_order_items_dataset.freight_value)`.
- Most aggregate tests use `execute=false`; detail/table tests can use `execute=true` for manual frontend checks.

## Registered Metric Cases

| ID | Question | Tenant | Key expectation |
| --- | --- | --- | --- |
| `olist_metric_001` | 统计 2018 年每个月的 GMV 趋势 | `admin` | Monthly GMV from `olist_order_items_dataset`. |
| `olist_metric_002` | 2017 年 GMV 最高的 10 个卖家是谁？ | `admin` | Seller ranking by GMV. |
| `olist_metric_003` | 按商品品类统计 2018 年上半年的 GMV，返回前 15 个品类 | `admin` | Join items to products and group by category. |
| `olist_metric_004` | 各客户州的订单数排名，统计 2018 年的 distinct orders | `admin` | Join items, orders, and customers; count distinct orders. |
| `olist_metric_005` | 每个卖家州的平均客单商品价格是多少？ | `admin` | Join sellers; average item price by seller state. |
| `olist_metric_006` | 2018 年不同支付方式贡献的 GMV 分别是多少？ | `admin` | Join payments; GMV by payment type. |
| `olist_metric_007` | 按月份统计 2018 年订单量和 GMV，一起展示 | `admin` | Multi-metric monthly aggregate. |
| `olist_metric_008` | Which product categories generated the most sales revenue in 2018? | `admin` | English sales revenue maps to GMV; category grouping. |
| `olist_metric_009` | 2018 年每个客户州的平均评价分是多少？ | `admin` | Review score by customer state. |
| `olist_metric_010` | 找出 2018 年 GMV 增长最快的月份 | `admin` | Monthly GMV with month-over-month growth. |
| `olist_metric_011` | 2017 年到 2018 年，各卖家州的 GMV 同比变化是多少？ | `admin` | GMV by seller state and year. |
| `olist_metric_012` | 按商品品类和支付方式交叉统计 GMV，查看 2018 年前 20 个组合 | `admin` | Category and payment-type cross aggregate. |

## Detail Row Cases

| ID | Question | Tenant | Key expectation |
| --- | --- | --- | --- |
| `olist_detail_001` | 列出最近 20 条订单明细，包括订单号、卖家、商品、价格、运费和发货截止时间 | `admin` | Return item rows ordered by `shipping_limit_date`. |
| `olist_detail_002` | 查看 2018 年 8 月价格最高的 30 个订单商品明细 | `admin` | Item detail ranking by `price`. |
| `olist_detail_003` | 找出运费超过商品价格的订单明细，返回 50 条 | `admin` | Filter `freight_value > price`. |
| `olist_detail_004` | 查询已取消订单的订单号、购买时间和客户州，按购买时间倒序返回 20 条 | `admin` | Join orders to customers and filter canceled orders. |
| `olist_detail_005` | 列出 2018 年评分为 1 星且有评论内容的差评订单，返回评论标题和评论文本 | `admin` | Review detail with score and non-empty comment filter. |
| `olist_detail_006` | 查看体积最大的商品，返回商品 id、品类、长宽高和重量，取前 25 个 | `admin` | Derived product volume ranking. |

## Cross-Table Join Cases

| ID | Question | Tenant | Key expectation |
| --- | --- | --- | --- |
| `olist_join_001` | 按客户城市统计 2018 年 GMV 前 20 名城市 | `admin` | Items -> orders -> customers. |
| `olist_join_002` | 按卖家城市统计订单数和 GMV，找出 2018 年排名前 20 的城市 | `admin` | Items -> sellers. |
| `olist_join_003` | 健康美容品类 2018 年每个月的 GMV 趋势 | `admin` | Category filter using Portuguese or translated English name. |
| `olist_join_004` | 比较 SP 和 RJ 两个客户州在 2018 年的 GMV 和订单数 | `admin` | Customer state filter for SP and RJ. |
| `olist_join_005` | 哪些商品品类平均评价分最低？至少有 100 条评价，返回前 10 个 | `admin` | Reviews -> items -> products with `HAVING` threshold. |
| `olist_join_006` | 每个订单状态下的支付金额合计和订单数量是多少？ | `admin` | Orders joined to payments; aggregate by status. |
| `olist_join_007` | 不同卖家州发往不同客户州的 GMV 分布，返回 GMV 最高的 30 个州组合 | `admin` | Seller-state to customer-state flow. |
| `olist_join_008` | 找出平均配送延迟最严重的客户州，按实际送达日期减预计送达日期计算 | `admin` | Delivery delay by customer state. |

## Payment Cases

| ID | Question | Tenant | Key expectation |
| --- | --- | --- | --- |
| `olist_payment_001` | 各支付方式的订单数和支付金额合计是多少？ | `admin` | Payment aggregate by type. |
| `olist_payment_002` | 信用卡分期数越多，平均支付金额是否更高？按分期数统计平均支付金额 | `admin` | Credit-card average payment by installments. |
| `olist_payment_003` | 列出支付金额最高的 20 个订单支付记录 | `admin` | Payment detail ranking. |
| `olist_payment_004` | 哪些订单使用了多次支付？列出 payment_sequential 大于 1 的订单数量 | `admin` | Multi-payment order detection. |

## Review Cases

| ID | Question | Tenant | Key expectation |
| --- | --- | --- | --- |
| `olist_review_001` | 按评分统计评价数量分布 | `admin` | Count by `review_score`. |
| `olist_review_002` | 2018 年每个月的平均评价分趋势 | `admin` | Monthly average review score. |
| `olist_review_003` | 哪些卖家的平均评分最低？至少有 50 条评价，返回前 20 个 | `admin` | Reviews joined to items, grouped by seller. |
| `olist_review_004` | 列出评价回复时间最长的 30 条评价 | `admin` | Review answer timestamp minus creation date. |

## Logistics Cases

| ID | Question | Tenant | Key expectation |
| --- | --- | --- | --- |
| `olist_logistics_001` | 2018 年按月统计平均实际配送天数 | `admin` | Average delivery duration by purchase month. |
| `olist_logistics_002` | 哪些订单实际送达晚于预计送达日期？返回最近 50 条 | `admin` | Late-delivery detail rows. |
| `olist_logistics_003` | 各订单状态的平均审批耗时是多少？ | `admin` | Approval duration by order status. |
| `olist_logistics_004` | 从购买到交给承运商平均需要多久？按卖家州统计 | `admin` | Carrier handoff duration by seller state. |

## Geography Cases

| ID | Question | Tenant | Key expectation |
| --- | --- | --- | --- |
| `olist_geo_001` | 客户最多的州和城市分别有哪些？返回前 20 个城市 | `admin` | Customer count by state and city. |
| `olist_geo_002` | 卖家数量最多的州和城市是哪里？ | `admin` | Seller count by state and city. |
| `olist_geo_003` | 按客户邮编前缀关联经纬度，找出 SP 州客户覆盖的经纬度点数量 | `admin` | Zip prefix join from customers to geolocation. |

## Tenant Scope Cases

Use tenant `3442f8959a84dea7ee197c632cb2df15` for these cases.

| ID | Question | Tenant | Key expectation |
| --- | --- | --- | --- |
| `olist_tenant_001` | 我的 2018 年 GMV 是多少？ | `3442f8959a84dea7ee197c632cb2df15` | Scope `olist_order_items_dataset` to seller id. |
| `olist_tenant_002` | 我的最近 20 条订单明细 | `3442f8959a84dea7ee197c632cb2df15` | Seller-scoped item detail rows. |
| `olist_tenant_003` | 我的商品品类 GMV 排名 | `3442f8959a84dea7ee197c632cb2df15` | Scope products through seller-owned order items. |
| `olist_tenant_004` | 我的订单平均评价分按月趋势 | `3442f8959a84dea7ee197c632cb2df15` | Scope reviews through seller-owned order items. |

## Follow-Up Context Cases

These are manual conversation tests. Use the previous-turn context shown inside the question, then ask only the follow-up utterance in the conversation UI.

| ID | Question | Tenant | Key expectation |
| --- | --- | --- | --- |
| `olist_followup_001` | 上一轮问题：2018 年 GMV 按月份趋势。追问：那按客户州拆一下 | `admin` | Preserve 2018 monthly GMV context and add customer state. |
| `olist_followup_002` | 上一轮问题：列出 2018 年 GMV 最高的商品品类。追问：只看信用卡支付呢？ | `admin` | Preserve category GMV ranking and add credit-card filter. |
| `olist_followup_003` | 上一轮问题：我的 2018 年 GMV 是多少。追问：按月份列出来 | `3442f8959a84dea7ee197c632cb2df15` | Preserve seller tenant scope and 2018 monthly GMV context. |
