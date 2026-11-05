"""Run the pinned Envoy integration in the existing static-policy CI job."""
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'local-llm/test_admission.py'
spec = importlib.util.spec_from_file_location('local_llm_admission', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
AdmissionTest = module.AdmissionTest
