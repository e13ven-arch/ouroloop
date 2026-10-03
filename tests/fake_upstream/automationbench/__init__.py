"""A miniature AutomationBench: see tests/fake_upstream/README.md."""
from automationbench.schema.world import WorldState

__version__ = "0.0.0-fake"
AutomationBenchEnv = None       # upstream sets this to None too when its runner's dependencies are missing
__all__ = ["AutomationBenchEnv", "WorldState", "__version__"]
