# Hermes runtime patch: schema 4 port

Copies of the image-baked durable-video pieces, ported from the schema 3 proposal tools to
the schema 4 Inbox flow. Nothing in `~/.hermes` was edited.

## What it replaces in `~/.hermes/deployment/hermes-runtime`

| Here | Replaces | Image path |
|---|---|---|
| `extension/` (whole dir) | `extension/` | `/opt/durable-video/` |
| `plugin/` (whole dir) | `plugin/` | `/opt/hermes/plugins/durable-video/` |

Files that changed: `extension/retention.py`, `extension/inbox.py`, `extension/tests/test_retention.py`,
`extension/tests/test_inbox.py`, `plugin/__init__.py`, `plugin/plugin.yaml`,
`plugin/skills/lifecycle/SKILL.md`. New: `extension/tests/test_brain_hooks.py`. Everything else
is an unchanged copy. `manifest.json`, `apply_runtime.py`, `patched/` and the Dockerfile are
untouched: the manifest hash-gates only the four patched Hermes files, not these directories.

## Diff summary

- **Hooks** (`plugin/__init__.py`)
  - `pre_tool_call brain_capture`: marks every owned job whose URL matches `source_url`
    as capture-in-flight (`inbox.protect_capture`).
  - `post_tool_call brain_capture`: clears the mark, on success or failure. A written Inbox
    note protects the job from then on.
  - `post_tool_call brain_approve` / `brain_discard`: writes `maintenance-request.json`
    unless the result carries an error.
  - Removed: `brain_prepare_save`, `brain_commit_save`, `brain_reject_save` hooks.
- **inbox.py**: `protect_proposal_source` and `link_proposal` become `protect_capture` and
  `release_capture`, matching by URL or numeric post/YouTube id instead of a file path. The
  `review_state` checks for linked proposal ids are gone. The marker is
  `capture_preparations` (was `proposal_preparations`).
- **retention.py**: decision state comes from the vault, not `.second-brain/proposals` or
  `Reviews/`:

  | Vault state for the video (match on `source_url` key) | Result |
  |---|---|
  | any file in `Inbox/` (including `status: approved` not yet promoted) | keep |
  | `Notes/` note, `status: approved`, every `sources` entry exists in `Sources/` or `Archive/Sources/` | prune |
  | `Notes/` note failing those checks | keep |
  | `Archive/Inbox/` note | prune (no quiet period) |
  | no match | unchanged rules: inline-review marker, then seven-day retention |

  A note counts as this job's capture only if its `updated` is at or after the item's
  `finished_at`; older notes cannot close a newer job. Fail closed: a missing or non-schema-4
  `.brain-schema.json`, a missing `Inbox/`, `Notes/` or `Archive/Inbox/` folder, a symlinked
  note, or an unparseable note that mentions the video all protect the job.
- **Skill** (`lifecycle/SKILL.md`): proposal and `brain_prepare_save` text replaced with the
  `brain_capture` flow. The video hooks assume `content` is text, not a file path.

## Tests

Run from this directory: `cd extension && python3 -m unittest discover -s tests -p "test_*.py"`.
Host result: 59 run, 57 pass. The 2 errors need Hermes internals that the host lacks
(`plugins` for `test_durable_updates`, `hermes_constants` for `test_storage_failure_is_not_confirmed`)
and exercise Telegram intake code this port does not touch. With `--with python-telegram-bot`
the other 5 `test_plugin` tests pass. The image build runs the full suite through
`selfcheck.py`, where those internals exist.

## Rebuild (existing procedure, from `~/.hermes/deployment/hermes-runtime/README.md` and `Dockerfile`)

The Dockerfile copies `extension/` to `/opt/durable-video/` and `plugin/` to
`/opt/hermes/plugins/durable-video/`, then `apply_runtime.py`, `test_apply_runtime.py` and
`selfcheck.py` run with no network. `selfcheck.py` discovers `test*.py` in
`/opt/durable-video/tests`, so these tests gate the build.

```bash
DEP=~/.hermes/deployment
cp -R $DEP/hermes-runtime $DEP/hermes-runtime.pre-v4.bak                 # [LIVE: backup dir]
cp -R deploy/hermes/runtime-patch/extension deploy/hermes/runtime-patch/plugin $DEP/hermes-runtime/   # [LIVE]
# bump the tag in $DEP/compose.yaml for hermes AND video-worker, for example
#   hermes-local:durable-video-20261104.1 -> hermes-local:durable-video-20261104.2   [LIVE]
cd $DEP && docker compose --env-file ../.env build hermes                 # [LIVE: new image, no restart]
# at deploy step 4 (after migration and config switch):
docker compose --env-file ../.env up -d --no-deps hermes video-worker     # [LIVE: restarts both]
docker exec --user hermes hermes /opt/hermes/.venv/bin/python /opt/durable-video/inbox.py status
```

Rollback: restore `hermes-runtime.pre-v4.bak`, set the compose tag back to
`hermes-local:durable-video-20261104.1` (the old image stays on disk), and recreate both services.
That old image only matches a schema 3 vault.

## Not ported or not verified

- Not run against a live Hermes. No end-to-end capture, approve and prune cycle was done.
- `/brain_approve` through the approval plugin uses `ctx.call_mcp`. I did not verify that
  it fires `post_tool_call`. If it does not, cleanup waits for the hourly worker run.
- Match keys are `source_url` only. The skills must pass the video's canonical URL as
  `source_url` on every capture, or the job is never linked.
- Discarded notes keep their capture-time `updated` and mtime, so no quiet period is possible; discard prunes at once.
