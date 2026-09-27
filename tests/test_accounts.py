from accounts import Account, account_label, claude_aliases


def test_every_claude_alias_is_an_account_in_the_order_the_file_defines_them(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text(
        "alias ll='ls -la'\n"
        "# alias claude4='CLAUDE_CONFIG_DIR=~/.claude-4 claude'\n"
        "alias claude3=\"CLAUDE_CONFIG_DIR=$HOME/.claude-3 claude --model opus\"\n"
        "  alias claude2='CLAUDE_CONFIG_DIR=~/.claude-2 claude'  # the second login\n"
        "alias claude='claude --verbose' cl=claude\n"
        "echo \"alias claude5='claude'\"\n"
    )

    assert claude_aliases(zshrc) == [
        Account("claude3", ("claude", "--model", "opus"), {"CLAUDE_CONFIG_DIR": f"{tmp_path}/.claude-3"}),
        Account("claude2", ("claude",), {"CLAUDE_CONFIG_DIR": f"{tmp_path}/.claude-2"}),
    ]
    assert claude_aliases(zshrc)[1].config_dir == f"{tmp_path}/.claude-2"


def test_an_alias_defined_again_runs_as_its_last_definition(tmp_path):
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("alias claude2='CLAUDE_CONFIG_DIR=/a claude'\nalias claude3=claude\nalias claude2='CLAUDE_CONFIG_DIR=/b claude'\n")

    assert [(a.name, a.config_dir) for a in claude_aliases(zshrc)] == [("claude2", "/b"), ("claude3", None)]


def test_an_alias_account_is_named_the_way_people_read_it():
    assert account_label("claude2") == "Claude 2"
    assert account_label("claude12") == "Claude 12"
    assert account_label("codex") == "codex"


def test_no_zshrc_or_an_alias_that_runs_nothing_gives_no_accounts(tmp_path):
    assert claude_aliases(tmp_path / ".zshrc") == []
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("alias claude2='CLAUDE_CONFIG_DIR=/a'\nalias claude3='unbalanced\n")

    assert claude_aliases(zshrc) == []
