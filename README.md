# aiops-data-foundation

## 定位

這個專案現階段做的事情，是把 **metrics（VictoriaMetrics）** 和 **log（Elasticsearch）** 依組織架構（BU / 處 / 部門）分流，並且用同一份組織樹（`org.yaml`）驅動各系統的權限設定（vmauth、Logstash、Kibana）。

但這個「依組織分流、控權限」只是**現階段的樣貌**，不是終點。真正的目標是先把 metrics/log 整理成一份**乾淨、有標籤（tenant / division / dept）的 MELT（Metrics, Events, Logs, Traces）資料層**，讓現在的權限控管跟未來要接的 **AIOps / 異常偵測 / 根因分析（RCA）模型**共用同一份組織真相來源（`org.yaml`），不用重工。

> 白話說：現在做的分流跟權限，其實是在打地基。地基打好之後，同一批貼好標籤的資料，之後可以直接餵給 AI 做問題根因分析，不用重新設計一套資料管線。

## 待確認的關鍵問題

在往下設計 metrics 用 label 還是 projectID、log index 怎麼切之前，需要先確認：

**部門之間的資料，只需要「看不到」（邏輯隔離 / label 過濾）就好，還是規定要實體隔離？**

這題的答案會決定：
- Metrics 要用 `extra_label` 過濾（邏輯隔離），還是 `accountID:projectID`（實體隔離）
- Log index 要不要每個部門一個 index，還是可以共用 index 靠 document-level security 過濾（需要 Elastic Enterprise 訂閱）

---

## 1. 身分模型：namespace 只標部門

namespace 上**只標一個部門代碼**，組織樹放在 Git 的 `org.yaml`，作為唯一記錄組織層級的地方：

```yaml
# namespace
metadata:
  name: it-mes
  labels:
    org-dept: D3120        # 建議用 HR 的部門代碼，不要用名稱
```

```yaml
# org.yaml：唯一記錄組織層級的地方
bus:
  it:
    metrics_tenant: 2
    divisions:
      it-d2:               # 某個處
        depts: [D3120, D3130]
      it-d3:
        depts: [D3210]
  imc:
    metrics_tenant: 1
    divisions:
      imc-d1:
        depts: [D1110, D1120]
```

- **組織調整（部門換處）只改 `org.yaml`**，namespace 和各 cluster 都不用動。
- **部門代碼要選不會變的**，不要把處或 BU 編進代碼裡，否則一改組織，代碼就要跟著改。

## 2. 三個層級各負責什麼

中央分流層依 `cluster;namespace` 查到部門，再往上推出處和 BU，一次加上四個 label：`cluster`、`bu`、`division`、`dept`。

| 層級 | Metrics（VictoriaMetrics） | Log（Elasticsearch） |
|---|---|---|
| BU | **tenant**，實體隔離 | index 前綴 `logs-it-*` |
| 處 | `division` label 加 vmauth 過濾 | 產生角色，列出該處底下各部門的 index |
| 部門 | `dept` label 加 vmauth 過濾 | index `logs-it-d3120-*` |

## 3. Metrics 的權限（免費版做得到）

vmauth 可以依使用者強制加上 `extra_label` 過濾：

```yaml
users:
- username: it-bu            # BU：看整個 IT
  url_prefix: "http://vmselect:8481/select/2/prometheus"
- username: it-d2            # 處
  url_prefix: "http://vmselect:8481/select/2/prometheus?extra_label=division=it-d2"
- username: dept-d3120       # 部門
  url_prefix: "http://vmselect:8481/select/2/prometheus?extra_label=dept=D3120"
```

**注意版本**：vmauth 有個漏洞，client 可以在 POST body 帶自己的參數，蓋過 vmauth 設定的過濾條件。v1.122.22、v1.136.9、v1.143.0 已修正（[#10908](https://github.com/VictoriaMetrics/VictoriaMetrics/issues/10908)），要確認實際部署版本在這之後。

用 label 過濾屬於**邏輯隔離**。如果部門之間要**實體隔離**，可以改用 `accountID = BU`、`projectID = 部門`，例如 `2:3120`。代價是 BU 層級要跨 project 查詢，比較麻煩。

## 4. Log 的權限：注意授權限制

Elasticsearch 自架版的 **document-level security 只有 Enterprise 訂閱才有**（[Elastic 訂閱表](https://www.elastic.co/pdf/subscriptions-2026-07-01.pdf)），免費版只能做到 index 層級的權限。所以：

- **沒有 Enterprise**：每個部門一個 index，例如 `logs-it-d3120-*`。BU 角色用 `logs-it-*`。處的角色由 `org.yaml` 產生，直接列出底下各部門的 index，所以部門換處時 index 名稱不用變。部門多時要搭配 ILM rollover，並把 primary shard 設小，避免 shard 數量爆掉。
- **有 Enterprise**：可以每個 BU 一個 index，用 `division`、`dept` 欄位做 document-level security。

## 5. 組織異動時會發生什麼

| 異動 | 要改的地方 | 新資料 | 歷史資料 |
|---|---|---|---|
| 部門換處（同 BU） | `org.yaml` | `division` label 變成新的處 | 還是舊的處，用部門查不受影響 |
| 部門換 BU | `org.yaml` | 進新的 tenant 和 index | 留在舊 BU，要決定舊 BU 還能不能看 |
| 新增 namespace | namespace YAML | 自動歸到所屬部門 | 無 |

腳本的輸出也跟著變多。從 `org.yaml` 加上各 cluster 的 namespace YAML，會產生：

- **中央 vmagent** 的 relabel 設定
- **Logstash** 的字典檔
- **vmauth** 的使用者設定
- **Kibana** 的角色設定

這樣所有權限都來自同一份組織樹，未來要接 AIOps/RCA 時，模型也能直接用這份組織樹去理解「這個異常屬於哪個部門/處/BU」，不用另外維護一份對照表。

## 6. 後續方向（尚未開始）

- [ ] 確認上面「邏輯隔離 vs 實體隔離」的問題
- [ ] `org.yaml` schema 定案
- [ ] Generator：`org.yaml` + namespace YAML → vmagent relabel / vmauth users / Logstash 字典 / Kibana roles
- [ ] AIOps/RCA 這層要怎麼消費這份資料層（模型、觸發方式、輸出去哪），目前還沒設計

---

## 參考資料

- [VictoriaMetrics: vmauth](https://docs.victoriametrics.com/victoriametrics/vmauth/)
- [Vmauth overwrites extra_filters from the client request · Issue #10908](https://github.com/VictoriaMetrics/VictoriaMetrics/issues/10908)
- [Elastic subscriptions (2026-07-01)](https://www.elastic.co/pdf/subscriptions-2026-07-01.pdf)
- [Licenses and subscriptions | Elastic Docs](https://www.elastic.co/docs/deploy-manage/license)
