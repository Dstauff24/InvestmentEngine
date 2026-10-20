"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from core import netguard


@pytest.fixture
def stubbed_transport(monkeypatch):
    """Bypass the D27 consent gate for tests that inject a fake HTTP session.

    Only safe where the session is a stub: those tests exercise URL
    construction and error classification, and nothing reaches a socket.
    Requesting this fixture is the test declaring that.

    `tests/test_netguard.py` deliberately does NOT use it — that module asserts
    the gate fires, including that it fires before the session is touched.
    """
    monkeypatch.setattr(netguard, "require_live_calls", lambda *a, **k: None)
