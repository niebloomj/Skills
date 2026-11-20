# Personal Agent Skills Catalog

A personal, portable collection of third-party and locally authored agent skills.
This repository is the source of truth, not a list of upstream installers.
Third-party content is pinned to reviewed commits in [`catalog.json`](catalog.json).

## Install on a machine

Requires Python 3.11+, symlink support, and a persistent checkout. GitHub CLI is needed to clone or
check upstream sources, but installation itself uses only local files.

```sh
gh repo clone niebloomj/Skills ~/Documents/PersonalSkills
cd ~/Documents/PersonalSkills
python3 scripts/sync_skills.py
python3 scripts/sync_skills.py --apply
python3 scripts/sync_skills.py --audit
```

The first command previews changes without modifying installed skills or profile data. `--apply` backs up
existing copies and retired entries before replacing them. `--audit` exits
nonzero for unmanaged, divergent, retired, or unexpectedly nested global skills.
Re-running an unchanged installation is a no-op.

The installer creates one definition per personal skill:

```text
agent/skills/name -> ~/.agents/skills/name -> this-checkout/skills/name
```

It detects existing Claude Code, Codex, OpenCode, Gemini, Pi, Cursor, and
CC-Switch skill directories. It also respects an existing
`OPENCODE_CONFIG_DIR`. Keep this checkout in place: moving it requires another
sync. Branch switches also change the live skills, so keep the canonical checkout
on an approved catalog revision. `scripts/sync-skills.sh` wraps the same Python command.

Installation never executes upstream scripts or installs their dependencies.
Individual skills may require tools, subscriptions, plugins, or MCP servers.
For example, `gpt-image-2` needs the Codex CLI and a suitable ChatGPT subscription.

## Coexist with a company registry

Keep company-managed skills with their registry rather than copying internal
content into this public repository. Register its lockfile once:

```sh
python3 scripts/sync_skills.py --external-lock /absolute/path/to/registry-lock.json
python3 scripts/sync_skills.py --external-lock /absolute/path/to/registry-lock.json --apply
```

An external lock must contain a `skills` mapping keyed by skill ID. The installer
preserves those canonical definitions and repairs stale projection links. It
does not replace registry-owned Claude-specific paths, whether real directories
or symlinked client variants. Missing registered lockfiles are errors, not silently ignored.

Machine-specific configuration is private, outside this repository:
`~/.local/state/personal-skills/config.json`. It remembers external lockfiles and
can specify absolute `extraRoots`, explicit runtime-owned `exemptRoots`, and
additional `retired` IDs. `externalOwners` preserves selected company repository
discovery entries when migrating CC-Switch.

An exact-path `retainedDanglingLinks` list can acknowledge deliberately retained
broken links. Audits still display them as warnings. If a target becomes an
active unmanaged skill, it is an error again, not a permanent ownership exception.

Application-bundled/system skills, enabled native runtime plugins, and
project-local contributor skills remain with their owners. Runtime-synchronized
skill containers must be explicitly listed in `exemptRoots`. Unknown personal
skills, including dangling links to temporarily unavailable storage, are reported
but never removed unless explicitly retired.

## CC-Switch

Fully quit CC-Switch, including its tray process, then preview and apply:

```sh
python3 scripts/sync_skills.py --cc-switch
python3 scripts/sync_skills.py --cc-switch --apply
python3 scripts/sync_skills.py --audit
```

The optional Unix offline adapter is tested against CC Switch 4.0.35. It snapshots the
database and settings, checks the schema and concurrent changes, preserves every
existing app-enablement flag and installation timestamp, and selects shared
storage with symlink-only projections. Unrelated preferences are preserved.

Personal rows intentionally become **Local** in CC-Switch. This prevents its
updater from replacing repository links with independent upstream downloads.
Upstream provenance remains in this catalog and each row's repository URL.
Only superseded source repositories are disabled; unrelated discovery choices
are preserved. Use this repository for content updates, not CC-Switch's update or uninstall buttons. CC-Switch can
still manage app projections; shared compatibility directories mean its flags
do not provide complete per-agent skill isolation.

The offline adapter is remembered for subsequent syncs, which require CC-Switch
to remain closed. If metadata migration fails, unchanged filesystem edits roll
back. Concurrent user edits are left intact and reported, with originals retained
in the private backup for reconciliation.
Private backups are under `~/.local/state/personal-skills/backups/`; they can
contain private configuration and must never be committed or shared.

## Names, invocation, and dependencies

- `sentry-code-review`: Sentry's general correctness, security, performance, and
  testing checklist.
- `standards-spec-review`: Matt Pocock's separate standards/spec review,
  explicitly invoked. There is no generic `code-review` ID in this catalog;
  application-bundled commands are outside its scope.
- Superpowers' complete fourteen-skill set shares one upstream revision. Its
  skill references are unqualified so no `superpowers:` plugin namespace is
  required. The duplicate Codex Superpowers plugin is disabled only when all
  fourteen replacements are present; native runtime plugins are untouched.
- `grill-me` is retired in favor of `grilling`.
- Trail of Bits retired `ask-questions-if-underspecified`; its obsolete copy is
  removed rather than frozen into this collection.
- Standalone Codex runtime-helper copies are retired. Their native plugin
  remains responsible for the matching helpers and runtime scripts.
- Curation retirements are recorded in `catalog.json`. Sync backs up and removes
  their global projections and CC-Switch rows instead of reinstalling them.

Vendored invocation-only skills carry both standard `disable-model-invocation` metadata
and Codex's `policy.allow_implicit_invocation: false`. Workflows such as
`second-opinion` and `standards-spec-review` are explicit-only. User and project
approval, scope, and delegation rules still take precedence over imported workflows.

## Add your own skill

Create `skills/my-workflow/SKILL.md` with `name: my-workflow` and a useful
`description` in YAML frontmatter. Add this catalog entry:

```json
"my-workflow": {"local": true}
```

Locally authored skills have no invented upstream pin and are never replaced by
the upstream updater. Keep their supporting files beside `SKILL.md`.

## Review and update

A weekly workflow and manual **Check upstream skills** action propose upstream
changes as draft PRs. Merging a PR updates the approved catalog, not installations
on other machines. Pull the reviewed catalog and run the sync/audit commands on
each machine. Do not refresh this collection through separate upstream installers.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/update_skills.py --check
python3 scripts/update_skills.py --verify-pins
```

To import new third-party entries after recording their source, commit, path,
and license, use `python3 scripts/update_skills.py --vendor`. It stages all
downloads before replacement, preserves executable companions, and fails closed
on missing paths, renamed skills, unsafe file types, or oversized content.

Renames (`upstreamName`), explicit invocation (`manual`), Markdown reference
rewrites (`replaceMarkdown`), and optional gallery exclusions (`exclude`) are
recorded transformations, reproduced by imports, verification, and updates.
Codex display names and manual-only policy are normalized for portable discovery.
The GPT Image 2 example gallery is excluded: its images are linked remotely and
are not dependencies of the included generation helpers.

## Attribution and licenses

[`catalog.json`](catalog.json) records every upstream repository, immutable
commit, selected path, transformation, and license notice.
[`licenses/`](licenses/) preserves those notices verbatim. Trail of Bits content
is CC-BY-SA-4.0; Sentry CLI uses FSL-1.1-Apache-2.0. Other selected sources declare
MIT or Apache-2.0. Those source-specific terms still apply to their content.

One upstream publishes an MIT declaration in its README rather than a standalone
license file. The declaration and limitation are explicitly preserved in the
catalog and notice; no missing license text is invented.

Remotion remains excluded pending licensing clarification. Browser fingerprint
spoofing/identity-evasion skills are not part of this curated collection.
