#!/bin/sh
# Grafana 的 initContainer：Grafana 啟動前把外掛裝進外掛資料夾（docs/pdca/phase5-learning-loop/2026-10-grafana-plugin-init.md）。
# 用 Grafana 同一個 image 跑（裡面有 curl、unzip、sha256sum），不用另外拉 image。
#
# 環境變數：
#   PLUGIN_ID      外掛的 id，例如 volkovlabs-form-panel（zip 裡的資料夾名字）
#   PLUGIN_URL     外掛 zip 的網址：放公司內部的 Nexus，Pod 不用連外網
#   PLUGIN_SHA256  zip 的 SHA-256：不符就失敗，Grafana 不會啟動（防止被換掉、下載不完整）
#   PLUGINS_DIR    外掛資料夾（預設 /var/lib/grafana/plugins，跟 Grafana 主容器掛同一個 volume）
#   CURL_OPTS      選填，例如 --cacert /etc/ssl/nexus-ca.crt
set -eu
dir="${PLUGINS_DIR:-/var/lib/grafana/plugins}"
mark="$dir/.$PLUGIN_ID.sha256"     # 記下裝的是哪個 zip；放在外掛資料夾外面，不然 Grafana 驗簽章會說檔案被改過

if [ -f "$mark" ] && [ "$(cat "$mark")" = "$PLUGIN_SHA256" ] && [ -f "$dir/$PLUGIN_ID/plugin.json" ]; then
  echo "$PLUGIN_ID 已經是這個版本（$PLUGIN_SHA256），不用重裝"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
echo "下載 $PLUGIN_URL"
# shellcheck disable=SC2086
curl -fsSL --retry 3 --max-time 120 ${CURL_OPTS:-} -o "$tmp/plugin.zip" "$PLUGIN_URL"

got="$(sha256sum "$tmp/plugin.zip" | cut -d' ' -f1)"
if [ "$got" != "$PLUGIN_SHA256" ]; then
  echo "SHA-256 不符：預期 $PLUGIN_SHA256，下載到的是 $got。不裝，Grafana 不啟動。" >&2
  exit 1
fi

unzip -q "$tmp/plugin.zip" -d "$tmp/x"
if [ ! -f "$tmp/x/$PLUGIN_ID/plugin.json" ]; then
  echo "zip 裡沒有 $PLUGIN_ID/plugin.json：PLUGIN_ID 對嗎？" >&2
  exit 1
fi
mkdir -p "$dir"
rm -rf "${dir:?}/$PLUGIN_ID"
mv "$tmp/x/$PLUGIN_ID" "$dir/$PLUGIN_ID"
echo "$PLUGIN_SHA256" > "$mark"
echo "已裝 $PLUGIN_ID → $dir/$PLUGIN_ID"
