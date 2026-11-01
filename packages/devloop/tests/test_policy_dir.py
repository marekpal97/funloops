"""The policy directory's two other seams (thinkweave#346, funloops half).

- **loop.toml layering** — ``cli.find_config`` is the repo's file (the
  cwd-upward ``docs/agents/loop.toml`` walk, then the packaged copy) and
  ``cli.find_policy_config`` is ``<policy_dir>/loop.toml``; ``load_config``
  layers the second on top. The policy file carries only ``POLICY_KEYS``
  (thinkweave's dec-6c3539b2): a repo key there is refused by name, so the
  repo's gates hold with or without a policy directory. ``cli.main`` reads
  the directory from ``--policy-dir``, else ``$DEVLOOP_POLICY_DIR``.
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


def _write(directory, text: str):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "loop.toml").write_text(text, encoding="utf-8")
    return directory / "loop.toml"


REPO_TOML = '[loop]\nmax_fix_rounds = 5\n\n[[gates]]\nid = "tests"\nkind = "command"\ncmd = "repo"\n'
POLICY_TOML = "[loop]\nmax_fix_rounds = 1\n\n[prime]\nlimit = 1\n"


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host repo whose loop.toml carries a gate and a fix-round budget,
    with the cwd inside it."""
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    _write(root / "docs" / "agents", REPO_TOML)
    monkeypatch.chdir(root)
    monkeypatch.delenv(pack.POLICY_ENV, raising=False)
    return root


def test_find_config_is_the_repo_file_and_the_policy_file_is_separate(host, tmp_path):
    walked = host / "docs" / "agents" / "loop.toml"
    configured = _write(tmp_path / "policy", POLICY_TOML)
    assert cli.find_config(host) == walked
    assert cli.find_policy_config(tmp_path / "policy") == configured
    assert cli.find_policy_config(tmp_path / "elsewhere") is None
    assert cli.find_policy_config(None) is None


def test_policy_dir_without_loop_toml_layers_nothing(tmp_path):
    (tmp_path / "policy").mkdir()
    assert cli.find_policy_config(tmp_path / "policy") is None
    assert cli.find_config(tmp_path) == cli.PACKAGE_CONFIG


def test_policy_file_layers_over_the_repo_file_and_keeps_its_gates(host, tmp_path):
    cfg = cli.load_config(cli.find_config(), _write(tmp_path / "policy", POLICY_TOML))
    assert cfg["loop"]["max_fix_rounds"] == 1
    assert cfg["prime"] == {"enabled": True, "limit": 1, "budget_chars": 1200}
    assert [g["cmd"] for g in cfg["gates"]] == ["repo"]


def _config(capsys, argv) -> dict:
    assert cli.main(["config", *argv]) == 0
    return json.loads(capsys.readouterr().out)


def test_config_verb_reads_the_env_var_and_the_flag_wins(host, tmp_path, monkeypatch, capsys):
    _write(tmp_path / "env", "[prime]\nlimit = 1\n")
    _write(tmp_path / "flag", "[prime]\nlimit = 2\n")
    assert _config(capsys, [])["prime"]["limit"] == 3
    monkeypatch.setenv(pack.POLICY_ENV, str(tmp_path / "env"))
    assert _config(capsys, [])["prime"]["limit"] == 1
    assert _config(capsys, ["--policy-dir", str(tmp_path / "flag")])["prime"]["limit"] == 2


def test_set_wins_over_the_policy_file(host, tmp_path, capsys):
    _write(tmp_path / "policy", POLICY_TOML)
    cfg = _config(capsys, ["--policy-dir", str(tmp_path / "policy"), "--set", "prime.limit=4"])
    assert cfg["prime"]["limit"] == 4 and cfg["loop"]["max_fix_rounds"] == 1


@pytest.mark.parametrize(("text", "name"), [
    ('[[gates]]\nid = "x"\nkind = "judge"\n', "repo-owned section 'gates'"),
    ('[triage]\nsensitive_paths = ["a/"]\n', "repo-owned section 'triage'"),
    ('[labels]\nrunnable = "go"\n', "repo-owned section 'labels'"),
    ('[loop]\nbranch_prefix = "x/"\n', "repo-owned key 'loop.branch_prefix'"),
    ('[loop]\nrequire_green_baseline = false\n', "repo-owned key 'loop.require_green_baseline'"),
    ('[dispatch.judge]\nposture = "reader"\n', "repo-owned key 'dispatch.judge.posture'"),
    ("[prime]\nfoo = 1\n", "unknown key 'prime.foo'"),
    ("[bogus]\nx = 1\n", "unknown section 'bogus'"),
])
def test_policy_file_refuses_a_repo_or_unknown_key_by_name(host, tmp_path, capsys, text, name):
    _write(tmp_path / "policy", text)
    assert cli.main(["config", "--policy-dir", str(tmp_path / "policy")]) == 2
    assert name in json.loads(capsys.readouterr().out)["error"]


def test_policy_file_may_carry_every_policy_key(host, tmp_path, capsys):
    """Each POLICY_KEYS entry at its default loads cleanly from the policy
    file: the allowlist and the defaults cannot disagree."""
    lines = []
    for section, keys in cli.POLICY_KEYS.items():
        if section == "dispatch":
            lines += ["[dispatch.judge]", 'transport = "herdr"', 'harness = "codex"',
                      'model = "m"', 'effort = "high"', 'args = ["-a"]']
            continue
        lines.append(f"[{section}]")
        for key in keys:
            value = cli.DEFAULT_CONFIG[section][key]
            lines.append(f"{key} = {json.dumps(value)}")
    _write(tmp_path / "policy", "\n".join(lines) + "\n")
    cfg = _config(capsys, ["--policy-dir", str(tmp_path / "policy")])
    assert cfg["dispatch"]["judge"]["transport"] == "herdr"
    assert {g["cmd"] for g in cfg["gates"]} == {"repo"}


def test_unknown_prime_key_is_refused_by_name_on_both_paths(tmp_path, capsys):
    _write(tmp_path / "repo", "[prime]\nfoo = 1\n")
    with pytest.raises(ValueError, match="unknown key 'prime.foo'"):
        cli.load_config(tmp_path / "repo" / "loop.toml")
    assert cli.main(["config", "--set", "prime.foo=1"]) == 2
    assert "unknown key 'prime.foo'" in json.loads(capsys.readouterr().out)["error"]


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
