"""补充核验必须拒绝改动、缺失或新添的迁移，不能靠旧清单漏报。"""

import pytest
from repair_benchmark_migration_provenance import digest, verify_files


@pytest.mark.parametrize("damage", ["changed", "missing", "extra"])
def test_pre_run_migration_manifest_rejects_changed_or_missing_or_extra_files(tmp_path, damage):
    (tmp_path / "migrations").mkdir()
    migration = tmp_path / "migrations/0001.py"
    migration.write_bytes(b"revision = '0001'\n")
    (tmp_path / "alembic.ini").write_bytes(b"script_location = migrations\n")
    expected = {
        name: digest((tmp_path / name).read_bytes())
        for name in ("alembic.ini", "migrations/0001.py")
    }
    verify_files(tmp_path, expected)
    if damage == "changed":
        migration.write_bytes(b"revision = 'changed'\n")
    elif damage == "missing":
        migration.unlink()
    else:
        (tmp_path / "migrations/0002.py").write_bytes(b"revision = '0002'\n")
    with pytest.raises(ValueError):
        verify_files(tmp_path, expected)
