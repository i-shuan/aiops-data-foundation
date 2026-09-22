#!/usr/bin/env bash
# A. app.yaml 驗證  B. app.yaml → namespace label
#
#   ./tests/app-yaml/run.sh
#
# 不需要 cluster，只要有 python（PyYAML）和 helm。
# 全部符合預期 exit 0，任何一個不符 exit 1。
set -euo pipefail
export PYTHONIOENCODING=utf-8

cd "$(dirname "$0")/../.."
CASES=tests/app-yaml/cases
VALIDATE="python platform/argocd/validate_observability.py"
fail=0

printf '=== A. app.yaml 驗證：結果要跟 # expect 一致 ===\n'
for f in "$CASES"/*.yaml; do
  want=$(sed -n 's/^# expect: *//p' "$f" | head -1)
  if $VALIDATE "$f" >/dev/null 2>&1; then got=OK; else got=FAIL; fi
  if [ "$got" = "$want" ]; then mark="✓"; else mark="✗ 不符"; fail=1; fi
  printf '  %-26s 預期 %-4s 實際 %-4s %s\n' "$(basename "$f")" "$want" "$got" "$mark"
done

# 兩份各自合法、但宣告同一個 namespace：要一起驗才抓得到
if $VALIDATE "$CASES"/duplicate/*.yaml >/dev/null 2>&1; then got=OK; else got=FAIL; fi
if [ "$got" = FAIL ]; then mark="✓"; else mark="✗ 不符"; fail=1; fi
printf '  %-26s 預期 FAIL 實際 %-4s %s\n' "duplicate/（同一個 namespace）" "$got" "$mark"

printf '\n=== B. app.yaml → namespace label：渲染結果要跟 # labels 一致 ===\n'
for f in "$CASES"/ok-*.yaml; do
  want=$(sed -n 's/^# labels: *//p' "$f" | head -1 | tr ' ' '\n' | sort | tr '\n' ' ')
  got=$(bash tests/app-yaml/render.sh "$f" | sort | tr '\n' ' ')
  if [ -z "$got" ]; then mark="✗ 渲染結果是空的"; fail=1
  elif [ "$got" = "$want" ]; then mark="✓"
  else mark="✗ 預期 $want"; fail=1; fi
  printf '  %-16s %-40s %s\n' "$(basename "$f")" "$got" "$mark"
done

printf '\n%s\n' "$([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
