# 新增一種原始資料來源

> **類型：操作 How-To**　對照表與每個成員的意思見 [data-sources-reference.md](data-sources-reference.md)

要接第三種 trace 格式（例如 Zipkin、Datadog）：寫一個適配器，登記，補測試。**不需要改合成、判斷、告警、根因**。

## 步驟

1. **看懂統一的 span**：[data-sources-reference.md](data-sources-reference.md)「統一的 span」。你的目標是把你的 raw 文件翻成那個 dict。
2. **拿真實文件**：從那個來源的索引各抓一筆（`GET <索引>/_search?size=1`）：一個入口、一個往外呼叫的 span、一個失敗的，存成測試用的 JSON。
3. **寫 `aiops-engine/src/data/source/<名字>.py`**：繼承 `SourceAdapter`（`adapter.py`），填完所有成員。要點：
   - `can_handle`：挑**你才有的**欄位（例如 Zipkin 的 `localEndpoint`），不要用「沒有別人的欄位」。
   - `to_span`：單位換成毫秒（`adapter.py` 有 `duration_ms`、`start_ms` 可用，壞值當 0）；`kind` 沒有就從文件種類推；`attrs`、`resource` 攤成 OTel 的名字。
   - `trace_fields` 把很大的欄位（stacktrace）排除。
4. **登記**：`data/source/__init__.py` 的 `ADAPTERS` 加一個 `名字: 你的適配器()`。順序是「問的順序」：要積極認的放前面。
5. **接環境變數**：`common/scenario/base.py` 的 `BUILTIN` 若要讓 `AIOPS_SOURCE=<名字>` 選到，補一份情境資料檔（`common/scenario/` 底下，可 `extends: it_otel`，只改 `source`、`owner`、`log` 的欄位對照）。
6. **補設定檔**：`aiops-engine/configs/<名字>/`（序列、規則、排程表；可複製 `configs/apm/` 改）。metrics 與 log 的差異在這裡處理，不在適配器。
7. **補測試**（照 `tests/test_source.py`）：
   - 一條呼叫鏈用你的格式與 OTel 各造一份，斷言 `to_span` 轉出的 `service`、`name`、`kind`、`dur_ms`、`error`、`parent` 一致。
   - 不認得的文件（別的來源的、空的）丟 `UnknownSourceError`。
   - `can_handle` 不會認領別人的文件：現有兩種來源的樣本文件對你的適配器回 False。
8. **跑檢查**：
   ```bash
   cd aiops-engine && python3 -m pytest -q          # 含 tests/test_layers.py、tests/test_source.py（來源判斷只在 data/source 的 AST 檢查）
   cd .. && python3 tools/check-docs.py             # 文件裡的程式路徑要存在
   ```
9. **對真實資料跑一次**：`python3 -m main detector trace-merge --config configs/<名字>/trace.yaml`，看有沒有 `UnknownSourceError`、`traces-merged-*` 的文件對不對；再跑 `investigator investigate` 看報告找不找得到那一段。
10. **更新文件**：[data-sources-reference.md](data-sources-reference.md) 的對照表加一欄，`docs/README.md` 若新增檔案要更新。

## 只是欄位名不一樣：不用寫程式，在網頁上改

歸屬（服務、namespace、部門、群組在哪個欄位）與 log（服務、等級、trace id、訊息在哪個欄位）的欄位名，可以不改 yaml、不重新部署：

1. 打開 engine 的 `/mapping`，挑來源與欄位、填新的欄位名，按「先試一下」：抽 20 筆真的資料，看這個欄位有沒有出現。
2. 試過了按「提出」：存成待核准（還沒生效）。
3. 有權限的人核准：`python3 -m main detector mapping approve --id <id> --by <你>`（核准前會再試一次，資料裡沒有就不給，`--force` 才行）。下一輪工作就用新的。
4. 改錯了：`detector mapping disable --id <id> --by <你>`，回到 yaml 的值。

存在 ES 的 `aiops-mapping`，換 engine 的 image 不會洗掉。穩定之後把它寫回 yaml（進 git）比較好。第一版只開放歸屬與 log；序列、trace 的索引還是改設定檔。

## 常見的坑

- **耗時單位**：OTel 奈秒、APM 微秒。換算錯一千倍不會報錯，只是所有慢請求都不見了或全部變慢。
- **失敗怎麼定義**：各家不同（狀態碼、`outcome`、`status.code`）；對一個你知道失敗的請求確認 `error` 是 True。
- **沒有 `kind`**：APM 沒有，要從文件種類推；推錯會讓「入口」判斷錯，根因分析的「依服務／API 挑請求」會挑不到。
- **`can_handle` 太寬**：會把別人的文件認走；順序排在前面時更糟。
