# 客户声音 MVP 任务清单

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

## 任务 5：纪要生成改造（完成）
- [x] 确认 `transcript_for_agent` 用合并后逐字稿做唯一输入
- [x] skill 输出包含行动项、风险、结论、精确数字（`llm_report_prompt` 统一要求 6/7）
- Commit：`66165c7` / `48aaba6` / `ec63140`

## 任务 6：导出（完成）
- [x] 逐字稿：MD/TXT/SRT
- [x] 纪要：MD
- [x] 文件放在项目目录下（`exports/`）
- Commit：`7b829b2 feat: add customer and file manifest to export header`

## 任务 7：测试与验收（完成）
- [x] 单元测试：数据模型、合并、导出（test_customers / test_segment_source / test_export / test_voiceprints / test_report_context）
- [x] 集成测试：主流程端到端（test_integration — 客户→多文件→命名→导出→声纹回写→纪要 prompt）
- [x] 用 Voi 资料做对照测试（test_voi_comparison — D 盘真实 AliMeeting 项目）
- Commit：本轮完成，45 tests passed
