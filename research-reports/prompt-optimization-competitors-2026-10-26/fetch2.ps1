# 2026-10-26 round 2: weekly trending list + key READMEs (raw.githubusercontent, no API quota used)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$raw  = Join-Path $root "raw"
New-Item -ItemType Directory -Force -Path $raw | Out-Null
$UA = "Mozilla/5.0 (pm-competitor-research)"

function Save-Url($url, $out) {
  try {
    Invoke-WebRequest -Uri $url -OutFile $out -Headers @{ "User-Agent" = $UA } -TimeoutSec 60 -UseBasicParsing
    $len = (Get-Item $out).Length
    Write-Output ("OK   {0,9}  {1}" -f $len, (Split-Path $out -Leaf))
  } catch {
    Write-Output ("FAIL {0} :: {1}" -f (Split-Path $out -Leaf), $_.Exception.Message)
  }
}

Save-Url "https://mshibanami.github.io/GitHubTrendingRSS/weekly/all.xml" (Join-Path $raw "trending-weekly.xml")
Save-Url "https://mshibanami.github.io/GitHubTrendingRSS/weekly/python.xml" (Join-Path $raw "trending-weekly-python.xml")

$repos = @(
  "ifixai-ai/iFixAi",
  "Q00/ouroboros",
  "sutro-sh/jev-align",
  "zgcagi/Harness-RSI",
  "Tencent/AI-Infra-Guard",
  "ckelsoe/prompt-architect",
  "hashgraph-online/hol-guard",
  "Nanako0129/pilotfish",
  "cloudflare/security-audit-skill",
  "affaan-m/ECC",
  "linshenkx/prompt-optimizer",
  "gepa-ai/gepa",
  "promptfoo/promptfoo",
  "langfuse/langfuse",
  "VoltAgent/awesome-agent-skills",
  "Giskard-AI/giskard-oss",
  "truera/trulens",
  "NVIDIA/garak"
)
foreach ($r in $repos) {
  $name = ($r -replace "/", "__") + ".md"
  $ok = $false
  foreach ($br in @("HEAD", "main", "master")) {
    $url = "https://raw.githubusercontent.com/$r/$br/README.md"
    try {
      Invoke-WebRequest -Uri $url -OutFile (Join-Path $raw $name) -Headers @{ "User-Agent" = $UA } -TimeoutSec 60 -UseBasicParsing
      Write-Output ("OK   {0,9}  {1}" -f (Get-Item (Join-Path $raw $name)).Length, $name)
      $ok = $true
      break
    } catch { }
  }
  if (-not $ok) { Write-Output "FAIL $name" }
}

Write-Output "=== trending weekly titles ==="
try {
  [xml]$x = Get-Content (Join-Path $raw "trending-weekly.xml") -Raw
  $i = 0
  foreach ($item in $x.rss.channel.item) {
    $i++
    Write-Output ("{0,3}. {1}" -f $i, $item.title)
  }
} catch { Write-Output "parse fail: $($_.Exception.Message)" }

Write-Output "=== trending weekly python titles ==="
try {
  [xml]$y = Get-Content (Join-Path $raw "trending-weekly-python.xml") -Raw
  $i = 0
  foreach ($item in $y.rss.channel.item) {
    $i++
    Write-Output ("{0,3}. {1}" -f $i, $item.title)
  }
} catch { Write-Output "parse fail: $($_.Exception.Message)" }
Write-Output "DONE"