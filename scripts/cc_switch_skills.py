"""Optional offline CC-Switch adapter. The repository remains the content owner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any

from sync_skills import STATE, read_json, within_home
from update_skills import validate_previous_names


def ensure_stopped() -> None:
    result = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError("Cannot verify that CC Switch is stopped")
    if any(
        "cc-switch" in line.lower() or "cc switch.app/" in line.lower()
        for line in result.stdout.splitlines()
    ):
        raise ValueError(
            "CC Switch is running. Fully quit it, including its tray process, before migration."
        )


def content_hash(folder: Path) -> str:
    root = folder.resolve()
    files = []
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories if not name.startswith(".") and name != "__pycache__"
        ]
        for name in directories + names:
            path = Path(current) / name
            if path.is_symlink():
                raise ValueError(f"Refusing a companion symlink while hashing: {path}")
        files.extend(
            Path(current) / name
            for name in names
            if not name.startswith(".") and not name.endswith(".pyc")
        )
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda path: path.relative_to(root).parts):
        digest.update(path.relative_to(root).as_posix().replace("\\", "/").encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def description(folder: Path) -> str:
    text = (folder / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("\n---\n", 1)[0]
    match = re.search(r"^description:\s*(.*)$", frontmatter, re.MULTILINE)
    if not match:
        raise ValueError(f"Skill has no description: {folder}")
    value = match.group(1).strip()
    if value in {">", ">-", "|", "|-"}:
        following = frontmatter[match.end() :].splitlines()
        lines = []
        for line in following:
            if line and not line.startswith((" ", "\t")):
                break
            if line.strip():
                lines.append(line.strip())
        value = " ".join(lines)
    elif value.startswith('"'):
        value = json.loads(value)
    elif value.startswith("'") and value.endswith("'"):
        value = value[1:-1].replace("''", "'")
    return value


def file_identity(status: os.stat_result) -> tuple[int, ...]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def database_files(database: Path) -> dict[str, tuple[tuple[int, ...], bytes] | None]:
    files = {}
    for suffix in ("", "-wal", "-journal"):
        path = database.with_name(database.name + suffix)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            files[suffix] = None
            continue
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"Refusing a non-regular CC-Switch snapshot file: {path}")
            data = source.read()
            after = os.fstat(source.fileno())
        try:
            current = path.stat(follow_symlinks=False)
        except FileNotFoundError as error:
            raise ValueError("CC-Switch database snapshot changed while reading") from error
        identity = file_identity(before)
        if identity != file_identity(after) or identity != file_identity(current):
            raise ValueError("CC-Switch database snapshot changed while reading")
        files[suffix] = (identity, data)
    return files


@contextmanager
def preview_database(database: Path) -> Iterator[Path]:
    original = database_files(database)
    if original[""] is None:
        raise ValueError("CC-Switch database snapshot changed before reading")
    with tempfile.TemporaryDirectory(prefix="cc-switch-preview-") as directory:
        snapshot = Path(directory) / "cc-switch.db"
        for suffix, contents in original.items():
            if contents is not None:
                path = snapshot.with_name(snapshot.name + suffix)
                path.write_bytes(contents[1])
                path.chmod(0o600)
        if database_files(database) != original:
            raise ValueError("CC-Switch database snapshot changed while copying")
        yield snapshot
        if database_files(database) != original:
            raise ValueError("CC-Switch database snapshot changed during preview")


def migrate(repository: Path, home: Path, *, apply: bool = False) -> dict[str, Any]:
    repository, home = repository.resolve(), home.resolve()
    database = home / ".cc-switch/cc-switch.db"
    settings_path = home / ".cc-switch/settings.json"
    within_home(database, home)
    within_home(settings_path, home)
    if not database.is_file():
        return {"updated": 0, "added": 0, "retired": 0, "backup": None}
    database = database.resolve()
    backups = home / STATE / "backups"
    if apply:
        ensure_stopped()
        within_home(backups, home)
    catalog = read_json(repository / "catalog.json", {})
    aliases = {
        name: validate_previous_names(name, entry) for name, entry in catalog["skills"].items()
    }
    sources = set(catalog.get("sources", {}))
    sources.update(entry["source"] for entry in catalog["skills"].values() if "source" in entry)
    superseded = set()
    for source in sources:
        if not isinstance(source, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source
        ):
            raise ValueError(f"Invalid CC-Switch catalog source: {source}")
        owner, name = source.split("/")
        superseded.add((owner, name))
    config = read_json(home / STATE / "config.json", {})
    retired = set(catalog.get("retired", {})) | set(config.get("retired", []))
    original_settings = settings_path.read_bytes() if settings_path.exists() else None
    settings = json.loads(original_settings) if original_settings is not None else {}
    new_settings = {**settings, "skillStorageLocation": "unified", "skillSyncMethod": "symlink"}
    resources = ExitStack()
    connection: sqlite3.Connection | None = None
    backup: Path | None = None
    settings_written = False
    applied_settings: bytes | None = None
    applied_identity: tuple[int, ...] | None = None
    try:
        selected = database if apply else resources.enter_context(preview_database(database))
        connection = resources.enter_context(
            closing(sqlite3.connect(selected.as_uri() + "?mode=rw", uri=True, timeout=2))
        )
        connection.row_factory = sqlite3.Row
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(skills)")}
        required = {
            "id",
            "directory",
            "name",
            "description",
            "repo_owner",
            "repo_name",
            "repo_branch",
            "readme_url",
            "content_hash",
        }
        if not required <= columns or any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column) for column in columns
        ):
            raise ValueError("Unsupported CC-Switch skill schema; no migration performed")
        original = [dict(row) for row in connection.execute("SELECT * FROM skills ORDER BY id")]
        by_directory = {row["directory"]: row for row in original}
        if len(by_directory) != len(original):
            raise ValueError(
                "CC-Switch has multiple rows for one skill directory; resolve before migration"
            )
        changes = []
        consumed = set()
        for name in catalog["skills"]:
            folder = repository / "skills" / name
            canonical = home / ".agents/skills" / name
            if apply and (not canonical.is_symlink() or canonical.resolve() != folder):
                raise ValueError(
                    f"Install the canonical repository skill before migrating CC-Switch: {name}"
                )
            candidates = [
                by_directory[candidate]
                for candidate in [name, *aliases[name]]
                if candidate in by_directory
            ]
            if len(candidates) > 1:
                raise ValueError(f"Multiple installed names could map to {name}")
            row = candidates[0] if candidates else None
            values = {
                "directory": name,
                "name": name,
                "description": description(folder),
                "repo_owner": None,
                "repo_name": None,
                "repo_branch": None,
                "readme_url": f"https://github.com/{catalog.get('repository', 'niebloomj/Skills')}/blob/main/skills/{name}/SKILL.md",
                "content_hash": content_hash(folder),
            }
            if row is not None:
                if row["id"] in consumed:
                    raise ValueError(
                        f"An installed row would be reused for multiple skills: {name}"
                    )
                consumed.add(row["id"])
                if any(row.get(key) != value for key, value in values.items()):
                    changes.append((row["id"], values))
            else:
                values["id"] = f"local:{name}"
                if "installed_at" in columns:
                    values["installed_at"] = int(time.time())
                if "updated_at" in columns:
                    values["updated_at"] = int(time.time())
                for column in columns:
                    if column.startswith("enabled_"):
                        app = column[len("enabled_") :]
                        values[column] = int(
                            settings.get("visibleApps", {}).get(
                                app, app in {"claude", "codex", "opencode"}
                            )
                        )
                changes.append((None, values))
        removed = [
            row["id"]
            for row in original
            if row["directory"] in retired and row["id"] not in consumed
        ]
        migrated_or_retired = consumed | set(removed)
        superseded.update(
            (row["repo_owner"], row["repo_name"])
            for row in original
            if row["id"] in migrated_or_retired and row["repo_owner"] and row["repo_name"]
        )
        allowed_owners = set(config.get("externalOwners", []))
        repositories = [
            dict(row) for row in connection.execute("SELECT owner, name, enabled FROM skill_repos")
        ]
        disabled = [
            row
            for row in repositories
            if row["enabled"]
            and (row["owner"], row["name"]) in superseded
            and row["owner"] not in allowed_owners
        ]
        report = {
            "updated": sum(identifier is not None for identifier, _ in changes),
            "added": sum(identifier is None for identifier, _ in changes),
            "retired": len(removed),
            "settingsChanged": settings != new_settings,
            "repositoriesDisabled": len(disabled),
            "backup": None,
        }
        if not apply or not (changes or removed or disabled or settings != new_settings):
            return report
        backup = backups / ("cc-switch-" + uuid.uuid4().hex)
        within_home(backup, home)
        backup.mkdir(parents=True, mode=0o700)
        backup.chmod(0o700)
        snapshot = sqlite3.connect(backup / "cc-switch.db")
        try:
            connection.backup(snapshot)
        finally:
            snapshot.close()
        (backup / "cc-switch.db").chmod(0o600)
        if settings_path.exists():
            shutil.copy2(settings_path, backup / "settings.json")
            (backup / "settings.json").chmod(0o600)
        connection.execute("BEGIN IMMEDIATE")
        if [
            dict(row) for row in connection.execute("SELECT * FROM skills ORDER BY id")
        ] != original:
            raise ValueError("CC-Switch metadata changed during preview; no migration performed")
        for identifier, values in changes:
            if identifier is not None:
                assignments = ", ".join(f'"{key}" = ?' for key in values)
                connection.execute(
                    f"UPDATE skills SET {assignments} WHERE id = ?", [*values.values(), identifier]
                )
            else:
                fields = ", ".join(f'"{key}"' for key in values)
                placeholders = ", ".join("?" for _ in values)
                connection.execute(
                    f"INSERT INTO skills ({fields}) VALUES ({placeholders})", list(values.values())
                )
        for identifier in removed:
            connection.execute("DELETE FROM skills WHERE id = ?", (identifier,))
        for row in disabled:
            connection.execute(
                "UPDATE skill_repos SET enabled = 0 WHERE owner = ? AND name = ?",
                (row["owner"], row["name"]),
            )
        enabled = [column for column in columns if column.startswith("enabled_")] + [
            column for column in ("installed_at", "updated_at") if column in columns
        ]
        after = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM skills")}
        for row in original:
            if row["id"] in removed:
                continue
            if any(after[row["id"]][column] != row[column] for column in enabled):
                raise ValueError(
                    "CC-Switch app enablement or installation timestamps changed unexpectedly"
                )
        if (settings_path.read_bytes() if settings_path.exists() else None) != original_settings:
            raise ValueError("CC-Switch settings changed during preview; no migration performed")
        temporary = settings_path.with_name("settings.json." + uuid.uuid4().hex + ".tmp")
        try:
            applied_settings = (json.dumps(new_settings, indent=2) + "\n").encode("utf-8")
            temporary.write_bytes(applied_settings)
            temporary.chmod(0o600)
            os.replace(temporary, settings_path)
            settings_written = True
            applied_identity = file_identity(settings_path.stat(follow_symlinks=False))
            connection.commit()
        finally:
            temporary.unlink(missing_ok=True)
        report["backup"] = str(backup)
        return report
    except Exception as error:
        if connection is not None:
            connection.rollback()
        if settings_written:
            try:
                unchanged = (
                    file_identity(settings_path.stat(follow_symlinks=False)) == applied_identity
                    and settings_path.read_bytes() == applied_settings
                )
            except OSError:
                unchanged = False
            if not unchanged:
                raise ValueError(
                    "CC-Switch settings changed after the migration write; reconciliation required. "
                    f"Current settings were preserved; original backup: {backup}"
                ) from error
            if backup is not None and (backup / "settings.json").exists():
                shutil.copy2(backup / "settings.json", settings_path)
            else:
                settings_path.unlink(missing_ok=True)
        raise
    finally:
        resources.close()
