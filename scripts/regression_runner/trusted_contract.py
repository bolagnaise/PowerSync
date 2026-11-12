"""Load the runner's contract, never a candidate-provided validation module."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "custom_components/power_sync/regression/contract.py"
_SPEC = spec_from_file_location("trusted_replay_contract_v1", _PATH)
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
for _name in dir(_MODULE):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_MODULE, _name)
