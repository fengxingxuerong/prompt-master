# Round 6 (2026-10-27) anonymous GitHub REST snapshot. ASCII only on purpose:
# PowerShell 5.1 decodes BOM-less .ps1 as GBK, and CJK comments break parsing.
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$data = Join-Path $root "data"
New-Item -ItemType Directory -Force -Path $data | Out-Null
$UA = @{ "User-Agent" = "pm-competitor-research"; "Accept" = "application/vnd.github+json" }

function Get-Json($url, $out) {
  for ($i = 1; $i -le 3; $i++) {
    try {
      $r = Invoke-RestMethod -Uri $url -Headers $UA -TimeoutSec 60
      ($r | ConvertTo-Json -Depth 6) | Set-Content -Path (Join-Path $data $out) -Encoding utf8
      Write-Output "OK   $out"
      return $true
    } catch {
      Write-Output ("FAIL($i) " + $out + " :: " + $_.Exception.Message)
      Start-Sleep -Seconds 6
    }
  }
  return $false
}

# A. repo coordinates (same set as round 5, so deltas are comparable)
$repos = @(
  "stanfordnlp/dspy",
  "linshenkx/prompt-optimizer",
  "promptfoo/promptfoo",
  "confident-ai/deepeval",
  "openai/evals",
  "gepa-ai/gepa",
  "comet-ml/opik",
  "langfuse/langfuse",
  "Arize-ai/phoenix",
  "microsoft/PromptWizard",
  "zou-group/textgrad",
  "UKGovernmentBEIS/inspect_ai",
  "meta-llama/prompt-ops",
  "Kiln-AI/Kiln",
  "fengxingxuerong/prompt-master"
)
foreach ($r in $repos) {
  $name = "repo-" + ($r -replace "/", "_") + ".json"
  [void](Get-Json ("https://api.github.com/repos/" + $r) $name)
  Start-Sleep -Seconds 2
}

# B. new entrants flagged in round 5 (track their momentum)
$entrants = @(
  "ifixai-ai/iFixAi",
  "Q00/ouroboros",
  "Tencent/AI-Infra-Guard",
  "truera/trulens",
  "hashgraph-online/hol-guard",
  "sutro-sh/jev-align"
)
foreach ($r in $entrants) {
  $name = "repo-" + ($r -replace "/", "_") + ".json"
  [void](Get-Json ("https://api.github.com/repos/" + $r) $name)
  Start-Sleep -Seconds 2
}

# C. searches (anonymous search limit is 10/min; 7s apart)
$searches = @(
  @{ f = "s1-created-since-round5.json"; q = "created%3A%3E2026-10-19+stars%3A%3E5&sort=stars&order=desc" },
  @{ f = "s2-created-last-48h.json";     q = "created%3A%3E2026-10-25+stars%3A%3E3&sort=stars&order=desc" },
  @{ f = "s3-prompt-optimization.json";  q = "prompt+optimization+in%3Aname%2Cdescription%2Creadme+stars%3A%3E300&sort=stars&order=desc" },
  @{ f = "s4-llm-evaluation.json";       q = "llm+evaluation+in%3Aname%2Cdescription%2Creadme+stars%3A%3E500&sort=stars&order=desc" },
  @{ f = "s5-agent-skills.json";         q = "%22agent+skills%22+in%3Aname%2Cdescription%2Creadme+stars%3A%3E100&sort=stars&order=desc" },
  @{ f = "s6-judge-calibration.json";    q = "%22llm-as-a-judge%22+in%3Areadme+stars%3A%3E100&sort=stars&order=desc" },
  @{ f = "s7-pushed-this-week.json";     q = "pushed%3A%3E2026-10-20+stars%3A%3E2000+topic%3Allm&sort=stars&order=desc" }
)
foreach ($s in $searches) {
  $u = "https://api.github.com/search/repositories?q=" + $s.q + "&per_page=100"
  [void](Get-Json $u $s.f)
  Start-Sleep -Seconds 7
}

Write-Output "=== rate_limit ==="
Get-Json "https://api.github.com/rate_limit" "rate-limit.json"
Write-Output "DONE"