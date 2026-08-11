# Data Analysis Agent 修复进度（2026-08-08）

## 继续执行说明

本文件最初是在 Codex 使用额度触及上限时写入的续跑检查点。2026-08-10 已完成剩余修复、自动化验证和真实浏览器回归；当前修改仍未提交。

## 当前总体状态

| 顺序 | 子任务 | 状态 | 验证情况 |
| --- | --- | --- | --- |
| 1 | 修复单表分析在 catalog 阶段后超时/失败及补充安全诊断 | 已完成 | 单元/集成测试及真实浏览器查询通过 |
| 2 | 收紧关系图激活门槛 | 已完成 | 后端关系校验测试通过；前端关系编辑器已支持基数填写和人工复核 |
| 3 | 持久化 failed/cancelled 会话 | 已完成 | 浏览器已验证：取消等待澄清的运行后，从历史重新打开可看到取消原因，不再为空白 |
| 4 | 修复结果、步骤与证据状态矛盾 | 已完成 | 前端测试及成功查询的浏览器证据链通过 |
| 5 | 改善错误信息和歧义澄清体验 | 已完成 | 浏览器已验证模糊问题会暂停并给出指标选项；422 格式化已有测试 |
| 6 | 完整端到端回归和收尾 | 已完成 | 后端、前端、类型检查、生产构建和 Olist 浏览器回归均通过 |

## 最终完成记录（2026-08-10）

- 修复语义图 `relationship.route` 将 dataclass 当作 Pydantic 模型调用 `model_dump()` 导致的 `AttributeError`，并补充 provider 回归测试。
- 修复聚合别名被错误当作逻辑字段传给关系路由的问题。
- 逻辑查询规划增加未知字段、跨孤立组件的确定性校验和最多三次修复；真实 Olist 查询不再选择孤立的可选翻译表。
- 修复结果表金额列暴露二进制浮点尾差的问题，图表和表格均显示稳定金额。
- 真实浏览器执行“按商品类别汇总全部订单商品金额，并指出金额最高的类别”成功：返回 74 行、1 项证据，最高类别为 `beleza_saude`，金额约 `1,258,681.34`。
- 浏览器确认结果分页、证据抽屉、规划/预览/执行模式、澄清继续、会话标题与搜索、失败/取消恢复均正常。
- 最终自动化验证：后端 `354 passed, 1 warning, 70 subtests passed`；前端 `17 files / 79 tests passed`；`tsc -b`、`vite build`、`git diff --check` 均通过。

## 已完成修复

### 1. Agent 编排、超时与诊断

- `src/data_agent/analysis_agent/evaluator.py`
  - 成功执行 `catalog.inspect` / 元数据工具且计划仍未完成时，走确定性继续路径，避免为“是否继续”额外调用一次模型并耗尽运行截止时间。
- `src/data_agent/analysis_agent/prompts.py`
  - 成功的 catalog/semantic 元数据观察不再把巨大安全预览重复塞入提示词。
  - 规划提示词要求：聚合问题没有明确指标时必须先澄清。
- `src/data_agent/analysis_agent/nodes.py`、`runtime.py`
  - 每个运行节点记录安全诊断信息（run/node/error type/diagnostic ID），不记录敏感数据或完整用户数据。
  - 运行异常、主动取消、等待输入时取消都会转为终态，并将仍在运行的计划步骤标为 blocked。
- `src/data_agent/tools/models.py`、`invoker.py`
  - 增加并正确分类 `GRAPH_NO_PATH`、`GRAPH_AMBIGUOUS_PATH`、`GRAPH_UNSAFE_FANOUT`。
  - 同时兼容枚举和字符串形式的异常 `code`，避免关系路由错误被错误降级为 provider/internal error。

### 2. 关系图激活安全门槛

- `src/data_agent/relationships/validator.py`
  - 激活校验只以 enabled 边为准。
  - LLM 推荐边必须人工 reviewed 或 user_edited 才可激活。
  - unknown cardinality 阻断激活。
  - many-to-many 未声明粒度时阻断激活。
  - expansion ratio 大于 10 时阻断激活。
  - 有环且没有 route rules 时阻断激活。
  - 被拒绝的推荐不阻断激活；孤立的 enabled role 给出 warning。
- `frontend/src/relationships/RelationshipGraphEditor.tsx`
  - 每条边增加基数选择器。
  - 用户编辑基数后标记为已审核、接受并启用。
  - 推荐状态与操作按钮文案更清晰。

### 3. 失败/取消会话持久化

- `src/data_agent/analysis_agent/composition.py`
  - 生产持久化包装器可构造并保存 success/failed/cancelled 响应。
  - 根据真实响应返回 `error`、`chart`、`table`、`analysis` message type，确保历史恢复时前端能正确渲染。
- `src/data_agent/analysis_agent/runtime.py`、`nodes.py`
  - 所有异常和取消入口均 best-effort 持久化终态。
- 浏览器实测：运行 `analysis-run-369b67162a904671adbae860f7ab4783` 在等待指标澄清时取消；新建分析后重新打开对应历史记录，可看到：
  - “运行未完成”
  - “analysis run was cancelled while waiting for input”
  - “运行已取消”

### 4. 证据状态一致性

- `src/data_agent/analysis_agent/nodes.py`
  - evaluator 的 required evidence keys 使用实际 `EvidenceRef.claim_key`，不再使用规划器自由文本占位。
- `frontend/src/evidenceRoute.ts`、`App.tsx`
  - 只有存在实际 SQL、rows、trace、artifacts 或 evidence 时才显示证据完成。
  - 证据抽屉始终展示证据数量（包括 0）和无证据说明。
  - validated/pending/error 状态按真实结果映射。

### 5. 错误和歧义体验

- `frontend/src/api.ts`
  - FastAPI 422 的 detail 对象/数组会格式化为字段级中文错误，不再只显示 `Unprocessable Entity`。
  - 修复 prefixed `sha256:` schema fingerprint 的 SSE 合同校验误报（此前截图中的 `Runtime emitted an invalid event` 根因之一）。
- `src/data_agent/analysis_agent/planner.py`
  - 初次规划时确定性识别“需要聚合/比较但没有明确指标”的问题，在调用模型前返回澄清。
  - 模糊问题浏览器实测成功：`按商品类别分析一下` 会给出“订单/记录数量、金额合计、平均值”选项。
  - 显式指标和明细请求继续进入模型。
  - 浏览器回归发现 `统计订单表中的订单总数` 被误判；已补充 `总数/条数/笔数/件数` 为显式指标，并通过单元测试及浏览器回归。

## 数据源与浏览器验收上下文

- 数据目录：`/Users/yehj/project/olist`
- 已将目录内全部 9 个 CSV 导入数据源：`Olist 全量 CSV 20260808`
- 数据源 ID：`source-6ccd7e39837f4a4e`
- 共 9 张表、52 个逻辑字段。
- 旧关系推荐包含 9 个角色、10 条边，所有基数为 unknown 且存在环；旧版本曾错误允许通过。新校验器会阻断该图再次激活，直到补齐基数、人工复核及环路规则。
- 前端当前仍在 `http://127.0.0.1:5173/` 运行。
- 验收时前后端分别在 `127.0.0.1:8000` 和 `127.0.0.1:5173` 正常运行。
- 登录凭据由用户在当前 Codex 对话中提供，不写入仓库文档。

## 已完成测试

- 后端全量：`354 passed, 1 warning, 70 subtests passed in 32.17s`
  - 唯一 warning 为现有 Starlette TestClient deprecation。
- 前端全量：`17 files passed, 79 tests passed`
- 前端类型检查：`tsc -b` 成功。
- 前端生产构建：`vite build` 成功。
- `git diff --check`：通过。

## 已完成的续跑顺序（历史记录）

1. 重启本地后端（需要允许监听本机 8000 端口）：

   ```bash
   .venv/bin/uvicorn api.app:app --host 127.0.0.1 --port 8000 --env-file .env
   ```

2. 用内置浏览器重新登录或复用会话，选中 `Olist 全量 CSV 20260808`。
3. 浏览器提交 `统计订单表中的订单总数`：
   - 必须直接进入规划/执行，不应再次弹出指标澄清。
   - 轮询最长约 180 秒，每次不超过 30 秒。
   - 应无 `Runtime emitted an invalid event`。
   - 应产生可渲染的 table/analysis 结果，而不是泛化 `Request failed`。
4. 打开“查看证据路径”，核对：
   - 结果、计划步骤、证据数量和 validated/pending/error 状态一致。
   - 无实际 SQL/rows/trace/artifact/evidence 时不得显示“证据完成”。
5. 关系编辑器浏览器回归：
   - 对旧 unknown/cycle 推荐执行校验，应明确阻断激活并显示基数/人工复核/环路规则错误。
   - 至少编辑一条边的基数，确认其状态变为 reviewed/accepted/enabled。
6. 重新运行完整验证（最后补丁后必须执行）：

   ```bash
   .venv/bin/python -m pytest -q
   cd frontend && pnpm test -- --run
   cd frontend && pnpm build
   git diff --check
   ```

7. 审阅最终 diff；只处理本任务文件，不要修改或删除下列用户/环境文件。
8. 浏览器回归发现的问题均已补测试、修复并重复步骤 3-6。
9. 本文件已更新为“已完成”。用户当前没有要求提交或推送，不要擅自 commit/push。

## 当前工作区

当前有 44 个已跟踪文件被修改，并新增本进度文档和 3 个任务测试文件；修改尚未提交。主要文件：

- `frontend/src/App.tsx`
- `frontend/src/api.ts`
- `frontend/src/api.test.ts`
- `frontend/src/agentEventContract.test.ts`
- `frontend/src/evidenceRoute.ts`
- `frontend/src/evidenceRoute.test.ts`
- `frontend/src/relationships/RelationshipGraphEditor.tsx`
- `src/data_agent/analysis_agent/{composition,evaluator,nodes,planner,prompts,runtime}.py`
- `src/data_agent/relationships/validator.py`
- `src/data_agent/tools/{invoker,models}.py`
- 对应的 unit/integration tests。

必须保留并忽略这些无关未跟踪文件/目录：

- `.env.codex-backup-20260704204355`（可能含敏感信息，不要读取、打印或提交）
- `.impeccable/`
- `.pnpm-store/`
- `frontend/pnpm-workspace.yaml`
- `docs/superpowers/.DS_Store`

## 已知非阻断限制

- DeepSeek 的复杂跨表最终综合可能耗时约两分钟；运行期间前端持续显示计划和证据进度，并支持取消。
- Olist 商品类别使用数据集原始葡萄牙语字段值；回答会明确提示未做中文翻译。
