# LLM usage tracker

LLM usage in the macOS menu bar. The icon is a pie of the session window of the CLI the next pass opens on,
filled clockwise from twelve with the share used. Clicking it lists every CLI — Claude, each alias account,
Codex, then Gemini — the one passes open on ticked, each with a pie of its own and its session and weekly
windows as the share used and when they reset. With every CLI spent the pie is full. A faint ring means
nothing is answering, or that CLI's reading failed.

## Where the numbers come from

It reads no usage of its own. Every 30 seconds it asks `http://127.0.0.1:5078/api/usage`, the endpoint
clear_backlog.py's page reads, so the two always match. The answer names the CLI the next pass opens on
(`null` while every CLI is spent) and each CLI's windows, `used` being the percent of the window used:

```json
{
  "engine": "claude2",
  "claude": {"ok": true, "error": null, "checked_at": 1790000000,
             "windows": [{"key": "five_hour", "label": "Session", "used": 8.4, "resets_at": 1790007200},
                         {"key": "seven_day", "label": "Weekly", "used": 34.0, "resets_at": 1790180000}]},
  "accounts": [{"name": "claude2", "ok": true, "error": null, "checked_at": 1790000000, "windows": []}],
  "codex": {"ok": true, "error": null, "checked_at": 1790000000, "windows": []},
  "gemini": {"ok": false, "error": "agy isn't signed in", "checked_at": null, "windows": []}
}
```

A CLI whose reading failed says why in `error`. One with `ok` still true and an `error` is showing the
last good windows, read at `checked_at`.

## Run it

It needs PyObjC, which is in `requirements.txt`.

```
python3 -m pip install -r requirements.txt
python3 menubar.py
```

It runs as the launch agent `com.john.llm-usage-tracker`
(`~/Library/LaunchAgents/com.john.llm-usage-tracker.plist`), apart from clear_backlog.py, so restarting
either leaves the other alone. It logs to `menubar.log`.

```
launchctl kickstart -k gui/$(id -u)/com.john.llm-usage-tracker                              # restart after changing menubar.py
launchctl bootout gui/$(id -u)/com.john.llm-usage-tracker                                   # take it out of the menu bar until next login
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.john.llm-usage-tracker.plist    # put it back
```

## Tests

```
python3 -m pytest
```
