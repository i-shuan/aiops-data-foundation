# Trace 進 Elasticsearch：規格

> **類型：規格 Reference**　同主題：[原理](README.md)・[操作（部署、驗證）](how-to.md)・[場景（案例）](scenarios.md)

ES 裡的欄位。為什麼這樣設計見 [README.md](README.md)，怎麼做見 [how-to.md](how-to.md)。

## 欄位

用 OTel 原生的格式存（ES 9 內建的 `traces-otel@template`，logsdb 模式）：

| 欄位 | 內容 |
|---|---|
| `trace_id`、`span_id`、`parent_span_id` | 請求與這一段的 ID |
| `name`、`kind`、`duration`、`status.code` | 這一段做什麼、花多久、成功與否 |
| `attributes.*` | 這一段的細節，例如 `http.request.method`、`http.response.status_code` |
| `resource.attributes.service.name` | Pod label `app.kubernetes.io/name`，跟 log、metrics 一樣；應用自己設的不同時被蓋掉 |
| `resource.attributes.service.version`、`.service.namespace` | Pod label，跟 log、metrics 一樣 |

- `service.name` 的順序：Pod label → 應用自己設的（`OTEL_SERVICE_NAME`）→ Deployment 名稱。
- 應用自己設的只在 Pod 沒有 label 時用到；chart 會把兩者設成一樣。
| `resource.attributes.group`、`.dept` | 分流結果、namespace 的 dept |
| `resource.attributes.k8s.*` | namespace、Pod、節點、Deployment、cluster |

pod label、`container.*` 不會帶進 trace（只給 log 用）。
