from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_database import (
    build_database,
    generate_candidates,
    load_existing_candidates,
    load_generation_state,
    select_unprocessed_poetry_files,
)


def candidate(
    name: str,
    identifier: str,
    score: float,
    status: str = "待核验",
    template: str = "action-image",
) -> dict:
    return {
        "id": identifier,
        "name": name,
        "leftToken": name[:2],
        "rightToken": name[2:],
        "template": template,
        "direction": "探索与科技",
        "pitch": "测试释义",
        "source": "测试来源",
        "tags": ["action", "image"],
        "scores": {"brand": score / 10},
        "score": score,
        "reasons": ["测试理由"],
        "notes": [],
        "status": status,
        "registryNote": "待核验",
    }


class IncrementalGenerationTests(unittest.TestCase):
    def test_legacy_state_read_does_not_create_tables_and_is_imported(self) -> None:
        legacy = {
            "processed_poetry_files": {"poet.song.0.json": "old-sha"},
            "last_source_file": "poet.song.0.json",
        }
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "names.db"
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("CREATE TABLE legacy_data (name TEXT)")
                connection.commit()
            with patch("build_database.legacy_generation_state", return_value=legacy):
                state = load_generation_state(db_path)
                with closing(sqlite3.connect(db_path)) as connection:
                    self.assertEqual(
                        connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                        [("legacy_data",)],
                    )
                build_database(list("甲乙"), {"actions": [], "images": [], "abstracts": []}, [], db_path)

            self.assertEqual(state["processed_poetry_files"], legacy["processed_poetry_files"])
            with patch("build_database.legacy_generation_state", return_value={}):
                imported = load_generation_state(db_path)
            self.assertEqual(imported["processed_poetry_files"], legacy["processed_poetry_files"])
            self.assertEqual(imported["last_source_file"], legacy["last_source_file"])

    def test_old_two_column_source_table_is_migrated_without_losing_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "names.db"
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("CREATE TABLE poetry_sources (path TEXT PRIMARY KEY, sha TEXT NOT NULL)")
                connection.execute("INSERT INTO poetry_sources VALUES ('poet.song.0.json', 'db-sha')")
                connection.commit()
            with patch("build_database.legacy_generation_state", return_value={
                "processed_poetry_files": {"poet.song.0.json": "stale-sha", "poet.song.1.json": "legacy-sha"},
            }):
                state = load_generation_state(db_path)
                self.assertEqual(state["processed_poetry_files"]["poet.song.0.json"], "db-sha")
                state["processed_poetry_files"]["poet.song.2.json"] = "new-sha"
                state["last_source_file"] = "poet.song.2.json"
                build_database(list("甲乙"), {"actions": [], "images": [], "abstracts": []}, [], db_path, state)
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertIn("processed_at", {row[1] for row in connection.execute("PRAGMA table_info(poetry_sources)")})
                self.assertEqual(dict(connection.execute("SELECT path, sha FROM poetry_sources")), {
                    "poet.song.0.json": "db-sha",
                    "poet.song.1.json": "legacy-sha",
                    "poet.song.2.json": "new-sha",
                })

    def test_ai_metadata_and_review_status_survive_rescoring_and_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "names.db"
            characters = list("甲乙丙丁")
            tokens = {"actions": ["甲乙"], "images": ["丙丁"], "abstracts": []}
            original = candidate("甲乙", "ai-stable-id", 82, status="已核验", template="ai-generated")
            original.update({"sourceType": "ai", "aiModel": "test-model", "source": "AI test-model"})
            build_database(characters, tokens, [original], db_path)
            loaded = load_existing_candidates(db_path)
            generated, _, _, _ = generate_candidates(characters, tokens, set(), loaded, {})
            ai_item = next(item for item in generated if item["name"] == "甲乙")
            self.assertEqual(
                {key: ai_item[key] for key in ("id", "template", "sourceType", "aiModel", "status", "source")},
                {key: original[key] for key in ("id", "template", "sourceType", "aiModel", "status", "source")},
            )
            build_database(characters, tokens, generated, db_path)
            rebuilt = next(item for item in load_existing_candidates(db_path) if item["name"] == "甲乙")
            self.assertEqual(rebuilt["aiModel"], "test-model")
            self.assertEqual(rebuilt["sourceType"], "ai")
            self.assertEqual(rebuilt["status"], "已核验")

    def test_loader_supports_legacy_candidate_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "names.db"
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("""CREATE TABLE name_candidates (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE, left_token TEXT, right_token TEXT,
                    template TEXT, direction TEXT, pitch TEXT, source TEXT, tags_json TEXT,
                    scores_json TEXT, total_score REAL, reasons_json TEXT, notes_json TEXT,
                    status TEXT, registry_note TEXT)""")
                connection.execute("""INSERT INTO name_candidates VALUES (
                    'old-id', '甲乙丙丁', '甲乙', '丙丁', 'poetry-seed', '东方与质感', '释义',
                    '诗词来源', '[]', '{}', 80, '[]', '[]', '已核验', '备注')""")
                connection.commit()
            loaded = load_existing_candidates(db_path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["sourceType"], "poetry")
            self.assertEqual(loaded[0]["aiModel"], "")
            self.assertEqual(loaded[0]["status"], "已核验")

    def test_literature_provenance_survives_generation_rebuild_and_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "names.db"
            characters = list("甲乙丙丁")
            tokens = {"actions": ["甲乙"], "images": ["丙丁"], "abstracts": []}
            original = candidate(
                "甲乙丙丁", "literature-stable-id", 82,
                status="出处已核对", template="literature-curated",
            )
            original.update({
                "source": "《梦溪笔谈》卷十八·技艺；正文第649行",
                "tags": ["文献取意", "协作软件"],
                "notes": ["原文：常作二鐵板，一板印刷，一板已自布字。", "名称为现代改写。"],
                "registryNote": "出处已核对；工商、商标与域名待核验。",
            })
            build_database(characters, tokens, [original], db_path)
            loaded = load_existing_candidates(db_path)
            self.assertEqual(loaded[0]["sourceType"], "literature")
            generated, _, source_counts, _ = generate_candidates(characters, tokens, set(), loaded, {})
            literature_item = next(item for item in generated if item["name"] == original["name"])
            preserved = ("id", "template", "source", "tags", "notes", "status", "registryNote")
            self.assertEqual(
                {key: literature_item[key] for key in preserved},
                {key: original[key] for key in preserved},
            )
            self.assertEqual(literature_item["sourceType"], "literature")
            self.assertEqual(source_counts["literature"], 1)
            self.assertEqual(source_counts["rule"], len(generated) - 1)

            build_database(characters, tokens, generated, db_path)
            # Empty rebuilds still run the source-type migration on existing rows.
            build_database(characters, tokens, [], db_path)
            rebuilt = next(item for item in load_existing_candidates(db_path) if item["name"] == original["name"])
            self.assertEqual(rebuilt["sourceType"], "literature")
            self.assertEqual(
                {key: rebuilt[key] for key in preserved},
                {key: original[key] for key in preserved},
            )

    def test_manifest_selection_has_no_fixed_shard_ceiling(self) -> None:
        manifest = [
            {"path": f"poet.tang.{index}.json", "sha": f"sha-{index}"}
            for index in range(400)
        ]
        processed = {
            item["path"]: item["sha"]
            for item in manifest[:320]
        }

        batch = select_unprocessed_poetry_files(
            {"processed_poetry_files": processed}, manifest, count=2
        )

        self.assertEqual([item["path"] for item in batch], [
            "poet.tang.320.json",
            "poet.tang.321.json",
        ])

    def test_existing_mirror_names_are_never_removed(self) -> None:
        first = candidate("甲乙丙丁", "c00001", 82, template="action-image")
        mirror = candidate("丙丁甲乙", "c00002", 80, template="image-action")

        generated, _, _, _ = generate_candidates(
            list("甲乙丙丁"),
            {"actions": ["甲乙"], "images": ["丙丁"], "abstracts": []},
            set(),
            [first, mirror],
            {},
        )

        self.assertTrue({"甲乙丙丁", "丙丁甲乙"}.issubset({item["name"] for item in generated}))

    def test_database_adds_candidates_without_removing_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "names.db"
            characters = list("甲乙丙丁戊己")
            tokens = {"actions": ["甲乙"], "images": ["丙丁"], "abstracts": []}
            first = candidate("甲乙丙丁", "c00001", 82, status="已核验")
            second = candidate("戊己丙丁", "c00002", 78)

            build_database(characters, tokens, [first], db_path)
            build_database(characters, tokens, [first, second], db_path)

            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute(
                    "SELECT id, name, status FROM name_candidates ORDER BY name"
                ).fetchall()

        self.assertEqual(
            {name: (identifier, status) for identifier, name, status in rows},
            {
                "甲乙丙丁": ("c00001", "已核验"),
                "戊己丙丁": ("c00002", "待核验"),
            },
        )


if __name__ == "__main__":
    unittest.main()
