import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.literature_quality_pilot import line_records, screen, sha256_file, stratified_sample
from scripts.review_literature_pilot import apply_review, fingerprint, verify_sources


class LiteraturePilotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "示例文献.txt"
        self.source.write_text("来源：详见目录.md\n月照星桥。\n", encoding="utf-8")
        record = list(line_records(self.source, sha256_file(self.source), 2, "月照星桥。"))[-1]
        # Use a complete two-character phrase with a real, local source.
        record["name"] = record["sourceReference"]["quote"] = "星桥"
        record["sourceReference"]["path"] = self.source.name
        record["length"] = 2
        record["recordId"] = "p0001"
        record, _ = screen(record, set())
        self.report = {"pilotId": "test", "records": [record], "knownNamesInSample": [],
                       "statistics": {"semanticPassed": 1, "sampleRecordCount": 1, "uniqueNameCount": 1,
                                      "shapePassed": 1, "dedupePassed": 1, "lexicalPassed": 1,
                                      "acceptedCompanyNameCount": 0}}
        self.review = {"pilotId": "test", "sampleFingerprint": fingerprint(self.report),
                       "reviewMethod": "AI 编辑复核测试", "records": [
                           {"recordId": "p0001", "name": "星桥", "decision": "accept", "reason": "完整连接意象",
                            "brandMeaning": "建立连接", "direction": "协作服务", "risk": "未查询注册"}]}

    def test_complete_review_counts_only_accepted_unique_names_without_mutating_input(self):
        before = json.dumps(self.report, ensure_ascii=False, sort_keys=True)
        verify_sources(self.report, self.root)
        result = apply_review(self.report, self.review, self.root)
        self.assertEqual(result["statistics"]["acceptedCompanyNameCount"], 1)
        self.assertEqual(result["statistics"]["persistedCompanyNameCount"], 0)
        self.assertTrue(result["records"][0]["screening"]["finalAcceptance"])
        self.assertEqual(before, json.dumps(self.report, ensure_ascii=False, sort_keys=True))

    def test_incomplete_duplicate_stale_and_renamed_decisions_fail_closed(self):
        reviews = []
        missing = copy.deepcopy(self.review)
        missing["records"] = []
        reviews.append(missing)
        duplicate = copy.deepcopy(self.review)
        duplicate["records"] *= 2
        reviews.append(duplicate)
        stale = copy.deepcopy(self.review)
        stale["sampleFingerprint"] = "0" * 64
        reviews.append(stale)
        renamed = copy.deepcopy(self.review)
        renamed["records"][0]["name"] = "其他"
        reviews.append(renamed)
        for review in reviews:
            with self.subTest(review=review), self.assertRaises(ValueError):
                apply_review(self.report, review, self.root)

    def test_review_cannot_promote_metadata_old_names_or_missing_brand_evidence(self):
        for change in ("metadata", "known-name", "missing-risk"):
            report, review = copy.deepcopy(self.report), copy.deepcopy(self.review)
            if change == "metadata":
                report["records"][0]["sourceReference"]["lineType"] = "metadata"
            elif change == "known-name":
                report["records"][0], _ = screen(report["records"][0], {"星桥"})
            else:
                review["records"][0].pop("risk")
            review["sampleFingerprint"] = fingerprint(report)
            with self.subTest(change=change), self.assertRaises(ValueError):
                apply_review(report, review, self.root)

    def test_tampered_stages_and_statistics_are_rejected_even_with_new_fingerprint(self):
        for change in ("empty-stages", "missing-stage", "forged-count"):
            report, review = copy.deepcopy(self.report), copy.deepcopy(self.review)
            if change == "empty-stages":
                report["records"][0]["screening"]["stages"] = {}
            elif change == "missing-stage":
                report["records"][0]["screening"]["stages"].pop("semantic")
            else:
                report["statistics"]["semanticPassed"] = 0
            review["sampleFingerprint"] = fingerprint(report)
            with self.subTest(change=change), self.assertRaises(ValueError):
                apply_review(report, review, self.root)

    def test_changed_source_and_fabricated_context_are_rejected(self):
        altered = copy.deepcopy(self.report)
        altered["records"][0]["sourceReference"]["context"] = "从来不存在星桥"
        with self.assertRaises(ValueError):
            verify_sources(altered, self.root)
        self.source.write_text("来源：说明\n文件已改变。\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            verify_sources(self.report, self.root)

    def test_metadata_is_rejected_and_sampling_is_repeatable(self):
        record = next(line_records(self.source, sha256_file(self.source), 1, "来源：详见目录.md"))
        record, _ = screen(record, set())
        self.assertFalse(record["screening"]["stages"]["lexical"])
        first, count = stratified_sample([self.source], 10, 23)
        second, other_count = stratified_sample([self.source], 10, 23)
        self.assertEqual(first, second)
        self.assertEqual(count, other_count)


if __name__ == "__main__":
    unittest.main()
