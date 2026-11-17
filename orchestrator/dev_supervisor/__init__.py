"""Development supervisor: telemetry and evidence model (lane L1, T-DEV-001).

Nothing in this package is an account-usage reading unless a UsageProvider supplies one.
"""
from .evidence import Evidence, Label, observed, reported, inferred, unavailable
from .telemetry import (DEFAULT_CONTEXT_WINDOWS, DEFAULT_THRESHOLDS, PressureConfig, StatuslineProvider,
                        UsageProvider, UnavailableProvider, context_pressure, read_last_usage, read_sessions,
                        snapshot)

__all__ = ["Evidence", "Label", "observed", "reported", "inferred", "unavailable", "DEFAULT_CONTEXT_WINDOWS",
           "DEFAULT_THRESHOLDS", "PressureConfig", "StatuslineProvider", "UsageProvider", "UnavailableProvider",
           "context_pressure", "read_last_usage", "read_sessions", "snapshot"]
