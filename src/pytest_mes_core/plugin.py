# Tell Pytest to automatically load all our sub-modules
pytest_plugins = [
    "pytest_mes_core.plugins.core_config",
    "pytest_mes_core.plugins.telemetry_hooks",
    "pytest_mes_core.plugins.hardware",
    "pytest_mes_core.plugins.orchestrator",
]
