from mailassistant.config import Settings


def test_addresses_split_and_lowercased(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("MY_ADDRESSES", "Mail@Example.nl, info@example.nl ,")
    s = Settings(_env_file=None)
    assert s.my_addresses == ["mail@example.nl", "info@example.nl"]
    assert s.timezone == "Europe/Amsterdam"
    assert "p@h" not in repr(s)  # secret never printed
