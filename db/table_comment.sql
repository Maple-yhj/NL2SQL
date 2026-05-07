-- 表级注释
COMMENT ON TABLE orders IS '订单主表，记录每笔交易';
COMMENT ON TABLE users  IS '用户账户表';
COMMENT ON TABLE products  IS '产品信息表，记录每个产品的详细信息';
COMMENT ON TABLE refunds  IS '退款订单信息表，记录每笔退款的交易';

-- 字段级注释
COMMENT ON COLUMN orders.id   IS '订单唯一ID，主键';
COMMENT ON COLUMN orders.tenant_id  IS '租户ID，用于多租户隔离';
COMMENT ON COLUMN orders.user_id  IS '标识订单所属的用户ID，外键';
COMMENT ON COLUMN orders.product_id  IS '表示订单购买的商品ID，外键';
COMMENT ON COLUMN orders.region     IS '订单所属的区域,具体值只有[华南,华东,华中,华北,西南,港澳]';
COMMENT ON COLUMN orders.quantity     IS '下单的商品数量';
COMMENT ON COLUMN orders.amount     IS '订单金额，单位：人民币';
COMMENT ON COLUMN orders.status     IS '订单状态：paid/pending/cancelled/refunded';
COMMENT ON COLUMN orders.paid_at     IS '订单的支付时间';
COMMENT ON COLUMN orders.created_at     IS '订单的创建时间';

COMMENT ON COLUMN users.id   IS '用户唯一ID，主键';
COMMENT ON COLUMN users.tenant_id  IS '租户ID，用于多租户隔离';
COMMENT ON COLUMN users.username  IS '用户名称';
COMMENT ON COLUMN users.email  IS '用户邮箱';
COMMENT ON COLUMN users.region  IS '用户所在地区,具体值只有[华南,华东,华中,华北,西南,港澳]';
COMMENT ON COLUMN users.user_type  IS '用户类型';
COMMENT ON COLUMN users.created_at  IS '用户注册时间';

COMMENT ON COLUMN products.id   IS '商品唯一ID，主键';
COMMENT ON COLUMN products.tenant_id  IS '租户ID，用于多租户隔离';
COMMENT ON COLUMN products.name  IS '商品名称';
COMMENT ON COLUMN products.category  IS '商品品类，如家居、运动等类别';
COMMENT ON COLUMN products.price  IS '商品售价';
COMMENT ON COLUMN products.cost  IS '商品成本';
COMMENT ON COLUMN products.is_active  IS '商品是否上架';
COMMENT ON COLUMN products.created_at  IS '商品创建时间';

COMMENT ON COLUMN refunds.id   IS '退款记录id';
COMMENT ON COLUMN refunds.tenant_id  IS '租户ID，用于多租户隔离';
COMMENT ON COLUMN refunds.order_id   IS '退款订单id，外键';
COMMENT ON COLUMN refunds.user_id   IS '退款用户id，外键';
COMMENT ON COLUMN refunds.amount   IS '退款金额';
COMMENT ON COLUMN refunds.reason   IS '退款原因';
COMMENT ON COLUMN refunds.status   IS '退款状态：pending,approved,rejected';
COMMENT ON COLUMN refunds.created_at   IS '退款记录创建时间';
