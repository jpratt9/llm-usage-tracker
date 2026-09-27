# LLM usage tracker

LLM usage in the macOS menu bar. The icon is a pie of the session window of the CLI the next pass opens on,
filled clockwise from twelve with the share used. Clicking it lists every CLI — Claude, each alias account,
Codex, then Gemini — the one passes open on ticked, each with a pie of its own and its session and weekly
windows as the share used and when they reset. With every CLI spent the pie is full. A faint ring means that
CLI's reading failed.

## Where the numbers come from

It reads them itself, each CLI at most once a minute:

- **Claude** (`usage.py`): the numbers `/usage` shows, asked of the endpoint the `/usage` dialog calls with
  the OAuth token Claude Code keeps in the macOS Keychain. The token is read for the request and never kept,
  printed or logged.
- **Each alias account** (`accounts.py`): every `claude<N>` alias in `~/.zshrc` that signs in with a config
  dir of its own, read the same way off its own Keychain login.

  ```
  alias claude2='CLAUDE_CONFIG_DIR=~/.claude-2 claude'
  ```

- **Codex** (`codex_usage.py`): off the rollout files Codex writes its turns to under `~/.codex-5/sessions/`.
  Only an interactive `codex` records its limits there; `codex exec` leaves them blank.
- **Gemini** (`gemini_usage.py`): `agy -p /usage`, which answers without running a model turn.

"The CLI the next pass opens on" is the first in that order not spent past 95% of its session or weekly
window. A login whose weekly window is spent rotates on to the next login before Codex and Gemini.

The usage endpoint rate limits. A reading that fails is not asked for again for five minutes, or for as long
as the refusal's `Retry-After` says, and the last good windows keep standing in the meantime, with the reason
they aren't refreshing under them. A login's Keychain token also expires while no Claude Code runs on it; the
next Claude Code session on that login renews it.

Everything read is kept in `~/Library/Caches/llm-usage-tracker/usage.json`. Any other process that reads
usage through these modules with the same file (clear-backlog's loop does) shares the readings, so between them each CLI is still read once a minute at most, and the last
good windows outlive a restart.

## Run it

It needs PyObjC, which is in `requirements.txt`.

```
python3 -m pip install -r requirements.txt
python3 menubar.py
```

It runs as the launch agent `com.john.llm-usage-tracker`
(`~/Library/LaunchAgents/com.john.llm-usage-tracker.plist`), through a login zsh so it has the terminal's
`PATH` for `agy`. It starts at login, launchd starts it again if it ever exits, and it logs to `menubar.log`.

```
launchctl kickstart -k gui/$(id -u)/com.john.llm-usage-tracker                              # restart after changing it
launchctl bootout gui/$(id -u)/com.john.llm-usage-tracker                                   # take it out of the menu bar until next login
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.john.llm-usage-tracker.plist    # put it back
```

## Tests

```
python3 -m pytest
```
