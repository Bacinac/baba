import importlib.util
import sys
from pathlib import Path

import pytest


def test_unknown_minimal_detector_stops_before_any_download(tmp_path, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[1] / "tools/download_models.py"
    spec = importlib.util.spec_from_file_location("baba_model_provisioning_probe", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", [str(path), "--minimal", "--detector", "missing-detector",
                                      "--models-dir", str(tmp_path)])

    def fetch(*_args):
        pytest.fail("an unknown selected detector must not download a replacement")

    monkeypatch.setattr(module, "fetch", fetch)
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert "unknown detector" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
