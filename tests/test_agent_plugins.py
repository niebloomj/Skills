import importlib
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class AgentPluginTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module("agent_plugins")
        except ModuleNotFoundError:
            self.fail("duplicate skill-plugin handling is missing")

    def test_duplicate_superpowers_is_disabled_without_changing_other_config(self):
        text = 'model = "example-model"\n\n[plugins."superpowers@example"]\nenabled = true\n\n[plugins."native-tools@example"]\nenabled = true\n'
        module = self.module()
        skills = {name: {"source": "obra/superpowers"} for name in module.SUPERPOWERS}
        result = module.disable_duplicate_superpowers(text, skills)
        parsed = tomllib.loads(result)
        self.assertFalse(parsed["plugins"]["superpowers@example"]["enabled"])
        self.assertTrue(parsed["plugins"]["native-tools@example"]["enabled"])
        self.assertEqual(parsed["model"], "example-model")

    def test_plugin_stays_enabled_until_the_full_replacement_is_present(self):
        text = '[plugins."superpowers@example"]\nenabled = true\n'
        result = self.module().disable_duplicate_superpowers(
            text, {"brainstorming": {"source": "obra/superpowers"}}
        )
        self.assertEqual(result, text)


if __name__ == "__main__":
    unittest.main()
