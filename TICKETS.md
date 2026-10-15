# 多听工作台 MVP 任务清单

依据 `SPEC.md`，任务按依赖顺序排列；每张票完成后单独 commit。

## 任务 1：数据模型（完成）
- [x] `customers.py`：客户、人物、声纹向量列表
- [x] `data/customers.json` 存储
- [x] `test_customers.py` 覆盖创建、去重、添加人物
- Commit：`e6bd9dc feat: add customer archive model with JSON storage and tests`

## 任务 2：导入流程改造（完成）
- [x] `app.py`：`upload_project` 接收 `customer_id` 或 `new_customer`
- [x] 自动生成 Thread 默认标题
- [x] `index.html`：导入弹窗加入客户下拉框和新客户输入框
- [x] `static/app.js`：加载客户选项、提交客户字段、确认人名后自动写回客户档案
- [x] `/api/customers` GET/POST、`/api/projects/<id>/save-speakers-to-customer`
- [x] `test_customer_api.py` 覆盖 API
- Commit：`33d2148 feat: add customer archive integration and API routes`

## 任务 3：多文件逐字稿合并（完成）
- [x] 每个文件单独说话人分离（`media_to_wav` concat 到同一时间轴后一次分离）
- [x] 按上传顺序合并时间线，时间戳累计偏移（`file_manifest` 记录每段起止）
- [x] 跨文件人名合并去重（`speaker_map` 统一映射，一次分离天然同一 speaker ID）
- Commit：`3dd366e feat: group transcript by source file with segment_source_at`

## 任务 4：声纹匹配与回写（完成）
- [x] 从客户档案读已有声纹向量（`customers.read_customers` + `voiceprints.match_speaker`）
- [x] 相似度 >=0.80 自动确认，0.65-0.80 人工确认，<0.65 新建占位
- [x] 用户确认后把新声纹写入档案（`voiceprint-save` API + 确认人名时自动触发）
- Commit：`2d033d6` / `4348a66` / `4bd6c02` / `54e5a2e`

## 任务 5：纪要生成改造（待办）
- [ ] 确认 `transcript_for_agent` 用合并后逐字稿做唯一输入
- [ ] skill 输出包含行动项、风险、结论、精确数字
- 说明：现有 `llm_report_prompt` 已有统一要求，需要对照 Voi 纪要做样本评测

## 任务 6：导出（待办）
- [ ] 逐字稿：MD/TXT/SRT
- [ ] 纪要：MD
- [ ] 文件放在项目目录下
- 说明：`render_exports` 已有基础，需要加客户和 Thread 元信息

## 任务 7：测试与验收（待办）
- [ ] 单元测试：数据模型、合并、导出
- [ ] 集成测试：主流程端到端
- [ ] 用 Voi 资料做对照测试