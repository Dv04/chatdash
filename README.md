# DHI Orbit

A local web dashboard that shows every Claude Code chat across one or more config directories on one page.

- A board of what needs you: questions, permission prompts, blocked jobs, and chats stalled on a usage limit.
- Reply to a chat from the page (one normal turn in that chat), stop it, or open it in Terminal.
- Usage limits per account (5-hour and 7-day), set up when you connect the account; never a guess.
- A graph of accounts, chats, work items and the files they touch.
- Optional WebGL "nebula" look (Settings, Look). The default look is plain and needs no GPU.
- Standard library only: no dependencies, no build step, no CDN.

Viewing costs zero model tokens: DHI Orbit only reads files. A reply you send is one normal turn in that chat.

## Screenshots

Screenshots are not included yet (placeholder).

## Install

Requires Python 3.10 or newer.

Homebrew (macOS or Linux):

```sh
brew install Dv04/dhi-orbit/dhi-orbit
```

pipx:

```sh
pipx install git+https://github.com/Dv04/dhi-orbit
```

pip, in any virtualenv:

```sh
python3 -m pip install git+https://github.com/Dv04/dhi-orbit
```

You also need Claude Code itself (`claude` on your `PATH`).

## Quick start

```sh
dhi-orbit
```

`orbit` is a shorter name for the same command. Then open <http://127.0.0.1:8787/v2/>. `dhi-orbit --help` lists the options (`--port`, `--window-hours`, `--every`, `--no-notify`). macOS notifications for chats that need you are on by default; `--no-notify` turns them off.

On first run DHI Orbit creates its data directory, `~/.config/dhi-orbit` (or `$DHI_ORBIT_HOME`), with mode 700, and an
access token inside it (`.token`, mode 600). The page gets the token injected when it is served from loopback.

### Upgrading from chatdash

DHI Orbit is the new name of chatdash. The `chatdash` command still works (as do `chatdash-ask-hook`,
`chatdash-stop-hook` and `chatdash-statusline`), an existing `~/.config/chatdash` data directory (with its
`chatdash.db`) keeps being used, and the `CHATDASH_*` environment variables are still read; the `DHI_ORBIT_*`
name wins when both are set. A status line meter or hook that chatdash installed keeps running (a small
`chatdash` module forwards to DHI Orbit) and the meter is rewritten to the new command the next time you turn
usage on for that account. While the old data directory is in use, the Schedules panel keeps managing your
`com.chatdash.*` launchd jobs; new installs use `com.dhi.orbit`. Python imports are now `dhi_orbit`.

## Connect your accounts

Open **Settings > Accounts** (a fresh install opens on that screen). Type a name, for example `work`, and press
**Connect account**. DHI Orbit creates `~/.claude-work` and runs Claude Code's own `claude auth login` for it:
a claude.com sign-in page opens in this computer's browser, you approve, and the account shows as signed in.
From another device (the page on your phone, say), open the sign-in link shown there instead, approve, and paste
the code claude.com shows; it is typed into Claude Code's own prompt.

DHI Orbit never sees, stores or sends your password or tokens: Claude Code writes its credentials into that
account's folder exactly as when you sign in in a terminal. Accounts you already use (`~/.claude`, any
`~/.claude-<name>`) appear by themselves.

- **Disconnect** hides an account from the board; its sign-in and chats are untouched. **Reconnect** shows it again.
- **Delete** signs the account out (`claude auth logout`) and moves its folder to the Trash. It needs you to type
  the account name, and refuses while a chat on it is running. `~/.claude` (your default) is only signed out and
  hidden, never moved.

Use an account in a terminal with `CLAUDE_CONFIG_DIR=~/.claude-work claude`, or start chats from the board.
Only connect accounts that are yours; Anthropic's terms do not allow sharing logins.

## Multi-account

DHI Orbit looks for config directories in your home folder: `~/.claude` and any `~/.claude-<name>` that contains
both `projects/` and `sessions/`. Each one is an account ("seat" in the UI): `~/.claude` is called `main`,
`~/.claude-work` is called `work`. Replies, stop and new chats run through the unmodified `claude` binary with that
account's `CLAUDE_CONFIG_DIR`.

Labels come from the directory name (`work` is shown as "Work"). Override them, and mark accounts as read-only
(shown, never acted on), in `config.json` in the data directory. Copy `config.example.json` to start.

| Key | Default | Meaning |
|---|---|---|
| `seat_labels` | `{}` | account name to display label |
| `read_only_accounts` | `[]` | accounts that are shown but never replied to, stopped, spawned on or hooked |
| `work_item_pattern` | `""` | regex; a chat whose name matches it belongs to that work item (for example `PROJ-\d+`). Empty: no work items, chats group by account and folder |
| `evidence_gate_work_items` | `[]` | work items whose chats get the evidence gate on by default |
| `default_cwd` | your home folder | folder for chats started from the page |
| `launchd_prefix` | `com.dhi.orbit` | label prefix of the launchd jobs the Schedules panel manages |
| `timezone` | this computer's | IANA name used to show clock times |
| `meter_log` | `<data dir>/meter.log` | where usage readings come from (below) |
| `public_url` | `""` | host name of a tunnel you set up yourself (below) |
| `hidden_accounts` | `[]` | accounts disconnected in Settings > Accounts (not shown on the board) |
| `plugins` | `{}` | optional helper modules, for example `{"usage_live": {"path": "/dir", "module": "usage_live"}}` |

The binary is found from `$CLAUDE_BIN`, then `PATH`, then `~/.local/bin/claude`.

## Usage limits

DHI Orbit does not call any usage endpoint. Claude Code hands its status line command each account's limits
(`rate_limits`: `five_hour` and `seven_day`, with `used_percentage` and `resets_at`), for every chat, background ones
included. DHI Orbit records them with a status line of its own:

- **Connecting an account** in Settings > Accounts turns it on when the account has no status line yet.
- **Any other account** has a **Show usage** button there. If the account already has a status line, DHI Orbit keeps
  it: its own command runs first to record the limits, then yours runs with the same input and its output is what
  your terminal shows. **Turn off**, Disconnect, or signing out `main` put your status line back exactly. A status
  line you changed yourself afterwards is never touched.
- Claude Code only runs the status line while a chat is open, so a new account shows "waiting for the first chat"
  until it has run one. With the meter off, the board says "usage not connected" instead.

The setting is the account's `settings.json` `statusLine` (your previous one is kept in `dhi-orbit-statusline.json`
beside it). The readings go to a meter log, one tab-separated line per change: `ISO time`, `config dir`,
`session id`, `rate_limits` JSON. `dhi-orbit-statusline` is the same command, if you prefer to call it from a status
line script of your own, and `contrib/statusline-meter.py` is a standalone copy that only records.

A reading whose reset time has passed shows "?" and "reset since the last reading", never 0%. An optional
`usage_live` plugin module (`fetch_live(account_dir_name)` returning `{"five": pct, "seven": pct}`) can supply a
fallback reading; without it nothing is fetched.

## Automatic actions

Everything that sends text into a chat on its own has a mode, `off`, `dry-run` or `on`, set in `config.json`
(or on the Settings page). The default is `dry-run`: it logs what it would do and sends nothing. The modes are
`limit_resume`, `decision_hook`, `stop_gate`, `shadow_drafts`, `corrections`, `handoff` and `idle_compact`.
Automatic keep-warm (a tiny ping before a chat's prompt cache expires) is off until you switch it on in the toolbar.
Permission prompts are never answered automatically.

Two optional Claude Code hooks feed the board. They are not installed for you; register them yourself in
the Claude Code configuration of each account you want covered:

- `dhi-orbit-ask-hook`, a `PreToolUse` hook with matcher `AskUserQuestion`: puts a background chat's question on the board.
- `dhi-orbit-stop-hook`, a `Stop` hook: records a receipt (files changed, checks run) for each turn and, when the
  evidence gate is on, asks the chat for verification evidence before it ends. At most 3 blocks per turn.

Both fail open: any error, a read-only account, or a non-background session prints nothing and the chat carries on.

## How a reply is delivered

| Chat | Route |
|---|---|
| background session, idle or stopped | typed into `claude attach <id>` through a pty, confirmed by reading the prompt back from the transcript |
| open in a terminal tab | refused: Return does not submit through a paste into Claude's input box, and two writers corrupt a session |
| closed chat with no background job | `claude --resume <id> --bg "<text>"` (a copy under a new id; the first reply re-caches the context once) |
| working right now | refused until idle, or queued and sent when it goes idle |

## Security model

- The server binds to 127.0.0.1 only. The `Host` header is checked (no DNS rebinding).
- Every `/api` call needs the token from `<data dir>/.token` (header `X-Token` or `?token=`). The token file is
  created with mode 600 in a mode 700 directory.
- Sends go only through the unmodified `claude` binary under each account. DHI Orbit makes no network calls of its own,
  except `gh pr view` (if `gh` is installed) to show whether a PR is still open, and, only if you configure the
  identity-header check below, one request to that identity endpoint.
- Reaching it from other devices is your choice and your setup: put a tunnel of your own in front of the loopback port
  and describe it in `<data dir>/public.json` (`host`, and either `"key_login": true` for a one-time key sign-in that sets a
  30-day cookie, or `team_domain` plus `emails` for an identity-header check). With no `public.json` and no `public_url`, any other
  `Host` is refused. With a host but no sign-in method configured, it stays locked.

## Compatibility

DHI Orbit reads undocumented Claude Code internals. These are not a stable interface and may change in any Claude Code update,
which can break parts of DHI Orbit:

- `<config dir>/sessions/<pid>.json` (live session records) and `<config dir>/jobs/<id>/state.json` (background jobs),
- the transcripts, `<config dir>/projects/*/*.jsonl`,
- the screen output of `claude attach` and `claude logs`, parsed to read and answer permission prompts and questions.

Tested with Claude Code 2.1.289 on macOS. If something looks wrong after an update, the board may be showing stale or
missing data; limits and health show UNKNOWN rather than OK when a source cannot be read.

macOS-only parts: opening a chat in Terminal and notifications use `osascript`, and the Schedules panel uses `launchctl`.
DHI Orbit is developed on macOS; a clean pipx install on Linux was tested end to end (accounts, sign-in, board, nebula).

## Development

```sh
python3 -m pytest -q
```

`tools/mock/server.py` serves the UI with synthetic data (no real chats are read); `tools/acceptance/` holds the UI
checks. `docs/DESIGN.md` records the visual design.

## About

Built by Dev Sanghvi at DHI (https://dhi-tech.com).

## License

MIT, see `LICENSE`.
