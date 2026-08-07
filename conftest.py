import sys
from pathlib import Path

# Insert project root into sys.path for pytest module imports
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
