# 2026-10-26 竞品对照：GitHub 实测抓取脚本（无 token，匿名 REST API）
# 产出：data/*.json（搜索原始结果）、data/repos-*.json（单仓坐标）
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$data = Join-Path $root "data"
New-Item -ItemType Directory -Force -Path $data | Out-Null

$UA = @{ "User-Agent" = "pm-competitor-research"; "Accept" = "application/vnd.github+json" }

function Get-Json($url, $out) {
  for ($i = 1; $i -le 4; $i++) {
    try {
      $r = Invoke-RestMethod -Uri $url -Headers $UA -TimeoutSec 60
      ($r | ConvertTo-Json -Depth 6) | Set-Content -Path (Join-Path $data $out) -Encoding utf8
      Write-Output "OK   $out"
      return $true
    } catch {
      $msg = $_.Exception.Message
      Write-Output "FAIL($i) $out :: $msg"
      Start-Sleep -Seconds 8
    }
  }
  return $false
}

# 搜索：每条最多 100 条结果，含 stargazers_count/pushed_at/license
$searches = @(
  @{ f = "s1-prompt-optimization.json";  q = "prompt+optimization+in%3Aname%2Cdescription%2Creadme+stars%3A%3E500&sort=stars&order=desc" },
  @{ f = "s2-prompt-engineering.json";   q = "prompt+engineering+in%3Aname%2Cdescription+stars%3A%3E300&sort=stars&order=desc" },
  @{ f = "s3-llm-evaluation.json";       q = "llm+evaluation+in%3Aname%2Cdescription%2Creadme+stars%3A%3E500&sort=stars&order=desc" },
  @{ f = "s4-evals-benchmark.json";      q = "evals+benchmark+in%3Aname%2Cdescription+stars%3A%3E300&sort=stars&order=desc" },
  @{ f = "s5-judge.json";                q = "%22llm-as-a-judge%22+in%3Areadme+stars%3A%3E100&sort=stars&order=desc" },
  @{ f = "s6-created-month.json";        q = "created%3A%3E2026-09-26+stars%3A%3E30&sort=stars&order=desc" },
  @{ f = "s7-created-week.json";         q = "created%3A%3E2026-10-19+stars%3A%3E5&sort=stars&order=desc" },
  @{ f = "s8-pushed-week-hot.json";      q = "pushed%3A%3E2026-10-19+stars%3A%3E2000+topic%3Allm&sort=stars&order=desc" },
  @{ f = "s9-topic-prompt-opt.json";     q = "topic%3Aprompt-optimization+stars%3A%3E50&sort=stars&order=desc" },
  @{ f = "s10-topic-llm-eval.json";      q = "topic%3Allm-evaluation+stars%3A%3E200&sort=stars&order=desc" },
  @{ f = "s11-context-engineering.json"; q = "context+engineering+in%3Aname%2Cdescription%2Creadme+stars%3A%3E300&sort=stars&order=desc" },
  @{ f = "s12-agent-skills.json";        q = "%22agent+skills%22+in%3Aname%2Cdescription%2Creadme+stars%3A%3E100&sort=stars&order=desc" },
  @{ f = "s13-prompt-cli-agent.json";    q = "prompt+in%3Aname+cli+in%3Adescription+stars%3A%3E200&sort=stars&order=desc" },
  @{ f = "s14-prompt-lint.json";         q = "prompt+lint+OR+prompt+guard+in%3Aname%2Cdescription+stars%3A%3E100&sort=stars&order=desc" }
)

foreach ($s in $searches) {
  $u = "https://api.github.com/search/repositories?q=" + $s.q + "&per_page=100"
  [void](Get-Json $u $s.f)
  Start-Sleep -Seconds 7   # 匿名搜索限额 10 次/分钟
}

# 单仓坐标：基线清单 + 本轮新入榜候选（可续加）
$repos = @(
  "stanfordnlp/dspy", "linshenkx/prompt-optimizer", "promptfoo/promptfoo",
  "confident-ai/deepeval", "openai/evals", "gepa-ai/gepa", "comet-ml/opik",
  "langfuse/langfuse", "Arize-ai/phoenix", "microsoft/PromptWizard",
  "zou-group/textgrad", "UKGovernmentBEIS/inspect_ai", "meta-llama/prompt-ops",
  "Kiln-AI/Kiln", "fengxingxuerong/prompt-master"
)
foreach ($r in $repos) {
  [void](Get-Json ("https://api.github.com/repos/" + $r) ("repo-" + ($r -replace "/", "_") + ".json"))
  Start-Sleep -Seconds 2
}

Write-Output "=== rate_limit ==="
Get-Json "https://api.github.com/rate_limit" "rate-limit.json"
Write-Output "DONE"