"""pytest 共享 fixture：确保合成样例数据存在（tests/data 不入库，可再生）。"""

import os
import sys
import warnings

warnings.filterwarnings("ignore", module="asammdf")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 测试必须离线：client.py 在 import 时 load_dotenv()，开发机配了 .env（本地
# LM Studio）会让 LLM.available 变 True，一批按「无 Key 离线快速路径」写的
# 用例就开始对真实端点发 HTTP 调用——表现是全量测试从 1 分钟变成半小时。
# 这里抢在所有 brake_analyzer 导入之前把 Key/向量模型置空（load_dotenv 默认
# 不覆盖已存在的环境变量），测试语义回到分支作者当时的离线前提。
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("OPENAI_EMBED_MODEL", "")

MF4_DIR = os.path.join(ROOT, "tests", "data", "mf4")
BLF_DIR = os.path.join(ROOT, "tests", "data", "blf")


def ensure_demo_data():
    if not os.path.isdir(MF4_DIR) or not any(f.endswith(".mf4") for f in os.listdir(MF4_DIR)):
        from scripts.generate_demo_mf4 import generate_all

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
