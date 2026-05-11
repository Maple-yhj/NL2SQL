# RAG

## 具体任务清单

- [x] PostgreSQL 启用 pgvector

- [x] 创建 semantic_index 表
- [x] 在 rag/documents.py 中定义 EmbeddingDocument 概念
- [x] 将 MetricRegistry.default() 转成 metric documents
- [x] 将 schema_catalog.json 转成 table / column documents
- [x] 封装 Gemini embedding client，固定 gemini-embedding-2 + 768 维
- [x] 封装 vector_store，支持 upsert
- [x] 编写 scripts/rebuild_embeddings.py
- [x] 执行脚本，确认 semantic_index 中有 metric/table/column 数据
- [x] 手工用“销售额”“退款率”“地区”等问题做相似度查询验证

#### PostgreSQL 启用 pgvector

```shell
psql -U postgres -d nl2sql_dev
set "PGROOT=C:\Program Files\PostgreSQL\17"
cd %TEMP%
git clone --branch v0.8.2 https://github.com/pgvector/pgvector.git
cd pgvector
nmake /F Makefile.win
nmake /F Makefile.win install
CREATE EXTENSION IF NOT EXISTS vector;
```

#### 创建 semantic_index 表

1、原本的metrics_registry表是业务指标表，semantic_index的作用是把这些业务对象转为可检索的向量对象

```sql
CREATE TABLE IF NOT EXISTS public.semantic_index
(
    id bigserial PRIMARY KEY,
    tenant_id varchar(32) NOT NULL DEFAULT 'demo',
    object_type varchar(32) NOT NULL,
    object_key text NOT NULL,
    source_table varchar(64),
    source_id integer,
    content text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(768) NOT NULL,
    embedding_model varchar(128) NOT NULL,
    embedding_dim integer NOT NULL DEFAULT 768,
    content_hash char(64) NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT semantic_index_object_type_check
        CHECK (object_type IN ('metric', 'table', 'column', 'example_query')),

    CONSTRAINT semantic_index_tenant_object_key_unique
        UNIQUE (tenant_id, object_type, object_key)
);
```

2、字段含义

| 字段              | 类型           | 含义                                                         |
| ----------------- | -------------- | ------------------------------------------------------------ |
| `id`              | `bigserial`    | 向量索引表自己的主键                                         |
| `tenant_id`       | `varchar(32)`  | 租户 ID，与你的 `metrics_registry.tenant_id` 对齐            |
| `object_type`     | `varchar(32)`  | 向量对象类型，例如 `metric`、`table`、`column`               |
| `object_key`      | `text`         | 业务唯一键，例如 `metric:gmv`、`table:orders`、`column:orders.amount` |
| `source_table`    | `varchar(64)`  | 原始来源表，例如 `metrics_registry`、`schema_catalog`        |
| `source_id`       | `integer`      | 原始来源表中的 ID，例如 `metrics_registry.id`                |
| `content`         | `text`         | 真正送去 embedding 模型的文本，目标是提升语义召回效果        |
| `metadata`        | `jsonb`        | 结构化元数据，用于后续返回给工具和拼 prompt                  |
| `embedding`       | `vector(768)`  | embedding 向量                                               |
| `embedding_model` | `varchar(128)` | 生成该向量使用的模型名                                       |
| `embedding_dim`   | `integer`      | 向量维度，当前固定为 768                                     |
| `content_hash`    | `char(64)`     | `content` 的 SHA-256 哈希，用于判断内容是否变化              |
| `is_active`       | `boolean`      | 是否启用，方便软删除或临时屏蔽                               |
| `created_at`      | `timestamptz`  | 创建时间                                                     |
| `updated_at`      | `timestamptz`  | 更新时间                                                     |

## Embedding

### 1、核心

embedding 的核心是将离散的、人类可读的对象映射到一个连续的高维向量空间，使得语义相似的对象在这个空间中的距离也相近。比如：
"心跳过快"和"心率偏高"的向量很近，即便没有共同词汇，因为它们的语义相近

### 2、相似度搜索运算符

| **操作符** | **名称**     | **物理意义**                             | **常见场景**                           |
| ---------- | ------------ | ---------------------------------------- | -------------------------------------- |
| `<->`      | **L2 距离**  | 两点之间的**直线距离**（欧几里得距离）。 | 图片搜索、通用特征匹配。               |
| `<=>`      | **余弦距离** | 衡量两个向量**方向**的差异，忽略长度。   | **自然语言处理 (NLP)**、文档相似度。   |
| `<#>`      | **负内积**   | 结合了方向和长度的度量。                 | 推荐系统（如计算用户偏好与商品特征）。 |



## Function Calling/Tool

1、函数的提示词应该包括:  函数的作用、什么时候调用该函数、什么时候不能调用该函数、函数参数的含义、函数的返回值以及类型