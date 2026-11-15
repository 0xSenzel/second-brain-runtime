---
name: second-brain-knowledge-capture
description: Capture extracted source material into the second-brain Inbox, then review and approve notes in batches.
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [second-brain, knowledge-capture, review-workflow, obsidian]
    related_skills: [obsidian, short-video-capture, social-video-content, youtube-content]
---

# Second-brain knowledge capture

Use this skill to save notes and revisions from extracted source material, to review the
Inbox, and to approve or discard items. Source-specific extraction belongs in the media or
document skill. A recap request needs only a response in chat; a save request calls
`brain_capture`.

Schema 4 splits saving from approval:

- **Capture** writes an unreviewed note to `Inbox/` at once. It needs no approval.
- **Approval** moves a note to `Notes/`. It needs the user's explicit decision on the
  exact text they saw.

Never write vault files by another path. Hermes has no write access to the vault; only
the brain tools save.

## Design the note for reuse

Name the note for the problem or task it helps with. Separate three fields:

- `summary`: concise prose for search, without Markdown headings, at most 1,500
  characters.
- `body`: the complete Markdown saved in the note. Include the usable method, worked
  example or exact prompt, limits, and evidence pointers.
- `content`: raw source material, kept in the linked source record. For video, preserve
  the timestamped transcript, captions, slide text, and extraction caveats. Pass the
  text itself. The brain runs in its own container and cannot read Hermes file paths.

Use the following body structure when its sections add useful content:

```markdown
## Use this when
[Situation, prerequisites, and expected outcome]

## Method
1. [Concrete action supported by the source]
2. [How to check the result]

## Example or reusable material
[Worked example, checklist, command, or complete original prompt]

## Limits
[Failure cases, costs, risks, and unverified claims]

## Evidence
- [Key point] — [timestamp or slide], [speech / screen / caption]
```

Attribute creator claims, extracted observations, agent inference, and independent
verification separately. Preserve exact original prompt wording; label translations and
adaptations. If timestamps are unavailable, identify the slide or evidence surface.
Avoid inventing a step, example, or time to complete the template.

For tools, explain when installation adds value over the user's existing stack, setup
requirements, and whether you recommend installation or retaining it as a reference.

## Capture

1. Search with `brain_find` first. It also returns Inbox items, marked `unreviewed`.
2. If an approved note covers the same method, read it with `brain_read` and capture a
   revision: pass its id as `revises`. A revision keeps the note's identity, `created`
   date, and every source.
3. Call `brain_list_tags` and choose one to five registered tags. Topic belongs in tags,
   never in a folder.
4. Choose `note_type` (`concept`, `prompt`, `skill`, `project`, `essay`) and `domain`
   (`personal`, `tech`).
5. Call `brain_capture` with `content`, `title`, `summary`, `body`, `note_type`,
   `domain`, `tags`, and optional `aliases`, `connections`, `source_url`, `source_kind`,
   `source_creator`, `revises`. Pass lists as JSON arrays. Connections are objects with
   `relation`, `target` (a plain title, no wikilink), and optional `reason`.
6. Set `source_url`, `source_kind`, and `source_creator` explicitly. Include submitted
   and canonical URLs, post ID, account, original language, and extraction date in the
   source text. Use real newlines in text fields.

If a suitable tag is missing, call `brain_register_tags` with a short meaning for each
new tag, and tell the user which tags you added. Aliases and maps are optional.

The same `source_url` with the same `revises` updates the existing Inbox item in place.
Use `brain_update_inbox` to correct fields of one Inbox item.

## Reply after capture

Do not print the whole note and do not ask for approval before saving. Reply with:

- the title, note type, domain, and tags;
- a one-line summary of what the note covers;
- that it is saved in the Inbox, not yet approved;
- every item in `problems` the tool returned, as a warning to fix before approval.

Example: `Saved to Inbox: "Prompt-chaining for code review" (prompt, tech; tags: prompting,
code-review). It covers a three-step review prompt. Not approved yet. Say "review inbox"
when you want to approve it.`

## Review and approve in batches

When the user asks to review, or a digest lists items:

1. Call `brain_inbox`. Present one numbered line per item: title, type, domain, tags,
   age, and warnings.
2. For each item the user wants to inspect, call `brain_inbox` or `brain_read` for it and
   reproduce its `summary` and `body` VERBATIM. Quote them; never paraphrase. If the
   message is too long, split it across messages. A metadata recap cannot replace the
   verbatim text, because approval of an unseen note means nothing.
3. Ask for the decision on named items: approve, edit, or discard.
4. Edit with `brain_update_inbox`, then show the changed text again. An edit changes the
   item's `content_sha`, so earlier approval no longer applies.

Approval of one item, or of tag registration, is not approval of any other item.

### Approve

- If the `brain_approve` tool is available: after the user explicitly approves the exact
  text you showed, call `brain_approve` with `note_id`, `content_sha` (from the same
  read), and `user_approved: true`. A `content_sha` mismatch means the note changed;
  re-read it, show it again, and ask again.
- If `brain_approve` is not available, approval is out of band. Tell the user to send
  `/brain_review` (shows the oldest Inbox note, or `/brain_review <note_id>` for one note)
  and then `/brain_approve`, which approves exactly the version that review showed; or to
  set `status: approved` in the note's frontmatter in Obsidian.
  Do not claim the note is approved until `brain_find` or `brain_read` shows it in
  `Notes/`.

If approval fails with problems (for example an unregistered tag), report them, fix them
with `brain_update_inbox` or `brain_register_tags`, and ask again.

### Discard

Call `brain_discard` with `note_id` and a short `reason` only after the user names the
item to discard. Discard archives the note with a receipt. It never deletes permanently.

## Delete an approved note

Deletion of an approved note keeps two steps. Call `brain_prepare_delete`, show the exact
archive changes, and call `brain_commit_delete` only after the user approves that exact
proposal id. Approval to build or test never approves a deletion.

## Verify the result

After a capture, confirm the note appears in `brain_inbox`. After an approval, confirm the
note appears in `brain_find` results as approved and that `brain_validate` reports no new
findings. Report a failed step with the tool's exact error. Do not retry a write that
returned an error before reading the current state with `brain_inbox` or `brain_read`.
Never delete or merge vault files by another path.
