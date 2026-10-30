"""The policy directory's loop.toml seam (thinkweave#346, funloops half).

- **loop.toml resolution** — ``cli.find_config``: ``<policy_dir>/loop.toml``
  first, then the cwd-upward ``docs/agents/loop.toml`` walk, then the packaged
  copy; ``cli.main`` reads the directory from ``--policy-dir``, else
  ``$DEVLOOP_POLICY_DIR``.

The pack texts' loader is pinned in test_pack.py.
"""

from __future__ import annotations

import json

from devloop import cli, pack


def _toml(directory, marker: str):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "loop.toml").write_text(
        f'[[gates]]\nid = "tests"\nkind = "command"\ncmd = "{marker}"\n', encoding="utf-8")
    return directory / "loop.toml"


def test_policy_loop_toml_wins_over_the_walk(tmp_path):
    host = tmp_path / "host"
    (host / ".git").mkdir(parents=True)
    walked = _toml(host / "docs" / "agents", "walked")
    configured = _toml(tmp_path / "policy", "configured")
    assert cli.find_config(host, tmp_path / "policy") == configured
    assert cli.find_config(host, tmp_path / "elsewhere") == walked
    assert cli.find_config(host) == walked


def test_policy_dir_without_loop_toml_falls_back_to_the_packaged_copy(tmp_path):
    (tmp_path / "policy").mkdir()
    assert cli.find_config(tmp_path, tmp_path / "policy") == cli.PACKAGE_CONFIG


def _gate_cmd(capsys, argv) -> str:
    assert cli.main(["config", *argv]) == 0
    return json.loads(capsys.readouterr().out)["gates"][0]["cmd"]


def test_config_verb_reads_the_env_var_and_the_flag_wins(tmp_path, monkeypatch, capsys):
    _toml(tmp_path / "env", "from-env")
    _toml(tmp_path / "flag", "from-flag")
    monkeypatch.setenv(pack.POLICY_ENV, str(tmp_path / "env"))
    assert _gate_cmd(capsys, []) == "from-env"
    assert _gate_cmd(capsys, ["--policy-dir", str(tmp_path / "flag")]) == "from-flag"


def test_unconfigured_config_verb_is_unchanged(capsys):
    """No folder, no env var: the config is exactly the walk's, as before."""
    assert cli.main(["config"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    expected = cli.load_config(cli.find_config())
    expected["constitution"] = [str(p) for p in cli.find_constitution()]
    assert cfg == expected
