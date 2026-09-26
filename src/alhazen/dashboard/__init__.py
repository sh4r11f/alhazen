"""The live monitor's pre-1.9 import path. Deprecated; removed in 2.0.

``alhazen.dashboard`` became ``alhazen.live_monitor`` in 1.9 so that
"dashboard" means one thing in alhazen: the experiment workspace,
``alhazen dashboard``. An experiment package written against 1.x still
imports this name, so importing it warns once and hands back the live
monitor's public classes under their old spellings, exactly as
docs/versioning.md §4 promises: a MINOR release that warns, a MAJOR one
that removes. ``tests/unit/test_versioning.py`` reads the removal version
below and fails the 2.0 bump until this package is gone.
"""

from alhazen._deprecation import warn_deprecated_name
from alhazen.live_monitor.spec import LiveMonitorPanel as DashboardPanel
from alhazen.live_monitor.spec import LiveMonitorSpec as DashboardSpec

warn_deprecated_name(
    "alhazen.dashboard", since="1.9", removed_in="2.0", instead="alhazen.live_monitor"
)

__all__ = ["DashboardPanel", "DashboardSpec"]
