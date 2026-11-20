"""Retire duplicated skill-only plugins without touching native runtime plugins."""

from __future__ import annotations

import copy
import re
import tomllib
from typing import Any

SUPERPOWERS = {
    "brainstorming",
    "dispatching-parallel-agents",
    "executing-plans",
    "finishing-a-development-branch",
    "receiving-code-review",
    "requesting-code-review",
    "subagent-driven-development",
    "systematic-debugging",
    "test-driven-development",
    "using-git-worktrees",
    "using-superpowers",
    "verification-before-completion",
    "writing-plans",
    "writing-skills",
}


def disable_duplicate_superpowers(text: str, skills: dict[str, Any]) -> str:
    installed = {
        name for name, entry in skills.items() if entry.get("source") == "obra/superpowers"
    }
    if not SUPERPOWERS <= installed:
        return text
    before = tomllib.loads(text)
    expected = copy.deepcopy(before)
    result = text
    for name, settings in before.get("plugins", {}).items():
        if not name.startswith("superpowers@") or not settings.get("enabled", False):
            continue
        headers = [re.escape(f'[plugins."{name}"]'), re.escape(f"[plugins.'{name}']")]
        pattern = r"(?ms)^(?:" + "|".join(headers) + r")[ \t]*\n(?P<body>.*?)(?=^\[|\Z)"
        match = re.search(pattern, result)
        if match is None:
            raise ValueError(f"Cannot safely edit the Codex plugin table: {name}")
        body, count = re.subn(
            r"(?m)^([ \t]*enabled[ \t]*=[ \t]*)true([ \t]*(?:#.*)?)$",
            r"\g<1>false\2",
            match["body"],
        )
        if count != 1:
            raise ValueError(f"Cannot safely edit the Codex plugin enablement: {name}")
        result = result[: match.start("body")] + body + result[match.end("body") :]
        expected["plugins"][name]["enabled"] = False
    if tomllib.loads(result) != expected:
        raise ValueError("Codex plugin edit would change unrelated configuration")
    return result
