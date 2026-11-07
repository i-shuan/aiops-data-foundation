"""prune.yaml：engine 自己的資料（PostgreSQL）的保留天數。"""
from __future__ import annotations

from typing import Literal

from common.config.common import NonNeg, PosInt, Strict, Tz


# ── prune.yaml ──

class PruneTable(Strict):
    # 哪一種資料（Repos 的名字）：anomalies 依異常的時間、segments 依統計的時間、alerts 只刪已結案的、依結案時間
    table: Literal["anomalies", "segments", "alerts"]
    keep_days: NonNeg


class PrunePack(Strict):
    # 超過保留天數的資料，刪之前先一天打包成一筆 gzip 放進 aiops_packed（common/infra/pack.py）；要看用 detector db restore 讀回
    keep_days: NonNeg | None = 365          # 打包的資料再留幾天；null = 不刪
    batch: PosInt = 20000                   # 一次讀幾筆、打包、再刪（記憶體約 batch × 1 KB）


class PruneConfig(Strict):
    timezone: Tz = "Asia/Taipei"            # 輸出的時間、打包的日期用這個時區
    pack: PrunePack | None = PrunePack()    # 寫 pack: null = 不打包、超過天數直接刪
    tables: list[PruneTable] = []
