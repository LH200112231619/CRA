# 每日「AI 生成 + 人工审核 + 自动入库」部署说明（增量）

本次为增量升级，不改动原有功能与既有正式表结构。**每日草稿由豆包工作（Doubao Work）定时任务在会话内联网检索并生成，直接写入 Supabase `pending_updates`；不再依赖火山方舟/GitHub Actions 调用 LLM。**

## 一、已完成的部署

### 1. Supabase 增量 SQL（已执行）
`supabase_pending_updates.sql`（幂等，可重复执行）已在 Supabase SQL Editor 成功执行，包含：
- 新表 `pending_updates`（AI 草稿队列，含去重列 `dedup_key`，字段：id / library / action / title / content_json / source / status / created_at / reviewed_at / reviewer_note / dedup_key）
- 两张新增正式归档表：`nuclides`（核素）、`vocab_chunks`（专业词块）；原有 6 张正式表未改动
- 内部函数 `_archive_pending`：审核通过时按 library 把 content_json 映射并 upsert 进对应正式表
- 5 个公开审核 RPC：`approve_pending` / `reject_pending` /
  `update_pending_content` / `approve_pending_batch` / `reject_pending_batch`

> 安全：RLS 配置为前端 anon key 对所有表**只读**（无任何 anon 写策略）；
> 审核写操作全部通过 SECURITY DEFINER RPC 在服务端完成，前端无法直接写正式表。

### 2. 前端「审核中心」（已上线）
- 位置：左侧导航 **学习中心 → 审核中心**
- 功能：待审核/已通过/已拒绝状态页签；6 类库筛选；标题搜索（防抖）；日期排序切换；分页（每页 8 条，Range + count=exact）；勾选/全选、批量通过/拒绝；行内快速通过/拒绝；详情弹窗可查看并编辑标题与 content_json（JSON 校验）后再通过；每次操作 Toast 提示
- 通过：RPC 把内容写入对应正式表并置 status=approved；核素/词块同时合并进本地练习模块
- 拒绝：填写 reviewer_note，status=rejected
- 空队列文案：「暂无待审核内容，明天再来看看」

### 3. 每日定时任务（已创建）
- 豆包工作定时任务「核药CRA每日AI草稿」，每天 **06:20（北京时间）** 运行
- 每日数量：靶点 1 / 核素 1 / 专业词块 10 / 外企面试题 5（含英文 STAR 参考答案）/ 法规解读 2 / 文献摘要 5（PubMed 真实文献 + CRA 启示），合计 24 条
- 任务依赖浏览器中 Supabase Dashboard 的登录会话；若登录态丢失会提示重新登录
- 火山方舟路线的 GitHub Actions 工作流 `llm-daily-update.yml` 已**停用**（Actions 页面手动 Enable 才会再跑）；`scripts/llm_daily_update.py` 仅留作参考

## 二、安全须知（重要）
1. 前端**只能使用 anon public key**；若在网站配置中填入 service_role key，页面顶部会弹出红色警告，提示立即前往 Supabase 重置。
2. `service_role` key 只允许出现在 GitHub Secrets / 服务端，**绝对不能**填进网站前端。
3. 若曾误填 service_role：到 Supabase **Settings → API** 轮换（roll/regenerate）service_role key；把 anon/public key 填入网站；并同步更新 GitHub Secret `SUPABASE_SERVICE_KEY`。
4. 所有 AI 生成内容必须经人工审核后才能进入正式库。

## 三、内容规则（定时任务内置）
- 每条草稿 content_json 强制包含 `aiNote = "AI 生成，待审核"`；
- 禁止编造来源与链接：靶点/核素/文献/法规必须基于真实可访问来源（PubMed、FDA/EMA/NMPA 等），找不到可靠来源一律写「待补充」；
- 文献照录原始题录与摘要，仅补充 craTakeaway（CRA 启示），不改原文；
- 每行设置 `dedup_key` 并 `on conflict do nothing`，幂等且不与历史草稿/正式库/预置内容重复；
- 单个库失败自动隔离，不影响其他库。

## 数据流总览
```
豆包工作定时任务（每天 06:20 北京时间）
   ├─ 联网检索（PubMed / FDA / EMA / NMPA 等）
   ├─ 生成严格 JSON（含真实来源；无可靠来源写“待补充”；标注 AI 生成待审核）
   └─ 经 Supabase Dashboard 写入 pending_updates(status=pending, dedup_key 判重)
                 │
网站「审核中心」(anon key, 只读 + RPC)
       ├─ 通过（可先编辑）→ RPC 写正式表 targets/nuclides/vocab_chunks/
       │                    interview_questions/regulations/literature
       └─拒绝 → reviewer_note + status=rejected
```
