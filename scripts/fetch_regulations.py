#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法规/监管动态 RSS 抓取脚本
- 使用 feedparser 解析 FDA / EMA / NMPA 的 RSS
- 写入 regulations 表，按 url 去重；status 默认“现行”
- 任一 RSS 不可用只打印提示，不报错退出
"""

import os
import sys

import feedparser
from supabase import create_client

# ---------- 1. 初始化 ----------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("[Regs] 缺少 SUPABASE_URL / SUPABASE_SERVICE_KEY，脚本退出")
    sys.exit(0)

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------- 2. RSS 源配置 ----------
# 说明：FDA 提供官方 RSS；EMA 提供全站 RSS；
#       NMPA（国家药监局）目前没有官方 RSS，此处保留官网地址，
#       feedparser 解析不到条目时会自动跳过并提示。
FEEDS = [
    {
        "region": "美国",
        "source": "FDA - Drugs",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml",
    },
    {
        "region": "美国",
        "source": "FDA - Medical Devices",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medical-devices/rss.xml",
    },
    {
        "region": "欧盟",
        "source": "EMA",
        "url": "https://www.ema.europa.eu/en/rss.xml",
    },
    {
        "region": "中国",
        "source": "NMPA",
        "url": "https://www.nmpa.gov.cn/",  # 无官方 RSS，预期解析为空
    },
]

def parse_feed(feed_cfg: dict) -> int:
    """解析单个 RSS 源并 upsert，返回写入条数"""
    parsed = feedparser.parse(feed_cfg["url"])

    # bozo=1 表示源存在格式问题；entries 为空也视为不可用
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        print(f"[Regs] {feed_cfg['source']} RSS 不可用或无条目，已跳过")
        return 0

    if not parsed.entries:
        print(f"[Regs] {feed_cfg['source']} 暂无更新条目，已跳过")
        return 0

    rows = []
    for entry in parsed.entries:
        link = entry.get("link", "").strip()
        title = entry.get("title", "").strip()
        if not link or not title:
            continue
        rows.append(
            {
                "title": title,
                "url": link,
                "source": feed_cfg["source"],
                "region": feed_cfg["region"],
                "summary": entry.get("summary", "")[:1000],
                "status": "现行",
                "published_at": entry.get("published", "")
                or entry.get("updated", ""),
            }
        )

    if rows:
        supabase.table("regulations").upsert(
            rows, on_conflict="url"
        ).execute()

    print(f"[Regs] {feed_cfg['source']}: 解析 {len(parsed.entries)} 条，写入 {len(rows)} 条")
    return len(rows)

def main() -> None:
    total = 0
    for feed_cfg in FEEDS:
        try:
            total += parse_feed(feed_cfg)
        except Exception as exc:
            # 单个源失败不影响其他源
            print(f"[Regs] {feed_cfg['source']} 抓取失败: {exc}")
    print(f"[Regs] 全部完成，共写入 {total} 条")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[Regs] 脚本发生未预期错误: {exc}")
    sys.exit(0)
