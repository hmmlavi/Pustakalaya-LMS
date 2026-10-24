from pathlib import Path

from app.config import AppConfig


def test_runtime_data_dir_keeps_database_and_logs_outside_install_tree(tmp_path: Path):
    data_dir = tmp_path / "PustakalayaData"
    config = AppConfig(data_dir=data_dir, secret_key="test-secret", testing=True)

    assert config.DATA_DIR == data_dir.resolve()
    assert config.DATABASE_URL == f"sqlite:///{(data_dir / 'pustakalaya.db').resolve().as_posix()}"
    assert config.HISTORICAL_SOURCE_PATH == data_dir / "historical_source_records.json"


def test_source_style_instance_url_maps_to_runtime_data_dir(tmp_path: Path):
    data_dir = tmp_path / "PustakalayaData"
    config = AppConfig(
        data_dir=data_dir,
        database_url="sqlite:///instance/pustakalaya.db",
        secret_key="test-secret",
        testing=True,
    )

    assert config.DATABASE_URL == f"sqlite:///{(data_dir / 'pustakalaya.db').resolve().as_posix()}"
