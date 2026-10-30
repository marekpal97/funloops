"""Suite-wide isolation: a developer's ``DEVLOOP_POLICY_DIR`` never reaches a
test. Every test starts unconfigured; a test that wants a policy directory
sets the env var or passes ``--policy-dir`` itself."""

from __future__ import annotations

import pytest

from devloop import pack


@pytest.fixture(autouse=True)
def _no_policy_dir(monkeypatch):
    monkeypatch.delenv(pack.POLICY_ENV, raising=False)
