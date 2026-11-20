#!/usr/bin/env python3
"""Link this catalog into installed agents. Default to a read-only preview."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_plugins import disable_duplicate_superpowers
from update_skills import repository_path, validate_previous_names, validate_skill

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(".local/state/personal-skills")
AGENTS = (
    (".claude", ".claude/skills"),
    (".codex", ".codex/skills"),
    (".config/opencode", ".config/opencode/skills"),
    (".gemini", ".gemini/skills"),
    (".pi/agent", ".pi/agent/skills"),
    (".cursor", ".cursor/skills"),
    (".cc-switch/skills", ".cc-switch/skills"),
)


def read_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def within_home(path: Path, home: Path) -> None:
    if not path.resolve().is_relative_to(home.resolve()):
        raise ValueError(f"Refusing a skill location outside the selected home: {path}")


def configuration(
    home: Path, external_locks: list[Path] | None = None, retired: list[str] | None = None
) -> dict[str, Any]:
    config = read_json(home / STATE / "config.json", {})
    config["externalLocks"] = sorted(
        set(config.get("externalLocks", []))
        | {str(path.absolute()) for path in external_locks or []}
    )
    config["retired"] = sorted(set(config.get("retired", [])) | set(retired or []))
    return config


def skill_roots(home: Path, config: dict[str, Any]) -> list[Path]:
    canonical = home / ".agents/skills"
    roots = {canonical}
    roots.update(home / target for parent, target in AGENTS if (home / parent).is_dir())
    for value in config.get("extraRoots", []):
        root = Path(value).expanduser()
        if not root.is_absolute():
            raise ValueError(f"Extra skill roots must be absolute: {root}")
        roots.add(root)
    environment = os.environ.get("OPENCODE_CONFIG_DIR")
    if environment:
        candidate = Path(environment).expanduser().absolute() / "skills"
        if candidate.is_relative_to(home.absolute()) and candidate.parent.is_dir():
            roots.add(candidate)
    unique: dict[Path, Path] = {}
    for root in [canonical, *sorted(roots - {canonical})]:
        within_home(root, home)
        physical = root.resolve()
        if physical in unique:
            continue
        if any(
            physical.is_relative_to(other) or other.is_relative_to(physical) for other in unique
        ):
            raise ValueError(f"Overlapping agent skill roots are unsafe: {root}")
        unique[physical] = root
    return sorted(unique.values())


def external_ids(config: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for filename in config.get("externalLocks", []):
        path = Path(filename)
        if not path.is_file():
            raise ValueError(f"Configured external registry lock is missing: {path}")
        lock = read_json(path, {})
        if not isinstance(lock.get("skills"), dict):
            raise ValueError(f"External registry lock has no skills mapping: {path}")
        names.update(lock["skills"])
    return names


def load_skills(repository: Path) -> dict[str, Any]:
    catalog = read_json(repository / "catalog.json", {})
    if not isinstance(catalog.get("skills"), dict) or not catalog["skills"]:
        raise ValueError("The repository has no skill catalog")
    for name in catalog["skills"]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError(f"Invalid catalog skill ID: {name}")
        validate_previous_names(name, catalog["skills"][name])
        folder = repository_path(f"skills/{name}", repository)
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f"Missing or unsafe catalog skill: {name}")
        files = {}
        for path in folder.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Catalog companions cannot be symlinks: {path}")
            if path.is_file():
                files[path.relative_to(folder).as_posix()] = path.read_bytes()
        validate_skill(name, files)
    return catalog


def link_matches(path: Path, target: Path) -> bool:
    return path.is_symlink() and Path(os.path.abspath(path.parent / os.readlink(path))) == target


def fingerprint(path: Path) -> str | None:
    if path.is_symlink():
        return "link:" + os.readlink(path)
    if path.is_file():
        return "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if path.is_dir():
        digest = hashlib.sha256()
        for entry in sorted(path.rglob("*")):
            digest.update(entry.relative_to(path).as_posix().encode() + b"\0")
            if entry.is_symlink():
                digest.update(b"link:" + hashlib.sha256(os.readlink(entry).encode()).digest())
            elif entry.is_file():
                digest.update(b"file:" + hashlib.sha256(entry.read_bytes()).digest())
            elif entry.is_dir():
                digest.update(b"directory")
            else:
                digest.update(b"other")
        return "directory:" + digest.hexdigest()
    return None


def plan(repository: Path, home: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    catalog = load_skills(repository)
    roots = skill_roots(home, config)
    canonical = home / ".agents/skills"
    claude_storage = (home / ".claude/skills").resolve()
    source_root = repository_path("skills", repository).resolve()
    for root in roots:
        if root.resolve().is_relative_to(source_root) or source_root.is_relative_to(root.resolve()):
            raise ValueError(f"Agent skill roots cannot overlap repository sources: {root}")
    external = external_ids(config)
    if external & catalog["skills"].keys():
        raise ValueError(
            f"Catalog IDs collide with an external registry: {sorted(external & catalog['skills'].keys())}"
        )
    retired = set(catalog.get("retired", {})) | set(config.get("retired", []))
    if retired & (catalog["skills"].keys() | external):
        raise ValueError("A retired skill is still present in an approved source")
    for name in retired | external:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise ValueError(f"Invalid external or retired skill ID: {name}")
    actions: list[dict[str, Any]] = []
    for root in roots:
        for name in catalog["skills"]:
            destination = root / name
            target = repository / "skills" / name if root == canonical else canonical / name
            if not link_matches(destination, target):
                actions.append({"kind": "link", "path": str(destination), "target": str(target)})
        for name in external:
            destination = root / name
            if root == canonical or not (canonical / name / "SKILL.md").is_file():
                continue
            if root.resolve() == claude_storage and (
                destination.exists() or destination.is_symlink()
            ):
                continue
            if (destination.exists() or destination.is_symlink()) and not link_matches(
                destination, canonical / name
            ):
                actions.append(
                    {"kind": "link", "path": str(destination), "target": str(canonical / name)}
                )
        if root.is_dir():
            for path in root.iterdir():
                if path.name in retired:
                    actions.append({"kind": "retire", "path": str(path)})
    legacy_lock = home / ".agents/.skill-lock.json"
    if legacy_lock.is_file():
        contents = read_json(legacy_lock, {})
        kept = {
            name: value
            for name, value in contents.get("skills", {}).items()
            if name not in catalog["skills"] and name not in retired
        }
        if kept != contents.get("skills", {}):
            contents["skills"] = kept
            actions.append({"kind": "json", "path": str(legacy_lock), "value": contents})
    codex_config = home / ".codex/config.toml"
    if codex_config.is_file():
        original = codex_config.read_text(encoding="utf-8")
        replacement = disable_duplicate_superpowers(original, catalog["skills"])
        if replacement != original:
            actions.append({"kind": "text", "path": str(codex_config), "value": replacement})
    state = {
        "repository": str(repository),
        "skills": {
            name: {"source": "catalog.json", "upstream": entry.get("source", "local")}
            for name, entry in catalog["skills"].items()
        },
    }
    for filename, value in (("config.json", config), ("installed.json", state)):
        path = home / STATE / filename
        within_home(path.parent, home)
        if read_json(path, None) != value:
            actions.append({"kind": "json", "path": str(path), "value": value})
    for action in actions:
        path = Path(action["path"])
        within_home(path.parent, home)
        action["parent"] = str(path.parent.resolve())
        action["before"] = fingerprint(path)
    return actions


def apply_actions(
    actions: list[dict[str, Any]], home: Path, after_apply: Callable[[], None] | None = None
) -> str | None:
    if not actions:
        if after_apply is not None:
            after_apply()
        return None
    directory = (
        home
        / STATE
        / "backups"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    )
    within_home(directory.parent, home)
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    changed: list[tuple[Path, Path | None, str | None, Path]] = []
    try:
        for action in actions:
            path = Path(action["path"])
            within_home(path.parent, home)
            parent = path.parent.resolve()
            if str(parent) != action.get("parent", str(parent)):
                raise ValueError(f"Skill location changed after the preview: {path}")
            if fingerprint(path) != action["before"]:
                raise ValueError(
                    f"Skill files changed after the preview; refusing to overwrite: {path}"
                )
            backup = (
                directory / path.relative_to(home) if path.exists() or path.is_symlink() else None
            )
            if backup is not None:
                within_home(backup.parent, home)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(backup))
            changed.append((path, backup, None, parent))
            path.parent.mkdir(parents=True, exist_ok=True)
            if action["kind"] == "link":
                path.symlink_to(action["target"], target_is_directory=True)
                changed[-1] = (path, backup, "link:" + action["target"], parent)
            elif action["kind"] in {"json", "text"}:
                temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    value = (
                        json.dumps(action["value"], indent=2, sort_keys=True) + "\n"
                        if action["kind"] == "json"
                        else action["value"]
                    )
                    temporary.write_bytes(value.encode("utf-8"))
                    temporary.chmod(0o600)
                    os.replace(temporary, path)
                    changed[-1] = (
                        path,
                        backup,
                        "file:" + hashlib.sha256(value.encode("utf-8")).hexdigest(),
                        parent,
                    )
                finally:
                    temporary.unlink(missing_ok=True)
        (directory / "actions.json").write_text(
            json.dumps(actions, indent=2) + "\n", encoding="utf-8"
        )
        if after_apply is not None:
            after_apply()
    except Exception as error:
        conflicts = []
        for path, backup, applied, parent in reversed(changed):
            if path.parent.resolve() != parent or fingerprint(path) != applied:
                conflicts.append(str(path))
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
            if backup is not None:
                shutil.move(str(backup), str(path))
        if conflicts:
            raise ValueError(
                f"Rollback preserved concurrent changes at {', '.join(conflicts)}; originals remain in {directory}"
            ) from error
        raise
    return str(directory)


def synchronize(
    repository: Path,
    home: Path,
    *,
    apply: bool = False,
    external_locks: list[Path] | None = None,
    retired: list[str] | None = None,
    cc_switch: bool = False,
) -> dict[str, Any]:
    repository, home = repository.resolve(), home.resolve()
    config = configuration(home, external_locks, retired)
    if cc_switch:
        config["ccSwitchManaged"] = True
    actions = plan(repository, home, config)
    cc_result = None
    adapter = None
    if config.get("ccSwitchManaged"):
        import cc_switch_skills

        adapter = cc_switch_skills
        if apply:
            adapter.ensure_stopped()
    elif apply and (home / ".cc-switch/cc-switch.db").is_file():
        raise ValueError(
            "CC-Switch detected. Review a preview with --cc-switch before applying the offline migration."
        )

    def migrate_cc_switch() -> None:
        nonlocal cc_result
        if adapter is not None:
            cc_result = adapter.migrate(repository, home, apply=True)

    backup = apply_actions(actions, home, migrate_cc_switch) if apply else None
    if not apply and adapter is not None:
        cc_result = adapter.migrate(repository, home, apply=False)
    return {"actions": actions, "backup": backup, "ccSwitch": cc_result}


def audit(repository: Path, home: Path, *, external_locks: list[Path] | None = None) -> list[str]:
    repository, home = repository.resolve(), home.resolve()
    catalog = load_skills(repository)
    config = configuration(home, external_locks)
    roots = skill_roots(home, config)
    external = external_ids(config)
    problems = []
    for action in plan(repository, home, config):
        if action["kind"] != "json" or action["path"].endswith(".skill-lock.json"):
            problems.append(f"{action['kind']} required: {action['path']}")
    exempt = {Path(path).expanduser().absolute() for path in config.get("exemptRoots", [])}

    def inspect(directory: Path, root: Path, ancestors: set[Path]) -> None:
        if directory.absolute() in exempt:
            return
        try:
            resolved = directory.resolve()
        except RuntimeError:
            problems.append(f"Cyclic skill container: {directory}")
            return
        if resolved in ancestors:
            problems.append(f"Cyclic skill container: {directory}")
            return
        if directory != root and (directory / "SKILL.md").is_file():
            if directory.parent != root or (
                directory.name not in catalog["skills"] and directory.name not in external
            ):
                problems.append(f"Unmanaged skill: {directory}")
            return
        if directory.is_symlink() and not resolved.is_relative_to(home):
            problems.append(f"Unmanaged external skill container: {directory}")
            return
        for path in directory.iterdir():
            if path.name.startswith(".") or path.name == "node_modules":
                continue
            if path.is_dir():
                inspect(path, root, ancestors | {resolved})
            elif path.is_symlink() and not path.exists():
                problems.append(f"Unmanaged dangling skill link: {path}")
            elif directory == root and path.is_file() and path.suffix == ".md":
                problems.append(f"Unmanaged flat skill: {path}")

    for root in roots:
        if root.is_dir():
            inspect(root, root, set())
    if config.get("ccSwitchManaged"):
        import cc_switch_skills

        metadata = cc_switch_skills.migrate(repository, home, apply=False)
        if any(
            metadata.get(key)
            for key in ("updated", "added", "retired", "settingsChanged", "repositoriesDisabled")
        ):
            problems.append("CC-Switch skill metadata or storage settings require synchronization")
    return problems


def audit_report(
    repository: Path, home: Path, *, external_locks: list[Path] | None = None
) -> dict[str, list[str]]:
    config = configuration(home.resolve(), external_locks)
    retained = set()
    for filename in config.get("retainedDanglingLinks", []):
        path = Path(filename).expanduser()
        if not path.is_absolute():
            raise ValueError(f"Retained dangling links must use absolute paths: {path}")
        retained.add(f"Unmanaged dangling skill link: {path}")
    findings = audit(repository, home, external_locks=external_locks)
    return {
        "problems": [finding for finding in findings if finding not in retained],
        "warnings": [finding for finding in findings if finding in retained],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="back up conflicting copies and apply the preview"
    )
    mode.add_argument(
        "--audit",
        action="store_true",
        help="fail if unmanaged, retired, or divergent skills remain",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="agent home directory (also useful for isolated verification)",
    )
    parser.add_argument(
        "--external-lock",
        type=Path,
        action="append",
        default=[],
        help="preserve skills tracked by another registry; remembered after --apply",
    )
    parser.add_argument(
        "--retire",
        action="append",
        default=[],
        help="back up and retire an additional machine-local skill ID",
    )
    parser.add_argument(
        "--cc-switch",
        action="store_true",
        help="also migrate CC-Switch offline, preserving app enablement",
    )
    arguments = parser.parse_args()
    try:
        if arguments.audit:
            report = audit_report(ROOT, arguments.home, external_locks=arguments.external_lock)
            for problem in report["problems"]:
                print(problem)
            for warning in report["warnings"]:
                print(f"Warning (explicitly retained): {warning}")
            print(
                f"Audit: {len(report['problems'])} problem(s), {len(report['warnings'])} retained-link warning(s)."
            )
            return 1 if report["problems"] else 0
        result = synchronize(
            ROOT,
            arguments.home,
            apply=arguments.apply,
            external_locks=arguments.external_lock,
            retired=arguments.retire,
            cc_switch=arguments.cc_switch,
        )
        for action in result["actions"]:
            print(
                f"{action['kind']}: {action['path']}"
                + (f" -> {action['target']}" if "target" in action else "")
            )
        print(f"{'Applied' if arguments.apply else 'Preview'}: {len(result['actions'])} action(s).")
        if result["backup"]:
            print(f"Backup: {result['backup']}")
        if result["ccSwitch"] is not None:
            print("CC-Switch: " + json.dumps(result["ccSwitch"], sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
