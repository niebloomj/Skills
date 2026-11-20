import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import update_skills
from update_skills import (
    load_catalog,
    safe_relative_path,
    skill_files,
    validate_local,
    validate_skill,
)


class CatalogTests(unittest.TestCase):
    def test_committed_catalog_is_complete_and_valid(self):
        catalog = load_catalog()
        validate_local(catalog)
        self.assertEqual(
            set(catalog["skills"]),
            {path.name for path in (Path(__file__).resolve().parents[1] / "skills").iterdir()},
        )

    def test_source_paths_reject_traversal_and_absolute_paths(self):
        for path in ("../outside", "/absolute/path", "skills/../../outside", "skills\\..\\outside"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_relative_path(path)

    def test_tree_selection_requires_root_skill_and_regular_files(self):
        tree = {
            "tree": [
                {"path": "repo/skills/example", "type": "tree", "mode": "040000"},
                {"path": "repo/skills/example/SKILL.md", "type": "blob", "mode": "100644"},
                {"path": "repo/skills/example/references", "type": "tree", "mode": "040000"},
                {
                    "path": "repo/skills/example/references/guide.md",
                    "type": "blob",
                    "mode": "100644",
                },
            ]
        }
        selected = skill_files(tree, "repo/skills/example")
        self.assertEqual(
            [item["path"] for item in selected],
            [
                "repo/skills/example/SKILL.md",
                "repo/skills/example/references/guide.md",
            ],
        )
        tree["tree"].append({"path": "repo/skills/example/link", "type": "blob", "mode": "120000"})
        with self.assertRaisesRegex(ValueError, "symlink"):
            skill_files(tree, "repo/skills/example")

    def test_skill_frontmatter_name_must_match_catalog_entry(self):
        content = b"---\nname: other\ndescription: Example skill.\n---\n"
        with self.assertRaisesRegex(ValueError, "name changed"):
            validate_skill("expected", {"SKILL.md": content})

    def test_valid_quoted_skill_names_are_accepted_without_rewriting(self):
        for quote in ('"', "'"):
            content = (
                f"---\nname: {quote}review{quote}\ndescription: Review changes.\n---\n".encode()
            )
            with self.subTest(quote=quote):
                try:
                    validate_skill("review", {"SKILL.md": content})
                except ValueError as error:
                    self.fail(f"valid YAML quoting was rejected: {error}")

    def render(self, name, entry, files):
        renderer = getattr(update_skills, "render_skill", None)
        self.assertTrue(
            callable(renderer), "catalog imports need deterministic skill transformations"
        )
        return renderer(name, entry, files)

    def test_renamed_skill_preserves_upstream_body_and_supporting_files(self):
        files = {
            "SKILL.md": b"---\nname: code-review\ndescription: Review changes.\n---\n\nReview the diff.\n",
            "references/guide.md": b"Reference text.\n",
        }
        result = self.render("sentry-code-review", {"upstreamName": "code-review"}, files)
        self.assertEqual(
            result["SKILL.md"],
            files["SKILL.md"].replace(b"name: code-review\n", b"name: sentry-code-review\n"),
        )
        self.assertEqual(result["references/guide.md"], files["references/guide.md"])
        self.assertIn(b"name: code-review\n", files["SKILL.md"])

    def test_renamed_skill_is_also_distinct_in_codex_discovery(self):
        files = {
            "SKILL.md": b"---\nname: code-review\ndescription: Review changes.\n---\n",
            "agents/openai.yaml": b'interface:\n  display_name: "Code Review"\n  short_description: "Review standards and spec"\n',
        }
        result = self.render("standards-spec-review", {"upstreamName": "code-review"}, files)
        self.assertIn(b'display_name: "standards-spec-review"', result["agents/openai.yaml"])
        self.assertIn(
            b'short_description: "Review standards and spec"', result["agents/openai.yaml"]
        )

    def test_manual_skill_is_not_automatically_invoked(self):
        files = {"SKILL.md": b"---\nname: review\ndescription: Review changes.\n---\n\nBody.\n"}
        result = self.render("review", {"manual": True}, files)
        self.assertIn(b"disable-model-invocation: true\n---\n", result["SKILL.md"])
        self.assertEqual(result["SKILL.md"].count(b"disable-model-invocation:"), 1)

    def test_manual_override_replaces_existing_frontmatter_value(self):
        files = {
            "SKILL.md": b"---\nname: review\ndescription: Review changes.\ndisable-model-invocation: false\n---\n\nBody.\n"
        }
        result = self.render("review", {"manual": True}, files)
        self.assertNotIn(b"disable-model-invocation: false", result["SKILL.md"])
        self.assertEqual(result["SKILL.md"].count(b"disable-model-invocation:"), 1)

    def test_manual_policy_is_enforced_in_codex_too(self):
        files = {"SKILL.md": b"---\nname: review\ndescription: Review changes.\n---\n"}
        result = self.render("review", {"manual": True}, files)
        self.assertIn(b"allow_implicit_invocation: false", result.get("agents/openai.yaml", b""))

    def test_upstream_manual_policy_is_preserved_for_codex_discovery(self):
        files = {
            "SKILL.md": b"---\nname: review\ndescription: Review changes.\ndisable-model-invocation: true\n---\n"
        }
        result = self.render("review", {}, files)
        self.assertIn(b"allow_implicit_invocation: false", result.get("agents/openai.yaml", b""))

    def test_manual_policy_preserves_other_codex_metadata(self):
        files = {
            "SKILL.md": b"---\nname: review\ndescription: Review changes.\n---\n",
            "agents/openai.yaml": b'interface:\n  display_name: "Review"\npolicy:\n  allow_implicit_invocation: true\n  another_setting: keep\n',
        }
        result = self.render("review", {"manual": True}, files)
        self.assertIn(b"allow_implicit_invocation: false", result["agents/openai.yaml"])
        self.assertIn(b"another_setting: keep", result["agents/openai.yaml"])
        self.assertIn(b'display_name: "Review"', result["agents/openai.yaml"])

    def test_global_skill_references_do_not_require_an_uninstalled_plugin(self):
        files = {
            "SKILL.md": b"---\nname: planning\ndescription: Plan changes.\n---\n\nUse superpowers:writing-plans.\n",
            "references/guide.md": b"Read superpowers:test-driven-development.\n",
            "scripts/helper.sh": b"echo superpowers:unchanged\n",
        }
        result = self.render("planning", {"replaceMarkdown": {"superpowers:": ""}}, files)
        self.assertIn(b"Use writing-plans.", result["SKILL.md"])
        self.assertEqual(result["references/guide.md"], b"Read test-driven-development.\n")
        self.assertEqual(result["scripts/helper.sh"], files["scripts/helper.sh"])

    def test_unmodified_skill_is_byte_identical(self):
        files = {"SKILL.md": b"---\nname: review\ndescription: Review changes.\n---\n\nBody.\n"}
        self.assertEqual(self.render("review", {}, files), files)

    def test_root_level_upstream_skill_is_supported(self):
        tree = {"tree": [{"path": "SKILL.md", "type": "blob", "mode": "100644"}]}
        try:
            selected = skill_files(tree, ".")
        except ValueError as error:
            self.fail(f"root-level skills cannot be imported: {error}")
        self.assertEqual(selected, tree["tree"])

    def test_vendor_installs_new_skill_with_its_recorded_alias(self):
        vendor = getattr(update_skills, "vendor_catalog", None)
        self.assertTrue(callable(vendor), "new catalog entries need a pinned import path")
        files = {
            "SKILL.md": b"---\nname: code-review\ndescription: Review changes.\n---\n\nReview.\n"
        }
        catalog = {
            "skills": {
                "sentry-code-review": {
                    "source": "example/skills",
                    "upstreamPath": "skills/code-review",
                    "upstreamName": "code-review",
                }
            },
            "sources": {
                "example/skills": {
                    "ref": "a" * 40,
                    "licensePath": "LICENSE",
                    "licenseFile": "licenses/example-LICENSE",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(update_skills, "ROOT", root),
                patch.object(update_skills, "fetch_skill", return_value=files),
                patch.object(update_skills, "read_source", return_value=b"License notice.\n"),
            ):
                vendor(catalog)
            self.assertIn(
                b"name: sentry-code-review\n",
                (root / "skills/sentry-code-review/SKILL.md").read_bytes(),
            )
            self.assertEqual((root / "licenses/example-LICENSE").read_bytes(), b"License notice.\n")

    def test_import_excludes_nonessential_gallery_before_downloading(self):
        tree = {
            "tree": [
                {"path": "skills/image/SKILL.md", "type": "blob", "mode": "100644"},
                {"path": "skills/image/gallery/demo.png", "type": "blob", "mode": "100644"},
            ]
        }
        content = b"---\nname: image\ndescription: Generate an image.\n---\n"

        def read_source(repository, ref, path):
            self.assertEqual(path, "skills/image/SKILL.md")
            return content

        with patch.object(update_skills, "read_source", side_effect=read_source):
            try:
                files = update_skills.fetch_skill(
                    "example/skills",
                    "a" * 40,
                    "skills/image",
                    "image",
                    {("example/skills", "a" * 40): tree},
                    exclude=["gallery/*"],
                )
            except TypeError as error:
                self.fail(f"selective upstream imports are not supported: {error}")
        self.assertEqual(files, {"SKILL.md": content})

    def test_authored_skill_does_not_require_a_fake_upstream_commit(self):
        catalog = {"schemaVersion": 1, "sources": {}, "skills": {"my-workflow": {"local": True}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(catalog))
            with patch.object(update_skills, "CATALOG_PATH", path):
                try:
                    loaded = update_skills.load_catalog()
                except ValueError as error:
                    self.fail(f"authored skills are not supported: {error}")
        self.assertEqual(loaded["skills"], {"my-workflow": {"local": True}})

    def test_previous_names_must_be_unique_skill_id_lists(self):
        for aliases in ("code-review", ["code-review", "code-review"], ["../outside"], ["review"]):
            catalog = {
                "schemaVersion": 1,
                "sources": {},
                "skills": {"review": {"local": True, "previousNames": aliases}},
            }
            with self.subTest(aliases=aliases), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "catalog.json"
                path.write_text(json.dumps(catalog))
                with (
                    patch.object(update_skills, "CATALOG_PATH", path),
                    self.assertRaisesRegex(ValueError, "previousNames"),
                ):
                    update_skills.load_catalog()

    def test_vendor_never_replaces_an_authored_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "skills/my-workflow"
            folder.mkdir(parents=True)
            text = b"---\nname: my-workflow\ndescription: My personal workflow.\n---\n\nAuthored content.\n"
            (folder / "SKILL.md").write_bytes(text)
            with patch.object(update_skills, "ROOT", root):
                try:
                    update_skills.vendor_catalog(
                        {"sources": {}, "skills": {"my-workflow": {"local": True}}}
                    )
                except KeyError as error:
                    self.fail(f"authored skill was treated as an upstream import: {error}")
            self.assertEqual((folder / "SKILL.md").read_bytes(), text)

    @unittest.skipIf(os.name == "nt", "POSIX executable mode")
    def test_vendor_preserves_executable_companions(self):
        catalog = {
            "skills": {"helper": {"source": "example/skills", "upstreamPath": "skills/helper"}},
            "sources": {
                "example/skills": {
                    "ref": "a" * 40,
                    "licensePath": "LICENSE",
                    "licenseFile": "licenses/example-LICENSE",
                }
            },
        }
        files = {
            "SKILL.md": b"---\nname: helper\ndescription: Run the helper.\n---\n",
            "scripts/run": b"#!/bin/sh\nprintf 'companion ran\\n'\n",
        }

        def fetch(repository, ref, source_path, name, cache, **kwargs):
            cache[(repository, ref)] = {
                "tree": [{"path": "skills/helper/scripts/run", "type": "blob", "mode": "100755"}]
            }
            return files

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(update_skills, "ROOT", root),
                patch.object(update_skills, "fetch_skill", side_effect=fetch),
                patch.object(update_skills, "read_source", return_value=b"License.\n"),
            ):
                update_skills.vendor_catalog(catalog)
            try:
                result = subprocess.run(
                    [str(root / "skills/helper/scripts/run")],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except PermissionError as error:
                self.fail(f"upstream executable mode was lost: {error}")
            self.assertEqual(result.stdout, "companion ran\n")

    def test_vendor_refuses_symlinked_repository_ancestors(self):
        for ancestor in ("skills", "licenses"):
            with self.subTest(ancestor=ancestor), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root, outside = base / "repository", base / "outside"
                root.mkdir()
                outside.mkdir()
                (outside / "original").write_text("User work.")
                (root / ancestor).symlink_to(outside, target_is_directory=True)
                catalog = {
                    "skills": {
                        "helper": {"source": "example/skills", "upstreamPath": "skills/helper"}
                    },
                    "sources": {
                        "example/skills": {
                            "ref": "a" * 40,
                            "licensePath": "LICENSE",
                            "licenseFile": "licenses/example-LICENSE",
                        }
                    },
                }
                files = {"SKILL.md": b"---\nname: helper\ndescription: Run the helper.\n---\n"}
                with (
                    patch.object(update_skills, "ROOT", root),
                    patch.object(update_skills, "fetch_skill", return_value=files),
                    patch.object(update_skills, "read_source", return_value=b"License.\n"),
                    self.assertRaisesRegex(ValueError, "symlink|outside"),
                ):
                    update_skills.vendor_catalog(catalog)
                self.assertEqual((outside / "original").read_text(), "User work.")
                self.assertEqual({path.name for path in outside.iterdir()}, {"original"})


if __name__ == "__main__":
    unittest.main()
