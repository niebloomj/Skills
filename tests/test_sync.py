import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.repo = self.root / "catalog"
        self.home.mkdir()
        self.repo.mkdir()
        for relative in (".claude", ".codex", ".config/opencode"):
            (self.home / relative).mkdir(parents=True)
        self.catalog = {
            "skills": {"sentry-code-review": {"source": "example/reviews"}},
            "retired": {"code-review": "Renamed review skill."},
        }
        self.skill(
            self.repo / "skills/sentry-code-review", "sentry-code-review", "Canonical review."
        )
        self.write_catalog()

    def module(self):
        try:
            return importlib.import_module("sync_skills")
        except ModuleNotFoundError:
            self.fail("a safe, repeatable skill synchronizer is missing")

    def write_catalog(self):
        (self.repo / "catalog.json").write_text(json.dumps(self.catalog))

    def skill(self, directory, name, body):
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Review changes.\n---\n\n{body}\n"
        )

    def test_dry_run_does_not_create_links_or_state(self):
        report = self.module().synchronize(self.repo, self.home, apply=False)
        self.assertTrue(report["actions"])
        self.assertFalse((self.home / ".agents").exists())
        self.assertFalse((self.home / ".local/state/personal-skills").exists())

    def test_agents_share_one_repository_definition(self):
        self.module().synchronize(self.repo, self.home, apply=True)
        canonical = self.home / ".agents/skills/sentry-code-review"
        self.assertTrue(canonical.is_symlink())
        self.assertEqual(canonical.resolve(), self.repo / "skills/sentry-code-review")
        for relative in (".claude/skills", ".codex/skills", ".config/opencode/skills"):
            projected = self.home / relative / "sentry-code-review"
            self.assertTrue(projected.is_symlink())
            self.assertEqual(projected.readlink(), canonical)
        self.assertEqual(self.module().audit(self.repo, self.home), [])

    def test_replaced_copy_and_retired_skill_are_backed_up(self):
        self.skill(
            self.home / ".agents/skills/sentry-code-review", "sentry-code-review", "Older review."
        )
        self.skill(self.home / ".claude/skills/code-review", "code-review", "Original review.")
        report = self.module().synchronize(self.repo, self.home, apply=True)
        backup = Path(report["backup"])
        self.assertIn(
            "Older review.", (backup / ".agents/skills/sentry-code-review/SKILL.md").read_text()
        )
        self.assertIn(
            "Original review.", (backup / ".claude/skills/code-review/SKILL.md").read_text()
        )
        self.assertFalse((self.home / ".claude/skills/code-review").exists())

    def test_curated_catalog_retires_approved_cuts_from_existing_installations(self):
        repository = Path(__file__).resolve().parents[1]
        removed = (
            "claude-md-improver",
            "driving-claude-code-sessions",
            "find-bugs",
            "parallel-debugging",
            "proactive-agent",
            "python-executor",
            "python-type-safety",
            "remembering-conversations",
            "security-review",
            "skill-improver",
        )
        roots = [
            self.home / relative
            for relative in (
                ".agents/skills",
                ".claude/skills",
                ".codex/skills",
                ".config/opencode/skills",
            )
        ]
        for root in roots:
            root.mkdir(parents=True, exist_ok=True)
            for name in removed:
                target = repository / "skills" / name if root == roots[0] else roots[0] / name
                (root / name).symlink_to(target, target_is_directory=True)

        module = self.module()
        report = module.synchronize(repository, self.home, apply=True)
        remaining = [
            name
            for name in removed
            if any((root / name).exists() or (root / name).is_symlink() for root in roots)
        ]
        self.assertEqual(remaining, [])
        backup = Path(report["backup"])
        for root in roots:
            for name in removed:
                self.assertTrue((backup / root.relative_to(self.home) / name).is_symlink())
        catalog = module.load_skills(repository)
        for name in catalog["skills"]:
            self.assertEqual((roots[0] / name).resolve(), repository / "skills" / name)
        self.assertEqual(
            module.audit_report(repository, self.home), {"problems": [], "warnings": []}
        )
        self.assertEqual(module.synchronize(repository, self.home, apply=True)["actions"], [])

    def test_external_registry_content_is_preserved(self):
        external = self.home / ".agents/skills/company-owned"
        self.skill(external, "company-owned", "Registry content.")
        lock = self.home / "registry.json"
        lock.write_text(
            json.dumps({"skills": {"company-owned": {"packageName": "company-package"}}})
        )
        self.module().synchronize(self.repo, self.home, apply=True, external_locks=[lock])
        self.assertFalse(external.is_symlink())
        self.assertIn("Registry content.", (external / "SKILL.md").read_text())
        self.assertEqual(self.module().audit(self.repo, self.home, external_locks=[lock]), [])

    def test_external_snapshot_is_relinked_without_overwriting_registry_content(self):
        canonical = self.home / ".agents/skills/company-owned"
        self.skill(canonical, "company-owned", "Current registry content.")
        projected = self.home / ".cc-switch/skills/company-owned"
        self.skill(projected, "company-owned", "Stale snapshot.")
        lock = self.home / "registry.json"
        lock.write_text(
            json.dumps({"skills": {"company-owned": {"packageName": "company-package"}}})
        )
        self.module().synchronize(self.repo, self.home, apply=True, external_locks=[lock])
        self.assertTrue(projected.is_symlink())
        self.assertEqual(projected.readlink(), canonical)
        self.assertIn("Current registry content.", (canonical / "SKILL.md").read_text())

    def test_dangling_managed_link_is_repaired_instead_of_retired_twice(self):
        canonical = self.home / ".agents/skills/sentry-code-review"
        canonical.parent.mkdir(parents=True)
        canonical.symlink_to(self.home / "missing-source")
        self.module().synchronize(self.repo, self.home, apply=True)
        self.assertEqual(canonical.resolve(), self.repo / "skills/sentry-code-review")
        self.assertEqual(self.module().audit(self.repo, self.home), [])

    def test_unknown_skill_is_preserved_and_reported(self):
        unknown = self.home / ".claude/skills/local-experiment"
        self.skill(unknown, "local-experiment", "User work.")
        self.module().synchronize(self.repo, self.home, apply=True)
        self.assertIn("User work.", (unknown / "SKILL.md").read_text())
        problems = self.module().audit(self.repo, self.home)
        self.assertTrue(any("local-experiment" in problem for problem in problems))

    def test_nested_unknown_skills_are_reported(self):
        self.skill(
            self.home / ".claude/skills/group/nested-experiment", "nested-experiment", "User work."
        )
        self.module().synchronize(self.repo, self.home, apply=True)
        problems = self.module().audit(self.repo, self.home)
        self.assertTrue(any("nested-experiment" in problem for problem in problems))

    def test_explicit_runtime_containers_are_not_treated_as_personal_skills(self):
        runtime = self.home / ".claude/skills/synced"
        self.skill(runtime / "vendor-skill", "vendor-skill", "Runtime-owned content.")
        config = self.home / ".local/state/personal-skills/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"exemptRoots": [str(runtime)]}))
        self.module().synchronize(self.repo, self.home, apply=True)
        self.assertEqual(self.module().audit(self.repo, self.home), [])

    def test_second_sync_is_a_no_op(self):
        self.module().synchronize(self.repo, self.home, apply=True)
        report = self.module().synchronize(self.repo, self.home, apply=True)
        self.assertEqual(report["actions"], [])
        self.assertIsNone(report["backup"])

    def test_skill_roots_cannot_escape_the_selected_home(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.home / ".agents").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.module().synchronize(self.repo, self.home, apply=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_backup_ancestor_cannot_escape_home(self):
        outside = self.root / "outside-backups"
        outside.mkdir()
        state = self.home / ".local/state/personal-skills"
        state.mkdir(parents=True)
        (state / "backups").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.module().synchronize(self.repo, self.home, apply=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_whole_root_alias_does_not_create_a_canonical_self_cycle(self):
        self.module().synchronize(self.repo, self.home, apply=True)
        root = self.home / ".codex/skills"
        for child in root.iterdir():
            child.unlink()
        root.rmdir()
        root.symlink_to(self.home / ".agents/skills", target_is_directory=True)
        self.module().synchronize(self.repo, self.home, apply=True)
        try:
            actual = (self.home / ".agents/skills/sentry-code-review").resolve()
        except RuntimeError as error:
            self.fail(f"a valid canonical skill became a self-cycle: {error}")
        self.assertEqual(actual, self.repo / "skills/sentry-code-review")
        self.assertEqual(self.module().audit(self.repo, self.home), [])

    def test_unknown_dangling_link_is_preserved_and_reported(self):
        root = self.home / ".claude/skills"
        root.mkdir()
        unknown = root / "local-experiment"
        unknown.symlink_to(self.home / "temporarily-unavailable")
        self.module().synchronize(self.repo, self.home, apply=True)
        self.assertTrue(unknown.is_symlink())
        self.assertTrue(
            any(
                "local-experiment" in problem
                for problem in self.module().audit(self.repo, self.home)
            )
        )

    def test_explicitly_retained_dangling_links_remain_visible_as_warnings(self):
        path = self.home / ".claude/skills/local-experiment"
        path.parent.mkdir()
        path.symlink_to(self.home / "offline-storage")
        config = self.home / ".local/state/personal-skills/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"retainedDanglingLinks": [str(path)]}))
        module = self.module()
        reporter = getattr(module, "audit_report", None)
        self.assertTrue(
            callable(reporter), "accepted dangling links need a distinct warning report"
        )
        module.synchronize(self.repo, self.home, apply=True)
        report = reporter(self.repo, self.home)
        self.assertEqual(report["problems"], [])
        self.assertTrue(any(str(path) in warning for warning in report["warnings"]))
        self.assertTrue(path.is_symlink())

    def test_retained_link_does_not_hide_a_newly_active_unknown_skill(self):
        target = self.home / "offline-storage"
        path = self.home / ".claude/skills/local-experiment"
        path.parent.mkdir()
        path.symlink_to(target)
        config = self.home / ".local/state/personal-skills/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"retainedDanglingLinks": [str(path)]}))
        module = self.module()
        reporter = getattr(module, "audit_report", None)
        self.assertTrue(
            callable(reporter), "accepted dangling links need a distinct warning report"
        )
        module.synchronize(self.repo, self.home, apply=True)
        self.skill(target, "local-experiment", "Newly available user content.")
        report = reporter(self.repo, self.home)
        self.assertTrue(any(str(path) in problem for problem in report["problems"]))
        self.assertEqual(report["warnings"], [])

    def test_registry_owned_claude_variant_symlink_is_preserved(self):
        canonical = self.home / ".agents/skills/company-owned"
        variant = self.home / ".registry/claude/company-owned"
        self.skill(canonical, "company-owned", "Generic content.")
        self.skill(variant, "company-owned", "Claude-specific content.")
        projected = self.home / ".claude/skills/company-owned"
        projected.parent.mkdir()
        projected.symlink_to(variant, target_is_directory=True)
        lock = self.home / "registry.json"
        lock.write_text(
            json.dumps({"skills": {"company-owned": {"packageName": "company-package"}}})
        )
        self.module().synchronize(self.repo, self.home, apply=True, external_locks=[lock])
        self.assertEqual(projected.readlink(), variant)
        self.assertIn("Claude-specific content.", (projected / "SKILL.md").read_text())

    def test_claude_registry_ownership_survives_a_whole_root_alias(self):
        canonical = self.home / ".agents/skills/company-owned"
        variant = self.home / ".registry/claude/company-owned"
        self.skill(canonical, "company-owned", "Generic content.")
        self.skill(variant, "company-owned", "Claude-specific content.")
        shared = self.home / ".cc-switch/skills"
        shared.mkdir(parents=True)
        (self.home / ".claude/skills").symlink_to(shared, target_is_directory=True)
        projected = shared / "company-owned"
        projected.symlink_to(variant, target_is_directory=True)
        lock = self.home / "registry.json"
        lock.write_text(
            json.dumps({"skills": {"company-owned": {"packageName": "company-package"}}})
        )
        self.module().synchronize(self.repo, self.home, apply=True, external_locks=[lock])
        self.assertEqual(projected.readlink(), variant)
        self.assertIn("Claude-specific content.", (projected / "SKILL.md").read_text())
        self.assertEqual(self.module().audit(self.repo, self.home, external_locks=[lock]), [])

    def test_rollback_preserves_a_concurrent_config_edit(self):
        module = self.module()
        path = self.home / ".codex/config.toml"
        original = 'model = "original"\n'
        path.write_text(original)
        actions = [
            {
                "kind": "text",
                "path": str(path),
                "value": 'model = "planned"\n',
                "before": module.fingerprint(path),
            }
        ]

        def concurrent_failure():
            path.write_text('model = "concurrent-user-edit"\n')
            raise ValueError("metadata fixture failure")

        with self.assertRaises(ValueError):
            module.apply_actions(actions, self.home, concurrent_failure)
        self.assertEqual(path.read_text(), 'model = "concurrent-user-edit"\n')
        self.assertTrue(
            any(
                p.read_text() == original
                for p in (self.home / ".local/state/personal-skills/backups").rglob("config.toml")
            )
        )

    def test_directory_fingerprints_distinguish_file_boundaries(self):
        first, second = self.root / "first", self.root / "second"
        first.mkdir()
        second.mkdir()
        (first / "a").write_bytes(b"b")
        (first / "c").write_bytes(b"d")
        (second / "a").write_bytes(b"bc\0d")
        self.assertNotEqual(self.module().fingerprint(first), self.module().fingerprint(second))

    def test_old_installer_sources_are_removed_without_changing_other_settings(self):
        lock = self.home / ".agents/.skill-lock.json"
        lock.parent.mkdir()
        lock.write_text(
            json.dumps(
                {
                    "version": 3,
                    "skills": {
                        "sentry-code-review": {"source": "example/reviews"},
                        "unrelated": {"source": "another/repo"},
                    },
                    "dismissed": {"findSkillsPrompt": True},
                }
            )
        )
        self.module().synchronize(self.repo, self.home, apply=True)
        updated = json.loads(lock.read_text())
        self.assertEqual(updated["skills"], {"unrelated": {"source": "another/repo"}})
        self.assertEqual(updated["dismissed"], {"findSkillsPrompt": True})

    def test_authored_skills_use_the_same_canonical_installation(self):
        self.catalog["skills"]["my-workflow"] = {"local": True}
        self.skill(self.repo / "skills/my-workflow", "my-workflow", "My own workflow.")
        self.write_catalog()
        try:
            self.module().synchronize(self.repo, self.home, apply=True)
        except KeyError as error:
            self.fail(f"authored skills cannot be synchronized: {error}")
        self.assertEqual(
            (self.home / ".agents/skills/my-workflow").resolve(), self.repo / "skills/my-workflow"
        )

    def test_metadata_failure_rolls_back_live_links_and_original_copies(self):
        import cc_switch_skills

        original = self.home / ".agents/skills/sentry-code-review"
        self.skill(original, "sentry-code-review", "Original user copy.")
        with (
            patch.object(cc_switch_skills, "ensure_stopped"),
            patch.object(
                cc_switch_skills, "migrate", side_effect=ValueError("metadata fixture failure")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "metadata fixture failure"):
                self.module().synchronize(self.repo, self.home, apply=True, cc_switch=True)
        self.assertFalse(original.is_symlink())
        self.assertIn("Original user copy.", (original / "SKILL.md").read_text())
        self.assertFalse((self.home / ".claude/skills/sentry-code-review").exists())
        self.assertFalse((self.home / ".local/state/personal-skills/config.json").exists())


if __name__ == "__main__":
    unittest.main()
