# 每日「AI 生成 + 人工审核 + 自动入库」部署说明（增量）

本次为增量升级，不改动原有功能与正式表结构。按以下 4 步操作。

## 第 1 步：在 Supabase 执行增量 SQL（约 1 分钟）
1. 打开 Supabase 项目 → 左侧 **SQL Editor** → **New query**。
2. 打开本仓库 `supabase_pending_updates.sql`，全文复制粘贴 → **Run**，提示 Success。
3. 执行后会得到：
   - 新表 `pending_updates`（AI 草稿队列，含去重列 `dedup_key`）
   - 两张新增正式归档表：`nuclides`（核素）、`vocab_chunks`（专业词块）
   - 5 个审核 RPC：`approve_pending` / `reject_pending` /
     `update_pending_content` / `approve_pending_batch` / `reject_pending_batch`
4. 验证（可选）：
   ```sql
   select count(*) from pending_updates;
   ```

> 安全说明：RLS 已配置为前端 anon key 对所有表**只读**；审核写操作全部
> 通过 SECURITY DEFINER RPC 在服务端完成，前端无法直接写正式表。

## 第 2 步：添加 2 个新的 GitHub Secrets
仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名 | 值 | 说明 |
|---|---|---|
| `ARK_API_KEY` | 火山方舟 API Key | 火山引擎控制台 → 方舟 → API Key 管理获取 |
| `ARK_MODEL` | 模型/接入点 ID | 如 `doubao-seed-1-6-250615`，或你创建的推理接入点 `ep-xxxx` |

原有的 `SUPABASE_URL`、`SUPABASE_SERVICE_KEY`、`NCBI_EMAIL` 保持不变。

> `SUPABASE_SERVICE_KEY`（service_role）只能放在 GitHub Secrets，
> **绝对不能**填进网站前端；前端只用 anon public key。

## 第 3 步：确认文件已上传
仓库应包含（新增/更新标 ★）：
```
├─ index.html                                    ★ 更新（新增审核中心）
├─ requirements.txt                              ★ 更新（含全部依赖）
├─ supabase_pending_updates.sql                  ★ 新增
├─ scripts/
│   ├─ llm_daily_update.py                       ★ 新增
│   └─ (原有 4 个抓取脚本不变)
└─ .github/workflows/
    ├─ llm-daily-update.yml                      ★ 新增（每天 UTC 22:00）
    └─ (原有 daily-update.yml 不变)
```

## 第 4 步：手动触发并验证
1. GitHub 仓库 **Actions** → 左侧 **LLM Daily Drafts** → **Run workflow**。
2. 等待 2–4 分钟，点进运行记录，日志末尾会打印六类库的生成报告：
   ```
   target       0/1 ...      （0 表示该来源已存在或被去重）
   nuclide      1
   vocabulary   10
   interview    5
   regulation   2
   literature   5
   ```
3. 打开网站 → **学习中心 → 审核中心**：
   - 按库筛选、搜索标题、按日期排序、翻页；
   - 点标题查看/编辑完整 `content_json`；
   - **通过**：自动写入对应正式表（status=approved）；
   - **拒绝**：填写理由（status=rejected）；
   - 勾选多条后可**批量通过 / 批量拒绝**。
4. 空队列时显示：「暂无待审核内容，明天再来看看」。

## 数据流总览
```
GitHub Actions(UTC 22:00)
   └─ llm_daily_update.py
        ├─ 火山方舟 LLM 生成严格 JSON（含真实来源；无可靠来源写“待补充”）
        └─ service_role 写入 pending_updates(status=pending)
                 │
网站「审核中心」(anon key, 只读 + RPC)
       ├─ 通过 → RPC 写正式表(targets/nuclides/vocab_chunks/
       │                      interview_questions/regulations/literature)
       └─ 拒绝 → reviewer_note + status=rejected
```

## 内容规则（已内置在脚本中）
- 每条草稿强制标注 `aiNote = "AI 生成，待审核"`；
- 禁止编造来源与链接，找不到来源一律写「待补充」；
- 文献先抓真实 PubMed 记录，LLM 只补充 CRA 启示，不改原始题录；
- 单个库失败自动隔离，不影响其他库与整个工作流。
