import hashlib
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class CcSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.repo = self.root / "catalog"
        (self.home / ".cc-switch").mkdir(parents=True)
        self.repo.mkdir()
        self.catalog = {
            "skills": {
                "sentry-code-review": {
                    "source": "example/reviews",
                    "previousNames": ["code-review"],
                }
            },
            "retired": {"code-review": "Renamed", "obsolete": "Retired"},
        }
        (self.repo / "catalog.json").write_text(json.dumps(self.catalog))
        source = self.repo / "skills/sentry-code-review"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: sentry-code-review\ndescription: Review changes.\n---\n\nReview.\n"
        )
        canonical = self.home / ".agents/skills/sentry-code-review"
        canonical.parent.mkdir(parents=True)
        canonical.symlink_to(source)
        self.database = self.home / ".cc-switch/cc-switch.db"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                CREATE TABLE skills (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                    directory TEXT NOT NULL, repo_owner TEXT, repo_name TEXT,
                    repo_branch TEXT, readme_url TEXT,
                    enabled_claude INTEGER DEFAULT 0, enabled_codex INTEGER DEFAULT 0,
                    enabled_opencode INTEGER DEFAULT 0, enabled_future_client INTEGER DEFAULT 0,
                    installed_at INTEGER DEFAULT 0, updated_at INTEGER DEFAULT 0, content_hash TEXT
                );
                CREATE TABLE skill_repos (owner TEXT, name TEXT, branch TEXT, enabled INTEGER, PRIMARY KEY(owner, name));
                INSERT INTO skills VALUES ('original-review', 'Review', 'Old description', 'code-review', 'example', 'reviews', 'main', 'old-url', 1, 0, 1, 1, 123, 456, 'old-hash');
                INSERT INTO skills VALUES ('company', 'Company', 'Internal', 'company-owned', NULL, NULL, NULL, NULL, 1, 1, 1, 0, 321, 654, 'company-hash');
                INSERT INTO skill_repos VALUES ('example', 'reviews', 'main', 1);
            """)
        self.settings = {
            "skillStorageLocation": "cc_switch",
            "skillSyncMethod": "auto",
            "unrelatedPreference": {"keep": True},
        }
        (self.home / ".cc-switch/settings.json").write_text(json.dumps(self.settings))

    def module(self):
        try:
            return importlib.import_module("cc_switch_skills")
        except ModuleNotFoundError:
            self.fail("guarded CC-Switch skill migration is missing")

    def rows(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return {row["id"]: dict(row) for row in connection.execute("SELECT * FROM skills")}

    def profile_files(self):
        return {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
            for path in self.database.parent.iterdir()
            if path.is_file()
        }

    def wal_writer(self):
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return connection

    def test_migration_preserves_enablement_timestamps_and_external_rows(self):
        before = self.rows()
        self.module().migrate(self.repo, self.home, apply=True)
        after = self.rows()
        review = after["original-review"]
        self.assertEqual(review["directory"], "sentry-code-review")
        for field in (
            "enabled_claude",
            "enabled_codex",
            "enabled_opencode",
            "enabled_future_client",
            "installed_at",
            "updated_at",
        ):
            self.assertEqual(review[field], before["original-review"][field])
        for field in ("repo_owner", "repo_name", "repo_branch"):
            self.assertIsNone(review[field])
        self.assertIn("niebloomj/Skills", review["readme_url"])
        self.assertEqual(after["company"], before["company"])
        settings = json.loads((self.home / ".cc-switch/settings.json").read_text())
        self.assertEqual(
            settings,
            {**self.settings, "skillStorageLocation": "unified", "skillSyncMethod": "symlink"},
        )

    def test_preview_does_not_change_database_or_settings(self):
        before = self.rows()
        settings = (self.home / ".cc-switch/settings.json").read_bytes()
        self.module().migrate(self.repo, self.home, apply=False)
        self.assertEqual(self.rows(), before)
        self.assertEqual((self.home / ".cc-switch/settings.json").read_bytes(), settings)

    def test_repeated_migration_is_a_no_op_without_another_backup(self):
        self.module().migrate(self.repo, self.home, apply=True)
        result = self.module().migrate(self.repo, self.home, apply=True)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["retired"], 0)
        self.assertIsNone(result["backup"])

    def test_app_running_blocks_all_migration_changes(self):
        module = self.module()
        before = self.rows()
        with patch.object(module, "ensure_stopped", side_effect=ValueError("CC Switch is running")):
            with self.assertRaisesRegex(ValueError, "running"):
                module.migrate(self.repo, self.home, apply=True)
        self.assertEqual(self.rows(), before)
        self.assertEqual(
            json.loads((self.home / ".cc-switch/settings.json").read_text()), self.settings
        )

    def test_missing_canonical_content_cannot_partially_change_metadata(self):
        (self.home / ".agents/skills/sentry-code-review").unlink()
        before = self.rows()
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.module().migrate(self.repo, self.home, apply=True)
        self.assertEqual(self.rows(), before)

    def test_content_hash_uses_component_order_and_ignores_generated_files(self):
        folder = self.root / "hash-fixture"
        (folder / "a").mkdir(parents=True)
        (folder / "a/file").write_bytes(b"nested")
        (folder / "a.txt").write_bytes(b"top")
        (folder / ".hidden").write_bytes(b"ignored")
        (folder / "cached.pyc").write_bytes(b"ignored")
        (folder / "__pycache__").mkdir()
        (folder / "__pycache__/cache").write_bytes(b"ignored")
        expected = hashlib.sha256(b"a/file\0nested\0a.txt\0top\0").hexdigest()
        self.assertEqual(self.module().content_hash(folder), expected)

    def test_folded_description_does_not_include_other_metadata(self):
        folder = self.root / "description-fixture"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            "---\nname: review\ndescription: >\n  Review the changes\n  for real bugs.\nmetadata:\n  origin: example\n---\n"
        )
        self.assertEqual(self.module().description(folder), "Review the changes for real bugs.")

    def test_concurrent_settings_change_aborts_without_discarding_that_change(self):
        module = self.module()
        original_hash = module.content_hash
        settings_path = self.home / ".cc-switch/settings.json"
        changed = {**self.settings, "unrelatedNewPreference": True}

        def alter_settings(folder):
            settings_path.write_text(json.dumps(changed))
            return original_hash(folder)

        before = self.rows()
        with patch.object(module, "content_hash", side_effect=alter_settings):
            with self.assertRaisesRegex(ValueError, "settings changed"):
                module.migrate(self.repo, self.home, apply=True)
        self.assertEqual(self.rows(), before)
        self.assertEqual(json.loads(settings_path.read_text()), changed)

    def test_backup_ancestors_cannot_escape_the_selected_home(self):
        outside = self.root / "outside"
        outside.mkdir()
        backups = self.home / ".local/state/personal-skills/backups"
        backups.parent.mkdir(parents=True)
        backups.symlink_to(outside, target_is_directory=True)
        before = self.profile_files()
        with self.assertRaisesRegex(ValueError, "outside"):
            self.module().migrate(self.repo, self.home, apply=True)
        self.assertEqual(self.profile_files(), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_canonical_and_previous_rows_are_ambiguous_even_with_different_flags(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO skills (id, name, directory, enabled_claude, enabled_codex) VALUES (?, ?, ?, ?, ?)",
                ("canonical-review", "Review", "sentry-code-review", 0, 1),
            )
        before = self.profile_files()
        for apply in (False, True):
            with self.subTest(apply=apply):
                with self.assertRaisesRegex(ValueError, "Multiple installed names"):
                    self.module().migrate(self.repo, self.home, apply=apply)
                self.assertEqual(self.profile_files(), before)
                self.assertFalse((self.home / ".local").exists())

    def test_malformed_previous_names_abort_before_any_profile_changes(self):
        self.catalog["skills"]["sentry-code-review"]["previousNames"] = "code-review"
        (self.repo / "catalog.json").write_text(json.dumps(self.catalog))
        before = self.profile_files()
        for apply in (False, True):
            with self.subTest(apply=apply):
                with self.assertRaisesRegex(ValueError, "previousNames"):
                    self.module().migrate(self.repo, self.home, apply=apply)
                self.assertEqual(self.profile_files(), before)
                self.assertFalse((self.home / ".local").exists())

    def test_only_selected_and_migrated_or_retired_origins_are_disabled(self):
        self.catalog["sources"] = {"catalog-owner/selected": {}}
        (self.repo / "catalog.json").write_text(json.dumps(self.catalog))
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript("""
                UPDATE skills SET repo_owner = 'old-owner', repo_name = 'origin'
                    WHERE id = 'original-review';
                INSERT INTO skills (id, name, directory, repo_owner, repo_name)
                    VALUES ('obsolete-row', 'Obsolete', 'obsolete', 'retired-owner', 'origin');
                INSERT INTO skill_repos VALUES ('old-owner', 'origin', 'main', 1);
                INSERT INTO skill_repos VALUES ('retired-owner', 'origin', 'main', 1);
                INSERT INTO skill_repos VALUES ('catalog-owner', 'selected', 'main', 1);
                INSERT INTO skill_repos VALUES ('example', 'unrelated', 'main', 1);
                INSERT INTO skill_repos VALUES ('unrelated-owner', 'unrelated', 'main', 1);
            """)
        self.module().migrate(self.repo, self.home, apply=True)
        with closing(sqlite3.connect(self.database)) as connection:
            enabled = {
                (owner, name): flag
                for owner, name, flag in connection.execute(
                    "SELECT owner, name, enabled FROM skill_repos"
                )
            }
        self.assertEqual(
            enabled,
            {
                ("example", "reviews"): 0,
                ("old-owner", "origin"): 0,
                ("retired-owner", "origin"): 0,
                ("catalog-owner", "selected"): 0,
                ("example", "unrelated"): 1,
                ("unrelated-owner", "unrelated"): 1,
            },
        )

    def test_configured_external_owners_remain_enabled_even_when_selected(self):
        config = self.home / ".local/state/personal-skills/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"externalOwners": ["example"]}))
        self.module().migrate(self.repo, self.home, apply=True)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT enabled FROM skill_repos WHERE owner = ? AND name = ?",
                    ("example", "reviews"),
                ).fetchone(),
                (1,),
            )

    def test_all_app_flags_future_columns_and_unrelated_tables_are_preserved(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            for index, app in enumerate(("gemini", "cursor", "pi", "copilot", "another_future")):
                connection.execute(f"ALTER TABLE skills ADD COLUMN enabled_{app} INTEGER DEFAULT 0")
                connection.execute(
                    f"UPDATE skills SET enabled_{app} = ? WHERE id = ?",
                    (index % 2, "original-review"),
                )
            connection.executescript("""
                ALTER TABLE skills ADD COLUMN future_metadata TEXT DEFAULT 'keep me';
                CREATE TABLE unrelated_settings (value TEXT);
                INSERT INTO unrelated_settings VALUES ('untouched');
            """)
        before = self.rows()
        result = self.module().migrate(self.repo, self.home, apply=True)
        after = self.rows()
        for key, value in before["original-review"].items():
            if key.startswith("enabled_") or key in {
                "installed_at",
                "updated_at",
                "future_metadata",
            }:
                self.assertEqual(after["original-review"][key], value)
        self.assertEqual(after["company"], before["company"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM unrelated_settings").fetchall(),
                [("untouched",)],
            )
        backup = Path(result["backup"])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        for name in ("cc-switch.db", "settings.json"):
            self.assertEqual((backup / name).stat().st_mode & 0o777, 0o600)

    def test_checkpointed_wal_preview_does_not_create_source_sidecars(self):
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertEqual(
            {path.name for path in self.database.parent.iterdir()},
            {"cc-switch.db", "settings.json"},
        )
        before = self.profile_files()
        self.module().migrate(self.repo, self.home, apply=False)
        self.assertEqual(self.profile_files(), before)
        self.assertFalse((self.home / ".local").exists())

    def test_preview_reads_latest_uncheckpointed_wal_without_source_sqlite_connection(self):
        writer = self.wal_writer()
        main_database = self.database.read_bytes()
        writer.execute(
            "INSERT INTO skills (id, name, directory) VALUES (?, ?, ?)",
            ("wal-only", "Obsolete", "obsolete"),
        )
        writer.commit()
        self.assertEqual(self.database.read_bytes(), main_database)
        self.assertGreater(self.database.with_name("cc-switch.db-wal").stat().st_size, 32)
        before = self.profile_files()
        connect = sqlite3.connect

        def private_connection(database, *args, **kwargs):
            self.assertNotIn(str(self.home), str(database))
            return connect(database, *args, **kwargs)

        with patch.object(self.module().sqlite3, "connect", side_effect=private_connection):
            result = self.module().migrate(self.repo, self.home, apply=False)
        self.assertEqual(result["retired"], 1)
        self.assertEqual(self.profile_files(), before)

    def test_preview_aborts_if_source_wal_changes_during_the_snapshot(self):
        writer = self.wal_writer()
        module = self.module()
        original_hash = module.content_hash
        main_database = self.database.read_bytes()
        settings = (self.home / ".cc-switch/settings.json").read_bytes()

        def change_source(folder):
            writer.execute("UPDATE skills SET enabled_codex = 1 WHERE id = 'original-review'")
            writer.commit()
            return original_hash(folder)

        with patch.object(module, "content_hash", side_effect=change_source):
            with self.assertRaisesRegex(ValueError, "snapshot.*changed|changed.*snapshot"):
                module.migrate(self.repo, self.home, apply=False)
        self.assertEqual(self.database.read_bytes(), main_database)
        self.assertEqual(self.rows()["original-review"]["enabled_codex"], 1)
        self.assertEqual((self.home / ".cc-switch/settings.json").read_bytes(), settings)
        self.assertFalse((self.home / ".local").exists())

    def test_commit_failure_preserves_concurrent_settings_and_original_backup(self):
        module = self.module()
        settings_path = self.home / ".cc-switch/settings.json"
        original_settings = settings_path.read_bytes()
        concurrent_settings = json.dumps(
            {
                **self.settings,
                "skillStorageLocation": "unified",
                "skillSyncMethod": "symlink",
                "concurrentPreference": "keep exactly",
            },
            separators=(",", ":"),
        ).encode()
        before = self.rows()
        connect = sqlite3.connect

        class CommitFailure(sqlite3.Connection):
            def commit(self):
                settings_path.write_bytes(concurrent_settings)
                raise sqlite3.OperationalError("fixture commit failure")

        def failing_connection(*args, **kwargs):
            return connect(*args, factory=CommitFailure, **kwargs)

        with patch.object(module.sqlite3, "connect", side_effect=failing_connection):
            with self.assertRaisesRegex(ValueError, "reconciliation required") as failure:
                module.migrate(self.repo, self.home, apply=True)
        self.assertEqual(settings_path.read_bytes(), concurrent_settings)
        self.assertEqual(self.rows(), before)
        backups = list((self.home / ".local/state/personal-skills/backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "settings.json").read_bytes(), original_settings)
        self.assertIn(str(backups[0]), str(failure.exception))

    def test_commit_failure_restores_original_settings_without_a_concurrent_edit(self):
        module = self.module()
        settings_path = self.home / ".cc-switch/settings.json"
        original_settings = settings_path.read_bytes()
        before = self.rows()
        connect = sqlite3.connect

        class CommitFailure(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("fixture commit failure")

        def failing_connection(*args, **kwargs):
            return connect(*args, factory=CommitFailure, **kwargs)

        with patch.object(module.sqlite3, "connect", side_effect=failing_connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, "fixture commit failure"):
                module.migrate(self.repo, self.home, apply=True)
        self.assertEqual(settings_path.read_bytes(), original_settings)
        self.assertEqual(self.rows(), before)


if __name__ == "__main__":
    unittest.main()
