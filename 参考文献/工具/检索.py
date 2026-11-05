#!/usr/bin/env python3
"""在本地简体检索版（若有）或原字形正文中查找字词与上下文。"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("keyword", help="搜索字词，推荐使用简体；忽略排版空白")
parser.add_argument("--book", default="", help="书名包含的文字")
parser.add_argument("--limit", type=int, default=20, help="最多显示的匹配条数，默认20")
args = parser.parse_args()
keyword = "".join(args.keyword.split())
if not keyword or args.limit < 1:
    parser.error("搜索词不能为空，limit应为正数")
pattern = re.compile(r"\s*".join(re.escape(char) for char in keyword))
books = json.loads((ROOT / "书目索引.json").read_text(encoding="utf-8"))
total = 0
shown = 0
for book in books:
    if args.book not in book["title"]:
        continue
    path = ROOT / (book.get("simplified_path") or book["text_path"])
    text = path.read_text(encoding="utf-8")
    for match in pattern.finditer(text):
        total += 1
        if shown >= args.limit:
            continue
        line = text.count("\n", 0, match.start()) + 1
        context = text[max(0, match.start()-70):match.end()+110].replace("\n", " ")
        print(f"《{book['title']}》 第{line}行\n  {context}\n  文件：{path}\n")
        shown += 1
print(f"共找到 {total} 处，显示 {shown} 处。选集与总集可能重复；简体转换仅为搜索便利。")
