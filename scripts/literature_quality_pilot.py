#!/usr/bin/env python3
"""Run a small, deterministic quality screen over local literature snippets.

This command is deliberately a *pilot*: it creates an auditable review pool and
never writes to ``data/names.db`` or to the production candidate snapshots.
Every sampled window keeps its source path, line, file hash, and context so that
the result can be reviewed before any name is admitted to the database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable

from pypinyin.phrases_dict import phrases_dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scoring import BRAND_CLICHES, IDIOMS


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "参考文献" / "简体检索"
DEFAULT_OUTPUT = ROOT / "data" / "literature-pilots"
CHINESE_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
CHINESE_NAME = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]{2,4}$")

# These are intentionally explicit, conservative review rules.  They are not
# a semantic model and a survivor is still only a human/model review candidate.
FUNCTION_CHARS = set("之乎者也而其以于与及或若则乃焉矣兮哉所无非不未莫何孰安谁")
NARRATIVE_TERMS = {
    "不可", "不能", "不得", "不见", "不敢", "不知", "不及", "不若", "以为",
    "何处", "如何", "有人", "此人", "其人", "我们", "你们", "下同", "无名氏",
    "将军", "刺史", "节度使", "国子监", "大舅子", "夫人", "大夫", "公子", "皇帝",
    "天子", "先生", "女子", "男子", "弟子", "儿子", "脱衣服", "酸梅汤", "家蚕",
    "跳月", "度曲", "半空", "身价", "有害", "埋葬", "冷汗",
}
NEGATIVE_TERMS = {
    "死亡", "死者", "死伤", "杀人", "杀戮", "流血", "血流", "战场", "战乱", "叛乱",
    "亡国", "破国", "国破", "家破", "家亡", "家破人亡", "祸从天降", "豺狼当路",
    "冢中枯骨", "大败", "有罪", "得罪", "艰难", "兴亡", "苦难", "悲凉", "有病",
    "疾病", "奸邪", "灾害", "灾祸", "兵乱", "饥荒", "暴乱", "刑罚", "囚徒",
}
# High-frequency office/grammar words make a poor first-pass company-name pool.
GENERIC_TERMS = {
    "人间", "天地", "君子", "春风", "江南", "何处", "将军", "夫人", "大夫", "天下",
    "世间", "当时", "今日", "明日", "昔日", "前日", "一时", "当年", "此地", "此间",
    "其中", "于是", "因此", "然后", "如今", "后来", "以前", "之后", "左右", "上下",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def read_known_names() -> set[str]:
    """Read existing names without modifying any production data."""
    names: set[str] = set()
    db = ROOT / "data" / "names.db"
    if db.exists():
        uri = f"file:{db.resolve()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as conn:
                names.update(row[0] for row in conn.execute("SELECT name FROM name_candidates"))
        except sqlite3.Error as error:
            raise RuntimeError("无法读取现有数据库，停止去重而不是把旧名当新名") from error
    for relative in (
        "data/candidates.json", "data/legacy-curated-names.json", "index.json",
        "pushed_names.json", "restored_index_all_history.json",
        "restored_pushed_names_all_history.json",
    ):
        path = ROOT / relative
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("pushed_names", payload.get("candidates", []))
        names.update(row["name"] for row in rows if isinstance(row, dict) and "name" in row)
        if isinstance(payload, dict):
            names.update(payload.get("names", []))
    for relative in (
        "已排除名字.md", "候选.txt", "状态.txt",
        "restored_candidates_all_history.txt", "restored_status_all_history.txt",
    ):
        path = ROOT / relative
        if path.exists():
            names.update(re.findall(r"(?<![\u4e00-\u9fff])[\u4e00-\u9fff]{2,4}(?![\u4e00-\u9fff])", path.read_text(encoding="utf-8")))
    return names


def is_metadata(line: str) -> bool:
    return bool(re.match(
        r"^\s*(?:#{1,6}\s|【|(?:来源|书名|题名|作者|底本|说明|整理说明|原始文件|本文件|简体检索|正文字符|更新时间)\s*[:：])",
        line,
    )) or "目录.md" in line or "来源清单.json" in line


def name_windows(line: str, lexicon: set[str] | None = None) -> Iterable[tuple[str, int, int]]:
    for run in CHINESE_RUN.finditer(line):
        text = run.group(0)
        for length in (2, 3, 4):
            for offset in range(0, len(text) - length + 1):
                name = text[offset : offset + length]
                if lexicon is None or name in lexicon:
                    yield name, run.start() + offset, length


def line_records(
    path: Path, file_hash: str, line_number: int, line: str, lexicon: set[str] | None = None
) -> Iterable[dict]:
    """Yield direct contiguous windows from one selected line."""
    for name, start, length in name_windows(line, lexicon):
        end = start + length
        context_start = max(0, start - 48)
        context_end = min(len(line), end + 96)
        yield {
            "name": name,
            "length": length,
            "sourceReference": {
                "book": path.stem,
                "path": display_path(path),
                "line": line_number,
                "lineType": "metadata" if is_metadata(line) else "body",
                "sha256": file_hash,
                "quote": name,
                "context": line[context_start:context_end],
            },
            "extractionType": "dictionary-word-match" if lexicon is not None else "direct-contiguous-window",
            "candidateBasis": "pypinyin.phrases_dict exact word match" if lexicon is not None else "all contiguous Chinese windows",
        }


def load_lexicon() -> set[str]:
    characters = json.loads((ROOT / "data" / "characters-8105.json").read_text(encoding="utf-8"))
    allowed = set(characters[:6500])
    return {
        word for word in phrases_dict
        if 2 <= len(word) <= 4 and all(char in allowed for char in word)
    }


def stratified_sample(
    paths: list[Path], limit: int, seed: int, lexicon: set[str] | None = None
) -> tuple[list[dict], int, int]:
    """Sample book-balanced windows, or dispatch to unique-lexicon sampling.

    Sampling lines before materializing windows avoids allocating tens of
    millions of temporary Python records for a small review pilot.
    """
    if not paths or limit < 1:
        return [], 0
    if lexicon is not None:
        return unique_lexicon_sample(paths, limit, seed, lexicon)
    quotas = [limit // len(paths)] * len(paths)
    for index in range(limit % len(paths)):
        quotas[index] += 1
    selected: list[dict] = []
    total = 0
    for path, quota in zip(paths, quotas):
        file_hash = sha256_file(path)
        rng = random.Random(f"{seed}:{display_path(path)}")
        line_reservoir: list[tuple[int, str]] = []
        eligible_line_count = 0
        with path.open(encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                line = raw_line.rstrip("\r\n")
                line_windows = sum(
                    max(0, len(match.group(0)) - length + 1)
                    for match in CHINESE_RUN.finditer(line)
                    for length in (2, 3, 4)
                )
                total += line_windows
                if not line_windows or quota == 0:
                    continue
                eligible_line_count += 1
                if len(line_reservoir) < quota:
                    line_reservoir.append((line_number, line))
                else:
                    index = rng.randrange(eligible_line_count)
                    if index < quota:
                        line_reservoir[index] = (line_number, line)
        window_reservoir: list[dict] = []
        window_count = 0
        for line_number, line in line_reservoir:
            for record in line_records(path, file_hash, line_number, line):
                window_count += 1
                if len(window_reservoir) < quota:
                    window_reservoir.append(record)
                else:
                    index = rng.randrange(window_count)
                    if index < quota:
                        window_reservoir[index] = record
        selected.extend(window_reservoir)
    # Sorting makes the artifact stable and easy to review regardless of the
    # order in which files were traversed.
    selected.sort(key=lambda item: (item["sourceReference"]["path"], item["sourceReference"]["line"], item["name"]))
    return selected, total, 0


def unique_lexicon_sample(
    paths: list[Path], limit: int, seed: int, lexicon: set[str]
) -> tuple[list[dict], int, int]:
    """Sample unique dictionary terms, retaining the first auditable occurrence."""
    pool: dict[str, dict] = {}
    occurrences = 0
    for path in paths:
        file_hash = sha256_file(path)
        with path.open(encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                line = raw_line.rstrip("\r\n")
                for record in line_records(path, file_hash, line_number, line, lexicon):
                    occurrences += 1
                    pool.setdefault(record["name"], record)

    rng = random.Random(seed)
    selected: list[dict] = []
    for index, name in enumerate(sorted(pool), 1):
        record = pool[name]
        if index <= limit:
            selected.append(record)
        else:
            replacement = rng.randrange(index)
            if replacement < limit:
                selected[replacement] = record
    selected.sort(key=lambda item: (item["sourceReference"]["path"], item["sourceReference"]["line"], item["name"]))
    return selected, occurrences, len(pool)


def screen(record: dict, known_names: set[str]) -> tuple[dict, str | None]:
    name = record["name"]
    reasons: list[str] = []
    stages = {"shape": False, "dedupe": False, "lexical": False, "semantic": False}
    if not CHINESE_NAME.fullmatch(name) or len(set(name)) < 2:
        reasons.append("shape:字数或字符结构不适合作为名称")
    else:
        stages["shape"] = True
    if name in known_names:
        reasons.append("dedupe:已存在于数据库或本地排除/候选记录")
    elif stages["shape"]:
        stages["dedupe"] = True
    if stages["dedupe"]:
        if record["sourceReference"].get("lineType") == "metadata":
            reasons.append("lexical:整理元数据或标题，不是文献正文")
        elif name in IDIOMS:
            reasons.append("lexical:命中项目成语/套话表")
        elif name in BRAND_CLICHES:
            reasons.append("lexical:命中通用行业或品牌结构词")
        else:
            stages["lexical"] = True
    if stages["lexical"]:
        if any(term in name for term in NEGATIVE_TERMS):
            reasons.append("semantic:负面、灾害或冲突含义")
        elif any(term in name for term in NARRATIVE_TERMS):
            reasons.append("semantic:叙事、人物、官职或功能表达")
        elif name in GENERIC_TERMS:
            reasons.append("semantic:过于通用，缺少公司名识别度")
        elif any(char in FUNCTION_CHARS for char in name):
            reasons.append("semantic:含高频虚词/功能字，语义不完整")
        else:
            stages["semantic"] = True
    first_reason = reasons[0] if reasons else None
    record["screening"] = {
        "stages": stages,
        "reasons": reasons,
        "reviewStatus": "review-pool" if stages["semantic"] else "rejected-by-pilot",
        "finalAcceptance": False,
        "knownNameMatch": name in known_names,
    }
    return record, first_reason


def build_markdown(report: dict) -> str:
    stats = report["statistics"]
    lines = [
        f"# 文献取名质量试跑（{report['pilotId']}）",
        "",
        "本报告只建立可审阅候选池，不把任何原文窗口直接认定为合格公司名。试跑不写入 `data/names.db` 或生产候选快照；通过确定性规则的记录仍需人工或模型复核。",
        "",
        f"- 来源目录：`{report['sourceDirectory']}`",
        f"- 固定种子：`{report['seed']}`；全库分层抽样总上限：`{report['sampleLimit']}`",
        f"- 文件数：{stats['sourceFileCount']}；扫描来源出现次数：{stats['rawWindowCount']:,}；抽样记录：{stats['sampleRecordCount']:,}",
        f"- 去重后的抽样名称：{stats['uniqueNameCount']:,}",
        f"- 形状通过：{stats['shapePassed']:,}；去重通过：{stats['dedupePassed']:,}；词法通过：{stats['lexicalPassed']:,}；语义规则通过（待复核）：{stats['semanticPassed']:,}",
        f"- 规则筛选池比例（相对抽样记录）：{stats['rulePoolRateOfSample']:.2%}",
        f"- 本轮正式认定合格并入库：{stats['acceptedCompanyNameCount']}；仅凭这些确定性规则无法证明名称语义完整和适合作为公司名。",
        "",
        "## 不合格原因（按首次拦截）",
        "",
        "| 原因 | 数量 |",
        "| --- | ---: |",
    ]
    if report["candidateMode"] == "lexicon":
        lines.insert(
            5,
            f"- 词典词条：{report['lexiconEntryCount']:,}；不同文献匹配词：{report['distinctLexiconCandidateCount']:,}；匹配出现次数：{stats['rawWindowCount']:,}",
        )
    for reason, count in report["rejectionReasons"].items():
        lines.append(f"| {reason} | {count:,} |")
    lines.extend(["", "## 待复核样例", "", "| 名称 | 书名 | 行号 | 原文上下文 |", "| --- | --- | ---: | --- |"])
    survivors = [item for item in report["records"] if item["screening"]["reviewStatus"] == "review-pool"]
    for item in survivors[:100]:
        source = item["sourceReference"]
        context = source["context"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| **{item['name']}** | {source['book']} | {source['line']} | {context} |")
    if not survivors:
        lines.append("| （无） |  |  | 本次抽样没有通过确定性语义规则的记录。 |")
    lines.extend(["", "> 通过规则只代表进入 review-pool，不代表企业字号、商标、域名或社交账号可用；这些状态均未查询。"])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample-limit", type=int, default=1200)
    parser.add_argument("--candidate-mode", choices=("windows", "lexicon"), default="windows")
    parser.add_argument("--seed", type=int, default=20261105)
    parser.add_argument("--pilot-id", default="literature-quality-2026-11-05-01")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.sample_limit < 500 or args.sample_limit > 2000:
        raise SystemExit("--sample-limit 必须在 500 到 2000 之间")
    source = args.source.resolve()
    paths = sorted(source.rglob("*.txt"))
    if not paths:
        raise SystemExit(f"未找到文本来源：{source}")
    lexicon = load_lexicon() if args.candidate_mode == "lexicon" else None
    records, raw_count, unique_lexicon_candidates = stratified_sample(
        paths, args.sample_limit, args.seed, lexicon
    )
    known_names = read_known_names()
    screened: list[dict] = []
    reason_counts: Counter[str] = Counter()
    seen_names: set[str] = set()
    for index, record in enumerate(records, 1):
        record["recordId"] = f"p{index:04d}"
        screened_record, first_reason = screen(record, known_names)
        if record["name"] in seen_names and screened_record["screening"]["stages"]["shape"]:
            first_reason = "dedupe:本次抽样中重复名称"
            screened_record["screening"] = {
                "stages": {"shape": True, "dedupe": False, "lexical": False, "semantic": False},
                "reasons": [first_reason], "reviewStatus": "rejected-by-pilot", "finalAcceptance": False,
                "knownNameMatch": record["name"] in known_names,
            }
        seen_names.add(record["name"])
        screened.append(screened_record)
        if first_reason:
            reason_counts[first_reason] += 1
    unique_names = len({item["name"] for item in screened})
    stats = {
        "sourceFileCount": len(paths),
        "rawWindowCount": raw_count,
        "sampleRecordCount": len(screened),
        "uniqueNameCount": unique_names,
        "shapePassed": sum(item["screening"]["stages"]["shape"] for item in screened),
        "dedupePassed": sum(item["screening"]["stages"]["dedupe"] for item in screened),
        "lexicalPassed": sum(item["screening"]["stages"]["lexical"] for item in screened),
        "semanticPassed": sum(item["screening"]["stages"]["semantic"] for item in screened),
        "acceptedCompanyNameCount": 0,
    }
    stats["rulePoolRateOfSample"] = stats["semanticPassed"] / len(screened) if screened else 0.0
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report = {
        "schemaVersion": 1,
        "pilotId": args.pilot_id,
        "generatedAt": generated_at,
        "seed": args.seed,
        "sampleLimit": args.sample_limit,
        "sourceDirectory": display_path(source),
        "candidateMode": args.candidate_mode,
        "method": (
            "在简体文献中精确检索 pypinyin.phrases_dict 的二至四字词条，从不同匹配词条中按固定种子等概率抽样；"
            if lexicon is not None else
            "每本简体文献按固定种子抽样所有连续二至四字窗口；"
        ) + "随后做形状、项目内去重、成语/行业套话、负面/叙事/功能词和通用度规则。规则通过记录仅供语义复核。",
        "limitations": [
            "rawWindowCount 是含重复的来源匹配出现次数，不是不同名称数。",
            "窗口模式按书等额、先抽行再抽窗口，不能外推全库通过率；词典模式按全库不同词条抽样，每个名称只保留首个来源。",
            "规则拦截存在误放与误杀，必须逐条语义复核；本批校准结果还需独立样本验证。",
        ],
        "knownNameCount": len(known_names),
        "knownNamesInSample": sorted({record["name"] for record in screened if record["screening"]["knownNameMatch"]}),
        "lexiconEntryCount": len(lexicon) if lexicon is not None else None,
        "distinctLexiconCandidateCount": unique_lexicon_candidates if lexicon is not None else None,
        "statistics": stats,
        "rejectionReasons": dict(reason_counts.most_common()),
        "records": screened,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{args.pilot_id}.json"
    md_path = args.output_dir / f"{args.pilot_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(build_markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), "statistics": stats}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
