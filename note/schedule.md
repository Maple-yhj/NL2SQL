# schedule

- [x] 新建 rag/documents.py
- [x] 定义 EmbeddingDocument 数据结构
- [x] 从 metrics_registry 读取 active metrics
- [x] 把每条 metric 拼成稳定 content
- [x] 把每条 metric 转成 metadata JSON
- [x] 新建 rag/embedding_client.py
- [x] 调 Gemini embedding，确认输出 768 维
- [x] 新建 rag/vector_store.py
- [ ] 实现 semantic_index upsert
- [ ] 新建 scripts/rebuild_embeddings.py
- [ ] 执行脚本，确认 semantic_index 中出现 metric 向量
- [ ] 用“销售额 / 退款率 / 订单数”做最小语义检索验证