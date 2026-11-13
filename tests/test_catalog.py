import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from update_skills import load_catalog, safe_relative_path, skill_files, validate_local, validate_skill


class CatalogTests(unittest.TestCase):
    def test_committed_catalog_is_complete_and_valid(self):
        catalog = load_catalog()
        validate_local(catalog)
        self.assertEqual(set(catalog["skills"]), {path.name for path in (Path(__file__).resolve().parents[1] / "skills").iterdir()})

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
                {"path": "repo/skills/example/references/guide.md", "type": "blob", "mode": "100644"},
            ]
        }
        selected = skill_files(tree, "repo/skills/example")
        self.assertEqual([item["path"] for item in selected], [
            "repo/skills/example/SKILL.md",
            "repo/skills/example/references/guide.md",
        ])
        tree["tree"].append({"path": "repo/skills/example/link", "type": "blob", "mode": "120000"})
        with self.assertRaisesRegex(ValueError, "symlink"):
            skill_files(tree, "repo/skills/example")

    def test_skill_frontmatter_name_must_match_catalog_entry(self):
        content = b"---\nname: other\ndescription: Example skill.\n---\n"
        with self.assertRaisesRegex(ValueError, "name changed"):
            validate_skill("expected", {"SKILL.md": content})


if __name__ == "__main__":
    unittest.main()
