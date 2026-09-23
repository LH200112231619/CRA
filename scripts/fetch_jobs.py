#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PharmiWeb 核药 CRA 岗位抓取脚本
- requests 拉取搜索页，BeautifulSoup 解析
- 采用“查找包含 /job 的链接”的宽容策略，页面结构微调时仍可工作
- 写入 jobs 表，status 默认“未投”；失败只警告不中断
"""

import os
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from supabase import create_client

# ---------- 1. 初始化 ----------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("[Jobs] 缺少 SUPABASE_URL / SUPABASE_SERVICE_KEY，脚本退出")
    sys.exit(0)

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

SEARCH_URL = "https://www.pharmiweb.jobs/searchjobs/"
PARAMS = {"keywords": "radiopharmaceutical CRA"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-learning-bot)"}
MAX_JOBS = 50

def fetch_jobs() -> int:
    """抓取并解析岗位列表，返回写入条数"""
    resp = requests.get(
        SEARCH_URL, params=PARAMS, headers=HEADERS, timeout=30
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # 宽容解析：收集所有 href 中包含 /job 的站内链接
    seen_urls = set()
    rows = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/job" not in href.lower():
            continue

        title = anchor.get_text(strip=True)
        # 过滤导航类短文本
        if not title or len(title) < 6:
            continue

        full_url = urljoin(SEARCH_URL, href)
        # 去掉常见的跟踪参数后缀
        full_url = full_url.split("?")[0]
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # 尽力从父级容器提取公司/地点（结构变化时允许为空）
        company, location = "", ""
        parent = anchor.parent
        if parent is not None:
            block_text = parent.get_text(" ", strip=True)
            parts = [p.strip() for p in block_text.split("|") if p.strip()]
            if len(parts) >= 2:
                company = parts[1]
            if len(parts) >= 3:
                location = parts[2]

        rows.append(
            {
                "title": title,
                "company": company,
                "location": location,
                "url": full_url,
                "source": "PharmiWeb",
                "status": "未投",
            }
        )
        if len(rows) >= MAX_JOBS:
            break

    if not rows:
        # 页面结构大改或反爬时给出警告，但不抛异常
        print("[Jobs] 警告：未解析到任何岗位，页面结构可能已变化")
        return 0

    supabase.table("jobs").upsert(rows, on_conflict="url").execute()
    print(f"[Jobs] 共解析并写入 {len(rows)} 个岗位")
    return len(rows)

if __name__ == "__main__":
    try:
        fetch_jobs()
    except Exception as exc:
        print(f"[Jobs] 抓取失败: {exc}")
    sys.exit(0)
