<#!
.SYNOPSIS
Exercises the frozen hook runner's governor dispatch with synthetic data.

.DESCRIPTION
Creates temporary JSONL transcripts with deliberately non-real session data,
then verifies the fail-open below-threshold path and the above-threshold Stop
decision.  No user transcript or MEMENTO_HANDOFF.md is read.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Executable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-GovernorHook {
    param([string]$Payload)

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Executable
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    [void]$startInfo.ArgumentList.Add("claude-governor-hook")
    [void]$startInfo.ArgumentList.Add("--enabled")
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Could not start $Executable"
    }
    $process.StandardInput.Write($Payload)
    $process.StandardInput.Close()
    $process.WaitForExit()
    $result = [pscustomobject]@{
        exit_code = $process.ExitCode
        stdout = $process.StandardOutput.ReadToEnd().Trim()
        stderr = $process.StandardError.ReadToEnd().Trim()
    }
    $process.Dispose()
    return $result
}

$root = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("memento-hook-functional-" + [Guid]::NewGuid().ToString("N"))
[System.IO.Directory]::CreateDirectory($root) | Out-Null
try {
    $belowTranscript = Join-Path $root "below.jsonl"
    $aboveTranscript = Join-Path $root "above.jsonl"
    $belowRecord = '{"type":"assistant","message":{"usage":{"cache_read_input_tokens":0,"cache_creation_input_tokens":9,"input_tokens":0}}}'
    $aboveRecord = '{"type":"assistant","message":{"usage":{"cache_read_input_tokens":0,"cache_creation_input_tokens":10,"input_tokens":0}}}'
    [System.IO.File]::WriteAllText(
        $belowTranscript,
        $belowRecord + [Environment]::NewLine
    )
    [System.IO.File]::WriteAllText(
        $aboveTranscript,
        $aboveRecord + [Environment]::NewLine
    )

    $env:MEMENTO_GOVERNOR_HYGIENE_TOKENS = "10"
    $env:MEMENTO_GOVERNOR_HANDOFF_TOKENS = "10"
    $env:MEMENTO_GOVERNOR_BLOCK_TOKENS = "10"
    $belowPayload = (@{
        hook_event_name = "Stop"
        session_id = "synthetic-functional-session-not-real"
        cwd = $root
        transcript_path = $belowTranscript
    } | ConvertTo-Json -Compress)
    $abovePayload = (@{
        hook_event_name = "Stop"
        session_id = "synthetic-functional-session-not-real"
        cwd = $root
        transcript_path = $aboveTranscript
    } | ConvertTo-Json -Compress)
    $belowResult = Invoke-GovernorHook $belowPayload
    $aboveResult = Invoke-GovernorHook $abovePayload
    $belowOutput = $belowResult.stdout
    $aboveOutput = $aboveResult.stdout
    if ($belowResult.exit_code -ne 0 -or $aboveResult.exit_code -ne 0) {
        throw "Governor runner exited nonzero: below=$($belowResult | ConvertTo-Json -Compress); above=$($aboveResult | ConvertTo-Json -Compress)"
    }
    if (-not $aboveOutput) {
        throw "Above-threshold governor payload produced no output: $($aboveResult | ConvertTo-Json -Compress)"
    }
    $aboveDecision = $aboveOutput | ConvertFrom-Json

    if ($belowOutput) {
        throw "Below-threshold governor payload unexpectedly produced: $belowOutput"
    }
    if ($aboveDecision.decision -ne "block") {
        throw "Above-threshold governor payload did not block: $aboveOutput"
    }
    [pscustomobject]@{
        below_threshold_stdout = $belowOutput
        above_threshold_stdout = $aboveOutput
    } | ConvertTo-Json -Compress
}
finally {
    Remove-Item -LiteralPath $root -Recurse -Force
}
