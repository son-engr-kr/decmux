"""Self-update version logic (network kept out; only the pure parts are exercised)."""

from __future__ import annotations

from decmux import update


def test_vtuple_parses_and_orders():
    assert update._vtuple("0.2.6") == (0, 2, 6)
    assert update._vtuple("v1.10.2") == (1, 10, 2)
    assert update._vtuple("0.2.6") < update._vtuple("0.2.7")
    assert update._vtuple("0.2.10") > update._vtuple("0.2.9")   # numeric, not lexical


def test_parse_version():
    assert update._parse_version('__version__ = "0.3.1"\n') == "0.3.1"
    assert update._parse_version("no version here") is None


def test_update_available(monkeypatch):
    monkeypatch.setattr(update, "current_version", lambda: "0.2.6")
    monkeypatch.setattr(update, "latest_version", lambda timeout=3.0: "0.2.7")
    assert update.update_available() == ("0.2.6", "0.2.7")
    monkeypatch.setattr(update, "latest_version", lambda timeout=3.0: "0.2.6")
    assert update.update_available() is None             # equal -> no update
    monkeypatch.setattr(update, "latest_version", lambda timeout=3.0: None)
    assert update.update_available() is None             # unreachable -> no update
