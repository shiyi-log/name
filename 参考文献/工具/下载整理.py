#!/usr/bin/env python3
"""下载并整理古籍参考资料；只写入本脚本所在的参考文献目录。"""
from __future__ import annotations

import concurrent.futures
import hashlib
import io
import importlib.util
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
POETRY_SHA = "b8594f81a89752241442f2ce267d6f66f96704ee"
POETRY_REPO = "chinese-poetry/chinese-poetry"
MANIFEST = []
BOOKS = []
try:
    from opencc import OpenCC
    CONVERTER = OpenCC("t2s")
except ImportError:
    CONVERTER = None


def save(path, data):
    path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


def download(url, path, expected_git_sha=None):
    target = ROOT / path
    if target.exists():
        data = target.read_bytes()
    else:
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "Local-Classics-Reference/1.0"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = response.read()
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(attempt + 1)
        save(path, data)
    if expected_git_sha:
        actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if actual != expected_git_sha:
            raise ValueError(f"源文件 Git blob 校验失败：{path}")
    return {"local_path": str(Path(path)), "download_url": url,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "git_blob_sha": expected_git_sha,
            "downloaded_at": datetime.fromtimestamp(target.stat().st_mtime, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "verified_at": NOW}, data


def raw_url(repo, sha, path):
    return f"https://raw.githubusercontent.com/{repo}/{sha}/" + urllib.parse.quote(path)


def render_json(value, depth=0, include_notes=True):
    """渲染正文、标题与来源附序/注记；跳过现代简介、评论、翻译。"""
    if isinstance(value, list):
        return "\n\n".join(render_json(x, depth, include_notes) for x in value)
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    heading = []
    for key in ("section", "type", "title", "chapter", "subchapter", "rhythmic"):
        if isinstance(value.get(key), str) and value[key] not in heading:
            heading.append(value[key])
    author = value.get("author")
    prefix = ("【" + " · ".join(heading) + "】\n") if heading else ""
    if isinstance(author, str):
        prefix += "作者：" + author.strip() + "\n"
    if isinstance(value.get("source"), str):
        prefix += "原文来源：" + value["source"] + "\n"
    body = value.get("paragraphs", value.get("para", value.get("content", [])))
    if isinstance(body, list) and all(isinstance(x, str) for x in body):
        body = "\n".join(body)
    else:
        body = render_json(body, depth + 1, include_notes)
    prologue = render_json(value.get("prologue", ""), depth + 1, include_notes)
    preface = render_json(value.get("preface", ""), depth + 1, include_notes)
    notes = render_json(value.get("notes", ""), depth + 1, include_notes) if include_notes else ""
    if preface:
        prefix += "序：\n" + preface + "\n\n"
    if prologue:
        prefix += "附序（来源 prologue 字段）：" + prologue + "\n"
    if notes:
        body += "\n附注（来源 notes 字段）：" + notes
    return prefix + body


def count_entries(value):
    if isinstance(value, list):
        return sum(count_entries(x) for x in value)
    if isinstance(value, dict):
        for key in ("paragraphs", "para"):
            if key in value:
                body = value[key]
                return count_entries(body) if isinstance(body, list) and any(isinstance(x, dict) for x in body) else 1
        if isinstance(value.get("content"), str):
            return 1
        content = value.get("content")
        if isinstance(content, list) and all(isinstance(x, str) for x in content):
            return 1
        return count_entries(content)
    return 0


def add_book(title, category, text, sources, count=None, note="", unit="条"):
    if not re.search(r"[\u3400-\u9fff]", text):
        raise ValueError(f"未检出中文正文：{title}")
    text = text.strip() + "\n"
    path = f"正文/{category}/{title}.txt"
    header = f"《{title}》\n来源：详见 目录.md 与 来源清单.json\n说明：保留来源字形；电子整理本，未逐字校勘。\n\n"
    save(path, header + text)
    simplified_path = None
    if CONVERTER:
        simplified_path = f"简体检索/{category}/{title}.txt"
        save(simplified_path, CONVERTER.convert(header + text))
    BOOKS.append({"title": title, "category": category, "text_path": path,
                  "simplified_path": simplified_path, "source_paths": sources,
                  "entries": count, "unit": unit, "body_characters": len(text),
                  "note": note})
    print(f"已整理 {title}：{count if count is not None else '-'}{unit}，{len(text):,} 字符", flush=True)


def poetry():
    tree_url = f"https://api.github.com/repos/{POETRY_REPO}/git/trees/{POETRY_SHA}?recursive=1"
    record, data = download(tree_url, "来源/chinese-poetry-tree.json")
    MANIFEST.append(record)
    tree = json.loads(data)
    if tree.get("truncated"):
        raise RuntimeError("GitHub 文件树不完整")
    entries = {x["path"]: x for x in tree["tree"] if x["type"] == "blob"}
    selections = [
        ("大学", "01_四书五经", ["四书五经/daxue.json"], ""),
        ("中庸", "01_四书五经", ["四书五经/zhongyong.json"], ""),
        ("论语", "01_四书五经", ["论语/lunyu.json"], "20篇"),
        ("孟子", "01_四书五经", ["四书五经/mengzi.json"], "14卷/章级对象"),
        ("诗经", "01_四书五经", ["诗经/shijing.json"], "305篇有正文的诗；不把六篇笙诗计为正文。"),
        ("全唐诗_开源整理本", "02_唐诗宋词", sorted(p for p in entries if re.fullmatch(r"全唐诗/poet\.tang\.\d+\.json", p)), "保留原记录与重复；有4条空正文。来源附注单列；不是逐篇校勘的学术定本。"),
        ("全宋词_开源整理本", "02_唐诗宋词", sorted(p for p in entries if re.fullmatch(r"宋词/ci\.song\.\d+\.json", p)), "宋词，不是全宋诗；保留上游重复记录。"),
        ("唐诗三百首_选集", "02_唐诗宋词", ["全唐诗/唐诗三百首.json"], "上游实际366条，与全唐诗有重叠；名称中的三百不是实测记录数。"),
        ("宋词三百首_选集", "02_唐诗宋词", ["宋词/宋词三百首.json"], "上游实际280条，与宋词库有重叠。"),
        ("楚辞", "03_诸子古文与典故", ["楚辞/chuci.json"], "17个大类，65条作品。"),
        ("古文观止", "03_诸子古文与典故", ["蒙学/guwenguanzhi.json"], "只生成古文正文；原始JSON另含现代简介。"),
        ("声律启蒙", "03_诸子古文与典故", ["蒙学/shenglvqimeng.json"], "适合意象、对仗和读音参考。"),
        ("幼学琼林", "03_诸子古文与典故", ["蒙学/youxueqionglin.json"], "典故索引性读物，具体典故仍需回溯原典。"),
        ("幽梦影", "03_诸子古文与典故", ["幽梦影/youmengying.json"], "检索正文不包含评论。"),
        ("全宋诗_开源整理本", "02_唐诗宋词", sorted(p for p in entries if re.fullmatch(r"全唐诗/poet\.song\.\d+\.json", p)), "254,248条来源记录，其中23条空正文；与全宋词分别保存，保留重复和缺文，非逐篇校勘本。"),
        ("元曲_开源整理本", "07_历代诗词曲", ["元曲/yuanqu.json"], "来源含散曲和杂剧曲文等记录，条目不等于独立剧目数。"),
        ("花间集", "07_历代诗词曲", sorted(p for p in entries if re.fullmatch(r"五代诗词/huajianji/huajianji-(?:[1-9]|x)-juan\.json", p)), "来源含十卷，实测497条记录；卷五48条、卷九49条，其余各50条，未据通行500首补齐。现代notes注释和序仅留原档。"),
        ("南唐二主词_选集", "07_历代诗词曲", ["五代诗词/nantang/poetrys.json"], "上游45条整理记录，不保证穷尽所有异文作品；现代注释仅保存在原始JSON。"),
        ("曹操诗集", "07_历代诗词曲", ["曹操诗集/caocao.json"], "上游26条记录，作者曹操。"),
        ("纳兰性德诗词_选集", "07_历代诗词曲", ["纳兰性德/纳兰性德诗集.json"], "来源题作诗集，实际兼含词；258条选编记录，未冒称全集。"),
        ("千家诗", "07_历代诗词曲", ["蒙学/qianjiashi.json"], "保留上下级篇题与组诗分首；与唐宋诗语料存在重叠。"),
        ("千字文", "08_蒙学格言", ["蒙学/qianziwen.json"], "古文正文；拼音保留于原始JSON。"),
        ("三字经_传统版", "08_蒙学格言", ["蒙学/sanzijing-traditional.json"], "采用上游传统版；未混入另一现代修订版。"),
        ("百家姓", "08_蒙学格言", ["蒙学/baijiaxing.json"], "阅读版保存正文；来源郡望字段仅留原JSON，未逐姓考证。"),
        ("弟子规", "08_蒙学格言", ["蒙学/dizigui.json"], "分章原文。"),
        ("增广贤文", "08_蒙学格言", ["蒙学/zengguangxianwen.json"], "上下集；民间格言汇编，异本较多。"),
        ("朱子家训_朱柏庐", "08_蒙学格言", ["蒙学/zhuzijiaxun.json"], "作者朱柏庐，即朱用纯；区别于朱熹。"),
        ("文字蒙求", "06_字书训诂与诸子", ["蒙学/wenzimengqiu.json"], "王筠著，含序及四卷；来源用●等替代部分古文字形。"),
    ]
    extra = ["LICENSE", "README.md", "四书五经/README.md", "论语/README.md", "诗经/README.md", "全唐诗/README.md", "宋词/README.md", "蒙学/README.md"]
    extra += [p for p in entries if p.endswith(("README.md", "Readme.md")) and p.split('/')[0] in ("五代诗词", "元曲", "曹操诗集", "纳兰性德")]
    extra += ["五代诗词/huajianji/huajianji-0-preface.json", "五代诗词/nantang/authors.json", "五代诗词/nantang/intro.json"]
    # 已留存的修订版只作来源备查；正文仍用传统版，不混合两个版本。
    extra += ["蒙学/sanzijing-new.json"]
    wanted = sorted(set(extra + [p for _, _, paths, _ in selections for p in paths]))
    def one(path):
        return path, download(raw_url(POETRY_REPO, POETRY_SHA, path),
                              "原始/chinese-poetry/" + path, entries[path]["sha"])
    loaded = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for path, (record, data) in pool.map(one, wanted):
            record.update(source="chinese-poetry", commit=POETRY_SHA, license="MIT", upstream_path=path)
            if path == "蒙学/sanzijing-new.json":
                record["reading_note"] = "仅保留修订版来源备查，未纳入阅读；阅读版使用sanzijing-traditional.json。"
            MANIFEST.append(record)
            loaded[path] = data
    for title, category, paths, note in selections:
        # 分片按数值次序合并；不把选集混入总库。
        paths = sorted(paths, key=lambda p: int(re.search(r"\.(\d+)\.json$", p).group(1)) if re.search(r"\.(\d+)\.json$", p) else 0)
        values = [json.loads(loaded[p]) for p in paths]
        if title == "文字蒙求":
            # 来源author字段含现代作者生平；阅读版只保留姓名，原字段完整留在JSON。
            for value in values:
                if isinstance(value, dict):
                    value["author"] = "王筠"
        text = "\n\n".join(render_json(v, include_notes=title not in ("花间集", "南唐二主词_选集")) for v in values)
        if title == "纳兰性德诗词_选集":
            text = "\n".join(line.rstrip() for line in text.splitlines())
        add_book(title, category, text,
                 ["原始/chinese-poetry/" + p for p in paths], sum(count_entries(v) for v in values), note)


PG_BOOKS = [
    ("周易", "01_四书五经", 25501, "含64卦及系辞、说卦、序卦、杂卦。"),
    ("礼记", "01_四书五经", 24048, "通行49篇，从曲礼上到丧服四制。"),
    ("道德经", "03_诸子古文与典故", 7337, "81章，保留来源异文。"),
    ("文心雕龙", "03_诸子古文与典故", 23822, "50篇；文学意象与修辞参考。"),
    ("史记", "04_史传名著", 24226, "原档含注释、表格与缺字标记，未逐卷校勘。"),
    ("红楼梦", "04_史传名著", 24264, "120回；源文本有繁简混排、错字，未确定具体底本。"),
    ("三国演义", "04_史传名著", 23950, "120回；原题《三国志演义》。"),
    ("水浒传_七十回本", "04_史传名著", 23863, "此电子版为楔子加70回，不是一百回/一百二十回本。"),
    ("西游记", "05_神话传说", 23962, "100回神魔小说，另见四大名著清单。"),
    ("山海经", "05_神话传说", 25288, "18篇；含缺字、拆字注记，未擅自补字。"),
    ("搜神记", "05_神话传说", 25362, "20卷；神异志怪原文，不是现代同名小说。"),
    ("淮南子", "05_神话传说", 24059, "21卷；整理版清除汉字间排版空格，原档完整保留。"),
    ("封神演义", "05_神话传说", 23910, "100回；作者归属有争议，原档保留来源署名。"),
    ("列子", "05_神话传说", 7341, "8篇；原档个别文字存在乱码。"),
]
EXTRA_PATH = ROOT / "工具/扩充书目.json"
EXTRAS = json.loads(EXTRA_PATH.read_text(encoding="utf-8")) if EXTRA_PATH.exists() else {}
PG_BOOKS += [(b["title"], b["category"], b["id"], b["note"]) for b in EXTRAS.get("project_gutenberg", [])]


def pg_body(s, compact=False, ident=None):
    s = s.replace("\r\n", "\n")
    match = re.search(r"\*\*\* START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", s, re.I)
    end = re.search(r"\*\*\* END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", s, re.I)
    if not match or not end or end.start() <= match.end():
        raise ValueError("Project Gutenberg 正文边界缺失")
    s = s[match.end():end.start()]
    s = re.sub(r"(?m)^Produced by .*\n?", "", s)
    s = re.sub(r"(?m)^End of Project Gutenberg.*\n?", "", s)
    s = re.sub(r"(?m)^Language: Chinese\s*$", "", s)
    if ident == 43009:
        s = re.sub(r"(?m)^(?:Title|Author):[^\n]*\n?", "", s)
    if ident == 7458:
        s = re.sub(r"(?m)^Volumes?\s+[\d–—-]+[ \t]*$", "", s, flags=re.I)
    if ident == 7215:
        s = re.sub(r"(?m)^(?:Deng Xi Zi \(Thought Of Deng Xi Zi\)|by Deng Xi|The original Chinese:)[ \t]*$", "", s)
    if ident == 25324:
        s = re.sub(r'<img\b[^>]*src=/bzk/(\w+\.bmp)[^>]*>', r'〔缺字：原档图片\1〕', s, flags=re.I)
        s = re.sub(r"(卷[一二三四五六七八九十]+)(?=\s|$)", r"\n\n\1\n\n", s)
    s = re.sub(r"(?m)^[ \t]*(?:/p|p/)[ \t]*$", "", s)
    s = re.sub(r"(?m)^[ \t]*[-_]{3,}[ \t]*$", "", s)
    # 来源的段首缩进是段落边界；先保留，再清理字间空格和固定列宽折行。
    s = re.sub(r"(?m)^[ \t]*\u3000[ \t]*\u3000", "\n\n", s)
    if compact:
        s = re.sub(r"[ \t\u3000\xa0]+", "", s)
    numbers = r"[一二三四五六七八九十百零〇○0-9０-９]+"
    if ident in (24264, 23950, 23863, 23962, 23910):
        # 有些回目紧接上回末句；仅将后接空白/冒号/换行的“第X回”视作回目。
        # 因此不会把叙述中的“第一回也”“第二回了”等误拆成章节。
        s = re.sub(rf"(第[ \t]*{numbers}[ \t]*回(?=[ \t\u3000\xa0：:\n])[^\n]*)", r"\n\n\1\n\n", s)
    # 保留卷、篇等独立标题，避免和下一段正文粘连。
    heading = rf"(?:第{numbers}(?:卷|章|卦)|卷第{numbers}[^\n]{{0,12}}|卷{numbers}[^\n]{{0,10}}|[^\n]{{1,8}}第{numbers})"
    s = re.sub(rf"(?m)^({heading})[ \t]*$", r"\n\n\1\n\n", s)
    if ident == 24059:
        s = re.sub(r"(?m)^([^\n]{1,4}訓|要略|淮南子)$", r"\n\n\1\n\n", s)
    # 原文件按固定列宽折行；段内连行便于短语搜索，保留空行形成的段落。
    s = "\n\n".join("".join(line.strip() for line in paragraph.splitlines())
                       for paragraph in re.split(r"\n\s*\n", s) if paragraph.strip())
    return s


def gutenberg():
    def one(book):
        title, category, ident, note = book
        url = f"https://www.gutenberg.org/cache/epub/{ident}/pg{ident}.txt"
        record, data = download(url, f"原始/Project_Gutenberg/pg{ident}_{title}.txt")
        record.update(source="Project Gutenberg", ebook_id=ident,
                      source_page=f"https://www.gutenberg.org/ebooks/{ident}",
                      license="来源页标示 Public domain in the USA；完整原档保留 Project Gutenberg 许可头尾。")
        return book, record, data
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        for (title, category, ident, note), record, data in pool.map(one, PG_BOOKS):
            MANIFEST.append(record)
            body = pg_body(data.decode("utf-8-sig"), compact=(ident == 24059), ident=ident)
            scope = {25501:(64,"卦（另含传文）"), 24048:(49,"篇"), 7337:(81,"章"),
                     23822:(50,"篇"), 24264:(120,"回"), 23950:(120,"回"),
                     23863:(70,"回（另含楔子）"), 23962:(100,"回"), 25288:(18,"篇"),
                     25362:(20,"卷"), 24059:(21,"卷"), 23910:(100,"回"), 7341:(8,"篇")}
            count, unit = scope.get(ident, (None, "条"))
            extra = next((b for b in EXTRAS.get("project_gutenberg", []) if b["id"] == ident), None)
            if extra:
                count, unit = extra.get("count"), extra.get("unit", "条")
            add_book(title, category, body, [record["local_path"]], count=count, note=note, unit=unit)


def clean_kanripo(s):
    s = re.sub(r"<pb:[^>]+>", "", s)
    s = re.sub(r"(?m)^#.*\n?", "", s)
    s = s.replace("¶", "")
    marker = r"(?m)^<<<<<<< HEAD\n(.*?)^=======\n(.*?)^>>>>>>> c8e280c \(Removed spurious pbs, but still problems visible\.\)$"
    def resolve_duplicate_conflict(match):
        normalize = lambda value: re.sub(r"[\s¶]+", "", value)
        if normalize(match.group(1)) != normalize(match.group(2)):
            raise ValueError("Kanripo重复冲突段内容不一致，停止清洗")
        return match.group(1)
    s = re.sub(marker, resolve_duplicate_conflict, s, flags=re.S)
    if re.search(r"(?m)^(?:<<<<<<<|=======|>>>>>>>)", s):
        raise ValueError("Kanripo正文仍包含未处理的冲突标记")
    s = re.sub(r"(?m)^\*+\s*", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    # 诗句/短语在来源标注中被逐行切开，段内连行，保留段落。
    return "\n\n".join("".join(x.strip() for x in p.splitlines()) for p in re.split(r"\n\s*\n", s) if p.strip())


def kanripo():
    selections = [
        ("尚书", "01_四书五经", "KR1b0001", "9fe0bc3d48dd383c54532e7b5b45f8adef56aca5"),
        ("春秋左传_含经文", "01_四书五经", "KR1e0001", "dc3dc18e0039e05679d98bc650c2f980f8dcabac"),
        ("庄子", "03_诸子古文与典故", "KR5c0126", "abb9cd323dfd9520894b56b3b8ba2ec0ea50554f"),
    ]
    for title, category, ident, sha in selections:
        url = f"https://codeload.github.com/kanripo/{ident}/zip/{sha}"
        record, data = download(url, f"原始/Kanripo/{ident}_{sha[:12]}.zip")
        record.update(source="Kanripo", repository=f"https://github.com/kanripo/{ident}", commit=sha,
                      license="具体书库未发现LICENSE；保留来源、BASEEDITION和标注。此处不将数字整理成果重标为MIT或CC0。")
        MANIFEST.append(record)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            texts = [(p, archive.read(p).decode("utf-8-sig")) for p in sorted(archive.namelist())
                     if re.search(rf"/{ident}_\d+\.txt$", p)]
        if ident == "KR1b0001":
            # 第059号是逸书材料，单独保留在原始归档，不计入通行58篇。
            texts = [(p, s) for p, s in texts if int(re.search(r"_(\d+)\.txt$", p).group(1)) <= 58]
            assert len(texts) == 58
        expected = {"KR1b0001":58, "KR1e0001":12, "KR5c0126":33}[ident]
        assert len(texts) == expected, (ident, len(texts))
        note = {"KR1b0001":"通行58篇；原ZIP另保留第059号逸书材料。",
                "KR1e0001":"十二公，经文与传文同存；A为经、B为传，未将左传冒称纯春秋。",
                "KR5c0126":"33篇；源文件BASEEDITION为CHANT。"}[ident]
        body = "\n\n".join(clean_kanripo(s) for _, s in texts)
        add_book(title, category, body, [record["local_path"]], len(texts), note, "篇/卷")
        if ident == "KR1e0001":
            passages = []
            years = 0
            for _, s in texts:
                active = False
                for line in re.sub(r"<pb:[^>]+>", "", s).splitlines():
                    if re.match(r"A\d+\.\d+《.*經》", line):
                        active = True
                        years += 1
                        passages.append("\n" + re.sub(r"^A[\d.]+", "", line))
                    elif re.match(r"B(?:\d|《)", line):
                        active = False
                    elif active and line.strip() and not line.startswith("#"):
                        passages.append(re.sub(r"^A[\d.]+", "", line).replace("¶", ""))
            add_book("春秋经_从春秋左传提取", category, "\n".join(passages), [record["local_path"]], years,
                     "依《左传》附经，含哀公十五、十六年续经，共244个年标题；区别于通常止于获麟的242年范围。按A/B标记提取，非独立校勘版本。", "年级标题")


def make_catalog():
    catalog = ["# 古籍参考资料目录", "", f"整理日期：{NOW}。共 {len(BOOKS)} 种书目/语料集（选集与总集存在重叠，不作独立作品数累加）。", "",
               "正文均可离线阅读；简体检索版由 OpenCC t2s 转换，仅为搜索便利，不替代原字形。", "",
               "快速入口：[使用说明](使用说明.md) · [本次扩充清单](本次扩充清单.md) · [神话故事出处索引](神话故事出处索引.md) · [神话故事补充索引](神话故事补充索引.md)", "",
               "| 分类 | 文献 | 数量/范围 | 备注 |", "| --- | --- | --- | --- |"]
    for b in BOOKS:
        number = str(b["entries"]) + b["unit"] if b["entries"] is not None else "见版本说明"
        catalog.append(f"| {b['category']} | [{b['title']}]({b['text_path']}) | {number} | {b['note']} |")
    catalog.extend(["", "## 来源与使用", "",
        f"- chinese-poetry：固定提交 `{POETRY_SHA}`；保留原JSON、README、MIT LICENSE；每个下载文件核对Git blob SHA。",
        "- Project Gutenberg：保留下载原文及许可头尾；阅读版清理服务头尾、排版空格、分隔线和硬折行，并将回目、卷目独立成段。原页面和文件SHA-256记录在来源清单中。",
        "- Kanripo：保留固定提交的完整ZIP、目录、BASEEDITION与页码标记；阅读版去除排版元数据。具体书库无独立LICENSE，未擅自重标授权。",
        "- 这些是公开电子整理本；错字、缺字、异文及作者争议可能存在，没有完成逐字校勘。取名引用需回看完整语境，并可再用影印本核对。",
        "- 未下载现代在售译注本。现代简介/评论未纳入整理后的古文正文。",
        "- 诗词原始数据中的 prologue、notes 通常单列为来源附序/附注；《花间集》《南唐二主词》的现代notes仅留原档。唐诗有4条、宋诗有23条空正文记录；记录数不代表独立、完整的作品数。",
        "- 原始文件、正文、简体检索版是同一材料的不同保存形式，不能重复计数。",
        "", "## 本地搜索", "", "```bash", 'python3 参考文献/工具/检索.py "望舒"', 'python3 参考文献/工具/检索.py "女娲" --book 淮南子', "```", ""])
    save("目录.md", "\n".join(catalog))
    save("书目索引.json", json.dumps(BOOKS, ensure_ascii=False, indent=2) + "\n")
    save("来源清单.json", json.dumps({"created_at": NOW, "files": MANIFEST}, ensure_ascii=False, indent=2) + "\n")
    checksums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(ROOT)}"
                 for p in sorted(ROOT.rglob("*")) if p.is_file() and p.name != "SHA256SUMS.txt"]
    save("SHA256SUMS.txt", "\n".join(checksums) + "\n")


def supplemental_kanripo():
    def one(book):
        ident, sha = book["repo"], book["commit"]
        url = f"https://codeload.github.com/kanripo/{ident}/zip/{sha}"
        record, data = download(url, f"原始/Kanripo/{ident}_{sha[:12]}.zip")
        record.update(source="Kanripo", repository=f"https://github.com/kanripo/{ident}", commit=sha,
                      license="具体书库未核实到独立LICENSE；保留固定提交、来源标注和版本信息，未重标授权。")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            texts = [(p, archive.read(p).decode("utf-8-sig")) for p in sorted(archive.namelist())
                     if re.search(rf"/{ident}_\d+\.txt$", p)]
        if "max_file" in book:
            texts = [(p, s) for p, s in texts if int(re.search(r"_(\d+)\.txt$", p).group(1)) <= book["max_file"]]
        if "expected_files" in book and len(texts) != book["expected_files"]:
            raise ValueError(f"{book['title']} 来源文件数异常：{len(texts)}")
        if not texts:
            raise ValueError(f"{book['title']} 未找到正文文件")
        return book, record, texts
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for book, record, texts in pool.map(one, EXTRAS.get("kanripo", [])):
            MANIFEST.append(record)
            body = "\n\n".join(clean_kanripo(s) for _, s in texts)
            add_book(book["title"], book["category"], body, [record["local_path"]],
                     book.get("count"), book["note"], book.get("unit", "条"))


def supplemental_mythology():
    """复用神话批次的固定版本与分卷核验，统一生成总索引。"""
    spec = importlib.util.spec_from_file_location("mythology_batch", Path(__file__).with_name("增补神话.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for title, body, record, expected, note in pool.map(module.load_book, module.SELECTIONS):
            MANIFEST.append(record)
            add_book(title, "05_神话传说", body, [record["local_path"]], expected, note, "卷")
    MANIFEST.extend(module.rejected_sources())


NOW = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
if __name__ == "__main__":
    poetry()
    gutenberg()
    kanripo()
    supplemental_kanripo()
    supplemental_mythology()
    make_catalog()
    print(f"完成：{len(BOOKS)} 种书目，{len(MANIFEST)} 个来源文件。", flush=True)
