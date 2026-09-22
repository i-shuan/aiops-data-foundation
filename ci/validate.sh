#!/usr/bin/env bash
# CI 的單一入口：三道檢查全過才算過。
#
#   1. app.yaml 本身（照 devops repo 的規則）
#   2. observability 區塊（歸屬寫對了沒、同一個 namespace 沒有被兩個 app 宣告）
#   3. chart 渲染得出來（app-common 的規範檢查都寫在模板裡）
set -uo pipefail
export PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.."

status=0
run() { printf '\n=== %s ===\n' "$1"; shift; "$@" || status=1; }

run "1. app.yaml"        python ci/validate_app_yaml.py applications/*/app.yaml
run "2. observability"   python platform/argocd/validate_observability.py applications/*/app.yaml
run "3. chart 渲染"      bash ci/validate-charts.sh

printf '\n%s\n' "$([ $status = 0 ] && echo 全部通過 || echo 有檢查沒過)"
exit $status
