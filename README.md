# Frum Finance Agent Skills

One reviewed catalog for agent skills used across Frum Finance. It reduces the drift between local skill folders and different agents: the same approved copies live in this repository, each with its upstream source and reviewed commit.

## Install the shared catalog

Install the whole catalog into your user-level folders for every supported agent on this machine:

```sh
npx --yes skills@1.8.1 add niebloomj/Skills --all --global --copy
```

To browse it first, run `npx --yes skills@1.8.1 add niebloomj/Skills --list`. To install selected skills, add `--skill <name>`. To sync after an approved catalog update, run `scripts/sync-skills.sh` or re-run the install with `--force`:

```sh
npx --yes skills@1.8.1 add niebloomj/Skills --all --global --copy --force
```

For project-scoped skills, run the command from the project directory and omit `--global`. The CLI writes to each selected agent's expected skills directory. For example, the test install wrote to both `~/.openhands/skills/` and `~/.agents/skills/`.

Merging an upstream update PR changes this catalog; it does not reach into people's machines. To keep everyone in sync automatically, call the sync script from your managed devcontainer, workstation setup, or agent-environment bootstrap. That keeps distribution repeatable without having each agent run arbitrary upstream update scripts or silently accept new skill instructions.

The official [Vercel Skills CLI](https://github.com/vercel-labs/skills) detects supported agents and creates the appropriate agent-specific skill directories. `skills/` in this repository is the canonical catalog; the CLI can discover its skills from the GitHub repository or a local clone.

For OpenHands only, use `npx --yes skills@1.8.1 add niebloomj/Skills --all --global --copy --agent openhands`. To share a consistent selection with a particular codebase, use the project-scoped CLI install and commit that project's generated lockfile rather than asking every agent to discover skills independently.

## How updates work

Third-party copies are pinned to immutable upstream commits in [`catalog.json`](catalog.json). A weekly workflow and a manual **Check upstream skills** action check those sources and open a pull request when selected skills change. It does not merge the pull request or execute upstream install scripts. Review the diff, skill instructions, accompanying files, and license before merging; merged content becomes the shared approved version. The repository must allow GitHub Actions to create pull requests (`Settings` → `Actions` → `General` → `Workflow permissions` → **Read and write permissions**); the workflow still grants write access only to its updater job.

Use the **Code** tab's **Pull requests** list to review updates. Add or remove catalog entries by editing `catalog.json` and reviewing the resulting content. The update script fails closed if a source path disappears, a skill's name changes, or an upstream file is not a regular text file.

## Catalog and attribution

- [`skills/`](skills/) contains the selected skill files and required companion references.
- [`catalog.json`](catalog.json) records each source repository, exact commit, upstream path, and license.
- [`licenses/`](licenses/) preserves source license texts. `sentry-cli` is under Sentry's Functional Source License (FSL-1.1-Apache-2.0), whose permitted-purpose and notice terms apply. The other included source repositories identify MIT or Apache-2.0 licenses.

The curated set includes the requested actively maintained skills plus explicit skill dependencies. `grill-me` and `grill-with-docs` are short aliases; `grilling` and `domain-modeling` are included because the aliases call them. The chief-of-staff workflow includes its referenced sibling skills. The writing-skills skill includes Superpowers' `test-driven-development` reference, and architecture/spec skills include the codebase-design and TDD companions they call.

Some requested sources were not copied:

- Matt Pocock's `resolving-merge-conflicts` has been removed upstream. The old `diagnose` skill was renamed `diagnosing-bugs`; that current skill is included.
- The Remotion skills repository has no repository-level license declaration, so its content is not redistributed here pending permission or licensing clarification.
- The anti-detect-browser skills teach browser fingerprint spoofing and cross-site identity techniques. They are excluded from a shared default catalog because they can enable identity and bot-detection evasion.
- Sentry CLI asks agents to confirm destructive commands before running them. Review any skill before installing it: skills are instructions to agents and may guide privileged actions in the user's environment.

This catalog is intentionally a small, reviewed distribution point, not an automatic mirror of every upstream repository.
