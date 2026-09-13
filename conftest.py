"""
Makes the project root importable from tests/ -- tests/ has no
__init__.py (plain pytest "rootdir" layout), so without this, `import
actions`/`import main`/etc. from a test module would fail to find them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
