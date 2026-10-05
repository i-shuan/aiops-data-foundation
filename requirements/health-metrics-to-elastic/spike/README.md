# spike — 地基試驗用的設定

2026-10-05 在本機 k3d cluster `obs-test` 上實測用的三個檔，結果記在
[`../2-spec.md`](../2-spec.md) 的「先實測過的」。

⚠️ 這是試驗用的最小設定，不是正式的。正式的設定之後放在 `platform/otel/`。

| 檔案 | 內容 |
| --- | --- |
| `elasticsearch.yaml` | 單節點的 Elasticsearch 9.5.3，開帳號密碼，資料掛磁碟 |
| `collector.yaml` | OpenTelemetry Collector contrib 0.161.0：抓有標註的 pod、讀 namespace 的門牌、擋冒充、送進 Elasticsearch |
| `fake.yaml` | 只回固定文字的假程式。裡面的 `NS`、`BODY` 要先換掉 |

## 怎麼重現

密碼不在檔案裡，要先建 Secret。下面的做法是當場隨機產生：

```bash
kubectl --context k3d-obs-test create namespace obs
kubectl --context k3d-obs-test -n obs create secret generic es-credentials --from-literal=password="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
kubectl --context k3d-obs-test apply -f requirements/health-metrics-to-elastic/spike/elasticsearch.yaml
kubectl --context k3d-obs-test apply -f requirements/health-metrics-to-elastic/spike/collector.yaml
```

## 設定裡不能改錯的地方

| 位置 | 改錯的後果 |
| --- | --- |
| `collector.yaml` 的 `retry.max_retries` | 寫 0 是不重試。Elasticsearch 停機時資料直接丟掉，不報錯。實測過 |
| `transform/podip` 那一段 | 拿掉的話讀門牌那一步對不到 pod，所有資料都進 `platform` |
| `transform/tenant` 裡先 `delete_key` 再 `set` 的順序 | 反過來的話程式自己吐的 `tenant` 會留在樣本上 |
| `elasticsearch.yaml` 的 `volumeClaimTemplates` | 拿掉的話 Elasticsearch 一停資料就沒了，驗不了補送 |
