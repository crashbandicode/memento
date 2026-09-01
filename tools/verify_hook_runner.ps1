<#!
.SYNOPSIS
Exercises the frozen hook runner's governor dispatch with synthetic data.

.DESCRIPTION
Creates temporary JSONL transcripts with deliberately non-real session data,
then verifies the fail-open below-threshold path and the above-threshold
operator-controlled advisory. No user transcript is read.
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
    $sessionId = "synthetic-functional-" + [Guid]::NewGuid().ToString("N")
    $belowTranscript = Join-Path $root "below.jsonl"
    $aboveTranscript = Join-Path $root "above.jsonl"
    $belowRecord = '{"type":"assistant","message":{"usage":{"cache_read_input_tokens":0,"cache_creation_input_tokens":7,"input_tokens":0}}}'
    $aboveRecord = '{"type":"assistant","message":{"usage":{"cache_read_input_tokens":0,"cache_creation_input_tokens":10,"input_tokens":0}}}'
    [System.IO.File]::WriteAllText(
        $belowTranscript,
        $belowRecord + [Environment]::NewLine
    )
    [System.IO.File]::WriteAllText(
        $aboveTranscript,
        $aboveRecord + [Environment]::NewLine
    )

    $env:MEMENTO_GOVERNOR_HYGIENE_TOKENS = "8"
    $env:MEMENTO_GOVERNOR_HANDOFF_TOKENS = "9"
    $env:MEMENTO_GOVERNOR_REMINDER_TOKENS = "10"
    $belowPayload = (@{
        hook_event_name = "PostToolUse"
        session_id = $sessionId
        cwd = $root
        transcript_path = $belowTranscript
    } | ConvertTo-Json -Compress)
    $abovePayload = (@{
        hook_event_name = "PostToolUse"
        session_id = $sessionId
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
    $belowDecision = $belowOutput | ConvertFrom-Json
    $aboveDecision = $aboveOutput | ConvertFrom-Json

    if (@($belowDecision.PSObject.Properties).Count -ne 0) {
        throw "Below-threshold governor payload unexpectedly produced: $belowOutput"
    }
    if (
        $null -ne $aboveDecision.PSObject.Properties["decision"] -and
        $aboveDecision.decision -eq "block"
    ) {
        throw "Above-threshold governor payload still blocks: $aboveOutput"
    }
    $context = $aboveDecision.hookSpecificOutput.additionalContext
    if (
        $context -notlike "*Operator-controlled handoff reminder*" -or
        $context -notlike "*Continue executing the active task*" -or
        $context -notlike "*every final response*"
    ) {
        throw "Above-threshold governor payload lacks advisory policy: $aboveOutput"
    }
    [pscustomobject]@{
        below_threshold_stdout = $belowOutput
        above_threshold_stdout = $aboveOutput
    } | ConvertTo-Json -Compress
}
finally {
    Remove-Item -LiteralPath $root -Recurse -Force
}
