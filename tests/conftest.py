"""pytest 共享 fixture：确保合成样例数据存在（tests/data 不入库，可再生）。"""

import os
import sys
import warnings

warnings.filterwarnings("ignore", module="asammdf")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MF4_DIR = os.path.join(ROOT, "tests", "data", "mf4")
BLF_DIR = os.path.join(ROOT, "tests", "data", "blf")


def ensure_demo_data():
    if not os.path.isdir(MF4_DIR) or not any(f.endswith(".mf4") for f in os.listdir(MF4_DIR)):
        from scripts.generate_demo_data import generate_all

        generate_all(MF4_DIR)


def ensure_blf_demo_data():
    """BLF demo 依赖 python-can/cantools；缺库时跳过（相关用例本身也会 skip）。"""
    try:
        import can          # noqa: F401
        import cantools     # noqa: F401
    except ImportError:
        return
    if not os.path.isdir(BLF_DIR) or not any(f.endswith(".blf") for f in os.listdir(BLF_DIR)):
        from scripts.generate_demo_blf import generate_all as generate_blf_all

        generate_blf_all(BLF_DIR)


ensure_demo_data()
ensure_blf_demo_data()
