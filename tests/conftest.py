import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# neither half is an installed package: the relay is one file handed out on its own, and the
# service is run from its folder.
sys.path.insert(0, str(ROOT / "relay"))
sys.path.insert(0, str(ROOT / "service"))
