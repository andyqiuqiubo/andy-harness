#!/usr/bin/env python3
"""把大模型新闻列表写入以日期为前缀的 txt 文件。

用法:
    echo '<JSON>' | python save_news.py --date 2026-09-28 --outdir news
    python save_news.py --input news.json --date 2026-09-28

输入 JSON 为数组，或含 "news"/"items" 键的对象；每条含
title/source/time/summary/url 字段。
"""
import argparse
import json
import os
import sys
from datetime import datetime


def load_items(path):
    # 显式按 UTF-8 读取，避免 Windows 默认编码导致中文乱码
    if path == "-":
        raw = sys.stdin.buffer.read().decode("utf-8")
    else:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("news", data.get("items", []))
    if not isinstance(data, list):
        raise ValueError("输入必须是新闻数组")
    return data


def render(date, items):
    lines = ["大模型新闻日报 " + date, "=" * 40, ""]
    for i, it in enumerate(items, 1):
        lines.append("%d. %s" % (i, it.get("title", "(无标题)")))
        meta = " | ".join(x for x in [it.get("source"), it.get("time")] if x)
        if meta:
            lines.append("   来源：" + meta)
        if it.get("summary"):
            lines.append("   摘要：" + it["summary"])
        if it.get("url"):
            lines.append("   链接：" + it["url"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    p.add_argument("--outdir", default="news", help="输出目录")
    p.add_argument("--input", default="-", help="JSON 文件路径，- 表示 stdin")
    args = p.parse_args()

    date = args.date or datetime.now().strftime("%Y-%m-%d")
    datetime.strptime(date, "%Y-%m-%d")  # 校验格式

    items = load_items(args.input)
    if len(items) != 5:
        print("警告：期望 5 条新闻，实际 %d 条" % len(items), file=sys.stderr)

    os.makedirs(args.outdir, exist_ok=True)
    path = os.path.join(args.outdir, date + "-llm-news.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(date, items))
    print(path)


if __name__ == "__main__":
    main()
