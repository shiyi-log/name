#!/usr/bin/env python3
"""Validate a complete editorial review and attach it to a literature pilot.

No production database or candidate snapshot is written. Review coverage and
source integrity must both pass before any record can count as accepted.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

try:
    from .literature_quality_pilot import ROOT, is_metadata, screen, sha256_file
except ImportError:
    from literature_quality_pilot import ROOT, is_metadata, screen, sha256_file


def fingerprint(report: dict) -> str:
    """Bind decisions to the sampled names and their exact source evidence."""
    body = {
        "pilotId": report["pilotId"],
        "candidateMode": report.get("candidateMode"),
        "method": report.get("method"),
        "lexiconEntryCount": report.get("lexiconEntryCount"),
        "distinctLexiconCandidateCount": report.get("distinctLexiconCandidateCount"),
        "records": report["records"],
        "statistics": report["statistics"],
        "knownNamesInSample": report.get("knownNamesInSample"),
    }
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_screening(report: dict) -> None:
    if report.get("candidateMode") not in {"windows", "lexicon"}:
        raise ValueError("候选抽取模式缺失或不支持")
    records = report.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("抽样记录缺失")
    record_ids = [record.get("recordId") for record in records]
    if record_ids != [f"p{index:04d}" for index in range(1, len(records) + 1)]:
        raise ValueError("抽样编号无效或记录顺序改变")
    names = [record.get("name") for record in records]
    known_names = report.get("knownNamesInSample")
    if not isinstance(known_names, list) or any(not isinstance(name, str) for name in known_names):
        raise ValueError("缺少本批已知名称去重快照")
    known_set = set(known_names)
    if len(known_set) != len(known_names) or not known_set.issubset(set(names)):
        raise ValueError("本批已知名称去重快照无效")

    first_seen: set[str] = set()
    counts = {"shapePassed": 0, "dedupePassed": 0, "lexicalPassed": 0, "semanticPassed": 0}
    for record in records:
        source = record.get("sourceReference")
        original_screening = record.get("screening")
        if not isinstance(source, dict) or not isinstance(original_screening, dict):
            raise ValueError(f"抽样证据或筛选字段缺失：{record.get('recordId')}")
        name = record.get("name")
        expected_basis = "pypinyin.phrases_dict exact word match" if report["candidateMode"] == "lexicon" else "all contiguous Chinese windows"
        if record.get("candidateBasis") != expected_basis:
            raise ValueError(f"候选来源依据与抽取模式不一致：{record['recordId']}")
        expected, _ = screen(copy.deepcopy(record), known_set)
        if name in first_seen and expected["screening"]["stages"]["shape"]:
            reason = "dedupe:本次抽样中重复名称"
            expected["screening"] = {
                "stages": {"shape": True, "dedupe": False, "lexical": False, "semantic": False},
                "reasons": [reason], "reviewStatus": "rejected-by-pilot", "finalAcceptance": False,
                "knownNameMatch": name in known_set,
            }
        first_seen.add(name)
        if original_screening != expected["screening"]:
            raise ValueError(f"抽样筛选数据被修改或不合法：{record['recordId']}")
        for stage, key in zip(("shape", "dedupe", "lexical", "semantic"), counts):
            counts[key] += int(expected["screening"]["stages"][stage])

    statistics = report.get("statistics")
    if not isinstance(statistics, dict):
        raise ValueError("抽样统计缺失")
    expected_stats = {
        "sampleRecordCount": len(records),
        "uniqueNameCount": len(set(names)),
        **counts,
        "acceptedCompanyNameCount": 0,
    }
    if any(statistics.get(key) != value for key, value in expected_stats.items()):
        raise ValueError("抽样统计与逐条筛选记录不一致")


def verify_sources(report: dict, root: Path = ROOT) -> None:
    cache: dict[str, tuple[str, list[str]]] = {}
    root = root.resolve()
    for record in report["records"]:
        source = record["sourceReference"]
        path = (root / source["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("来源路径超出仓库")
        if source["path"] not in cache:
            file_hash = sha256_file(path)
            if file_hash != source["sha256"]:
                raise ValueError(f"来源文件哈希变化：{source['path']}")
            cache[source["path"]] = (file_hash, path.read_text(encoding="utf-8").splitlines())
        file_hash, lines = cache[source["path"]]
        if file_hash != source["sha256"]:
            raise ValueError(f"同一来源的哈希不一致：{record['recordId']}")
        line_number = source["line"]
        if not isinstance(line_number, int) or not 1 <= line_number <= len(lines):
            raise ValueError("来源行号无效")
        line = lines[line_number - 1]
        if (source["quote"] != record["name"] or source["quote"] not in source["context"]
                or source["context"] not in line):
            raise ValueError(f"原文证据不匹配：{record['recordId']}")
        if source.get("lineType") != ("metadata" if is_metadata(line) else "body"):
            raise ValueError(f"来源正文分类不匹配：{record['recordId']}")


def apply_review(report: dict, review: dict, root: Path = ROOT) -> dict:
    validate_screening(report)
    verify_sources(report, root)
    if report["candidateMode"] == "lexicon":
        try:
            from .literature_quality_pilot import load_lexicon
        except ImportError:
            from literature_quality_pilot import load_lexicon
        lexicon = load_lexicon()
        if any(record["name"] not in lexicon for record in report["records"]):
            raise ValueError("候选并非项目词典中的精确词条")
    if review.get("pilotId") != report["pilotId"] or review.get("sampleFingerprint") != fingerprint(report):
        raise ValueError("审核文件与本次抽样不匹配")
    decisions = review.get("records", [])
    if not isinstance(review.get("reviewMethod"), str) or not review["reviewMethod"].strip():
        raise ValueError("缺少审核方式说明")
    indexed = {row["recordId"]: row for row in decisions}
    expected = {row["recordId"] for row in report["records"]}
    if len(expected) != len(report["records"]) or len(indexed) != len(decisions) or set(indexed) != expected:
        raise ValueError("审核必须覆盖全部样本，且不能有重复或未知编号")
    result = copy.deepcopy(report)
    accepted_names: set[str] = set()
    for record in result["records"]:
        decision = indexed[record["recordId"]]
        if (decision.get("name") != record["name"] or decision.get("decision") not in {"accept", "reject"}
                or not isinstance(decision.get("reason"), str) or not decision["reason"].strip()):
            raise ValueError(f"审核字段无效：{record['recordId']}")
        accepted = decision["decision"] == "accept"
        if accepted:
            stages = record["screening"].get("stages")
            if (not isinstance(stages, dict)
                    or set(stages) != {"shape", "dedupe", "lexical", "semantic"}
                    or any(type(value) is not bool for value in stages.values())
                    or not all(stages.values())
                    or record["sourceReference"].get("lineType") != "body"
                    or record["name"] in accepted_names):
                raise ValueError(f"审核不能绕过基础规则、来源或去重门槛：{record['recordId']}")
            for key in ("brandMeaning", "direction", "risk"):
                if not isinstance(decision.get(key), str) or not decision[key].strip():
                    raise ValueError(f"通过项缺少 {key}：{record['recordId']}")
            accepted_names.add(record["name"])
        record["editorialReview"] = decision
        record["screening"]["finalAcceptance"] = accepted
        record["screening"]["reviewStatus"] = "accepted-editorial" if accepted else "rejected-after-review"
    count = len(accepted_names)
    stats = result["statistics"]
    stats.update({
        "reviewedRecordCount": len(expected),
        "acceptedCompanyNameCount": count,
        "persistedCompanyNameCount": 0,
        "finalAcceptanceRateOfSample": count / len(expected) if expected else 0,
        "reviewPoolPrecision": count / stats["semanticPassed"] if stats["semanticPassed"] else 0,
        "rulePoolRejectedAfterReview": stats["semanticPassed"] - count,
    })
    result["reviewMethod"] = review["reviewMethod"]
    result["verification"] = {"literature": "所有抽样的本地来源哈希、行号和上下文已复核",
                              "companyRegistry": "未查询", "trademark": "未查询", "domains": "未查询"}
    return result


def render_review(report: dict) -> str:
    stats = report["statistics"]
    accepted = [record for record in report["records"] if record["screening"]["finalAcceptance"]]
    lines = [f"# 文献名称语义复核：{report['pilotId']}", "", report["reviewMethod"], "",
             f"抽样 {stats['sampleRecordCount']} 条，规则放行 {stats['semanticPassed']} 条；逐条复核 {stats['reviewedRecordCount']} 条，最终通过 **{len(accepted)}** 个不同名称。",
             f"样本通过率 **{stats['finalAcceptanceRateOfSample']:.2%}**；规则审核池的通过比例 **{stats['reviewPoolPrecision']:.2%}**。",
             "", "通过表示本轮 AI 编辑判定适合进入公司名候选清单，仍是主观判断；企业字号、商标、域名均未查询。本报告不写生产数据库，已入库 0 条。",
             "", "按书等额、先抽行后抽窗口；本批用于校准，不能外推全库通过率或千万规模。扫描窗口总量包含重复出现，不能计为合格名称。", "",
             "## 通过清单", "", "| 名称 | 书名/行号 | 品牌寓意 | 适用方向 | 风险 |", "| --- | --- | --- | --- | --- |"]
    for record in accepted:
        source, decision = record["sourceReference"], record["editorialReview"]
        cells = [record["name"], f"{source['book']}:{source['line']}", decision["brandMeaning"], decision["direction"], decision["risk"]]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in cells) + " |")
    lines.extend(["", "## 未通过样例", "", "| 名称 | 原因 |", "| --- | --- |"])
    rejected = [record for record in report["records"] if not record["screening"]["finalAcceptance"]]
    for record in rejected[:30]:
        lines.append(f"| {record['name']} | {record['editorialReview']['reason'].replace('|', '/')} |")
    lines.extend(["", "完整的逐条理由及来源证据见同名 JSON 文件。", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pilot", type=Path)
    parser.add_argument("review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pilot = json.loads(args.pilot.read_text(encoding="utf-8"))
    review = json.loads(args.review.read_text(encoding="utf-8"))
    result = apply_review(pilot, review)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(render_review(result), encoding="utf-8")
    print(json.dumps(result["statistics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
