"""``alhazen.dashboard.spec``: the module every experiment package imported
its panels from before 1.9. Deprecated; removed in 2.0.

Importing it imports the package, which warns; the names here are the live
monitor's own classes under their old spellings, so a task declaring
``DashboardSpec(panels=[DashboardPanel(...)])`` builds the same objects a
task written for 1.9 does.
"""

from alhazen.live_monitor.spec import LiveMonitorPanel as DashboardPanel
from alhazen.live_monitor.spec import LiveMonitorSpec as DashboardSpec

__all__ = ["DashboardPanel", "DashboardSpec"]
