# Project notes for Claude Code sessions working on this repo

## `.claude-plugin/marketplace.json` may be dirty during local dev — don't commit the dev state

This repo is published as `claude-speak@thu-san` (GitHub-installed by end users). The committed value of `.claude-plugin/marketplace.json` **must always** be:

```json
{ "name": "thu-san", ... }
```

**But during local development**, the maintainer installs the repo as a local marketplace via `/plugin install claude-speak@local`. Claude Code derives the plugin's data directory from `marketplace.json:name` — so with the committed value, a local-dev install and a production install on the same machine would collide on the same `~/.claude/plugins/data/claude-speak-thu-san/` dir.

To get a separate data dir during dev, the maintainer manually edits the file to `"name": "local"` and leaves it uncommitted.

### What this means for you (future Claude session)

**Before committing** — whenever a commit would include `.claude-plugin/marketplace.json` **OR** `.claude-plugin/plugin.json`:

1. **Check the current value** of each file's relevant field:
   - `marketplace.json:name` → should be `"thu-san"` to commit.
   - `plugin.json:version` → should not have a `-dev` / `-local` suffix to commit.

2. **If `marketplace.json:name == "local"`** (or `plugin.json:version` contains a dev suffix like `1.x.y-dev`):
   - **Do not stage that file.**
   - Flag it to the user: *"`.claude-plugin/marketplace.json` has `name: "local"` (dev-mode override). I'll skip it from this commit. If you meant to publish a name change, flip it back to `thu-san` first and tell me."*
   - Commit the rest of the changes without that file.

3. **If the values are canonical** (`"thu-san"` / no dev suffix): commit normally.

### Why not gitignore it

`.gitignore` would hide the file from staging, but Claude Code and other users need the canonical value tracked in the repo. So it has to stay versioned — we just have to notice when the local value is a dev override.

### Summary

- `main` and any release branch: `marketplace.json:name = "thu-san"`. Always. If a commit on `main` shows otherwise, that's a mistake — revert.
- Dev branches and dirty working trees: can have `"local"`. Safe to keep locally; unsafe to commit.
- Claude sessions: guard commits against the dev override leaking in.

## `.env` is per-machine, auto-generated — never commit it

`install.py` writes `<plugin_root>/.env` during SessionStart, containing this machine's `CLAUDE_PLUGIN_DATA=...` so the user can `source .env` for terminal CLI use. It's gitignored, but if you ever see it appear in `git status` as untracked and tempting to add — don't. It's per-machine and commits would leak absolute home paths + pin everyone else's terminal to your dir.
