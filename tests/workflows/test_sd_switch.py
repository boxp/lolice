"""Run the SD switch suites in the existing static-policy CI job."""
import importlib.util
from pathlib import Path

directory = Path(__file__).resolve().parents[1] / 'sd-switch'


def load(name):
    spec = importlib.util.spec_from_file_location('sd_switch_' + name, directory / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


intake = load('test_intake')
switcher = load('test_switcher')
manifests = load('test_manifests')
envoy = load('test_gateway_envoy')

IntakeTest = intake.IntakeTest
HttpSurfaceTest = intake.HttpSurfaceTest
HappyPath = switcher.HappyPath
Exclusion = switcher.Exclusion
Drain = switcher.Drain
Window = switcher.Window
Recovery = switcher.Recovery
BoundaryTest = manifests.BoundaryTest
GatewayRoutingTest = envoy.GatewayRoutingTest
