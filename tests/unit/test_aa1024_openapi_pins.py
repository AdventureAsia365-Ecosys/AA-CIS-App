"""AA-1024 G6: scripts/export_openapi.py refuses an interpreter whose FastAPI / Pydantic differ from the pins.

Lesson (root lessons log, CI family): openapi.json built with a local FastAPI 0.110 went red on CI, which builds it with
the requirements.txt pins. The refusal happens before the app import, so these tests never import api.main.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("export_openapi_pins", ROOT / "scripts" / "export_openapi.py")
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)


def test_requirements_pin_both_packages():
    pins = export.pinned_versions()
    assert set(pins) == set(export.PINNED_PACKAGES)
    assert all(v[0].isdigit() for v in pins.values())


def test_pin_problems_name_each_mismatch(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("uvicorn==1.0\nfastapi==9.9.1  # pinned\nPydantic == 8.8.8\n", encoding="utf-8")
    pins = export.pinned_versions(req)
    assert pins == {"fastapi": "9.9.1", "pydantic": "8.8.8"}
    assert export.pin_problems(pins, {"fastapi": "9.9.1", "pydantic": "8.8.8"}) == []
    problems = export.pin_problems(pins, {"fastapi": "0.110.1", "pydantic": None})
    assert problems == ["fastapi 0.110.1 here, requirements.txt pins 9.9.1",
                        "pydantic not installed here, requirements.txt pins 8.8.8"]
    assert export.pin_problems({"fastapi": "1"}, {"fastapi": "1", "pydantic": "2"}) == [
        "requirements.txt has no 'pydantic==<version>' pin"]


def test_main_refuses_before_importing_the_app(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(export, "pin_problems", lambda: ["fastapi 0.110.1 here, requirements.txt pins 9.9.1"])
    monkeypatch.setattr(export, "build_spec", lambda: (_ for _ in ()).throw(AssertionError("app imported")))
    out = tmp_path / "openapi.json"
    assert export.main(["--output", str(out)]) == 2
    assert export.main(["--check", "--output", str(out)]) == 2
    assert not out.exists()
    assert "requirements.txt pins 9.9.1" in capsys.readouterr().out
    monkeypatch.setattr(export, "build_spec", lambda: {"openapi": "3.1.0", "info": {}, "paths": {}})
    assert export.main(["--allow-unpinned", "--output", str(out)]) == 0
    assert out.exists()


def test_pytest_puts_the_repo_root_on_sys_path():
    """AA-1024 G6: an integration import broke when a shard changed which conftest loaded first."""
    import tomllib

    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert cfg["tool"]["pytest"]["ini_options"]["pythonpath"] == ["."]
