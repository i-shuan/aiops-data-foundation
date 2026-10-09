#!/usr/bin/env bash
# Fluent Bit 重啟會不會掉 log：產生器每秒 RATE 行（預設 200）跑 240 秒，中途重啟 Fluent Bit 兩次，
# 用序號算遺失、重複，並列出缺口在哪個時間點。
#
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/load/run-restart.sh [變體]
#   current     正式設定（預設）
#   grace       正式設定，關機等待（service.grace）從 5 秒加長到 25 秒
#   parser-cri  讀檔改用 parser: cri（不接 containerd 切開的長行）
# 變體用 fluent-bit-dedup 那個測試用 overlay 套上，跑完換回正式設定。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
variant="${1:-current}"
RATE="${RATE:-200}"
A=elastic:changeme-test-only
es() { curl -sS -m 60 -u "${A}" -H 'Content-Type: application/json' "$@"; }
ts() { date -u +%H:%M:%S; }
src="${here}/../../../platform/cluster-addons/fluent-bit/fluent-bit.yaml"
dst="${here}/../fluent-bit-dedup/fluent-bit.yaml"

case "${variant}" in
  current) cp "${src}" "${dst}" ;;
  grace) sed -e 's/^  flush: 1$/  flush: 1\n  grace: 25/' "${src}" > "${dst}" ;;
  parser-cri)
    # parser: cri 把內容放在 message，pipeline 讀的是 log；YAML 設定也要自己載入內建的 parsers.conf
    sed -e 's/^      multiline.parser: cri, docker$/      parser: cri/' \
        -e 's/^  flush: 1$/  flush: 1\n  parsers_file: \/fluent-bit\/etc\/parsers.conf/' \
        -e 's/^  filters:$/  filters:\n    - name: modify\n      match: kube.*\n      rename: message log/' "${src}" > "${dst}" ;;
  *) echo "用法：$0 current|grace|parser-cri"; exit 2 ;;
esac
restore() {
  kubectl apply -k "${here}/../fluent-bit" >/dev/null
  kubectl -n logging rollout status daemonset/fluent-bit --timeout=180s >/dev/null
}
trap restore EXIT
kubectl apply -k "${here}/../fluent-bit-dedup" >/dev/null
kubectl -n logging rollout restart daemonset/fluent-bit >/dev/null
kubectl -n logging rollout status daemonset/fluent-bit --timeout=180s >/dev/null

run="rst-${variant}-$(date +%H%M%S)"
q="{\"bool\":{\"filter\":[{\"term\":{\"app.run\":\"${run}\"}},{\"exists\":{\"field\":\"app.seq\"}}]}}"
kubectl apply -f "${here}/generator.yaml" >/dev/null
sed -e "s/\${RUN}/${run}/g" -e "s/\${RATE}/${RATE}/g" -e "s/\${DURATION}/240/g" "${here}/job.tpl.yaml" | kubectl apply -f - >/dev/null
echo "== ${variant}：每秒 ${RATE} 行 240 秒，中途重啟 Fluent Bit 兩次（run=${run}）"
for i in 1 2; do
  sleep 60
  kubectl -n logging rollout restart daemonset/fluent-bit >/dev/null
  echo "   $(ts) 第 ${i} 次重啟"
  kubectl -n logging rollout status daemonset/fluent-bit --timeout=180s >/dev/null
done
until kubectl -n load-test get job "gen-${run}" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null | grep -qE 'Complete|Failed'; do sleep 5; done
emitted=$(cat /var/log/pods/load-test_gen-${run}-*/gen/*.log | grep -o '"emitted": [0-9]*' | grep -o '[0-9]*')
sleep 30; es -X POST "${ES_URL}/k8s-imc-local/_refresh" >/dev/null

# 逐頁列出收到的序號，算遺失、重複、缺口
seqs="$(mktemp)"; after=""
while :; do
  r="$(es "${ES_URL}/k8s-imc-local/_search" -d "{\"size\":0,\"query\":${q},\"aggs\":{\"s\":{\"composite\":{\"size\":10000,
    \"sources\":[{\"seq\":{\"terms\":{\"field\":\"app.seq\"}}}]${after:+,\"after\":${after}}}}}}")"
  jq -r '.aggregations.s.buckets[] | "\(.key.seq) \(.doc_count)"' <<<"${r}" >> "${seqs}"
  after="$(jq -c '.aggregations.s.after_key // empty' <<<"${r}")"; [[ -n "${after}" ]] || break
done
got=$(es "${ES_URL}/k8s-imc-local/_count" -d "{\"query\":${q}}" | jq .count)
uniq=$(wc -l < "${seqs}")
echo "   送出 ${emitted}、收到 ${got}，遺失 $(( emitted - uniq ))、重複 $(( got - uniq ))"
# 缺口：序號不連續的地方（約每秒 RATE 筆，換算成秒數）
sort -n "${seqs}" | awk -v r="${RATE}" 'BEGIN{p=0} $1!=p+1 {printf "   缺 seq %d～%d（%d 筆，約 %.1f 秒）\n", p+1, $1-1, $1-p-1, ($1-p-1)/r} {p=$1}'
rm -f "${seqs}"
