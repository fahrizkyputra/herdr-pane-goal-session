# Pane Goal & Session

A [Herdr](https://herdr.dev) plugin that remembers, per pane:

- a **goal** — free text describing what that pane / agent is working on
- the **agent session ID** — recorded automatically, so after a Herdr restart or an agent
  crash you know exactly which conversation to resume, with a copy-ready command

```
● my-app · auth
  pi · pi:3f9c2a1b                  ← $session
  Add Google login to the API       ← $goal
```

```
Resume: cd ~/projects/my-app && pi --session ~/.pi/agent/sessions/…/…3f9c2a1b….jsonl
Copied to clipboard.
```

## Features

- **Popup editor** on a keybinding: goal (free text) + session (auto-filled, editable)
- **Automatic session tracking** for every agent pane, updated when the agent starts a new
  session; an empty value never overwrites a saved one
- **Manual override**: edit the session field to pin a value; clear it to go back to auto
- **Resume command** with the pane's working directory, copied to the clipboard
- **Survives restarts**: state is saved to disk and re-published by a startup hook, even when
  the agent did not come back
- Follows panes moved to another workspace; removed when the pane closes
- State kept per Herdr session; Python 3 standard library only

## Requirements

- Herdr 0.9.0 or newer, with the official integration for your agent installed
  (`herdr integration install pi`, `claude`, `codex`, …) so Herdr reports session IDs
- `python3` on `PATH` (3.8+)
- Linux or macOS

## Install

```bash
herdr plugin install fahrizkyputra/herdr-pane-goal-session
```

Add a keybinding and sidebar tokens to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "pane-goal-session.edit"
description = "edit pane goal & session"

[ui.sidebar.agents]
rows = [
  ["state_icon", "machine", "workspace", "tab"],
  ["agent", { token = "$session", dim = true }],
  [{ token = "$goal", fg = "#89b4fa" }],
]
```

If you already customize `[ui.sidebar.agents]`, add `"$goal"` and `"$session"` to your rows.
Then reload:

```bash
herdr server reload-config
```

## Usage

1. Focus a pane.
2. Press `prefix` then `i` (default prefix is `Ctrl+B`).
3. `Goal ›` — type a goal, Enter.
4. `Session ›` — prefilled with the agent's session; Enter to keep it.
5. The resume command is shown and copied. Enter closes the popup.

| Field | Empty + Enter |
| --- | --- |
| Goal | Clears the goal |
| Session | Unpins a manual value and returns to automatic tracking |

`Ctrl+C` cancels without saving.

### After a restart

If Herdr restarts and the agent does not come back, the pane becomes a plain shell, and Herdr
shows agent sidebar rows only for live agents. Open the popup in that pane (`prefix+i`) or run
`list` to get the saved resume command.

### CLI

From a shell inside a Herdr pane, in the plugin directory
(`herdr plugin list` shows it):

```bash
python3 goal.py list                     # every pane: goal, session, resume command
python3 goal.py resume                   # resume command for this pane
python3 goal.py set "Add Google login"   # set goal
python3 goal.py session <id>             # pin a session id
python3 goal.py session --auto           # back to automatic tracking
```

Add `--pane <id>` to target another pane.

## Supported agents

Resume commands follow Herdr's native session restore table:

| Agent | Command |
| --- | --- |
| pi | `pi --session <id>` |
| claude | `claude --resume <id>` |
| codex | `codex resume <id>` |
| opencode | `opencode --session <id>` |
| omp | `omp --resume=<id>` |
| cursor | `cursor-agent --resume <id>` |
| copilot | `copilot --resume=<id>` |
| others | grok, devin, droid, kimi, qodercli, qwen, letta, kilo, hermes, mastracode, antigravity-cli |

Unknown agents show the agent name and raw session ID without a guessed command.

## How it works

- State: `$HERDR_PLUGIN_STATE_DIR/goals-<session>.json`
  (usually `~/.local/state/herdr/plugins/pane-goal-session/`).
- Sessions come from Herdr's `agent_session` on each pane, recorded on `pane.agent_detected`
  and `pane.agent_status_changed`.
- Values are published as pane metadata tokens `goal` and `session`. Herdr does not restore
  token metadata after a restart, so a `[[startup]]` hook re-publishes them and prunes panes
  that no longer exist.
- Upgrading from the `pane-goal` plugin: saved goals are migrated automatically on first run.

## Limitations

- Goal max 80 characters (Herdr's token cap).
- Sidebar tokens appear only in **agent** rows; plain shell panes still store their data.
- Restore relies on Herdr keeping pane IDs stable across a restart.

## Uninstall

```bash
herdr plugin uninstall pane-goal-session
```

Remove the keybinding and tokens from your config. Saved data stays in the state directory.

## License

MIT
