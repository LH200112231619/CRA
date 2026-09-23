#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PubMed 核药文献抓取脚本
- 使用 Bio.Entrez 按靶点检索最新文献
- 通过 supabase-py 以 upsert 方式写入 literature 表（按 url 去重）
- 可独立运行：依赖环境变量 SUPABASE_URL / SUPABASE_SERVICE_KEY
              以及 NCBI_EMAIL / NCBI_API_KEY（API Key 可选但建议配置）
- 兼容 biopython 1.85 之前（键名 UID）与 1.86+（键名 Id）两种 esummary 结构
"""

import os
import sys
import time

from Bio import Entrez
from supabase import create_client

# ---------- 1. 读取环境变量并初始化客户端 ----------
Entrez.email = os.environ.get("NCBI_EMAIL", "")
Entrez.api_key = os.environ.get("NCBI_API_KEY", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("[PubMed] 缺少 SUPABASE_URL / SUPABASE_SERVICE_KEY，脚本退出")
    sys.exit(0)  # 退出码 0，避免拖垮整个 workflow

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------- 2. 靶点与检索式配置 ----------
TARGETS = ["PSMA", "CAIX", "CD73", "Nectin-4", "SSTR2", "FAP", "GRPR"]
PER_TARGET_LIMIT = 20

def build_term(target: str) -> str:
    """按需求拼装 PubMed 检索式"""
    return (
        f'("{target}"[Title/Abstract]) '
        f'AND ("radiopharmaceutical"[Title/Abstract] '
        f'OR "radioligand"[Title/Abstract])'
    )

def get_pmid(doc) -> str:
    """从 esummary 记录中取 PMID，兼容不同 biopython 版本的键名差异。

    biopython <=1.85 的 esummary 记录用 "UID"；
    biopython 1.86/1.88 起改为 XML 原始键名 "Id"。
    """
    raw = doc.get("UID")
    if raw is None:
        raw = doc.get("Id")
    if raw is None:
        raw = doc.get("uid")
    return str(raw or "").strip()

def fetch_one_target(target: str) -> int:
    """抓取单个靶点的最新文献，返回写入条数"""
    # 2.1 esearch：拿到最新的 PMID 列表
    search_handle = Entrez.esearch(
        db="pubmed",
        term=build_term(target),
        retmax=PER_TARGET_LIMIT,
        sort="pub+date",
    )
    search_record = Entrez.read(search_handle)
    search_handle.close()
    time.sleep(0.2)  # 遵守 NCBI 限速（无 key 3 次/秒，有 key 10 次/秒）

    pmids = search_record.get("IdList", [])
    if not pmids:
        print(f"[PubMed] {target}: 未检索到文献")
        return 0

    # 2.2 esummary：按 PMID 批量获取题录信息
    summary_handle = Entrez.esummary(db="pubmed", id=",".join(pmids))
    summaries = Entrez.read(summary_handle)
    summary_handle.close()
    time.sleep(0.2)

    rows = []
    for doc in summaries:
        pmid = get_pmid(doc)
        title = (doc.get("Title") or "").strip().rstrip(".")
        if not pmid or not title:
            # 打印一条诊断信息，便于发现字段再次变化
            print(f"[PubMed] {target}: 跳过记录，pmid={pmid!r}, title={title[:30]!r}")
            continue
        rows.append(
            {
                "pmid": pmid,
                "title": title,
                "journal": doc.get("FullJournalName") or doc.get("Source", ""),
                "pub_date": doc.get("PubDate", ""),
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "target_name": target,
            }
        )

    # 2.3 upsert 写入 Supabase，url 冲突时更新而非报错
    if rows:
        try:
            supabase.table("literature").upsert(
                rows, on_conflict="url"
            ).execute()
        except Exception as exc:
            # 写入失败要明确打印，不能静默吞掉
            print(f"[PubMed] {target}: upsert 写入失败: {exc}")
            return 0

    print(f"[PubMed] {target}: 获取 {len(pmids)} 篇，写入 {len(rows)} 篇")
    return len(rows)

# ---------- 3. 逐靶点执行，单点失败不影响整体 ----------
def main() -> None:
    total = 0
    for target in TARGETS:
        try:
            total += fetch_one_target(target)
        except Exception as exc:  # 网络/解析/写入异常均捕获
            print(f"[PubMed] {target} 抓取失败: {exc}")
        finally:
            time.sleep(0.2)
    print(f"[PubMed] 全部完成，共写入 {total} 篇")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # 顶层兜底：任何意外都不让 workflow 失败
        print(f"[PubMed] 脚本发生未预期错误: {exc}")
    sys.exit(0)
