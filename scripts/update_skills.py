#!/usr/bin/env python3
"""Check selected upstream skill paths and safely refresh their vendored copies."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "catalog.json"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
MAX_FILE_BYTES = 5_000_000
MAX_SKILL_BYTES = 20_000_000
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def request(url: str) -> bytes:
    headers = {"User-Agent": "frumfinance-skills-updater", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=45) as response:
        return response.read(MAX_FILE_BYTES + 1)


def json_request(url: str) -> dict[str, Any]:
    payload = request(url)
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError(f"GitHub API response exceeds {MAX_FILE_BYTES} bytes: {url}")
    return json.loads(payload)


def safe_relative_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if "\\" in raw or path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe repository path: {raw!r}")
    return path


def skill_files(tree: dict[str, Any], source_path: str) -> list[dict[str, Any]]:
    prefix = safe_relative_path(source_path).as_posix().rstrip("/") + "/"
    entries = [entry for entry in tree["tree"] if entry["path"].startswith(prefix)]
    if not entries:
        raise ValueError(f"Upstream skill directory not found: {source_path}")
    if not any(entry["path"] == prefix + "SKILL.md" for entry in entries):
        raise ValueError(f"Upstream skill has no root SKILL.md: {source_path}")
    if any(
        entry["type"] not in {"blob", "tree"}
        or (entry["type"] == "blob" and entry.get("mode") not in {"100644", "100755"})
        or (entry["type"] == "tree" and entry.get("mode") != "040000")
        for entry in entries
    ):
        raise ValueError(f"Upstream skill contains a symlink, submodule, or non-regular file: {source_path}")
    return sorted((entry for entry in entries if entry["type"] == "blob"), key=lambda entry: entry["path"])


def validate_skill(name: str, files: dict[str, bytes]) -> None:
    content = files.get("SKILL.md")
    if content is None:
        raise ValueError(f"{name}: SKILL.md is missing")
    text = content.decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"{name}: SKILL.md has no valid YAML frontmatter block")
    frontmatter = text[4 : text.index("\n---\n", 4)]
    skill_name = re.search(r"^name:\s*([A-Za-z0-9_-]+)\s*$", frontmatter, re.MULTILINE)
    description = re.search(r"^description:\s*(\S.*)$", frontmatter, re.MULTILINE)
    if not skill_name or skill_name.group(1) != name:
        raise ValueError(f"{name}: upstream skill name changed or does not match the catalog")
    if not description:
        raise ValueError(f"{name}: SKILL.md description is missing")
    for path, data in files.items():
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{name}/{path}: file exceeds {MAX_FILE_BYTES} bytes")
        if b"\0" in data:
            raise ValueError(f"{name}/{path}: binary content is not permitted")
        data.decode("utf-8")


def load_catalog() -> dict[str, Any]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if catalog.get("schemaVersion") != 1 or not catalog.get("sources") or not catalog.get("skills"):
        raise ValueError("catalog.json has an unsupported or incomplete schema")
    destinations: set[str] = set()
    for name, skill in catalog["skills"].items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError(f"Invalid skill name: {name}")
        destination = f"skills/{name}"
        if destination in destinations:
            raise ValueError(f"Duplicate destination: {destination}")
        destinations.add(destination)
        if skill.get("source") not in catalog["sources"]:
            raise ValueError(f"{name}: source is not present in catalog.json")
        safe_relative_path(skill["upstreamPath"])
    for source, details in catalog["sources"].items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
            raise ValueError(f"Invalid GitHub repository: {source}")
        if not SHA_RE.fullmatch(details.get("ref", "")):
            raise ValueError(f"{source}: ref must be a full immutable commit SHA")
        license_file = safe_relative_path(details["licenseFile"])
        if license_file.parts[0] != "licenses":
            raise ValueError(f"{source}: license must be stored under licenses/")
        safe_relative_path(details["licensePath"])
    return catalog


def validate_local(catalog: dict[str, Any]) -> None:
    expected_dirs = {name for name in catalog["skills"]}
    actual_dirs = {path.name for path in (ROOT / "skills").iterdir() if path.is_dir()}
    if actual_dirs != expected_dirs:
        raise ValueError(f"Catalog folder mismatch; missing={sorted(expected_dirs - actual_dirs)}, extra={sorted(actual_dirs - expected_dirs)}")
    for name, entry in catalog["skills"].items():
        folder = ROOT / "skills" / name
        files: dict[str, bytes] = {}
        for path in folder.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"{name}: symlinks are not permitted in the catalog")
            if path.is_file():
                files[path.relative_to(folder).as_posix()] = path.read_bytes()
        validate_skill(name, files)
    for details in catalog["sources"].values():
        license_file = ROOT / safe_relative_path(details["licenseFile"]).as_posix()
        if not license_file.is_file() or not license_file.read_bytes().strip():
            raise ValueError(f"Missing or empty license file: {details['licenseFile']}")


def latest_ref(repository: str) -> str:
    metadata = json_request(f"{API}/repos/{repository}")
    branch = urllib.parse.quote(metadata["default_branch"], safe="")
    commit = json_request(f"{API}/repos/{repository}/commits/{branch}")
    sha = commit["sha"]
    if not SHA_RE.fullmatch(sha):
        raise ValueError(f"{repository}: GitHub returned an invalid commit SHA")
    return sha


def read_source(repository: str, ref: str, path: str) -> bytes:
    url = f"{RAW}/{repository}/{ref}/{urllib.parse.quote(path, safe='/')}"
    data = request(url)
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Missing, empty, or oversized upstream file: {repository}/{path}")
    return data


def fetch_skill(repository: str, ref: str, source_path: str, name: str, tree_cache: dict[tuple[str, str], dict[str, Any]]) -> dict[str, bytes]:
    key = (repository, ref)
    if key not in tree_cache:
        tree = json_request(f"{API}/repos/{repository}/git/trees/{ref}?recursive=1")
        if tree.get("truncated"):
            raise ValueError(f"GitHub returned a truncated repository tree for {repository}")
        tree_cache[key] = tree
    prefix = safe_relative_path(source_path).as_posix().rstrip("/") + "/"
    files: dict[str, bytes] = {}
    total = 0
    for entry in skill_files(tree_cache[key], source_path):
        relative = entry["path"][len(prefix) :]
        safe_relative_path(relative)
        data = read_source(repository, ref, entry["path"])
        total += len(data)
        if total > MAX_SKILL_BYTES:
            raise ValueError(f"{name}: skill exceeds {MAX_SKILL_BYTES} bytes")
        files[relative] = data
    validate_skill(name, files)
    return files


def current_files(name: str) -> dict[str, bytes]:
    folder = ROOT / "skills" / name
    files: dict[str, bytes] = {}
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError(f"Missing or unsafe catalog folder: skills/{name}")
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"{name}: symlinks are not permitted in the catalog")
        if path.is_file():
            files[path.relative_to(folder).as_posix()] = path.read_bytes()
    return files


def verify_pins(catalog: dict[str, Any]) -> None:
    tree_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for name, skill in catalog["skills"].items():
        source = catalog["sources"][skill["source"]]
        pinned_files = fetch_skill(
            skill["source"], source["ref"], skill["upstreamPath"], name, tree_cache
        )
        if pinned_files != current_files(name):
            raise ValueError(f"skills/{name} does not match its pinned upstream source")
    for repository, details in catalog["sources"].items():
        license_data = read_source(repository, details["ref"], details["licensePath"])
        license_path = ROOT / safe_relative_path(details["licenseFile"]).as_posix()
        if license_data != license_path.read_bytes():
            raise ValueError(f"{details['licenseFile']} does not match {repository}@{details['ref']}")
    print(f"Pinned content verified: {len(catalog['skills'])} skills from {len(catalog['sources'])} repositories.")


def update_catalog(catalog: dict[str, Any]) -> list[str]:
    catalog = json.loads(json.dumps(catalog))
    tree_cache: dict[tuple[str, str], dict[str, Any]] = {}
    skill_updates: dict[str, dict[str, bytes]] = {}
    license_updates: dict[str, bytes] = {}
    changed_sources: dict[str, str] = {}

    for repository, details in catalog["sources"].items():
        new_ref = latest_ref(repository)
        updates = {
            name: files
            for name, skill in catalog["skills"].items()
            if skill["source"] == repository
            and (files := fetch_skill(repository, new_ref, skill["upstreamPath"], name, tree_cache)) != current_files(name)
        }
        license_data = read_source(repository, new_ref, details["licensePath"])
        license_data.decode("utf-8")
        license_file = ROOT / safe_relative_path(details["licenseFile"]).as_posix()
        if not license_file.is_file():
            raise ValueError(f"Missing current license file: {details['licenseFile']}")
        if updates or license_data != license_file.read_bytes():
            details["ref"] = new_ref
            skill_updates.update(updates)
            if license_data != license_file.read_bytes():
                license_updates[details["licenseFile"]] = license_data
            changed_sources[repository] = new_ref

    if not changed_sources:
        print("No selected skills or licenses changed upstream.")
        return []

    temp_root = Path(tempfile.mkdtemp(prefix="skills-update-", dir=ROOT))
    replacements: list[tuple[Path, Path]] = []
    try:
        for name, files in skill_updates.items():
            target = temp_root / "skills" / name
            for relative, data in files.items():
                path = target / safe_relative_path(relative).as_posix()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        for path, data in license_updates.items():
            target = temp_root / safe_relative_path(path).as_posix()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        catalog_temp = temp_root / "catalog.json"
        catalog_temp.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        staged: list[tuple[Path, Path]] = [
            (ROOT / "skills" / name, temp_root / "skills" / name)
            for name in skill_updates
        ]
        staged.extend(
            (ROOT / safe_relative_path(path).as_posix(), temp_root / safe_relative_path(path).as_posix())
            for path in license_updates
        )
        staged.append((CATALOG_PATH, catalog_temp))
        backup_root = temp_root / "backups"
        for index, (destination, replacement) in enumerate(staged):
            if destination.is_symlink() or not destination.exists():
                raise ValueError(f"Missing or unsafe destination: {destination.relative_to(ROOT)}")
            backup = backup_root / str(index)
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, backup)
            replacements.append((destination, backup))
            os.replace(replacement, destination)
    except Exception:
        for destination, backup in reversed(replacements):
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
            if backup.exists():
                os.replace(backup, destination)
        raise
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    print("Updated selected upstream content:")
    for repository, ref in sorted(changed_sources.items()):
        print(f"  {repository} -> {ref}")
    for name in sorted(skill_updates):
        print(f"  skills/{name}")
    for path in sorted(license_updates):
        print(f"  {path}")
    return sorted(skill_updates)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate the local catalog without network access")
    parser.add_argument("--verify-pins", action="store_true", help="verify the vendored files match their pinned upstream commits")
    args = parser.parse_args()
    if args.check and args.verify_pins:
        parser.error("--check and --verify-pins are mutually exclusive")
    try:
        catalog = load_catalog()
        validate_local(catalog)
        if args.check:
            print(f"Catalog valid: {len(catalog['skills'])} skills from {len(catalog['sources'])} pinned sources.")
        elif args.verify_pins:
            verify_pins(catalog)
        else:
            update_catalog(catalog)
            validate_local(load_catalog())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
