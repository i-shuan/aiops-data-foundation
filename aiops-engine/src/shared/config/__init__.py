"""設定檔：一種設定檔（KINDS）一個模型，一個檔一種（series.py、rules.py…）；共用的型別在 common.py；讀檔與驗證在 loader.py。
值在 configs/ 的 yaml；這裡只定義「可以長怎樣」，鍵打錯、型別錯會擋下來，訊息有檔名與欄位。環境變數不在這裡，在 settings.py。"""
from shared.config.loader import KINDS, MODELS, ConfigError, load_config, validate

__all__ = ["KINDS", "MODELS", "ConfigError", "load_config", "validate"]
