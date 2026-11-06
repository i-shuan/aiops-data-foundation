"""測試與 e2e 都從 aiops-engine/ 跑；程式在 src/，這裡加進 import 路徑（pytest、python -m tests.xxx_e2e 都會先載入這個檔）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
