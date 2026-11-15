# Schema 4 deployment runbook

Staged files only. Nothing here has been applied. Every step marked **[LIVE]** changes live
state and needs your approval. Run from the repository root unless a step says otherwise.
Shell variables: `DEP=~/.hermes/deployment`, `VAULT=~/Documents/second-brain`,
`RT=~/Desktop/codes/second-brain-runtime`.

## Files

| Path | Use |
|---|---|
| `second-brain/Dockerfile` | Image with the tagged runtime as a wheel, non-root, git installed |
| `compose.second-brain.yaml`, `second-brain.env.example` | The `second-brain` writer service |
| `hermes/config-snippet.yaml` | Hermes `mcp_servers` (HTTP, approver entry, stdio fallback) |
| `hermes/skills/` | Four rewritten skills |
| `hermes/cron-jobs.json` | Two cron jobs in the exact `jobs.json` record schema |
| `hermes/approval-plugin/` | `/brain-review`, `/brain-approve`, `/brain-discard` |
| `hermes/runtime-patch/` | Schema 4 port of the Hermes image's durable-video plugin and retention worker (see its README) |
| `clients/` | Proposed Claude Code and Codex entries |

## Order and downtime

Steps 0 to 2 prepare. Step 3 (migration) needs Hermes and every Mac client stopped, because
a schema 3 runtime must not write a schema 4 vault. Expect 10 to 20 minutes of Hermes downtime.
Pause Obsidian Sync first (Obsidian > Settings > Sync > Pause). Otherwise the phone receives
the whole file move as one burst and may conflict.

## 0. Snapshot the live vault **[LIVE: writes a git commit and tag in the vault]**

```bash
cd ~/Documents/second-brain
git status --short | wc -l            # about 177 expected
git check-ignore .second-brain        # must print .second-brain
git add -A
git commit -m "Snapshot before schema 4 migration"
git tag pre-v4-snapshot
git status --short | wc -l            # must print 0
cp -R ~/Documents/second-brain ~/Documents/second-brain.pre-v4.bak   # optional file-level copy
```

Rollback (use only after step 3): hard-reset the vault repository to the tag
`pre-v4-snapshot`, then remove untracked files with `git clean -fd`. The gitignored
`.second-brain/` stays.

## 1. Tag the runtime and build the image

Merge `redesign/v4` first. Tag the merge commit.

```bash
git -C $RT tag -a v4.0.0 -m "Schema 4" <merge-commit>        # [LIVE: local tag only, no push]
```

Build from a local export of the tag (no network, no push):

```bash
TMP=$(mktemp -d)
git -C $RT archive v4.0.0 --prefix=rt/ | tar -x -C "$TMP"
cp -R deploy/second-brain $DEP/second-brain                   # [LIVE: new dir in the deployment]
docker build --build-context repo="$TMP/rt" -t second-brain-runtime:4.0.0 $DEP/second-brain   # [LIVE: new local image]
docker run --rm --entrypoint python second-brain-runtime:4.0.0 -c "import second_brain; print(second_brain.__file__)"
```

The printed path must be under `site-packages`, not an editable path.
To build from GitHub instead, drop `--build-context` and push the tag first.

Also build the patched Hermes image now (no restart): follow `hermes/runtime-patch/README.md`,
section Rebuild, up to `docker compose build hermes`. **[LIVE: copies files into the deployment, edits the compose tag, builds an image]**

Rollback: `docker image rm second-brain-runtime:4.0.0; rm -r $DEP/second-brain`; for the Hermes image, restore `hermes-runtime.pre-v4.bak` and the old tag.

## 2. Prepare the compose service and migration maps

```bash
umask 077
printf 'SECOND_BRAIN_HTTP_TOKEN=%s\n' "$(openssl rand -hex 32)" > $DEP/second-brain.env   # [LIVE: new secret file]
cp deploy/compose.second-brain.yaml $DEP/compose.second-brain.yaml                        # [LIVE]
```

Add `- path: compose.second-brain.yaml` to the `include:` list at the top of
`$DEP/compose.yaml`. Check: `docker compose -f $DEP/compose.yaml config -q`.

Write the two migration files outside the vault, so the vault tree stays clean:

- `$DEP/migration/tag-map.yaml`: `{old-tag: new-tag, unwanted-tag: null}`. Every
  registered tag not listed keeps its name.
- `$DEP/migration/map-tags.yaml`: `{Map Name: [tag, tag]}`, one entry per file in `Maps/`.

Drafts of both, built from the vault's current tags and maps, are reviewed with you
before this step. JSON is also accepted, because `plan()` reads the files as YAML.

Dry run. It is read-only, so it is safe with Hermes running:

```bash
docker run --rm -v $VAULT:/vault:ro -v $DEP/migration:/migration:ro \
  -e SECOND_BRAIN_VAULT=/vault --entrypoint brain second-brain-runtime:4.0.0 \
  migrate-v4 --dry-run --tag-map /migration/tag-map.yaml --map-tags /migration/map-tags.yaml > /tmp/v4-plan.json
```

Read the plan: title clashes, dropped tags, pending proposals converted, unmapped maps.
Fix the map files until the plan is acceptable. Do not continue with unresolved clashes.

## 3. Migrate **[LIVE: rewrites the vault]**

```bash
# Stop writers. Quit Codex and Claude Code sessions that use the brain. Pause Obsidian Sync.
docker compose -f $DEP/compose.yaml stop hermes video-worker
docker run --rm -v $VAULT:/vault -v $DEP/migration:/migration:ro \
  -e SECOND_BRAIN_VAULT=/vault --entrypoint brain second-brain-runtime:4.0.0 \
  migrate-v4 --apply --tag-map /migration/tag-map.yaml --map-tags /migration/map-tags.yaml
git -C $VAULT log --oneline -3          # one migration commit on top of the snapshot
cat $VAULT/.brain-schema.json           # schema_version 4
```

After the migration, copy `deploy/vault/AGENTS.md` over `$VAULT/AGENTS.md` and `$VAULT/CLAUDE.md`. The file
replaces both. The live `CLAUDE.md` does not point at `AGENTS.md`: it is a separate, shorter copy of the same
schema 3 rules. Commit the change in the vault.

If the container user cannot write the bind mount (permission denied), stop and report it.
Do not run it as root without deciding that first.

Rollback: hard-reset the vault repository to `pre-v4-snapshot` and run `git clean -fd`.
Then delete `$VAULT/.second-brain/index.sqlite3` so the old runtime reindexes.

## 4. Start the writer, then switch Hermes **[LIVE]**

Start the writer and check it:

```bash
docker compose -f $DEP/compose.yaml up -d second-brain
docker compose -f $DEP/compose.yaml ps second-brain          # healthy
TOKEN=$(grep ^SECOND_BRAIN_HTTP_TOKEN $DEP/second-brain.env | cut -d= -f2)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18765/mcp                     # 401 without token
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/json, text/event-stream' http://127.0.0.1:18765/mcp           # not 401
docker exec second-brain brain doctor
```

Back up, then edit the Hermes config, skills, plugin:

```bash
docker cp hermes:/opt/data/config.yaml ~/Documents/hermes-backups/config.yaml.pre-v4
docker cp hermes:/opt/data/skills ~/Documents/hermes-backups/skills.pre-v4
docker cp hermes:/opt/data/config.yaml /tmp/config.live.yaml
```

1. Edit `/tmp/config.live.yaml`: replace `mcp_servers.second-brain` with Option A from
   `deploy/hermes/config-snippet.yaml`; add `second-brain-approver` and the `plugins` block.
   Skip the approver, the plugin and `tools.exclude` if you want the model to approve in chat.
2. Validate: `docker cp /tmp/config.live.yaml hermes:/tmp/c.yaml && docker exec hermes /opt/hermes/.venv/bin/python -c "import yaml;yaml.safe_load(open('/tmp/c.yaml'))"`
3. Install the config, skills, plugin, and token through a helper container, so every file
   stays owned by `hermes` (`docker cp` can leave files owned by root, which the gateway
   cannot read). Stage the files in `$STAGE` first: `config.yaml`, `skills/` from
   `deploy/hermes/skills`, `plugin/` from `deploy/hermes/approval-plugin`, and a copy of
   `$DEP/second-brain.env`:

   ```bash
   docker run --rm --user root --entrypoint sh -v hermes-data:/d -v $STAGE:/stage:ro \
     hermes-local:durable-video-20261115.1 -c '
   install -o hermes -g hermes -m 640 /stage/config.yaml /d/config.yaml
   grep -q "^SECOND_BRAIN_HTTP_TOKEN=" /d/.env || grep "^SECOND_BRAIN_HTTP_TOKEN=" /stage/second-brain.env >> /d/.env
   for s in media/social-video-content media/youtube-content note-taking/second-brain-knowledge-capture personal/short-video-capture; do
     cp -R /stage/skills/$s/. /d/skills/$s/; chown -R hermes:hermes /d/skills/$s; done
   cp -R /stage/plugin /d/plugins/second-brain-approval; chown -R hermes:hermes /d/plugins/second-brain-approval'
   rm -rf $STAGE
   ```

   The token goes in the profile `.env` (`/opt/data/.env`), which Hermes reads to resolve
   `${SECOND_BRAIN_HTTP_TOKEN}` in `config.yaml`. Compose needs no token for Hermes.
4. Remove schema 3 references from the four skills. They describe `brain_prepare_save` and
   `Reviews/Pending`: `second-brain-knowledge-capture/references/{commit-and-index-repair,
   inline-review-retrieval,scratch-source-lifecycle}.md` and
   `short-video-capture/references/proposal-review.md`.
5. Compose: set `read_only: true` on the hermes `/vault` bind mount. Leave out a
   `depends_on` on `second-brain`, so Telegram stays up when the writer is down.
6. Writer host names: the writer must list `second-brain:8765` in
   `SECOND_BRAIN_ALLOWED_HOSTS` (runtime v4.0.1 or later). Otherwise the MCP library answers
   Hermes with `421 Invalid Host header`.
7. Cron. `jobs.json` is owned by the running gateway; do not hand-edit it. Create the jobs with
   the CLI and the prompts from `deploy/hermes/cron-jobs.json`:
   `docker exec hermes hermes cron create --help`, then
   `docker exec hermes hermes cron create "30 8 * * 1-5" "<prompt 1>" --name second-brain-inbox-digest --deliver telegram`
   and `"0 18 * * 0"` with prompt 2 and name `second-brain-weekly-report`. Argument order is
   unverified; follow `--help`. The timezone is global: the container has `TZ=Asia/Kuala_Lumpur`.
   Existing jobs show `+08:00` offsets, so no per-job field exists or is needed.

Restart only what changed. This uses the image built in step 1, so the video worker and the
hooks run the schema 4 port:

```bash
docker compose -f $DEP/compose.yaml up -d hermes video-worker   # recreates both for the new image, env and mount
docker exec --user hermes hermes /opt/hermes/.venv/bin/python /opt/durable-video/inbox.py status
```

Rollback: stop `second-brain`, restore `config.yaml.pre-v4` and `skills.pre-v4` with `docker cp`,
remove the plugin dir and `plugins` block, remove the two cron jobs
(`hermes cron remove <id>`), revert the compose edits, and start `hermes` again. The old
stdio server only works on a schema 3 vault, so also roll back the vault (step 3).

## 5. Verify

```bash
docker exec second-brain brain doctor
docker exec second-brain brain find "prompt"             # approved notes found
docker exec second-brain brain validate
docker exec hermes hermes mcp list                        # second-brain connected; brain_approve absent if excluded
docker logs --tail 50 hermes | grep -i "second-brain"     # no 401 or placeholder errors
```

Round trip on a test note, from Telegram:

1. Send Hermes: "Save a test note titled 'zz deploy test' with the summary 'Test only.' and
   one registered tag". Expect a reply naming the title and saying it is in the Inbox.
2. `docker exec second-brain brain inbox` shows the item. `ls $VAULT/Inbox` shows the file.
3. Send "review inbox". Expect the item listed. With the plugin: `/brain-review <note_id>`,
   then `/brain-approve <note_id> <sha8>`. Without it: reply "approve it" in chat.
4. Check `$VAULT/Notes/zz deploy test.md` exists and `Inbox/` no longer has it.
5. Discard test: capture a second test note, then `/brain-discard <note_id>` (or tell Hermes).
   The file appears under `Archive/Inbox/` with a receipt in `Archive/Receipts/`.
6. Edit race: capture, `/brain-review`, edit the note in Obsidian, run the old
   `/brain-approve`. Expect a refusal.
7. Obsidian path: set `status: approved` on a third test note. It moves to `Notes/` after the
   next read tool call or `docker exec second-brain brain promote`.
8. Digest: `docker exec hermes hermes cron run <digest-job-id>` (check `hermes cron --help`).
   Expect the digest text, or "Inbox empty." when empty.
9. `git -C $VAULT log --oneline -8` shows one commit per write.
10. Remove the test notes through `brain_prepare_delete` or Obsidian, then resume Obsidian Sync
    and check the phone.

Mac clients: merge the entries from `clients/` into the real configs yourself **[LIVE]**, export
`SECOND_BRAIN_HTTP_TOKEN`, restart the clients, and call `brain_find`.

## Known gaps to settle before step 4

- The image-baked video hooks and retention worker are ported in `hermes/runtime-patch/`. The
  port is untested against a live Hermes, and skills must pass the video's canonical URL as
  `source_url` so retention can link the note to the job.
- File paths cannot cross containers. Skills now pass source text as `content`.
