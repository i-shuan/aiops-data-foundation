"""prune.yaml：engine 自己的資料（PostgreSQL）的保留天數。"""
from __future__ import annotations

from typing import Literal

from common.config.common import NonNeg, PosInt, Strict, Tz


# ── prune.yaml ──

class PruneTable(Strict):
    # 哪一種資料（Repos 的名字）：anomalies 依異常的時間、segments 依統計的時間、alerts 只刪已結案的、依結案時間
    table: Literal["anomalies", "segments", "alerts"]
    keep_days: NonNeg


class PruneArchive(Strict):
    # 刪之前先壓縮存成檔案（common/infra/archive.py）：<dir>/<表>/<年>/<表>-<年-月-日>.jsonl.gz；環境變數 AIOPS_ARCHIVE_DIR 蓋過 dir
    dir: str = "/mnt/aiops-archive"
    keep_days: NonNeg | None = None         # 歸檔檔案本身留幾天；不填 = 不刪（資料夾會一直長，看磁碟多大）
    batch: PosInt = 20000                   # 一次讀幾筆、寫檔、再刪（記憶體約 batch × 1 KB）


class PruneConfig(Strict):
    timezone: Tz = "Asia/Taipei"            # 輸出的時間、歸檔檔名的日期用這個時區
    archive: PruneArchive | None = None     # 不寫 = 不歸檔、直接刪
    tables: list[PruneTable] = []
