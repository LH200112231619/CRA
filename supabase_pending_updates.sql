-- =============================================================================
-- 核药CRA英文训练舱 - 每日「AI 生成 + 人工审核 + 自动入库」增量 SQL
-- 在 Supabase Dashboard -> SQL Editor 中整段执行即可（可重复执行，幂等）
--
-- 本脚本做四件事：
--   1. 新建待审核表 pending_updates（正式表结构保持不变）
--   2. 为原先没有正式表的两个库（核素 nuclide、专业词块 vocabulary）
--      补充两张正式归档表（仅“新增”，不修改任何既有表）
--   3. 配置 RLS：前端 anon key 对所有表只读；审核写操作全部走
--      SECURITY DEFINER 存储过程（RPC），前端无法直接写正式表
--   4. 创建审核 RPC：通过 / 拒绝 / 编辑保存 / 批量通过 / 批量拒绝
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. 待审核表 pending_updates
-- -----------------------------------------------------------------------------
create table if not exists public.pending_updates (
  id            serial primary key,
  library       text not null,        -- target / nuclide / vocabulary / interview / regulation / literature
  action        text not null default 'create',  -- create / update
  title         text,
  content_json  jsonb,
  source        text,
  status        text not null default 'pending', -- pending / approved / rejected
  created_at    timestamptz not null default now(),
  reviewed_at   timestamptz,
  reviewer_note text,
  -- 新增工具列：去重键（library + 来源URL 或 library + 标题 的哈希），
  -- 供后端脚本 upsert / 判重，同一来源不会反复进入审核队列
  dedup_key     text unique
);

-- 审核中心常用查询索引
create index if not exists idx_pending_status_library
  on public.pending_updates (status, library);
create index if not exists idx_pending_created
  on public.pending_updates (created_at desc);

-- -----------------------------------------------------------------------------
-- 2. 两张“新增”的正式归档表（既有 6 张正式表结构完全不动）
--    核素库、专业词块库此前只存在于前端 localStorage，
--    审核通过后需要服务端正式库来归档，故在此补建。
-- -----------------------------------------------------------------------------

-- 2.1 核素正式表
create table if not exists public.nuclides (
  id                   bigint generated always as identity primary key,
  nuclide              text not null unique,   -- 如 Ga-68、Lu-177
  half_life            text,
  decay_mode           text,
  emission_type        text,                    -- 诊断 / 治疗 / 诊疗一体
  common_targets       text,                    -- 数组以“, ”拼接
  representative_drugs text,
  production_method    text,
  monitoring_points    text,                    -- CRA 监查要点，以“; ”拼接
  "references"         jsonb,                   -- 原始引用数组 [{t,u}]
  content_json         jsonb,                   -- 完整原始内容
  created_at           timestamptz not null default now()
);

-- 2.2 专业词块正式表
create table if not exists public.vocab_chunks (
  id           bigint generated always as identity primary key,
  en           text not null unique,            -- 英文词块
  ipa          text,                            -- 音标
  zh           text,                            -- 中文释义
  cat          text,                            -- 分类
  lv           text,                            -- 难度
  ex           text,                            -- 英文例句
  ex_zh        text,                            -- 例句翻译
  target       text,                            -- 关联靶点
  ptype        text,                            -- 关联项目类型
  content_json jsonb,
  created_at   timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- 3. RLS 行级安全
--    前端 anon key：对 pending_updates / 正式表均“只读”；
--    所有写操作（审核通过写正式表、拒绝、改草稿）只能通过下面的
--    SECURITY DEFINER RPC 完成，正式表不向 anon 开放任何写策略。
-- -----------------------------------------------------------------------------
alter table public.pending_updates enable row level security;
alter table public.nuclides        enable row level security;
alter table public.vocab_chunks    enable row level security;

-- 3.1 待审核表：anon / authenticated 只读
drop policy if exists pending_read on public.pending_updates;
create policy pending_read on public.pending_updates
  for select using (true);

-- 3.2 两张新正式表：公开只读（与既有正式表口径一致）
drop policy if exists public_read_nuclides on public.nuclides;
create policy public_read_nuclides on public.nuclides
  for select using (true);

drop policy if exists public_read_vocab_chunks on public.vocab_chunks;
create policy public_read_vocab_chunks on public.vocab_chunks
  for select using (true);

-- 注意：此处刻意“不创建”任何 anon 的 insert/update/delete 策略。
-- service_role（后端 Python 脚本）默认绕过 RLS，负责写入 pending_updates。

-- -----------------------------------------------------------------------------
-- 4. 审核 RPC（SECURITY DEFINER，以表属主身份执行，可绕过 RLS 写正式表）
-- -----------------------------------------------------------------------------

-- 4.0 内部函数：把一条待审核草稿按 library 写入对应正式表
create or replace function public._archive_pending(p public.pending_updates)
returns void
language plpgsql
set search_path = public
as $$
declare
  c jsonb := p.content_json;
  v_url text;
begin
  if c is null then
    raise exception 'content_json 为空，无法入库';
  end if;

  -- 统一取首个可用参考链接（兼容多种字段命名）
  v_url := nullif(coalesce(
    c->>'url', c->>'sourceUrl',
    c->'keyReferences'->0->>'u',
    c->'references'->0->>'u'
  ),'');

  -- ---------------- 靶点 -> targets ----------------
  if p.library = 'target' then
    insert into public.targets (name, full_name, category, indications, notes, url)
    select
      c->>'name',
      c->>'fullName',
      coalesce(nullif(c->>'category',''), '核药靶点'),
      case when jsonb_typeof(c->'indications') = 'array'
           then (select string_agg(x, ', ')
                   from jsonb_array_elements_text(c->'indications') t(x))
           else c->>'indications' end,
      concat('AI 生成，已审核。',
        '生物学功能：', coalesce(c->>'pathway',''),
        '；代表药物：', coalesce((select string_agg(x, '; ')
                   from jsonb_array_elements_text(c->'representativeDrugs') t(x)), ''),
        '；CRA监查要点：', coalesce((select string_agg(x, '; ')
                   from jsonb_array_elements_text(c->'craMonitoringPoints') t(x)), '')),
      v_url
    on conflict (name) do update set
      full_name  = excluded.full_name,
      category    = excluded.category,
      indications = excluded.indications,
      notes       = excluded.notes,
      url         = coalesce(excluded.url, public.targets.url);

  -- ---------------- 核素 -> nuclides ----------------
  elsif p.library = 'nuclide' then
    insert into public.nuclides
      (nuclide, half_life, decay_mode, emission_type, common_targets,
       representative_drugs, production_method, monitoring_points, "references", content_json)
    select
      c->>'nuclide',
      c->>'halfLife',
      c->>'decayMode',
      c->>'emissionType',
      (select string_agg(x, ', ') from jsonb_array_elements_text(c->'commonTargets') t(x)),
      (select string_agg(x, '; ') from jsonb_array_elements_text(c->'representativeDrugs') t(x)),
      c->>'productionMethod',
      (select string_agg(x, '; ') from jsonb_array_elements_text(c->'craMonitoringPoints') t(x)),
      coalesce(c->'references', '[]'::jsonb),
      c
    on conflict (nuclide) do update set
      half_life            = excluded.half_life,
      decay_mode           = excluded.decay_mode,
      emission_type        = excluded.emission_type,
      common_targets       = excluded.common_targets,
      representative_drugs = excluded.representative_drugs,
      production_method    = excluded.production_method,
      monitoring_points    = excluded.monitoring_points,
      "references"         = excluded."references",
      content_json         = excluded.content_json;

  -- ---------------- 专业词块 -> vocab_chunks ----------------
  elsif p.library = 'vocabulary' then
    insert into public.vocab_chunks
      (en, ipa, zh, cat, lv, ex, ex_zh, target, ptype, content_json)
    select
      c->>'en', c->>'ipa', c->>'zh', c->>'cat', c->>'lv',
      c->>'ex', c->>'exZh',
      coalesce(c->>'target',''), coalesce(c->>'ptype',''), c
    on conflict (en) do update set
      ipa          = excluded.ipa,
      zh           = excluded.zh,
      cat          = excluded.cat,
      lv           = excluded.lv,
      ex           = excluded.ex,
      ex_zh        = excluded.ex_zh,
      target       = excluded.target,
      ptype        = excluded.ptype,
      content_json = excluded.content_json;

  -- ---------------- 外企面试题 -> interview_questions ----------------
  elsif p.library = 'interview' then
    insert into public.interview_questions
      (question, category, difficulty, answer, url)
    select
      c->>'question',
      c->>'category',
      c->>'difficulty',
      coalesce(c->>'referenceAnswer', c->>'answer'),
      v_url
    on conflict (url) do update set
      question   = excluded.question,
      category   = excluded.category,
      difficulty = excluded.difficulty,
      answer     = excluded.answer;

  -- ---------------- 法规解读 -> regulations ----------------
  elsif p.library = 'regulation' then
    insert into public.regulations
      (title, url, source, region, summary, status, published_at)
    select
      c->>'title',
      v_url,
      coalesce(c->>'org', c->>'source'),
      c->>'region',
      coalesce(c->>'summary', c->>'clauseEN', c->>'craTakeaway'),
      '现行',
      coalesce(c->>'issue', c->>'publishedAt', c->>'date')
    on conflict (url) do update set
      title        = excluded.title,
      source       = excluded.source,
      region       = excluded.region,
      summary      = excluded.summary,
      status       = excluded.status,
      published_at = excluded.published_at;

  -- ---------------- 文献摘要 -> literature ----------------
  elsif p.library = 'literature' then
    insert into public.literature
      (pmid, title, journal, pub_date, url, target_name, abstract)
    select
      nullif(c->>'pmid',''),
      c->>'title',
      c->>'journal',
      coalesce(c->>'pubDate', c->>'date'),
      coalesce(v_url,
        case when nullif(c->>'pmid','') is not null
             then concat('https://pubmed.ncbi.nlm.nih.gov/', c->>'pmid', '/') end),
      coalesce(c->>'target', c->>'targetName'),
      concat(coalesce(c->>'abstract',''),
        case when nullif(c->>'craTakeaway','') is not null
             then concat(' 【CRA启示】', c->>'craTakeaway') else '' end)
    on conflict (url) do update set
      pmid        = excluded.pmid,
      journal     = excluded.journal,
      pub_date    = excluded.pub_date,
      target_name = excluded.target_name,
      abstract    = excluded.abstract;
  else
    raise exception '未知 library: %', p.library;
  end if;
end $$;

-- 4.1 通过草稿（可同时传入人工编辑后的标题内容；p_note 为审核备注，可空）
create or replace function public.approve_pending(
  p_id bigint,
  p_content jsonb default null,
  p_note text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  p public.pending_updates;
begin
  select * into p from public.pending_updates where id = p_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'error', '草稿不存在');
  end if;
  if p.status <> 'pending' then
    return jsonb_build_object('ok', false, 'error', '该草稿已处理，请勿重复操作');
  end if;

  -- 采用人工编辑后的内容（如有）
  if p_content is not null then
    p.content_json := p_content;
  end if;

  perform public._archive_pending(p);

  update public.pending_updates
    set status = 'approved',
        reviewed_at = now(),
        reviewer_note = coalesce(nullif(p_note,''), reviewer_note)
    where id = p_id;

  return jsonb_build_object('ok', true, 'id', p_id, 'library', p.library);
exception
  when others then
    raise warning 'approve_pending(id=%) 失败: % [%]', p_id, SQLERRM, SQLSTATE;
    return jsonb_build_object('ok', false, 'error', SQLERRM, 'state', SQLSTATE);
end $$;

-- 4.2 拒绝草稿（p_note 为拒绝理由）
create or replace function public.reject_pending(
  p_id bigint,
  p_note text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  p public.pending_updates;
begin
  select * into p from public.pending_updates where id = p_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'error', '草稿不存在');
  end if;
  if p.status <> 'pending' then
    return jsonb_build_object('ok', false, 'error', '该草稿已处理，请勿重复操作');
  end if;

  update public.pending_updates
    set status = 'rejected',
        reviewed_at = now(),
        reviewer_note = p_note
    where id = p_id;

  return jsonb_build_object('ok', true, 'id', p_id, 'library', p.library);
exception
  when others then
    raise warning 'reject_pending(id=%) 失败: % [%]', p_id, SQLERRM, SQLSTATE;
    return jsonb_build_object('ok', false, 'error', SQLERRM, 'state', SQLSTATE);
end $$;

-- 4.3 保存人工对草稿的编辑（仅 pending 状态可改：标题 + content_json）
create or replace function public.update_pending_content(
  p_id bigint,
  p_title text,
  p_content jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_status text;
begin
  select status into v_status from public.pending_updates where id = p_id;
  if not found then
    return jsonb_build_object('ok', false, 'error', '草稿不存在');
  end if;
  if v_status <> 'pending' then
    return jsonb_build_object('ok', false, 'error', '已处理的草稿不能再编辑');
  end if;

  update public.pending_updates
    set title = p_title, content_json = p_content
    where id = p_id;

  return jsonb_build_object('ok', true, 'id', p_id);
exception
  when others then
    return jsonb_build_object('ok', false, 'error', SQLERRM, 'state', SQLSTATE);
end $$;

-- 4.4 批量通过
create or replace function public.approve_pending_batch(p_ids bigint[])
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id bigint;
  r jsonb;
  n_ok int := 0;
  n_fail int := 0;
begin
  foreach v_id in array p_ids loop
    r := public.approve_pending(v_id, null, null);
    if (r->>'ok') = 'true' then n_ok := n_ok + 1; else n_fail := n_fail + 1; end if;
  end loop;
  return jsonb_build_object('ok', true, 'approved', n_ok, 'failed', n_fail);
end $$;

-- 4.5 批量拒绝
create or replace function public.reject_pending_batch(
  p_ids bigint[],
  p_note text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id bigint;
  r jsonb;
  n_ok int := 0;
  n_fail int := 0;
begin
  foreach v_id in array p_ids loop
    r := public.reject_pending(v_id, p_note);
    if (r->>'ok') = 'true' then n_ok := n_ok + 1; else n_fail := n_fail + 1; end if;
  end loop;
  return jsonb_build_object('ok', true, 'rejected', n_ok, 'failed', n_fail);
end $$;

-- -----------------------------------------------------------------------------
-- 5. 授权：前端 anon / authenticated 只能“调用 RPC”和“读取”，不能直接写表
-- -----------------------------------------------------------------------------
grant execute on function public.approve_pending(bigint, jsonb, text)
  to anon, authenticated;
grant execute on function public.reject_pending(bigint, text)
  to anon, authenticated;
grant execute on function public.update_pending_content(bigint, text, jsonb)
  to anon, authenticated;
grant execute on function public.approve_pending_batch(bigint[])
  to anon, authenticated;
grant execute on function public.reject_pending_batch(bigint[], text)
  to anon, authenticated;

-- _archive_pending 为内部函数，不授予前端
revoke all on function public._archive_pending(public.pending_updates) from public;

-- =============================================================================
-- 完成。执行后可在 Table Editor 看到 pending_updates、nuclides、vocab_chunks。
-- =============================================================================
