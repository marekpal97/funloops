"""The policy directory's two other seams (thinkweave#346, funloops half).

- **loop.toml resolution** — ``cli.find_config``: ``<policy_dir>/loop.toml``
  first, then the cwd-upward ``docs/agents/loop.toml`` walk, then the packaged
  copy; ``cli.main`` reads the directory from ``--policy-dir``, else
  ``$DEVLOOP_POLICY_DIR``.
- **the run manifest** — ``trajectory --manifest-json`` lands the host's
  manifest on the payload's top-level ``manifest`` key, uninterpreted, for
  the host's run recorder; without it the payload has no such key.

The pack texts' loader is pinned in test_pack.py.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from devloop import cli, github, pack
from devloop.trajectory import mint


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


MANIFEST = {"harness": "claude-code", "readers": {"devloop": "funloops@abc"},
            "rsi_rail": {"commit": "a19d447"}}


def _payload(**kw) -> dict:
    return mint.build_trajectory(
        {"number": 3, "title": "x", "labels": []}, branch="b", commits=[], numstat="",
        gates=[], fix_rounds=0, outcome="shipped", **kw)


def test_manifest_passes_through_uninterpreted():
    assert _payload(manifest=MANIFEST)["manifest"] == MANIFEST
    assert _payload(manifest={"anything": [1]})["manifest"] == {"anything": [1]}
    assert "manifest" not in _payload()
    assert "manifest" not in _payload()["frontmatter"]


def test_non_object_manifest_is_refused():
    with pytest.raises(ValueError, match="manifest must be a JSON object"):
        _payload(manifest=["not", "an", "object"])


def test_trajectory_verb_lands_the_manifest_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(github, "run", lambda args, cwd=None: (
        "null" if args[-1].endswith("/parent") else json.dumps(
            {"number": 88, "title": "t", "labels": [], "html_url": "u"})))
    monkeypatch.setattr(github, "fetch_parent", lambda repo, n: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout="", stderr=""))
    (tmp_path / "g.json").write_text("[]", encoding="utf-8")
    (tmp_path / "m.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    argv = ["trajectory", "88", "--cwd", str(tmp_path), "--branch", "b",
            "--gates-json", str(tmp_path / "g.json"), "--outcome", "shipped"]
    assert cli.main([*argv, "--manifest-json", str(tmp_path / "m.json")]) == 0
    assert json.loads(capsys.readouterr().out)["manifest"] == MANIFEST
    assert cli.main(argv) == 0
    assert "manifest" not in json.loads(capsys.readouterr().out)
