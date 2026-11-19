#!/usr/bin/env python3
"""从 appstore.json 留言板中移除失效 URL 及其名称标签。"""
import json

SRC = "appstore.json"
RES = "dead_check_result.json"

# 失效 = 检测为 DEAD（404/连接失败，双重确认）+ SUSPECT（持续 403/423/451）
res = json.load(open(RES))
dead = set(res["dead"]) | set(res["suspect"])

# 块标题特征：即使紧跟失效 URL 也不随之删除
def is_block_header(text: str) -> bool:
    return (
        text.startswith("（以下")
        or text.startswith("更多音源见仓库")
        or text.startswith("仓库直链打不开时")
        or text.startswith("软件下载无内容")
        or text.startswith("临时提供直播订阅")
        or text.startswith("直播（自行下载保存）")
    )


d = json.load(open(SRC))
total_removed = 0
report = []

for mb in d["messageBoard"]:
    contents = mb["contents"]
    out = []
    removed_here = []
    i = 0
    while i < len(contents):
        c = contents[i]
        text = c["text"].strip()
        if text in dead:
            # 记录标签
            label = None
            if out:
                prev = out[-1]
                if prev["canCopy"] is False and not is_block_header(prev["text"]):
                    label = out.pop()
            removed_here.append((label["text"] if label else "(无标签)", text))
            total_removed += 1
            i += 1
            continue
        out.append(c)
        i += 1
    # 清理悬空的块标题（后面紧跟另一个标题或结尾）
    cleaned = []
    for j, c in enumerate(out):
        t = c["text"].strip()
        if (
            c["canCopy"] is False
            and is_block_header(t)
            and (
                j == len(out) - 1
                or (out[j + 1]["canCopy"] is False and is_block_header(out[j + 1]["text"].strip()))
            )
        ):
            continue
        cleaned.append(c)
    mb["contents"] = cleaned
    if removed_here:
        report.append((mb["title"], removed_here))

with open(SRC, "w") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
    f.write("\n")

for title, items in report:
    print(f"### {title}（移除 {len(items)} 条）")
    for label, url in items:
        print(f"  - {label}  →  {url}")
print(f"\n共移除 {total_removed} 条失效条目")
