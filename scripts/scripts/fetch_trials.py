#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClinicalTrials.gov v2 API 临床试验抓取脚本
- 按靶点检索 radiopharmaceutical 相关试验
- 结果写入 daily_updates 表，category='trial'，按 url 去重
"""

import os
import sys
import time
import datetime

import requests
from supabase import create_client

# ---------- 1. 初始化 ----------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("[Trials] 缺少 SUPABASE_URL / SUPABASE_SERVICE_KEY，脚本退出")
    sys.exit(0)

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

API_URL = "https://clinicaltrials.gov/api/v2/studies"
TARGETS = ["PSMA", "CAIX", "CD73", "Nectin-4", "SSTR2", "FAP", "GRPR"]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-learning-bot)"}

def fetch_one_target(target: str) -> int:
    """检索单个靶点的最新 20 项试验并写入"""
    params = {
        "query.term": f"{target} radiopharmaceutical",
        "pageSize": 20,
        "sort": "LastUpdatePostDate:desc",  # 按最近更新日期倒序
    }
    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    studies = resp.json().get("studies", [])

    rows = []
    for study in studies:
        protocol = study.get("protocolSection", {})
        ident = protocol.get("identificationModule", {})
        status = protocol.get("statusModule", {})

        nct_id = ident.get("nctId", "")
        title = ident.get("briefTitle") or ident.get("officialTitle", "")
        if not nct_id or not title:
            continue

        url = f"https://clinicaltrials.gov/study/{nct_id}"
        rows.append(
            {
                "category": "trial",
                "title": title.strip(),
                "url": url,
                "run_date": datetime.date.today().isoformat(),
                "payload": {
                    "nct_id": nct_id,
                    "target": target,
                    "overall_status": status.get("overallStatus", ""),
                    "last_update_post_date": status.get(
                        "lastUpdatePostDateStruct", {}
                    ).get("date", ""),
                },
            }
        )

    if rows:
        # url 冲突时更新（同一试验再次被抓到则刷新 payload）
        supabase.table("daily_updates").upsert(
            rows, on_conflict="url"
        ).execute()

    print(f"[Trials] {target}: 获取 {len(studies)} 项，写入 {len(rows)} 项")
    return len(rows)

def main() -> None:
    total = 0
    for target in TARGETS:
        try:
            total += fetch_one_target(target)
        except Exception as exc:
            print(f"[Trials] {target} 抓取失败: {exc}")
        finally:
            time.sleep(0.5)  # 对公开 API 保持礼貌间隔
    print(f"[Trials] 全部完成，共写入 {total} 项")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[Trials] 脚本发生未预期错误: {exc}")
    sys.exit(0)
