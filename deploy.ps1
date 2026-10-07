# 一鍵部署到「本機」的 K8s（Rancher Desktop，容器引擎 containerd）。
#
#   .\deploy.cmd                        對 rancher-desktop 部署，動手前會先問
#   .\deploy.cmd -Yes                   不問，直接部署
#   .\deploy.cmd -Context 別的名稱       對別的本機 cluster
#   .\deploy.cmd -SaveImages           不部署。把部署要用的 image 與 Helm chart 存進 image\ 資料夾
#
# 沒有網路、或公司的網路拉不到 image 時：
#   1. 在拉得到的電腦上先部署一次，再跑 .\deploy.cmd -SaveImages
#   2. 把整個 repo 連同 image\ 資料夾帶到另一台（image\ 不在 git 裡，要另外複製）
#   3. 在那一台跑 .\deploy.cmd。image\ 裡有東西就從檔案載入，不上網，也不用重建範例程式
#
# 會裝這些（都是本機測試用的規模）：
#   otel-operator          OpenTelemetry Operator（不裝 cert-manager，憑證由 Helm 產生）
#   otel-collector         兩份收集程式、trace 的埋點設定（platform\otel\）
#   elastic                一座單節點的 Elasticsearch，加三個測試用的假程式（tests\obs\backends.yaml）
#   demo-tsid、demo-bsid   兩組範例程式（app\demo-svc\）
#
# 需要 kubectl、helm、nerdctl 在 PATH 裡。Rancher Desktop 的
# Preferences - Application - Environment 選 Automatic，再重開終端機就會有。
#
# 可以用環境變數調整（PowerShell 的寫法是 $env:名稱 = "值"，cmd 的寫法是 set 名稱=值）：
#   BASE_IMAGE     範例程式的 base image。不設就用 Dockerfile 預設的（公司的 Nexus）。在家裡設成 python:3.12-slim
#   SKIP_BUILD     設成 1 就不重建範例程式的 image
#   CLUSTER_NAME   資料上的 cluster 標籤。不設就用 context 的名字
#
# 密碼當場隨機產生，只放進 cluster 裡的 Secret，不會寫進任何檔案，也不會印出來。
# ⚠ 只給本機用。正式環境不要用這支：Elasticsearch 是測試用的、沒有開 TLS。
param(
    [string]$Context = "rancher-desktop",
    [switch]$Yes,
    [switch]$SaveImages
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot

$ColNs = "otel-collector"
$EsNs = "elastic"
$OpNs = "otel-operator"
$ClusterName = if ($env:CLUSTER_NAME) { $env:CLUSTER_NAME } else { $Context }
$ImageDir = Join-Path $PSScriptRoot "image"
$ChartVersion = "0.124.1"
# 部署會用到的 image。版本要跟 platform\otel\、tests\obs\backends.yaml、Operator 的 chart 一致
$Images = [ordered]@{
    "elasticsearch"        = "elasticsearch:9.5.3"
    "otel-collector"       = "otel/opentelemetry-collector-contrib:0.161.0"
    "otel-operator"        = "ghcr.io/open-telemetry/opentelemetry-operator/opentelemetry-operator:0.160.0"
    "otel-autoinst-python" = "ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-python:0.66b0"
    "busybox"              = "busybox:1.36"
    "demo-svc"             = "demo-svc:test"
}

function Stop-Deploy([string]$Message) {
    Write-Host ""
    Write-Host "[停止] $Message" -ForegroundColor Red
    exit 1
}

function Step([string]$Title) {
    Write-Host ""
    Write-Host "=== $Title ===" -ForegroundColor Cyan
}

# 原生指令失敗時不會自己停，每一步都要看結束代碼
function Check([string]$Message) {
    if ($LASTEXITCODE -ne 0) { Stop-Deploy $Message }
}

# ---------------------------------------------------------------- 只存 image，不部署
if ($SaveImages) {
    foreach ($tool in @("nerdctl", "helm")) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Stop-Deploy "找不到 $tool。" }
    }
    New-Item -ItemType Directory -Force -Path $ImageDir | Out-Null
    Step "把 image 存進 $ImageDir"
    foreach ($name in $Images.Keys) {
        Write-Host ("  {0,-22} {1}" -f $name, $Images[$name])
        # nerdctl 實際跑在 Rancher Desktop 的虛擬機裡，看不懂 Windows 的路徑，-o 會寫到不知道哪裡去。
        # 所以讓它印到標準輸出，由 cmd 導進檔案。不能用 PowerShell 的 >：它會把內容轉碼，檔案就壞了
        cmd /c "nerdctl --namespace k8s.io save --platform linux/amd64 $($Images[$name]) > image\$name.tar"
        Check "存不出 $($Images[$name])。這台要先部署過一次，containerd 裡才有這個 image。"
        if ((Get-Item (Join-Path $ImageDir "$name.tar")).Length -lt 100000) {
            Stop-Deploy "$name.tar 太小，應該是沒存成功。"
        }
    }
    Step "把 Operator 的 Helm chart 存進 $ImageDir"
    helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts *> $null
    helm repo update open-telemetry *> $null
    helm pull open-telemetry/opentelemetry-operator --version $ChartVersion -d $ImageDir
    Check "抓不到 Operator 的 Helm chart"
    Write-Host ""
    Get-ChildItem $ImageDir -File | Where-Object { $_.Name -ne "README.md" } |
        ForEach-Object { Write-Host ("  {0,8:N0} MB  {1}" -f ($_.Length / 1MB), $_.Name) }
    Write-Host ""
    Write-Host "  存好了。image\ 不在 git 裡，要帶到別台電腦的話整個資料夾另外複製過去。"
    exit 0
}

foreach ($tool in @("kubectl", "helm")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Stop-Deploy "找不到 $tool。Rancher Desktop 的 PATH 設成 Automatic 後重開終端機。"
    }
}

kubectl --context $Context get namespace kube-system *> $null
Check "連不上 context $Context。先確認 Rancher Desktop 的 Kubernetes 已經啟動。"

Write-Host ""
Write-Host "  要部署到的 cluster：$Context"
kubectl --context $Context get nodes
Write-Host ""
if (-not $Yes) {
    $answer = Read-Host "確定要部署到上面這個 cluster 嗎？輸入 y 再按 Enter"
    if ($answer -ne "y") {
        Write-Host "已取消，沒有動任何東西。"
        exit 1
    }
}

# image\ 裡有檔案就先載進 containerd，後面的步驟就不用上網拉
$tars = @()
if (Test-Path $ImageDir) { $tars = @(Get-ChildItem $ImageDir -Filter *.tar -File) }
$loaded = @{}
if ($tars.Count -gt 0) {
    Step "0/6 從 image\ 載入 $($tars.Count) 個 image（不上網）"
    if (-not (Get-Command nerdctl -ErrorAction SilentlyContinue)) {
        Stop-Deploy "找不到 nerdctl，沒辦法載入 image\ 裡的檔案。"
    }
    foreach ($tar in $tars) {
        Write-Host "  $($tar.Name)"
        # 同樣的原因，用導向餵進去，不給它路徑
        cmd /c "nerdctl --namespace k8s.io load < image\$($tar.Name)" | Out-Null
        Check "載入 $($tar.Name) 失敗。檔案可能沒複製完整"
        $loaded[$tar.BaseName] = $true
    }
}

Step "1/6 OpenTelemetry Operator"
$chart = "open-telemetry/opentelemetry-operator"
$localChart = Join-Path $ImageDir "opentelemetry-operator-$ChartVersion.tgz"
if (Test-Path $localChart) {
    Write-Host "  用 image\ 裡的 Helm chart"
    $chart = $localChart
} else {
    helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts *> $null
    helm repo update open-telemetry *> $null
}
helm --kube-context $Context upgrade --install opentelemetry-operator $chart `
    --version $ChartVersion -n $OpNs --create-namespace `
    --set admissionWebhooks.certManager.enabled=false `
    --set admissionWebhooks.autoGenerateCert.enabled=true `
    --set admissionWebhooks.autoGenerateCert.recreate=false `
    --wait --timeout 15m | Out-Null
Check "Operator 裝不起來。多半是拉不到 Helm chart 或 image。看 kubectl --context $Context -n $OpNs get pods"
Write-Host "  好了"

Step "2/6 namespace 與 Elasticsearch 的帳密"
kubectl --context $Context apply -f platform\otel\common.yaml | Out-Null
Check "套用 platform\otel\common.yaml 失敗"
kubectl --context $Context apply -f tests\obs\backends.yaml | Out-Null
Check "套用 tests\obs\backends.yaml 失敗"

kubectl --context $Context -n $ColNs get secret es-credentials *> $null
$hasCol = ($LASTEXITCODE -eq 0)
kubectl --context $Context -n $EsNs get secret es-credentials *> $null
$hasEs = ($LASTEXITCODE -eq 0)
if ($hasCol -and $hasEs) {
    Write-Host "  帳密已經有了，沿用"
} else {
    # 同一組帳密要放兩份：Elasticsearch 自己用一份，收集程式用一份。Secret 不能跨 namespace 讀
    $pw = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    foreach ($ns in @($ColNs, $EsNs)) {
        kubectl --context $Context -n $ns delete secret es-credentials --ignore-not-found | Out-Null
        kubectl --context $Context -n $ns create secret generic es-credentials `
            --from-literal=username=elastic --from-literal=password=$pw | Out-Null
        Check "在 $ns 建立 Secret 失敗"
    }
    $pw = $null
    Write-Host "  產生了新的帳密，放在兩個 namespace 的 Secret es-credentials"
}

Step "3/6 等 Elasticsearch 起來（第一次要拉 image，會比較久）"
kubectl --context $Context -n $EsNs rollout status statefulset/elasticsearch --timeout=1800s
Check "Elasticsearch 起不來。看 kubectl --context $Context -n $EsNs describe pod elasticsearch-0"

Step "4/6 收集程式"
kubectl --context $Context apply -f platform\otel\instrumentation.yaml | Out-Null
Check "套用埋點的設定失敗。Operator 可能還沒準備好，等一分鐘再跑一次"
# repo 裡的位址是佔位。這裡直接產生 cluster 裡的那一份，指到剛剛裝的那一座，不動檔案
$site = "ctsp"
$m = Select-String -Path platform\otel\site-ctsp.yaml -Pattern '^\s*SITE:\s*"?([^"\s]+)"?' | Select-Object -First 1
if ($m) { $site = $m.Matches[0].Groups[1].Value }
kubectl --context $Context -n $ColNs create configmap otel-site `
    "--from-literal=SITE=$site" "--from-literal=CLUSTER=$ClusterName" `
    "--from-literal=ES_ENDPOINT=http://elasticsearch.$($EsNs).svc:9200" `
    --dry-run=client -o yaml | kubectl --context $Context apply -f - | Out-Null
Check "建立 otel-site 設定失敗"
kubectl --context $Context apply -f platform\otel\cluster-collector.yaml -f platform\otel\node-collector.yaml | Out-Null
Check "套用收集程式失敗"
kubectl --context $Context -n $ColNs rollout restart statefulset/otel-cluster-collector daemonset/otel-node-collector | Out-Null
kubectl --context $Context -n $ColNs rollout status statefulset/otel-cluster-collector --timeout=1200s
Check "cluster 那一份收集程式起不來"
kubectl --context $Context -n $ColNs rollout status daemonset/otel-node-collector --timeout=1200s
Check "節點那一份收集程式起不來"

Step "5/6 範例程式的 image"
if ($loaded["demo-svc"]) {
    Write-Host "  已經從 image\demo-svc.tar 載入，不用重建"
} elseif ($env:SKIP_BUILD -eq "1") {
    Write-Host "  SKIP_BUILD=1，略過"
} else {
    if (-not (Get-Command nerdctl -ErrorAction SilentlyContinue)) {
        Stop-Deploy "找不到 nerdctl。containerd 要用它把 image 建進 K8s 看得到的地方。"
    }
    # 一定要建在 k8s.io 這個命名空間，K8s 才看得到。用 docker build 建的它看不到
    $buildArgs = @("--namespace", "k8s.io", "build")
    if ($env:BASE_IMAGE) { $buildArgs += @("--build-arg", "BASE_IMAGE=$($env:BASE_IMAGE)") }
    $buildArgs += @("-t", "demo-svc:test", "app\demo-svc")
    & nerdctl @buildArgs
    Check "image 建不起來。拉不到 base image 的話，先設 BASE_IMAGE 為 python:3.12-slim 再跑一次。"
}

Step "6/6 範例程式"
helm --kube-context $Context upgrade --install demo-tsid app\demo-svc\k8s -n default --reset-values --wait --timeout 10m | Out-Null
Check "demo-tsid 裝不起來。看 kubectl --context $Context -n demo-tsid get pods"
helm --kube-context $Context upgrade --install demo-bsid app\demo-svc\k8s -n default --reset-values --wait --timeout 10m `
    --set namespace=demo-bsid --set observability.tenant=bsid --set observability.dept=D4100 | Out-Null
Check "demo-bsid 裝不起來。看 kubectl --context $Context -n demo-bsid get pods"
Write-Host "  好了"

Step "部署完成，等 90 秒讓資料進來"
Start-Sleep -Seconds 90

Write-Host ""
Write-Host "  每個 namespace 的 pod："
foreach ($ns in @($OpNs, $ColNs, $EsNs, "demo-tsid", "demo-bsid")) {
    kubectl --context $Context get pods -n $ns --no-headers
}

Write-Host ""
Write-Host "  Elasticsearch 裡三種資料各有幾筆（都大於 0 就是通了）："
foreach ($t in @("metrics", "logs", "traces")) {
    $cmd = "curl -s -u elastic:`$ELASTIC_PASSWORD 'localhost:9200/_cat/count/$t-*?h=count'"
    $n = kubectl --context $Context -n $EsNs exec elasticsearch-0 -- sh -c $cmd
    Write-Host ("    {0,-8} {1}" -f $t, ("$n").Trim())
}

Write-Host ""
Write-Host "  依部門分開的資料流："
$cmd = "curl -s -u elastic:`$ELASTIC_PASSWORD 'localhost:9200/_data_stream?filter_path=data_streams.name'"
$json = kubectl --context $Context -n $EsNs exec elasticsearch-0 -- sh -c $cmd
try {
    ($json | ConvertFrom-Json).data_streams | ForEach-Object { $_.name } | Sort-Object | ForEach-Object { Write-Host "    $_" }
} catch {
    Write-Host "    （讀不出來：$json）"
}

Write-Host ""
Write-Host "  要拿掉全部："
Write-Host "    helm --kube-context $Context uninstall demo-tsid demo-bsid -n default"
Write-Host "    kubectl --context $Context delete namespace demo-tsid demo-bsid fake-none fake-typo fake-spoof $ColNs $EsNs"
Write-Host "    helm --kube-context $Context uninstall opentelemetry-operator -n $OpNs"
