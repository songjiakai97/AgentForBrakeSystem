"""pytest 共享 fixture：确保合成样例数据存在（tests/data/mf4 不入库，可再生）。"""

import os
import sys
import warnings

warnings.filterwarnings("ignore", module="asammdf")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA = os.path.join(ROOT, "tests", "data", "mf4")


def ensure_demo_data():
    if not os.path.isdir(DATA) or not any(f.endswith(".mf4") for f in os.listdir(DATA)):
        from scripts.generate_demo_data import generate_all

        generate_all(DATA)


ensure_demo_data()
