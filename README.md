---
name: mail-skill
description: Manage multiple IMAP/SMTP mailboxes from Codex. Use when the user asks Codex to take over, inspect, triage, summarize, draft replies for, mark read, archive, move, delete advertising mail, or send confirmed replies across QQ, school, enterprise, or other IMAP mail accounts.
---

# Multi-Mailbox Mail Skill

Use this skill to operate the user's configured IMAP/SMTP mailboxes through `scripts/mail_tool.py`.

Default files:

- Account config: `configs/accounts.json`
- Account config example: `configs/accounts.example.json`
- Secret template: `configs/.env.example`
- Optional real secrets file: `configs/.env`
- Draft output folder: `configs/drafts/`

Never ask the user to paste passwords or authorization codes into chat. Store secrets in environment variables or in `configs/.env`.

## Safety Rules

- Read-only actions can be run directly: `list_accounts`, `fetch_recent`, `summarize_inbox`, `draft_reply`.
- Before any state-changing action, ask the user to confirm the exact action and message/account.
- The script also requires explicit confirmation flags:
  - `send_reply`: `--confirm SEND`
  - `mark_read`: `--confirm MARK_READ`
  - `archive_or_move`: `--confirm MOVE`
- `delete_message`: `--confirm DELETE_AD`
- If the user explicitly names specific non-ad messages to delete, use `delete_message --allow-explicit --confirm DELETE_EXPLICIT`.
- `delete_message` is intentionally conservative. It only proceeds when the message is categorized as advertising or newsletter-like by the local heuristic. It moves to a trash/deleted folder when possible.
- After every recent-mail summary, report every message in every category with a stable number and one-sentence summary. Include account ID and UID so the user can refer to either the number or the raw message key.

## Common Commands

Run commands from the skill folder:

```powershell
python .\scripts\mail_tool.py list_accounts
python .\scripts\mail_tool.py fetch_recent --account all --days 7 --limit 20
python .\scripts\mail_tool.py summarize_inbox --account all --days 0 --total-limit 300
```

Natural-language command mapping:

- `汇总最近邮件+数字`: match commands such as `汇总最近邮件50`, `汇总最近邮件100`, or `汇总最近邮件300`. Run `summarize_inbox --account all --days 0 --total-limit <数字>`, then present the `numbered_brief` list grouped by category. Do not add a recent-days filter for this command. `--total-limit` is divided across enabled accounts; with 3 mailboxes, `汇总最近邮件300` reads the latest 100 messages from each mailbox by current mailbox order.
- `删除广告`: after user confirmation, run `delete_advertising --account all --days 7 --limit 50 --confirm DELETE_ADS`; if the user specifies a wider range, pass that range.

Create a local draft only:

```powershell
python .\scripts\mail_tool.py draft_reply --account qq --uid 123 --body "Draft text here"
```

Send a previously created draft only after user confirmation:

```powershell
python .\scripts\mail_tool.py send_reply --draft .\configs\drafts\draft-example.json --confirm SEND
```

Mark, move, or delete only after user confirmation:

```powershell
python .\scripts\mail_tool.py mark_read --account qq --uid 123 --confirm MARK_READ
python .\scripts\mail_tool.py archive_or_move --account qq --uid 123 --dest-folder Archive --confirm MOVE
python .\scripts\mail_tool.py delete_message --account qq --uid 123 --confirm DELETE_AD
python .\scripts\mail_tool.py delete_message --account qq --uid 123 --allow-explicit --confirm DELETE_EXPLICIT
python .\scripts\mail_tool.py delete_advertising --account all --days 7 --limit 50 --confirm DELETE_ADS
```

## Workflow

1. Copy `configs/accounts.example.json` to `configs/accounts.json` and fill in non-secret account metadata.
2. Copy `configs/.env.example` to `configs/.env` and store authorization codes there, or set the same variables in the environment.
3. Run `list_accounts` first to verify account IDs, hosts, and whether secrets are present.
4. If an account reports `secret_loaded: false`, ask the user to add the corresponding authorization code to `configs/.env`.
5. For school or enterprise mailboxes, keep `provider` as `imap`. If the domain CNAME points to Tencent Exmail, prefer `imap.exmail.qq.com` and `smtp.exmail.qq.com` to avoid SSL hostname mismatch.
6. Use `fetch_recent` to collect raw message metadata and snippets.
7. Use `summarize_inbox` for a first-pass triage into `urgent`, `needs_reply`, `notification`, `advertising`, and `fyi`. For `汇总最近邮件+数字`, always use `--days 0 --total-limit <数字>` so the tool reads latest messages without a date filter.
8. Use Codex judgment on top of the script output before recommending deletes or replies.
