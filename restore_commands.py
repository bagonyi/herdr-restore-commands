#!/usr/bin/env python3
"""Bring back chosen commands, such as `gh dash`, when a session restarts.

Herdr restores a session's layout, folders and agent conversations, but the
other panes come back as empty shells. This plugin keeps a snapshot of the
commands running in panes, limited to the ones listed in commands.txt, and
types each one back into its pane when the session starts again. Pane ids
keep their number across a restart, so each command returns to its own pane.
A listed command can come with variable assignments, such as GH_TOKEN=$(...),
which restore types in front of it: Herdr sees a command's arguments but not
its environment.

The startup hook hands over to a background worker and returns at once, so it
doesn't hold one of Herdr's plugin command slots. The worker restores the last
snapshot, then saves a new one every few seconds until its server stops.

Herdr gives a plugin one state folder for all sessions, so each session's
files are named after the session.

Usage: restore_commands.py start | work [--restore] | save | restore
"""
import fcntl
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time

STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "herdr", "plugins", "bagonyi.restore-commands",
)
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "herdr", "plugins", "config", "bagonyi.restore-commands",
)
COMMANDS_FILE = os.path.join(CONFIG_DIR, "commands.txt")
SOCKET_PATH = os.environ.get("HERDR_SOCKET_PATH", "")

SAVE_INTERVAL = 10  # seconds
# A session's server usually starts together with its window. Herdr learns
# the window's size and colours from it, and so do programs started in a pane,
# so restore gives the window a moment to attach first. Herdr's own agent
# resume waits for the window too.
WINDOW_WAIT = 2
# How long restore waits for a pane's shell to reach its prompt.
PROMPT_TIMEOUT = 30
# A typed command can fail at once, so restore looks again after this.
VERIFY_DELAY = 1.5
# When a session ends, its panes' programs can exit a moment before the server
# does. A save in that moment would see nothing running and overwrite the
# snapshot meant to survive the restart. So a command that stopped is
# forgotten only after this many saves in a row without it, which a stopping
# server doesn't live to see.
MISSES_BEFORE_FORGETTING = 2
LOG_LIMIT = 200_000  # bytes


# MARK: Herdr

class ServerGone(Exception):
    """The session's server isn't running."""


def call(method, **params):
    """Send one request to the session's server and return its result."""
    request = json.dumps({"id": "1", "method": method, "params": params}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5)
            conn.connect(SOCKET_PATH)
            conn.sendall(request.encode())
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                reply += chunk
    except (FileNotFoundError, ConnectionRefusedError) as error:
        raise ServerGone(str(error)) from error
    response = json.loads(reply)
    if "error" in response:
        raise RuntimeError(f"{method}: {response['error']}")
    return response.get("result") or {}


def notify(title, body):
    try:
        call("notification.show", title=title, body=body)
    except Exception:
        pass


def list_panes():
    return {pane["pane_id"]: pane for pane in call("pane.list").get("panes", [])}


def process_info(pane_id):
    return call("pane.process_info", pane_id=pane_id).get("process_info") or {}


def at_prompt(info):
    """Whether the pane's shell is in front, waiting for a command."""
    shell = info.get("shell_pid")
    return bool(shell) and info.get("foreground_process_group_id") == shell


def typed_process(info):
    """The command typed at the pane's prompt, as Herdr describes the process.

    That is the foreground group's leader. The other processes in the group
    are its children: `gh dash` runs the gh-dash extension as a child.
    """
    group = info.get("foreground_process_group_id")
    if not group or at_prompt(info):
        return None
    for process in info.get("foreground_processes") or []:
        if process.get("pid") == group and process.get("argv"):
            return process
    return None


# MARK: Commands

ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def load_listed():
    """The commands to bring back, each as (words, assignments).

    A line holds a command's first words, optionally after variable
    assignments written as in a shell: `GH_TOKEN=$(gh auth token) gh dash`.
    The assignments stay shell text, so `$(...)` runs again on each restore
    and no token ends up in the snapshot.
    """
    try:
        with open(COMMANDS_FILE) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    listed = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            assignments, rest = split_assignments(line)
            words = shlex.split(rest)
            if words:
                listed.append((words, assignments))
    return listed


def split_assignments(line):
    """A line's leading variable assignments, as shell text, and the rest."""
    assignments = []
    rest = line
    while ASSIGNMENT.match(rest):
        end = word_end(rest)
        assignments.append(rest[:end])
        rest = rest[end:].lstrip()
    return " ".join(assignments), rest


def word_end(text):
    """Where the shell word text starts with ends: at its first blank outside
    quotes, $(...), ${...} and backticks."""
    closers = []  # What closes each open quote or group, innermost last.
    i = 0
    while i < len(text):
        c = text[i]
        closer = closers[-1] if closers else None
        if closer == "'":
            if c == "'":
                closers.pop()
        elif c == "\\":
            i += 1  # Skip the escaped character.
        elif c == closer:
            closers.pop()
        elif c == "$" and text[i + 1:i + 2] in ("(", "{"):
            closers.append(")" if text[i + 1] == "(" else "}")
            i += 1
        elif c in "\"`" or (c == "'" and closer != '"'):
            closers.append(c)
        elif c == "(" and closer == ")":
            closers.append(")")
        elif c.isspace() and not closers:
            break
        i += 1
    return min(i, len(text))


def listed_match(argv, listed):
    """The listed (words, assignments) that argv starts with, or None. Of
    several, the one with the most words."""
    words = [os.path.basename(argv[0]), *argv[1:]]
    matches = [entry for entry in listed if words[:len(entry[0])] == entry[0]]
    return max(matches, key=lambda entry: len(entry[0]), default=None)


def running_commands(listed):
    """Pane id -> the listed command running in it, and the folder it runs in."""
    commands = {}
    if not listed:
        return commands
    for pane_id, pane in list_panes().items():
        if pane.get("agent"):
            continue  # Herdr resumes agents itself.
        process = typed_process(process_info(pane_id))
        if process and listed_match(process["argv"], listed):
            commands[pane_id] = {
                "argv": process["argv"],
                "cwd": process.get("cwd") or pane.get("foreground_cwd") or pane.get("cwd"),
            }
    return commands


def describe(command):
    return " ".join(command["argv"])


def shell_quote(word):
    """Quote a word for a POSIX shell the way Herdr quotes its resume commands."""
    if not word:
        return "''"
    if all(c.isascii() and (c.isalnum() or c in "_-./:@%+=") for c in word):
        return word
    return "'" + word.replace("'", "'\\''") + "'"


def shell_text(command, pane, assignments=""):
    """What to type: the command exactly as it ran, in the folder it ran in,
    after the variable assignments listed with it.

    `command` skips aliases and shell functions. The saved argv is what ran
    after those were expanded, so expanding them again would change it: an
    alias `lnav='lnav -q'` would turn `lnav -q` into `lnav -q -q`.
    """
    text = "command " + " ".join(shell_quote(word) for word in command["argv"])
    if assignments:
        text = f"{assignments} {text}"
    cwd = command.get("cwd")
    if cwd and cwd != (pane.get("foreground_cwd") or pane.get("cwd")):
        text = f"cd {shell_quote(cwd)} && {text}"
    return text


# MARK: Snapshot

def session_name():
    """The session this server runs: its socket is sessions/<name>/herdr.sock."""
    folder = os.path.dirname(os.path.realpath(SOCKET_PATH))
    if os.path.basename(os.path.dirname(folder)) == "sessions":
        return os.path.basename(folder)
    return "default"


def session_file(suffix):
    folder = os.path.join(STATE_DIR, "sessions")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, session_name() + suffix)


def load_snapshot():
    try:
        with open(session_file(".json")) as f:
            return json.load(f).get("panes", {})
    except (OSError, ValueError):
        return {}


def store_snapshot(commands):
    path = session_file(".json")
    with open(path + ".tmp", "w") as f:
        json.dump({"panes": commands}, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(path + ".tmp", path)


def apply_changes(saved, running, misses):
    """The snapshot after a save: running commands replace saved ones at once,
    but a saved command that stopped stays until it has been missing for
    MISSES_BEFORE_FORGETTING saves in a row. `misses` counts them."""
    updated = dict(running)
    for pane_id, command in saved.items():
        if pane_id in running:
            misses.pop(pane_id, None)
            continue
        misses[pane_id] = misses.get(pane_id, 0) + 1
        if misses[pane_id] < MISSES_BEFORE_FORGETTING:
            updated[pane_id] = command
        else:
            del misses[pane_id]
    return updated


def log(message):
    with open(session_file(".log"), "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")


# MARK: Restore

def restore(saved, wait):
    """Type each saved command into its pane once the pane's shell is at its
    prompt. A pane that is busy or gone is left alone; with `wait`, restore
    keeps checking for PROMPT_TIMEOUT seconds first.

    Returns (restored, problems), one line per command.
    """
    listed = load_listed()
    pending = dict(saved)
    typed = {}
    problems = []
    deadline = time.monotonic() + (PROMPT_TIMEOUT if wait else 0)
    while True:
        panes = list_panes()
        for pane_id, command in list(pending.items()):
            pane = panes.get(pane_id)
            if pane is None or pane.get("agent"):
                continue
            try:
                info = process_info(pane_id)
            except RuntimeError:
                continue  # The pane closed a moment ago.
            process = typed_process(info)
            if process and process["argv"] == command["argv"]:
                del pending[pane_id]  # Already running.
            elif at_prompt(info):
                match = listed_match(command["argv"], listed)
                call(
                    "pane.send_input",
                    pane_id=pane_id,
                    text=shell_text(command, pane, match[1] if match else ""),
                    keys=["Enter"],
                )
                typed[pane_id] = command
                del pending[pane_id]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    for pane_id, command in pending.items():
        problems.append(f"{describe(command)} in {pane_id}: the pane is busy or gone")
    restored = []
    if typed:
        time.sleep(VERIFY_DELAY)
        for pane_id, command in typed.items():
            try:
                running = typed_process(process_info(pane_id))
            except RuntimeError:
                running = None
            if running:
                restored.append(f"{describe(command)} in {pane_id}")
            else:
                problems.append(f"{describe(command)} in {pane_id}: exited right away")
    return restored, problems


# MARK: Worker

def worker_file():
    return session_file(".worker")


def spawn_worker(*args):
    """Start the worker in its own session, so it outlives this command."""
    with open(session_file(".log"), "a") as log_file:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "work", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,
            close_fds=True,
        )


def trim_log():
    path = session_file(".log")
    try:
        if os.path.getsize(path) > LOG_LIMIT:
            os.replace(path, path + ".old")
    except OSError:
        pass


def work(restore_first):
    """Restore the snapshot if asked, then save every SAVE_INTERVAL seconds.

    The newest worker wins: it names itself in the .worker file, and an older
    one, left from a server that has since restarted, sees that and exits
    within a second. The lock keeps them from overlapping until then.
    """
    me = str(os.getpid())
    with open(worker_file() + ".tmp", "w") as f:
        f.write(me)
    os.replace(worker_file() + ".tmp", worker_file())
    lock = open(session_file(".lock"), "a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    trim_log()
    log(f"worker {me} started")

    if restore_first:
        time.sleep(WINDOW_WAIT)
        saved = load_snapshot()
        if saved:
            try:
                restored, problems = restore(saved, wait=True)
            except Exception as error:
                restored, problems = [], [f"restore failed: {error}"]
            for line in restored:
                log("restored " + line)
            for line in problems:
                log("not restored: " + line)
            if problems:
                notify("Some commands weren't restored", "\n".join(problems))

    def still_mine():
        try:
            with open(worker_file()) as f:
                return f.read().strip() == me
        except OSError:
            return False

    misses = {}
    next_save = time.monotonic()
    while True:
        if not still_mine():
            log(f"worker {me} replaced by a newer one")
            return
        if not os.path.exists(SOCKET_PATH):
            log(f"worker {me} stops: the server is gone")
            return
        if time.monotonic() >= next_save:
            next_save = time.monotonic() + SAVE_INTERVAL
            try:
                running = running_commands(load_listed())
            except ServerGone:
                log(f"worker {me} stops: the server is gone")
                return
            except Exception as error:
                log(f"save skipped: {error}")
                running = None
            if running is not None and still_mine():
                saved = load_snapshot()
                updated = apply_changes(saved, running, misses)
                if updated != saved:
                    store_snapshot(updated)
        time.sleep(1)


# MARK: Actions

def save_now():
    listed = load_listed()
    if not listed:
        notify("Nothing to save", f"No commands are listed in {COMMANDS_FILE}")
        return 1
    commands = running_commands(listed)
    store_snapshot(commands)
    # The new worker takes over from a running one, so after an update the
    # worker runs the current code without a restart.
    spawn_worker()
    body = "\n".join(describe(c) for c in commands.values()) or "None of the listed commands is running"
    notify(f"Saved {len(commands)} command(s)", body)
    return 0


def restore_now():
    restored, problems = restore(load_snapshot(), wait=False)
    lines = [f"Restored {line}" for line in restored] + [f"Not restored: {line}" for line in problems]
    notify("Restore commands", "\n".join(lines) or "Nothing saved to restore")
    return 0


def main():
    if not SOCKET_PATH:
        print("HERDR_SOCKET_PATH isn't set; run this from Herdr.", file=sys.stderr)
        return 2
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "start":
        spawn_worker("--restore")
        return 0
    if command == "work":
        work(restore_first="--restore" in sys.argv[2:])
        return 0
    if command == "save":
        return save_now()
    if command == "restore":
        return restore_now()
    print(__doc__.strip().splitlines()[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
