#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核药CRA英文训练舱 - 每日 LLM 草稿生成脚本
=====================================================================
流程：豆包/火山方舟(Ark) API 生成六类草稿 -> 写入 Supabase
      pending_updates 表(status=pending) -> 用户在网站「审核中心」
      人工审核 -> 通过后由数据库 RPC 自动写入对应正式表。

六类库与每日草稿数量：
  target      靶点        1 个（新靶点或更新）
  nuclide     核素        1 个
  vocabulary  专业词块    10 个
  interview   外企面试题  5 道（含参考答案）
  regulation  法规解读    2 条
  literature  文献摘要    5 条（先抓 PubMed，再让 LLM 写 CRA 启示）

环境变量（由 GitHub Actions 从 Secrets 注入）：
  ARK_API_KEY            火山方舟 API Key
  ARK_MODEL              模型/接入点 ID（如 doubao-seed-1-6-250615 或 ep-xxxx）
  SUPABASE_URL           Supabase 项目 URL
  SUPABASE_SERVICE_KEY   Supabase service_role key（仅后端使用，绝不能进前端）
  NCBI_EMAIL             可选，访问 PubMed 时的联系邮箱

设计约束：
  * 单个库失败 try/except 隔离，不影响其他库，脚本始终以 0 退出
  * 所有内容强制标注 aiNote="AI 生成，待审核"
  * 禁止编造来源：无可靠来源时 source="待补充"、sourceUrl=""
  * 通过 dedup_key 判重 + upsert，重复运行/跨天不产生重复草稿
"""

import os
import sys
import json
import time
import hashlib
import datetime
import re

import requests
from supabase import create_client

# =====================================================================
# 0. 环境变量与客户端初始化
# =====================================================================
ARK_API_KEY = os.environ.get("ARK_API_KEY", "")
ARK_MODEL = os.environ.get("ARK_MODEL", "") or "doubao-seed-1-6-250615"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
NCBI_EMAIL = os.environ.get("NCBI_EMAIL", "")

ARK_CHAT_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("[LLM] 缺少 SUPABASE_URL / SUPABASE_SERVICE_KEY，脚本退出")
    sys.exit(0)  # 退出码 0，不拖垮 workflow

if not ARK_API_KEY:
    print("[LLM] 缺少 ARK_API_KEY，脚本退出")
    sys.exit(0)

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

AI_NOTE = "AI 生成，待审核"

# =====================================================================
# 1. 火山方舟调用 & 严格 JSON 解析
# =====================================================================
def call_ark(system_prompt, user_prompt, max_tokens=4096, retries=2):
    """调用 Ark Chat Completions，返回消息文本。失败返回 None。"""
    headers = {
        "Authorization": "Bearer " + ARK_API_KEY,
        "Content-Type": "Application/json",
    }
    base_body = {
        "model": ARK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.5,
        "max_tokens": max_tokens,
    }

    for attempt in range(retries + 1):
        # 多数豆包模型支持 response_format=json_object，保证输出合法 JSON
        body = dict(base_body)
        body["response_format"] = {"type": "json_object"}
        try:
            resp = requests.post(
                ARK_CHAT_URL, headers=headers, json=body, timeout=180
            )
            # 个别模型/接入点不支持 response_format，去掉该参数重试
            if resp.status_code == 400 and "response_format" in resp.text:
                body.pop("response_format", None)
                resp = requests.post(
                    ARK_CHAT_URL, headers=headers, json=body, timeout=180
                )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            print(f"[LLM] Ark 调用失败(第{attempt + 1}次): {exc}")
            time.sleep(2 * (attempt + 1))
    return None


def extract_json(text):
    """从模型输出中提取 JSON（兼容 ```json 代码块包裹、前后多余文字）。"""
    if not text:
        return None
    t = text.strip()
    # 去掉 markdown 代码块围栏
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    # 直接解析
    try:
        return json.loads(t)
    except Exception:
        pass
    # 退化：截取第一个 { 到最后一个 }（或 [ ]）
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None


def extract_items(parsed, key_candidates=("items", "data", "list", "results")):
    """
    归一化模型输出：
      - 数组类任务：模型返回 {"items":[...]}，取出列表
      - 单对象任务：直接返回该对象
    """
    if parsed is None:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for k in key_candidates:
            if isinstance(parsed.get(k), list):
                return parsed[k]
        # 没有包装数组键时，视为单个对象
        return [parsed]
    return []


# =====================================================================
# 2. 通用 system prompt（严格 JSON + 来源纪律 + AI 标注）
# =====================================================================
SYSTEM_RULES = """You are a senior clinical research educator specializing in \
radiopharmaceutical / nuclear medicine CRA (Clinical Research Associate) \
English training for Chinese learners.

Strict output rules:
1. Output ONE valid JSON object only. No markdown, no commentary outside JSON.
2. For list tasks, put all records under the key "items" as a JSON array.
3. Every record MUST contain the field "aiNote" with the exact value \
"AI 生成，待审核".
4. Sources: cite only REAL, verifiable sources with a working URL \
(Supabase formal pages, PubMed, ICH, FDA, EMA, NMPA, IAEA, EANM, SNMMI, \
company career pages, etc.). NEVER invent or guess a source or URL.
5. If you cannot verify a source for a record, set "source" to "待补充" and \
"sourceUrl" to "". Do not fabricate.
6. English content must be natural, professional, interview-grade. \
Chinese explanations must be accurate.
7. Use the exact field names requested in the user instruction.
"""


def stamp(item):
    """强制给单条草稿加 AI 标注并规范来源字段。"""
    if not isinstance(item, dict):
        return item
    item.setdefault("aiNote", AI_NOTE)
    item["aiGenerated"] = True
    src = str(item.get("source", "")).strip()
    url = str(item.get("sourceUrl", item.get("url", ""))).strip()
    if not src:
        item["source"] = "待补充"
    if not url or url.lower() in ("待补充", "none", "null"):
        item["sourceUrl"] = ""
    return item


# =====================================================================
# 3. 各库任务定义（prompt 与每日数量）
# =====================================================================

# 3.1 靶点（1 个；要求生成库中尚不存在的新靶点）
EXISTING_TARGETS = (
    "PSMA, CAIX, CD73, Nectin-4, SSTR2, FAP, GRPR, CXCR4, HER2, Trop-2, "
    "KLK3, STEAP1, CLDN18.2, B7H3, CCK2R, GPC3, DLL3, MUC1, EGFR, EpCAM, "
    "Integrin αvβ3, NTSR1, MC1R, CD38, CD20, PD-L1, VEGFR, FGFR, TIGIT"
)

PROMPT_TARGET = """Generate ONE NEW radiopharmaceutical target that is NOT in \
this existing list: {existing}.
Pick a genuinely researched target (e.g. GPRC5D, CD45, CD33, Mesothelin, \
GCC/GUCY2C, CXCR5, LAT1, SSTR5, NK1R, EphA2, etc.) with real radiotracer \
literature.

Return JSON:
{{
  "items": [
    {{
      "name": "abbreviation, e.g. GPRC5D",
      "fullName": "full English name",
      "category": "成像靶点 / 治疗靶点 / 成像/治疗靶点",
      "pathway": "biological function and why it suits radiopharmaceuticals (2-4 English sentences)",
      "indications": ["indications in English"],
      "diagnosticIsotopes": ["e.g. Ga-68"],
      "therapeuticIsotopes": ["e.g. Lu-177"],
      "representativeDrugs": ["real drug / tracer names"],
      "trialPhase": "approved / Phase III / Phase II / Phase I / preclinical",
      "craMonitoringPoints": ["3-6 CRA monitoring points in Chinese or English"],
      "keyReferences": [{{"t": "real source title", "u": "real URL (PubMed/FDA/company)"}}],
      "source": "verifiable source name",
      "sourceUrl": "real URL or empty string",
      "aiNote": "AI 生成，待审核"
    }}
  ]
}}""".format(existing=EXISTING_TARGETS)

# 3.2 核素（1 个；库中尚不存在的新核素）
EXISTING_NUCLIDES = (
    "Ga-68, F-18, Cu-64, Zr-89, C-11, N-13, O-15, Rb-82, Tc-99m, In-111, "
    "I-123, I-131, Lu-177, Y-90, Re-188, Sm-153, Ho-166, Ra-223, Ac-225, "
    "Th-227, Tb-161, Sc-44, Sc-47, Pb-212, At-211"
)

PROMPT_NUCLIDE = """Generate ONE NEW medical radionuclide that is NOT in this \
existing list: {existing}.
Choose a real, clinically used or trialed nuclide (e.g. Bi-213, Pb-203, Pb-211, \
Co-55, Mn-52, Y-86, Ac-227, Re-186, Sr-89, Sn-117m, Lu-177m? etc.).

Return JSON:
{{
  "items": [
    {{
      "nuclide": "e.g. Bi-213",
      "halfLife": "half-life with unit, e.g. 45.6 分钟",
      "decayMode": "decay type: β+ / β- / α / γ / EC, with percentages if known",
      "emissionType": "诊断 / 治疗 / 诊疗一体",
      "commonTargets": ["targets it is commonly labeled to"],
      "representativeDrugs": ["real drug names"],
      "productionMethod": "generator / cyclotron / reactor, with route if known",
      "craMonitoringPoints": ["4-6 CRA monitoring points: radiation safety, dose calibration, decay correction, transport window, waste, dosimeter"],
      "references": [{{"t": "real source", "u": "real URL (IAEA/SNMMI/EANM/PubMed)"}}],
      "source": "verifiable source name",
      "sourceUrl": "real URL or empty string",
      "aiNote": "AI 生成，待审核"
    }}
  ]
}}""".format(existing=EXISTING_NUCLIDES)

# 3.3 专业词块（10 个）
PROMPT_VOCAB = """Generate 10 NEW professional English word chunks for a \
radiopharmaceutical CRA. Cover a mix of these categories: GCP 与法规, 监查流程, \
核药专业, 影像与剂量学, 安全与 PD/AE, 数据管理与 SDV, 系统与工具, 沟通与软技能.
Prefer high-frequency terms a CRA actually uses at site visits and in foreign \
company interviews; do NOT duplicate obvious basics like "informed consent".

Return JSON:
{{
  "items": [
    {{
      "en": "English term",
      "ipa": "IPA phonetic, e.g. /doʊˈsɪmətri/",
      "zh": "中文释义",
      "ex": "one natural English example sentence",
      "exZh": "例句中文翻译",
      "cat": "one of the listed categories",
      "lv": "初级 / 中级 / 高级",
      "target": "related target abbreviation or empty string",
      "ptype": "related project type or empty string",
      "source": "verifiable source (e.g. ICH E6(R3)) or 待补充",
      "sourceUrl": "real URL or empty string",
      "aiNote": "AI 生成，待审核"
    }}
  ]
}}"""

# 3.4 外企面试题（5 道，含参考答案）
PROMPT_INTERVIEW = """Generate 5 foreign-company (CRO / radiopharmaceutical \
sponsor) CRA interview questions. Vary categories: self-intro, motivation, \
monitoring workflow, nuclear medicine specifics, behavioral, situational.

Return JSON:
{{
  "items": [
    {{
      "question": "English question",
      "questionCN": "中文翻译",
      "category": "自我介绍 / 动机 / 监查流程 / 核药 / 行为 / 情景 / 薪资 / 反问",
      "company": "the company type/name this question is typical of, e.g. IQVIA / PPD / Novartis",
      "difficulty": "初级 / 中级 / 高级",
      "referenceAnswer": "model answer in English, 150-300 words, STAR structure where behavioral",
      "keyPoints": ["3-5 answer key points"],
      "commonMistakes": "common wrong-answer pitfalls (Chinese or English)",
      "followUps": ["2-3 follow-up questions"],
      "source": "Glassdoor / Blind / Reddit / craresources / 猎头内推 / 待补充",
      "sourceUrl": "real URL or empty string",
      "aiNote": "AI 生成，待审核"
    }}
  ]
}}"""

# 3.5 法规解读（2 条）
PROMPT_REGULATION = """Generate 2 regulation / guidance interpretations \
relevant to radiopharmaceutical clinical research. Prefer recent or core \
documents (ICH E6(R3), FDA dosimetry optimization draft 2025, EMA clinical \
evaluation guidance, NMPA 放射性体内诊断药物指导原则, IAEA safety standards, \
EANM/SNMMI procedure guidelines). Only use documents that genuinely exist.

Return JSON:
{{
  "items": [
    {{
      "title": "English document title",
      "titleCN": "中文名称",
      "org": "issuing body: FDA / EMA / NMPA / PMDA / ICH / IAEA / EANM / SNMMI",
      "region": "美国 / 欧盟 / 中国 / 日本 / 国际",
      "issue": "issue date, e.g. 2025-03",
      "effective": "effective date or empty string",
      "cat": "GCP / 核药 / 数据完整性 / 辐射防护 / 伦理",
      "clauseEN": "key clause excerpt in English (verbatim where possible)",
      "clauseCN": "条款中文翻译",
      "craTakeaway": "how a CRA applies it during monitoring (Chinese, 2-4 sentences)",
      "url": "real official URL",
      "source": "issuing body / official site",
      "sourceUrl": "same real URL",
      "aiNote": "AI 生成，待审核"
    }}
  ]
}}"""

# =====================================================================
# 4. 文献：先抓 PubMed，再让 LLM 写 CRA 启示
# =====================================================================
PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def fetch_pubmed_candidates(limit=5):
    """检索最新核药文献，返回 [{pmid,title,journal,pubDate,url,abstract}]。"""
    term = (
        '(radiopharmaceutical[Title/Abstract] OR radioligand[Title/Abstract] '
        'OR theranostics[Title/Abstract] OR "radioligand therapy"[Title/Abstract])'
    )
    params = {
        "db": "pubmed",
        "term": term,
        "retmax": 15,
        "sort": "date",
        "retmode": "json",
    }
    if NCBI_EMAIL:
        params["email"] = NCBI_EMAIL
    resp = requests.get(PUBMED_ESEARCH, params=params, timeout=30)
    resp.raise_for_status()
    ids = resp.json().get("esearchresult", {}).get("idlist", [])
    time.sleep(0.4)
    if not ids:
        return []

    # esummary 批量取题录
    params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "json",
    }
    if NCBI_EMAIL:
        params["email"] = NCBI_EMAIL
    resp = requests.get(PUBMED_ESUMMARY, params=params, timeout=30)
    resp.raise_for_status()
    result = resp.json().get("result", {})
    time.sleep(0.4)

    candidates = []
    for pmid in ids:
        doc = result.get(pmid)
        if not doc:
            continue
        title = (doc.get("title") or "").strip().rstrip(".")
        if not title:
            continue
        candidates.append(
            {
                "pmid": pmid,
                "title": title,
                "journal": doc.get("fulljournalname") or doc.get("source", ""),
                "pubDate": doc.get("pubdate", ""),
                "url": "https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid,
                "abstract": "",
            }
        )
        if len(candidates) >= limit:
            break

    # 逐篇取摘要（纯文本）；失败则留空，不影响主流程
    for item in candidates:
        try:
            params = {
                "db": "pubmed",
                "id": item["pmid"],
                "rettype": "abstract",
                "retmode": "text",
            }
            if NCBI_EMAIL:
                params["email"] = NCBI_EMAIL
            r = requests.get(PUBMED_EFETCH, params=params, timeout=30)
            if r.ok:
                item["abstract"] = r.text.strip()[:3000]
            time.sleep(0.4)
        except Exception as exc:
            print(f"[PubMed] {item['pmid']} 摘要获取失败: {exc}")
    return candidates


PROMPT_LITERATURE_TMPL = """Below are {n} REAL records freshly fetched from \
PubMed (title/journal/date/abstract are real; do not alter them). For EACH \
record, infer the related target and write a concise "craTakeaway" (2-4 \
Chinese sentences) explaining what a radiopharmaceutical CRA should pay \
attention to (e.g. imaging window, dosimetry, IP accountability, safety, \
endpoints). Keep the same number of records, same order, same pmid.

Return JSON:
{{
  "items": [
    {{
      "pmid": "real pmid",
      "title": "real title, unchanged",
      "journal": "real journal, unchanged",
      "pubDate": "real date, unchanged",
      "url": "real PubMed URL, unchanged",
      "target": "inferred related target abbreviation, or empty string",
      "abstract": "real abstract (may be shortened to 1500 chars)",
      "craTakeaway": "CRA implications in Chinese",
      "source": "PubMed",
      "sourceUrl": "the real PubMed URL",
      "aiNote": "AI 生成，待审核"
    }}
  ]
}}

Real PubMed records (JSON):
{records}"""


def build_literature_items():
    """抓 PubMed -> LLM 补 CRA 启示，返回草稿条目列表。"""
    candidates = fetch_pubmed_candidates(limit=5)
    if not candidates:
        print("[Literature] PubMed 未检索到候选文献")
        return []
    records_json = json.dumps(candidates, ensure_ascii=False)
    prompt = PROMPT_LITERATURE_TMPL.format(
        n=len(candidates), records=records_json
    )
    text = call_ark(SYSTEM_RULES, prompt, max_tokens=4096)
    parsed = extract_json(text)
    items = extract_items(parsed)
    # 兜底：LLM 若漏条，用原始 PubMed 记录补齐（仍为 AI 待审核）
    by_pmid = {str(i.get("pmid")): i for i in items if i.get("pmid")}
    for c in candidates:
        if c["pmid"] not in by_pmid:
            c.update(
                {
                    "target": "",
                    "craTakeaway": "",
                    "source": "PubMed",
                    "sourceUrl": c["url"],
                }
            )
            items.append(c)
    return items[:5]


# =====================================================================
# 5. 标题字段、dedup_key 与写入
# =====================================================================
def item_title(lib, item):
    """按库取标题字段。"""
    keys = {
        "target": ("name",),
        "nuclide": ("nuclide",),
        "vocabulary": ("en",),
        "interview": ("question",),
        "regulation": ("title",),
        "literature": ("title",),
    }
    for k in keys.get(lib, ("title",)):
        v = item.get(k)
        if v:
            return str(v)
    return "untitled"


def make_dedup_key(lib, item, title):
    """生成去重键：优先来源 URL，其次标题。"""
    url = str(item.get("sourceUrl") or item.get("url") or "").strip()
    url = url.split("?")[0].rstrip("/") if url and url != "待补充" else ""
    if url:
        base = lib + "|" + url.lower()
    else:
        norm = re.sub(r"\s+", " ", title.lower().strip())
        base = lib + "|" + norm
    return lib + ":" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def write_pending(lib, items):
    """判重后 upsert 写入 pending_updates，返回新增条数。"""
    rows = []
    for item in items:
        item = stamp(item)
        title = item_title(lib, item)
        dedup_key = make_dedup_key(lib, item, title)
        rows.append(
            {
                "library": lib,
                "action": "create",
                "title": title,
                "content_json": item,
                "source": str(item.get("source", "待补充"))[:200],
                "status": "pending",
                "dedup_key": dedup_key,
            }
        )

    if not rows:
        return 0

    # 查询已存在的 dedup_key（任意状态），避免重复/避免把已处理项刷回 pending
    keys = [r["dedup_key"] for r in rows]
    existing = set()
    try:
        res = (
            supabase.table("pending_updates")
            .select("dedup_key")
            .in_("dedup_key", keys)
            .execute()
        )
        existing = {r["dedup_key"] for r in res.data}
    except Exception as exc:
        print(f"[{lib}] 判重查询失败，按全新写入: {exc}")

    new_rows = [r for r in rows if r["dedup_key"] not in existing]
    if not new_rows:
        print(f"[{lib}] 无新增（{len(rows)} 条均已存在）")
        return 0

    # upsert（on_conflict=dedup_key），并发重跑也不会产生重复
    supabase.table("pending_updates").upsert(
        new_rows, on_conflict="dedup_key"
    ).execute()
    print(f"[{lib}] 生成 {len(rows)} 条，新增入库 {len(new_rows)} 条")
    return len(new_rows)


# =====================================================================
# 6. 主流程：逐库执行，单点失败隔离
# =====================================================================
def run_generic(lib, prompt, max_tokens=4096):
    """通用库：调用 LLM -> 解析 -> 写入。"""
    text = call_ark(SYSTEM_RULES, prompt, max_tokens=max_tokens)
    parsed = extract_json(text)
    items = extract_items(parsed)
    return write_pending(lib, items)


def main():
    report = {}
    # (library, 期望数量, 构造函数)
    jobs = [
        ("target", 1, lambda: run_generic("target", PROMPT_TARGET, 2048)),
        ("nuclide", 1, lambda: run_generic("nuclide", PROMPT_NUCLIDE, 2048)),
        ("vocabulary", 10, lambda: run_generic("vocabulary", PROMPT_VOCAB, 4096)),
        ("interview", 5, lambda: run_generic("interview", PROMPT_INTERVIEW, 4096)),
        ("regulation", 2, lambda: run_generic("regulation", PROMPT_REGULATION, 4096)),
        ("literature", 5, lambda: write_pending("literature", build_literature_items())),
    ]

    for lib, expect, fn in jobs:
        try:
            n = fn()
            report[lib] = n
            if n < expect:
                print(f"[{lib}] 期望 {expect} 条，实际新增 {n} 条（可能为判重或模型少给）")
        except Exception as exc:
            # 单库失败隔离：打印错误，继续下一个库
            report[lib] = -1
            print(f"[{lib}] 生成失败，已跳过: {exc}")

    print("=" * 60)
    print("[LLM] 每日草稿生成报告（-1 表示该库失败）")
    for lib, n in report.items():
        print(f"  {lib:<12} {n}")
    print("  run at:", datetime.datetime.now().isoformat())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # 顶层兜底：任何意外都不让 workflow 失败
        print(f"[LLM] 脚本发生未预期错误: {exc}")
    sys.exit(0)
