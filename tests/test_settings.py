"""Env-alias contract of ``Settings``: aliased fields read their ``HABR_MCP_*``
env vars with correct types, and the alias beats a bare un-prefixed name.
Hermetic: ``Settings(_env_file=None)`` and unset HABR_MCP_* vars are removed."""

from src.settings import Settings

# All env aliases the Settings model honours; cleared per test to stay hermetic.
_ALIAS_VARS = (
    "HABR_MCP_HOST",
    "HABR_MCP_PORT",
    "HABR_MCP_STATE_DIR",
)


def _clear_aliases(monkeypatch):
    # Drop any inherited HABR_MCP_* so a polluted environment cannot leak in.
    for name in _ALIAS_VARS:
        monkeypatch.delenv(name, raising=False)


def test_aliases_populate_fields_with_correct_types(monkeypatch):
    _clear_aliases(monkeypatch)
    monkeypatch.setenv("HABR_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("HABR_MCP_PORT", "9999")
    monkeypatch.setenv("HABR_MCP_STATE_DIR", "/tmp/somewhere")

    s = Settings(_env_file=None)

    assert s.host == "0.0.0.0"
    # port must be a real int, not the raw "9999" string from the env.
    assert s.port == 9999
    assert isinstance(s.port, int)
    assert s.state_dir == "/tmp/somewhere"


def test_alias_takes_priority_over_bare_name(monkeypatch):
    # NOTE: with populate_by_name=True, pydantic-settings 2.x ALSO reads a bare
    # field-name env var (HOST/PORT), so a bare name is not strictly ignored.
    # The deploy contract that actually matters is that the HABR_MCP_* alias is
    # honoured AND wins over any bare name — i.e. the documented deploy var is
    # what configures the bind. A silent break (alias stops being read, bare
    # name takes over) would flip these assertions.
    _clear_aliases(monkeypatch)
    monkeypatch.setenv("HABR_MCP_HOST", "10.0.0.1")
    monkeypatch.setenv("HABR_MCP_PORT", "5555")
    # Bare names set too; the prefixed alias must take priority over them.
    monkeypatch.setenv("HOST", "1.2.3.4")
    monkeypatch.setenv("PORT", "1")

    s = Settings(_env_file=None)

    assert s.host == "10.0.0.1"
    assert s.port == 5555
