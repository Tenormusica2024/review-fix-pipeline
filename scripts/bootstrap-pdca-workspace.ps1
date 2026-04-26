[CmdletBinding()]
param(
    [string]$WorkspaceRoot = (Get-Location).Path,
    [string]$ReviewFixPipelinePath = "",
    [string]$ClaudeReviewPdcaPath = "",
    [switch]$SetUserEnv
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Resolve-RepoPath {
    param(
        [string]$Explicit,
        [string]$FallbackName
    )

    if ($Explicit) {
        return (Resolve-Path -LiteralPath $Explicit).Path
    }

    $workspaceResolved = (Resolve-Path -LiteralPath $WorkspaceRoot).Path
    if ((Split-Path -Leaf $workspaceResolved) -eq $FallbackName) {
        return $workspaceResolved
    }

    $candidate = Join-Path $workspaceResolved $FallbackName
    if (Test-Path -LiteralPath $candidate) {
        return (Resolve-Path -LiteralPath $candidate).Path
    }

    return $null
}

function Test-CommandExists {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

$rfp = Resolve-RepoPath -Explicit $ReviewFixPipelinePath -FallbackName "review-fix-pipeline"
$pdca = Resolve-RepoPath -Explicit $ClaudeReviewPdcaPath -FallbackName "claude-review-pdca"

Write-Host "== PDCA workspace bootstrap stub ==" -ForegroundColor Cyan
Write-Host "WorkspaceRoot: $WorkspaceRoot"
Write-Host ""

if (-not (Test-CommandExists python)) {
    Write-Warning "python was not found"
} else {
    python --version
}

if (-not (Test-CommandExists git)) {
    Write-Warning "git was not found"
} else {
    git --version
}

Write-Host ""
Write-Host "[Repo detection]"
Write-Host "review-fix-pipeline : $rfp"
Write-Host "claude-review-pdca  : $pdca"

if (-not $rfp) {
    Write-Warning "review-fix-pipeline was not found"
}
if (-not $pdca) {
    Write-Warning "claude-review-pdca was not found"
}

if ($pdca) {
    $env:CLAUDE_REVIEW_PDCA_ROOT = $pdca
    Write-Host ""
    Write-Host "[Session env]"
    Write-Host "CLAUDE_REVIEW_PDCA_ROOT=$($env:CLAUDE_REVIEW_PDCA_ROOT)"

    if ($SetUserEnv) {
        [Environment]::SetEnvironmentVariable("CLAUDE_REVIEW_PDCA_ROOT", $pdca, "User")
        Write-Host "Saved CLAUDE_REVIEW_PDCA_ROOT to User environment"
    }
}

Write-Host ""
Write-Host "[Next steps]" -ForegroundColor Green
Write-Host "1. Open review-fix-pipeline/docs/quickstart-from-fork.md"
Write-Host "2. Confirm sibling repo layout or CLAUDE_REVIEW_PDCA_ROOT"
Write-Host "3. Use the unified runner example below for the minimum flow"
Write-Host ""
Write-Host "python scripts/pdca_bridge_runner.py --kind output --input-file C:\tmp\review-output.md --reviewer sc-ifr --runtime codex --mode review-only --repo-root C:/path/to/actual-target-repo --forward-to-pdca"
Write-Host ""
Write-Host "This is a bootstrap stub. It does not fully automate hook registration or DB setup yet."
