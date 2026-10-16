from pathlib import Path

from app.config import DEFAULTS, ENV_MAP, describe_timezone, load_config


def test_defaults_when_no_file():
    cfg = load_config(None)
    assert cfg.rdr_host == "192.168.1.168"
    assert cfg.rdr_port == 8160
    assert cfg.rdr_antenna == 1
    assert cfg.dedup_window == 10.0
    assert cfg.duplicate_window == 120.0
    assert cfg.use_device_beep is False
    assert isinstance(cfg.db_path, Path)
    assert isinstance(cfg.roster_file, Path)


def test_yaml_overrides_default(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("rdr_host: 10.0.0.9\nrdr_port: 9000\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.rdr_host == "10.0.0.9"
    assert cfg.rdr_port == 9000
    assert cfg.rdr_antenna == 1  # 未覆盖的用默认


def test_env_overrides_yaml(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("rdr_host: 10.0.0.9\n", encoding="utf-8")
    monkeypatch.setenv("RDR_HOST", "172.16.0.1")
    cfg = load_config(p)
    assert cfg.rdr_host == "172.16.0.1"


def test_type_coercion_from_env(monkeypatch):
    monkeypatch.setenv("RDR_PORT", "9100")
    monkeypatch.setenv("DEDUP_WINDOW", "3.5")
    monkeypatch.setenv("USE_DEVICE_BEEP", "true")
    cfg = load_config(None)
    assert cfg.rdr_port == 9100
    assert cfg.dedup_window == 3.5
    assert cfg.use_device_beep is True


def test_bool_coercion_false_values(monkeypatch):
    for raw in ("0", "false", "FALSE", "no", "off", ""):
        monkeypatch.setenv("USE_DEVICE_BEEP", raw)
        assert load_config(None).use_device_beep is False


def test_path_fields_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    assert load_config(None).db_path == tmp_path / "x.db"


def test_none_valued_yaml_key_does_not_wipe_default(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("rdr_host: null\n", encoding="utf-8")
    assert load_config(p).rdr_host == "192.168.1.168"


def test_env_map_covers_every_default_key():
    assert set(ENV_MAP) == set(DEFAULTS)


def test_describe_timezone_mentions_current_time():
    text = describe_timezone()
    assert "时区=" in text
    assert "当前本地时间=" in text
