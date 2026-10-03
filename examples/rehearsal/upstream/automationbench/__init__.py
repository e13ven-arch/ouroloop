"""The rehearsal's miniature AutomationBench (docs/REHEARSAL.md §2)."""
from automationbench.schema.world import WorldState

__version__ = "0.0.0-rehearsal"
AutomationBenchEnv = None       # upstream sets this to None too when its runner's dependencies are missing
__all__ = ["AutomationBenchEnv", "WorldState", "__version__"]
