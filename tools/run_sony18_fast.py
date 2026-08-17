from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.harvest_sony18_fast as runner

if __name__ == "__main__":
    runner.h.main()
