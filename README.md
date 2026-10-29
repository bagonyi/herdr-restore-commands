# Restore commands for Herdr

When a session restarts, Herdr brings back its layout and resumes agents, but
every other pane comes back as an empty shell. This plugin brings back the
commands you list, such as `gh dash` and `lnav`, in the panes they ran in.

## How it works

- While a session runs, a background worker records every 10 seconds which
  panes run a listed command, with its arguments and folder. Each session has
  its own record.
- When the session starts again, the worker waits about 2 seconds for the
  window to attach. Then it types each command into its pane as soon as the
  pane's shell is at its prompt, for example `command lnav -q /var/log/x.log`.
  `command` skips aliases, because the recorded command already has them
  expanded.
- Agent panes, and panes running anything else, are never touched. Herdr
  resumes agents itself.
- Quitting a command removes it from the record within about 20 seconds.
  Commands closed by a shutdown or a crash are kept.

## Commands to restore

List them in `commands.txt` in the plugin's config folder
(`~/.config/herdr/plugins/config/bagonyi.restore-commands/`, also printed by
`herdr plugin config-dir bagonyi.restore-commands`), one per line. A pane
matches when its command starts with the listed words, so `lnav` also matches
`lnav -q /var/log/system.log`:

```
gh dash
lnav
```

Herdr sees a command's arguments but not the variables it was started with.
If a command needs one, start its line with the assignment, written as you'd
type it. Restore types it in front of the command:

```
GH_TOKEN=$(gh auth token --user work-account) gh dash
```

The assignment is kept as you wrote it, so `$(...)` runs again each time the
command comes back, and the token itself isn't saved anywhere.

Changes apply from the next save, without a restart.

## Requirements

- Herdr 0.9.3 or later
- macOS or Linux
- `python3` on `PATH` (standard library only)

Tested on macOS.

## Install

```sh
herdr plugin install bagonyi/herdr-restore-commands
```

Herdr has no plugin update command yet; run the same command again to update.
To work on the plugin, clone this repo and link your checkout instead:
`herdr plugin link /path/to/herdr-restore-commands`.

The worker starts when a session starts. In a session that was already
running, run the Save action once to start it, or that session's commands
come back only from its second restart. Run it after an update too, so the
worker runs the new version:

```sh
herdr plugin action invoke save --plugin bagonyi.restore-commands
```

## Actions

- `save` records the listed commands now and starts a new worker, which takes
  over from the running one.
- `restore` types the saved commands into their panes now, skipping panes
  that are busy.

Both report back in a Herdr notification.

## Files

In `~/.local/state/herdr/plugins/bagonyi.restore-commands/sessions/`, per
session: `<session>.json` (the record) and `<session>.log` (what was
restored or skipped, and worker starts and stops).

## Uninstall

```sh
herdr plugin uninstall bagonyi.restore-commands
```

If you linked a checkout, use `herdr plugin unlink bagonyi.restore-commands`
instead. The command list and the saved records stay in the folders above.

## License

Copyright 2026 David Bagonyi. Licensed under the [Apache License 2.0](LICENSE).
