#Requires -Version 5.0
<#
.SYNOPSIS
    PromptMaster 本地 e2e 桩服务联调（Windows / PowerShell）—— examples/run_e2e_stub.sh 的等价脚本。

.DESCRIPTION
    与 Unix 版一致的验证目标：走**真实 HTTP 链路**（ChatOpenAI 客户端 → 网络请求 → 响应解析
    → Pydantic 校验），覆盖三条结构化输出通道：

      场景 1  桩服务 --uncooperative（声明 JSON 模式却返回带围栏 JSON）
              → 期望自动降级为文本解析：日志出现 channel=json_fallback / 「降级为 JSON 文本解析」
      场景 2  桩服务 --tools（端点配合结构化输出）
              → 期望走原生 json_schema 通道：channel=structured_output，且无降级告警
      场景 3  桩服务 --tools + PM_STRUCT_METHOD=function_calling
              → 期望走 function-calling 通道：channel=function_calling（A10：langchain-openai
                默认 json_schema，请求体不带 tools，桩的 --tools 分支原本是死码；
                必须显式指定 method 才能真正覆盖到这条通道）

    Windows 适配要点（相对 .sh）：
      - 解释器   .venv\Scripts\python.exe（桩服务同用一个 venv，不需要 python3.11）
      - 临时目录  $env:TEMP\pm_e2e_stub（替代 /tmp）
      - 后台进程  Start-Process / Stop-Process（替代 `&` + kill + trap），finally 里必清理
      - 端口     8130 被占用时自动顺延，避免撞上别家残留进程
      - 编码     先设 PYTHONIOENCODING=utf-8，否则 GBK 控制台下中文乱码
      - 缓存     关掉 PM_EVAL_CACHE / PM_TARGET_CACHE，否则会被 logs/*.json 历史缓存短路，
                 等于没打 HTTP，"降级/原生通道" 的结论也就不可信
      - 评委端点  PM_JUDGES=2 时 evaluator_b / arbiter 在 .env 里指向真实外部 API；
                 本地桩联调必须把它们一并显式指向桩服务，否则会真打起外部端点（花钱且不可复现）

.PARAMETER Port
    桩服务端口起点（默认 8130；被占用则自动 +1 顺延）。

.PARAMETER WorkDir
    日志/探针脚本输出目录（默认 $env:TEMP\pm_e2e_stub）。不写入项目目录。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\examples\run_e2e_stub.ps1

.EXAMPLE
    # 只跑场景 1，并把桩服务 stdout 留在指定目录
    .\examples\run_e2e_stub.ps1 -Scenario 1 -WorkDir D:\tmp\pm_e2e
#>
[CmdletBinding()]
param(
    [int]$Port = 8130,
    [string]$Task = '帮我写个 prompt 让 AI 分析销售数据',
    [int]$Cases = 3,
    [int]$MaxIter = 2,
    [string]$ProjectRoot = '',
    [string]$PythonExe = '',
    [string]$WorkDir = '',
    [ValidateSet(0, 1, 2, 3)]
    [int]$Scenario = 0          # 0 = 三个场景都跑
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------
# 路径与编码
# --------------------------------------------------------------------------
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
if (-not $PythonExe)   { $PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe' }
if (-not $WorkDir)     { $WorkDir = Join-Path $env:TEMP 'pm_e2e_stub' }

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'run.py'))) {
    throw "项目根不对：$ProjectRoot 下找不到 run.py（用 -ProjectRoot 指定）"
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "找不到解释器：$PythonExe（用 -PythonExe 指定 venv 里的 python.exe）"
}
Set-Location -LiteralPath $ProjectRoot   # 等价于 .sh 里的 cd "$(dirname "$0")/.."
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

# 中文不乱码：子进程 UTF-8 输出 + 控制台按 UTF-8 解码（无控制台时忽略即可）
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Text.UTF8Encoding]::new($false)
} catch { }

function Write-Line { param([string]$Text = '') Write-Host $Text }
function Write-Head { param([string]$Text) Write-Line ''; Write-Line ('=' * 64); Write-Line $Text; Write-Line ('=' * 64) }

# --------------------------------------------------------------------------
# 端口 / 就绪探测
# --------------------------------------------------------------------------
function Test-PortBusy {
    param([int]$Port)
    $client = New-Object Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        return ($ar.AsyncWaitHandle.WaitOne(300) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-FreePort {
    param([int]$Start)
    $p = $Start
    while ((Test-PortBusy -Port $p) -and ($p -lt ($Start + 20))) {
        Write-Line "端口 $p 已被占用，顺延一位……"
        $p++
    }
    if (Test-PortBusy -Port $p) { throw "在 $Start..$p 区间找不到空闲端口" }
    return $p
}

function Wait-StubReady {
    param([int]$Port, [int]$TimeoutSec = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 3
            if ($r.ok) { return $r }
        } catch { Start-Sleep -Milliseconds 300 }
    }
    throw "桩服务 ${TimeoutSec}s 内未就绪：http://127.0.0.1:$Port/healthz"
}

# --------------------------------------------------------------------------
# 桩服务生命周期
# --------------------------------------------------------------------------
function Start-Stub {
    param([string]$Tag, [int]$Port, [string[]]$StubArgs)
    $out = Join-Path $WorkDir "stub_$Tag.out.log"
    $err = Join-Path $WorkDir "stub_$Tag.err.log"
    Remove-Item -LiteralPath $out, $err -Force -ErrorAction SilentlyContinue
    $procArgs = @((Join-Path $ProjectRoot 'examples\openai_stub_server.py'), '--port', "$Port") + $StubArgs
    $p = Start-Process -FilePath $PythonExe -ArgumentList $procArgs `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    return [pscustomobject]@{ Proc = $p; Out = $out; Err = $err }
}

function Stop-Stub {
    param($Stub)
    if (-not $Stub) { return }
    $p = $Stub.Proc
    if ($p -and -not $p.HasExited) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
    if ($p) {
        try { [void]$p.WaitForExit(5000) } catch { }
    }
    Start-Sleep -Milliseconds 700   # 给端口一点释放时间
}

# --------------------------------------------------------------------------
# 配置自检：确认每个角色的 base_url 都指向本地桩，避免误打真实端点
# --------------------------------------------------------------------------
$ProbeSrc = @'
import sys
sys.path.insert(0, sys.argv[1])
expected = sys.argv[2]
from pm.llm import build_config

ROLES = ("clarifier", "optimizer", "mockgen", "evaluator", "evaluator_b", "arbiter",
         "reviser", "target", "comparator")
bad = []
for role in ROLES:
    cfg = build_config(role)
    print("%-12s %-34s %s" % (role, cfg.base_url or "(unset)", cfg.model))
    if (cfg.base_url or "") != expected:
        bad.append(role)
print(("BAD:" + ",".join(bad)) if bad else "ALL_LOCAL")
'@

function Invoke-Py {
    <# 调起 python 并合并 stdout/stderr。PS 5.1 会把 native 命令的 stderr 包成
       ErrorRecord，配上 EAP=Stop 会当终止错误抛（NativeCommandError），所以调
       子进程时临时降到 Continue，把输出当纯文本处理。 #>
    param([string[]]$PyArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $PythonExe @PyArgs 2>&1 | ForEach-Object { [string]$_ })
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{ ExitCode = $code; Lines = $lines }
}

function Invoke-ConfigCheck {
    param([string]$BaseUrl)
    $probeFile = Join-Path $WorkDir 'config_probe.py'
    [IO.File]::WriteAllText($probeFile, $ProbeSrc, [Text.UTF8Encoding]::new($false))
    $res = Invoke-Py -PyArgs @($probeFile, $ProjectRoot, $BaseUrl)
    $res.Lines | ForEach-Object { Write-Line "  $_" }
    if (($res.Lines -join "`n") -match 'BAD:(\S+)') {
        throw "角色 $($matches[1]) 的 base_url 未指向桩服务（$BaseUrl）——本地 e2e 会打到真实端点，已中止。"
    }
}

# --------------------------------------------------------------------------
# 跑一次 run.py（结果同时落盘，方便回看完整输出）
# --------------------------------------------------------------------------
function Invoke-Run {
    param([string]$Tag)
    $log = Join-Path $WorkDir "run_$Tag.log"
    $res = Invoke-Py -PyArgs @(
        (Join-Path $ProjectRoot 'run.py'),
        '--task', $Task,
        '--target-model', 'stub-model',
        '--cases', "$Cases",
        '--max-iter', "$MaxIter"
    )
    Set-Content -LiteralPath $log -Value $res.Lines -Encoding UTF8
    return [pscustomobject]@{ ExitCode = $res.ExitCode; Log = $log; Lines = $res.Lines }
}

function Select-KeyLine {
    param([string[]]$Lines, [int]$Head = 40)
    $pat = 'run_id|^- 状态|^- 迭代|^- LLM|^- 目标模型|评分|平均分|最低分|通过用例|判定|channel|降级|结构化输出失败|RuntimeError|Traceback|报告已保存'
    @($Lines | Where-Object { $_ -match $pat } | Select-Object -First $Head)
}

# --------------------------------------------------------------------------
# 环境变量：全部指向桩服务
# --------------------------------------------------------------------------
function Set-PmEnv {
    param([int]$Port)
    $base = "http://127.0.0.1:$Port/v1"
    $env:PM_PROVIDER            = 'openai'
    $env:PM_API_KEY             = 'stub-key'
    $env:PM_API_KEYS            = ''
    $env:PM_BASE_URL            = $base
    $env:PM_MODEL               = 'stub-model'
    $env:PM_TARGET_MODEL        = 'stub-model'
    $env:PM_TIMEOUT             = '30'
    # 逐角色钉死到桩：只设全局 PM_BASE_URL 是不够的——load_dotenv 不覆盖已存在的变量，
    # 但 .env 里任何一条 `PM_<ROLE>_BASE_URL` 都会原样注回来，"本地联调"于是
    # 静默变成真打付费端点（2026-09-18 实测：新加的 PM_COMPARATOR_BASE_URL 漏钉，
    # 桩 e2e 里 comparator 直接去打了真端点）。列全角色，新角色默认进表。
    foreach ($role in 'CLARIFIER','OPTIMIZER','MOCKGEN','EVALUATOR','EVALUATOR_B',
                      'ARBITER','REVISER','TARGET','COMPARATOR') {
        Set-Item -Path "Env:PM_${role}_API_KEY"  -Value 'stub-key'
        Set-Item -Path "Env:PM_${role}_BASE_URL" -Value $base
        Set-Item -Path "Env:PM_${role}_MODEL"    -Value 'stub-model'
    }
    # 关掉结果缓存：保证每一步都真的发一次 HTTP
    $env:PM_EVAL_CACHE          = '0'
    $env:PM_TARGET_CACHE        = '0'
    # 结构化输出方法基线：空 = 用 langchain 默认（json_schema）；场景 3 会临时覆盖
    $env:PM_STRUCT_METHOD       = ''
    # 与 .sh 对齐（A11）：桩评委固定给 8 分上下，阈值不调高则首轮就 passed，
    # revise / 早停 / 迭代上限分支在桩 e2e 里 0 覆盖
    $env:PM_PASS_THRESHOLD      = '9.5'
    # 桩 e2e 验的是“客户端 → HTTP → 解析 → 校验”链路，不是统计置信度；
    # 采样固定为 1，否则每条用例×每个评委都要再打一轮桓，又慢又没新信息
    $env:PM_SAMPLES_PER_CASE    = '1'
    # 基线与成对盲评保持开启：两个新节点同样需要真实 HTTP 覆盖
    $env:PM_BASELINE            = '1'
    $env:PM_PAIRWISE            = '1'
    return $base
}

# --------------------------------------------------------------------------
# 单场景：起桩 → 跑 → 断言 → 收报告路径
# --------------------------------------------------------------------------
function Invoke-Scenario {
    param(
        [string]$Tag,
        [string]$Title,
        [string[]]$StubArgs,
        [string]$ExpectChannel,
        [bool]$ForbidFallback,
        [string]$StructMethod = ''   # 非空时设置 PM_STRUCT_METHOD（A10 场景 3）
    )
    Write-Head $Title
    $base = Set-PmEnv -Port $Port
    if ($StructMethod) { $env:PM_STRUCT_METHOD = $StructMethod }
    Write-Line "端口 $Port  配置自检（各角色 base_url 都应是桩）："
    Invoke-ConfigCheck -BaseUrl $base

    $stub = $null
    try {
        Write-Line ''
        Write-Line "启动桩服务：openai_stub_server.py $($StubArgs -join ' ')"
        $stub = Start-Stub -Tag $Tag -Port $Port -StubArgs $StubArgs
        $health = Wait-StubReady -Port $Port
        Write-Line "桩服务就绪 pid=$($stub.Proc.Id) support_tools=$($health.support_tools)"

        $res = Invoke-Run -Tag $Tag
        Write-Line ''
        Write-Line "---- 关键日志（完整日志：$($res.Log)）----"
        (Select-KeyLine -Lines $res.Lines) | ForEach-Object { Write-Line "  $_" }

        $text = $res.Lines -join "`n"
        $reportPath = ''
        if ($text -match '报告已保存：(.+)$') { $reportPath = $matches[1].Trim() }
        $runId = ''
        if ($text -match 'run_id:\s*([0-9a-f]+)') { $runId = $matches[1] }
        $chanHits = @([regex]::Matches($text, "channel=$ExpectChannel")).Count
        $fallbackHits = @([regex]::Matches($text, '降级为 JSON 文本解析')).Count
        $hardFail = $text -match '结构化输出失败|Traceback|RuntimeError'

        # 退出码协议（feat(cli) 起）：0=达标、1=未达标但已交付——桩端点的评分行为
        # 不代表真实水位，e2e 验证的是链路（channel 命中 + 报告完整），两者都算跑通；
        # 2/3（参数错/运行失败）才是真失败。
        $ok = ($res.ExitCode -in @(0, 1)) -and ($chanHits -gt 0) -and (-not $hardFail)
        if ($ForbidFallback) { $ok = $ok -and ($fallbackHits -eq 0) }
        else { $ok = $ok -and ($fallbackHits -gt 0) }

        # 测量层必须真的跑过：只看 exit code 会漏掉“节点被静默跳过”
        $reportText = ''
        if ($reportPath -and (Test-Path -LiteralPath $reportPath)) {
            $reportText = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8
        }
        $missMeasure = @()
        foreach ($sec in @('与基线对比', '成对盲评', '置信度与采样噪声')) {
            if ($reportText -notmatch [regex]::Escape($sec)) { $missMeasure += $sec }
        }
        if ($missMeasure.Count -gt 0) {
            $ok = $false
            Write-Line "⚠ 报告缺少测量层小节：$($missMeasure -join ' / ')"
        }

        Write-Line ''
        Write-Line "判定：channel=$ExpectChannel 命中 $chanHits 次；降级告警 $fallbackHits 次；exit=$($res.ExitCode) → $(if ($ok) {'PASS'} else {'FAIL'})"
        if (-not $ok) {
            Write-Line '---- 末尾 40 行（定位用）----'
            ($res.Lines | Select-Object -Last 40) | ForEach-Object { Write-Line "  $_" }
            if (Test-Path -LiteralPath $stub.Err) {
                Write-Line "---- 桩服务 stderr ----"
                (Get-Content -LiteralPath $stub.Err -Encoding UTF8 -Tail 20 -ErrorAction SilentlyContinue) |
                    ForEach-Object { Write-Line "  $_" }
            }
        }
        return [pscustomobject]@{
            Tag            = $Tag
            Title          = $Title
            Pass           = $ok
            RunId          = $runId
            ExitCode       = $res.ExitCode
            Channel        = $ExpectChannel
            ChannelHits    = $chanHits
            FallbackHits   = $fallbackHits
            Log            = $res.Log
            Report         = $reportPath
        }
    } finally {
        Stop-Stub -Stub $stub
        Remove-Item Env:PM_STRUCT_METHOD -ErrorAction SilentlyContinue
        Write-Line "桩服务已清理（pid=$($stub.Proc.Id)）"
    }
}

# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
$Port = Get-FreePort -Start $Port
Write-Head "PromptMaster 本地 e2e 桩服务联调  (port=$Port)"
Write-Line "项目根  ：$ProjectRoot"
Write-Line "解释器  ：$PythonExe"
Write-Line "工作目录：$WorkDir"
Write-Line "命令    ：run.py --task `"$Task`" --target-model stub-model --cases $Cases --max-iter $MaxIter"

$results = @()
if ($Scenario -eq 0 -or $Scenario -eq 1) {
    $results += Invoke-Scenario -Tag 'uncooperative' `
        -Title '场景 1：端点返回带围栏的 JSON —— 应自动降级为文本解析（channel=json_fallback）' `
        -StubArgs @('--uncooperative') `
        -ExpectChannel 'json_fallback' `
        -ForbidFallback $false
}
if ($Scenario -eq 0 -or $Scenario -eq 2) {
    if ($results.Count) { Start-Sleep -Milliseconds 500 }
    $results += Invoke-Scenario -Tag 'tools' `
        -Title '场景 2：端点支持结构化输出 —— 应走原生 json_schema 通道（channel=structured_output，无降级告警）' `
        -StubArgs @('--tools') `
        -ExpectChannel 'structured_output' `
        -ForbidFallback $true
}
if ($Scenario -eq 0 -or $Scenario -eq 3) {
    if ($results.Count) { Start-Sleep -Milliseconds 500 }
    $results += Invoke-Scenario -Tag 'function_calling' `
        -Title '场景 3：PM_STRUCT_METHOD=function_calling —— 应走 function-calling 通道（channel=function_calling）' `
        -StubArgs @('--tools') `
        -ExpectChannel 'function_calling' `
        -ForbidFallback $true `
        -StructMethod 'function_calling'
}

Write-Head '总览'
foreach ($r in $results) {
    Write-Line ("  [{0}] {1}" -f $(if ($r.Pass) { 'PASS' } else { 'FAIL' }), $r.Title)
    Write-Line "        run_id=$($r.RunId)  exit=$($r.ExitCode)  channel=$($r.Channel)×$($r.ChannelHits)  降级=$($r.FallbackHits)"
    Write-Line "        报告：$($r.Report)"
    Write-Line "        日志：$($r.Log)"
}
Write-Line ''
Write-Line "报告与运行日志（run_id 级 JSON）另见：$(Join-Path $ProjectRoot 'logs')"

$failed = @($results | Where-Object { -not $_.Pass })
exit $(if ($failed.Count) { 1 } else { 0 })
