"""The other Claude Code accounts on this Mac, as the zsh aliases that sign in to them.

Claude Code signed in to a second account is Claude Code run against a config
dir of its own, and ~/.zshrc gives each one an alias:

    alias claude2='CLAUDE_CONFIG_DIR=~/.claude-2 claude'

Every alias named claude and a number is one, in the order the file defines
them, and passes fall back through them between Claude and Codex (see
usage.py). The file is read as it stands each time it is asked, so an alias
added to it is used without starting anything again.

An alias is run the way zsh runs it: the assignments it opens with go into the
environment, the rest is the command, and ~ and $VARS are expanded.
"""

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

ZSHRC = Path.home() / ".zshrc"
ALIAS_NAME = re.compile(r"claude(\d+)")
ALIAS_LINE = re.compile(r"\s*alias\s")
ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)=(.*)", re.S)


@dataclass(frozen=True)
class Account:
    # The alias, which is also the engine a pass on the account records.
    name: str
    # What the alias runs, before a step's own arguments.
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)

    @property
    def config_dir(self) -> str | None:
        return self.env.get("CLAUDE_CONFIG_DIR")


def claude_aliases(zshrc: Path = ZSHRC) -> list[Account]:
    """Each claude<N> alias the file defines, in the order it first defines
    them. One defined twice runs as its last definition, as in zsh."""
    try:
        text = zshrc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: dict[str, Account] = {}
    for line in text.splitlines():
        if not ALIAS_LINE.match(line):
            continue
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        for word in words[1:]:
            name, _, value = word.partition("=")
            if ALIAS_NAME.fullmatch(name) and (account := alias_account(name, value)):
                found[name] = account
    return list(found.values())


def account_label(name: str) -> str:
    """How an alias account is named where people read it: claude2 is "Claude 2"."""
    found = ALIAS_NAME.fullmatch(name)
    return f"Claude {found[1]}" if found else name


def alias_account(name: str, value: str) -> Account | None:
    """The account an alias's text runs, or None when it runs no command."""
    try:
        words = shlex.split(value)
    except ValueError:
        return None
    env = {}
    while words and (assignment := ASSIGNMENT.fullmatch(words[0])):
        env[assignment[1]] = expand(assignment[2])
        words.pop(0)
    return Account(name, tuple(expand(word) for word in words), env) if words else None


def expand(word: str) -> str:
    return os.path.expanduser(os.path.expandvars(word))
