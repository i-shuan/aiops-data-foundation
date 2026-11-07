"""情境：資料檔（同資料夾的 *.yaml）＋兩個策略（it/、factory/）。主程式用 load_scenario() 拿一份、往下傳。"""
from common.scenario.base import Chain, ChainStrategy, ChangeStrategy, NoChain, NoChange, Scenario, load_scenario

__all__ = ["Chain", "ChainStrategy", "ChangeStrategy", "NoChain", "NoChange", "Scenario", "load_scenario"]
