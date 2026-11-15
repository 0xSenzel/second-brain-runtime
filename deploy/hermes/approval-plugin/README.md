# second-brain-approval (Hermes plugin)

Out-of-band approval: the user types a slash command in Telegram and plugin code, not the
model, calls the brain's `brain_approve` with the right `content_sha`.

## Why this works (Hermes source, read-only)

| Fact | Evidence |
|---|---|
| Plugins register slash commands with `ctx.register_command(name, handler, description, args_hint)`; handler is `fn(raw_args) -> str`. | `hermes_cli/plugins.py:649-686` |
| Gateway runs a plugin command itself, with no model turn. | `gateway/session_commands.py:180-243` (`_runner_command`, `_run_runner_command`) |
| A plugin can call an MCP tool: `ctx.call_mcp(server, tool, args, timeout)`. Default-deny; needs `plugins.entries.<id>.mcp_allowlist`. | `hermes_cli/plugins.py:502-531`, docs `website/docs/user-guide/features/plugins.md:1140-1157` |
| `call_mcp` goes straight to the server handler, so it works for a server whose tools are hidden from the model. | `tools/mcp_tool_handlers.py:692-722` vs filter in `tools/mcp_tool_registration.py:249-263` |
| Manifest: `plugin.yaml` + `register(ctx)`; enable with `hermes plugins enable <name>`. | `hermes_cli/plugins_manifest.py:27`, docs `developer-guide/plugins/index.md:81` |

## Design

- `second-brain` entry (model-facing): `tools.exclude: [brain_approve]`.
- `second-brain-approver` entry (plugin-only): same URL and token, `tools.include: []`.
- Server runs `SECOND_BRAIN_APPROVAL=chat`, so the plugin's call (`user_approved=true`) is accepted.
  Do not use `human` mode with this plugin: the plugin would be refused too.
- `/brain_approve <id> <sha8>` re-reads the Inbox and approves only when `sha8` matches the
  current note. An edit after review changes the sha and the command refuses.

## Install (changes live state; see deploy/README.md step 3)

```bash
docker cp deploy/hermes/approval-plugin hermes:/opt/data/plugins/second-brain-approval
# merge deploy/hermes/config-snippet.yaml (Option A) into /opt/data/config.yaml
docker exec hermes hermes plugins enable second-brain-approval
docker compose restart hermes
```

The live plugin directory is `~/.hermes/plugins` on the host. In the container it is
`/opt/data/plugins` (inside the hermes-data volume). Confirm the path before copying.

## Limits

- Commands, not inline buttons. Telegram buttons need `ctx.register_telegram_handler(factory)`
  with a `CallbackQueryHandler(pattern=...)` (`hermes_cli/plugins.py:848-853`); core already
  registers a catch-all, so a bad pattern would swallow core button flows. Not built here.
- Who may run a command is set by the gateway's slash-access policy and Telegram allow-list.
- Assumes `brain_inbox` returns items with `note_id`, `title`, `summary`, `body`,
  `content_sha`. Edit `_items` if the schema-4 tool differs.
- Not run against a live Hermes. Test with `/brain_review` on a test note first.
