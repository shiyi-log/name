#!/usr/bin/env python3
"""下载神话、仙传和志怪古籍；总目录由主流程合并，避免覆盖其他批次。"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import io
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("classics_download", Path(__file__).with_name("下载整理.py"))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

# 固定至已核对的原始仓库提交；expected为实际正文分卷文件数。
SELECTIONS = [
    ("列仙传", "KR5a0306", "82f25730b71f2e01b2c4c0644dbb50322297f950", 2,
     "上下二卷；保留道藏本原文与赞，旧题刘向撰，作者归属须辨；不采用现代译注本。"),
    ("穆天子传", "KR3l0092", "683f28717234d3eae26f5f005614096524d22992", 6,
     "六卷，郭璞注；古文与旧注同存，存在阙文，非现代神话改写。"),
    ("神异经", "KR3l0093", "10a3ad53f42138b35b1d0210306843082d95de97", 1,
     "一卷；旧题东方朔，原提要已质疑署名；保留来源缺字实体。"),
    ("海内十洲记", "KR3l0094", "e3bf4318f56806cb1e3b57f36f6f0e33c8e63c60", 1,
     "一卷，十洲及附记；旧题东方朔，成书与署名有争议；同一原文件内提要与正文已分开。"),
    ("洞冥记", "KR3l0097", "ec0a78f55a1557cbf7741c07d958d7f779d8e504", 4,
     "四卷；旧题郭宪，记录汉武帝及东方朔等神异故事，不能当作实录。"),
    ("拾遗记", "KR3l0098", "68d09673bbae1688a935a53a8c5eaa179ac022de", 10,
     "十卷；旧题王嘉，梁萧绮搜辑，保留古注及录语；原十九卷本并非完整传世。"),
    ("搜神后记", "KR3l0100", "a582ba42a250ed54fdee1876b6a6703ad163557d", 10,
     "十卷，旧题陶潜；署名有争议，与《搜神记》分别保存。"),
    ("续齐谐记", "KR3l0102", "f1b93d53adedf7d5be957516077b9eeef308525a", 1,
     "一卷，吴均；含节俗、神异与传说材料，不等于《齐谐记》全本。"),
    ("宣室志", "KR3l0110", "0dbd752d1bf3579026225eebf47e6ae6112ec377", 11,
     "十卷及补遗一卷，张读；唐代志怪笔记。"),
    ("太平广记", "KR3l0118", "7990feeb9c302e37942661d1b29abc22818cc204", 500,
     "五百卷，李昉等编；按原卷顺序汇集，卷中引书出处随正文保留；与其他志怪原典有重叠。"),
    ("夷坚志_四库五十卷本", "KR3l0122", "060e06390006ebd8779cc11bf221184374702734", 50,
     "甲、乙、丙、丁、戊各十卷，合五十卷；仓库名虽题夷坚志甲，正文实际含五集；不是洪迈全书。"),
    ("博物志", "KR3l0123", "56dade949bb60b0d9d7422a5909f567995e5f3f7", 10,
     "十卷，旧题张华；博物、地理、异闻混合，传本文字并非科学事实或医学建议。"),
    ("述异记_任昉题本", "KR3l0124", "b2ef3b2acb29a427ed9090d402e5cbd935f4e878", 2,
     "上下二卷，旧题任昉；区别于祖冲之同名书，作者归属有争议。"),
    ("续幽怪录", "KR3l0126", "beb48ea0a1ec1135fbc7a75a76428bad1809df00", 4,
     "四卷，李复言；正文另附原始目录和后序于来源归档；与通行续玄怪录题名相关。"),
    ("续博物志", "KR3l0128", "bd911b48851478e21b0442b4e017087ccbd965c5", 10,
     "十卷，宋李石；区别于晋张华《博物志》，不沿用旧本误题晋李石。"),
]


def rejected_sources():
    """保留有问题的已下载原档和诊断，不让其混入阅读、检索书库。"""
    ident, sha = "KR3l0125", "feb12bdf04173f7ccddbc09df64fd1ae2a23093a"
    record, _ = base.download(f"https://codeload.github.com/kanripo/{ident}/zip/{sha}",
                              f"原始/Kanripo/{ident}_{sha[:12]}.zip")
    record.update(source="Kanripo", repository=f"https://github.com/kanripo/{ident}", commit=sha,
                  license="具体书库未核实到独立LICENSE；只保留原始来源，未重标授权。",
                  excluded_from_reading=True,
                  exclusion_reason="酉阳杂俎源文件有前集/续集混合，卷二、卷十五夹入其他文献，且缺前集卷十八；暂不纳入正文和简体检索。")
    return [record]


def load_book(selection):
    title, ident, sha, expected, note = selection
    record, data = base.download(
        f"https://codeload.github.com/kanripo/{ident}/zip/{sha}",
        f"原始/Kanripo/{ident}_{sha[:12]}.zip",
    )
    record.update(source="Kanripo", repository=f"https://github.com/kanripo/{ident}", commit=sha,
                  license="具体书库未发现LICENSE；保留完整归档、BASEEDITION、旧注与标记，未重标MIT或CC0。")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        texts = [(p, archive.read(p).decode("utf-8-sig")) for p in sorted(archive.namelist())
                 if re.search(rf"/{ident}_\d+\.txt$", p)]
    if ident == "KR3l0094":
        assert len(texts) == 1
        text = texts[0][1]
        marker = "#+PROPERTY: JUAN 海内十洲記"
        assert marker in text
        texts = [(texts[0][0], text.split(marker, 1)[1])]
    else:
        texts = [(p, s) for p, s in texts if not p.endswith("_000.txt")]
    if ident == "KR3l0126":
        # 005号是后序，不冒充第五卷。
        texts = [(p, s) for p, s in texts if not p.endswith("_005.txt")]
    assert len(texts) == expected, (title, len(texts), expected)
    assert all(re.search(r"[\u3400-\u9fff]", s) for _, s in texts)
    if ident == "KR3l0125":
        assert "前集卷之十九" in texts[-2][1]
        assert "前集卷之二十" in texts[-1][1]
    record["body_part_count"] = len(texts)
    record["edition"] = "HFL" if ident == "KR5a0306" else "SBCK" if ident in ("KR3l0125", "KR3l0126") else "WYG"
    body = "\n\n".join(base.clean_kanripo(s) for _, s in texts)
    # 道藏版本偶有md/pb里程标记，读本移除，完整标记仍在ZIP内。
    body = re.sub(r"<(?:md|pb):[^>]+>", "", body)
    return title, body, record, expected, note


def main():
    if base.CONVERTER is None:
        raise RuntimeError("需要安装 requirements.txt 中的OpenCC后再运行，以生成简体检索版。")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for title, body, record, expected, note in pool.map(load_book, SELECTIONS):
            base.MANIFEST.append(record)
            base.add_book(title, "05_神话传说", body, [record["local_path"]], expected, note, "卷")
    base.MANIFEST.extend(rejected_sources())
    base.save("来源/增补神话_书目.json", json.dumps(base.BOOKS, ensure_ascii=False, indent=2) + "\n")
    base.save("来源/增补神话_文件.json", json.dumps(base.MANIFEST, ensure_ascii=False, indent=2) + "\n")
    print(f"神话批次完成：{len(base.BOOKS)}种；已输出独立书目与来源清单，未改写总索引。", flush=True)


if __name__ == "__main__":
    main()
