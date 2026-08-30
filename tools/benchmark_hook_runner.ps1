<#!
.SYNOPSIS
Measures a Claude hook executable with waited-process timings.

.DESCRIPTION
Uses ProcessStartInfo with redirected stdin and WaitForExit, matching the
production measurement method.  The realistic-shaped payload deliberately
points to a nonexistent transcript and uses a synthetic session id.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Executable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Start-HookInvocation {
    param(
        [string[]]$Arguments,
        [string]$Payload
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Executable
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    foreach ($argument in $Arguments) {
        [void]$startInfo.ArgumentList.Add($argument)
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    if (-not $process.Start()) {
        throw "Could not start $Executable"
    }
    $process.StandardInput.Write($Payload)
    $process.StandardInput.Close()
    return [pscustomobject]@{ Process = $process; Stopwatch = $stopwatch }
}

function Complete-HookInvocation {
    param($Invocation)

    $Invocation.Process.WaitForExit()
    $Invocation.Stopwatch.Stop()
    $stdout = $Invocation.Process.StandardOutput.ReadToEnd()
    $stderr = $Invocation.Process.StandardError.ReadToEnd()
    $result = [pscustomobject]@{
        milliseconds = [math]::Round($Invocation.Stopwatch.Elapsed.TotalMilliseconds, 2)
        exit_code = $Invocation.Process.ExitCode
        stdout = $stdout.Trim()
        stderr = $stderr.Trim()
    }
    $Invocation.Process.Dispose()
    return $result
}

$payloads = [ordered]@{
    empty = "{}"
    realistic_nonexistent_transcript = (@{
        hook_event_name = "PostToolUse"
        session_id = "synthetic-benchmark-session-not-real"
        transcript_path = (Join-Path $env:TEMP "memento-nonexistent-synthetic.jsonl")
        cwd = (Join-Path $env:TEMP "memento-benchmark-nonexistent")
        tool_name = "PowerShell"
        tool_input = @{ command = "Write-Output synthetic" }
    } | ConvertTo-Json -Depth 5 -Compress)
}
$commands = [ordered]@{
    claude_hook = @("claude-hook")
    claude_governor_hook_enabled = @("claude-governor-hook", "--enabled")
}

# The production baseline is warm. Prime each command once so the reported
# waited-process measurements exclude its first import and image-cache load.
foreach ($command in $commands.GetEnumerator()) {
    $warmupInvocation = Start-HookInvocation -Arguments $command.Value -Payload $payloads.empty
    $warmup = Complete-HookInvocation $warmupInvocation
    if ($warmup.exit_code -ne 0) {
        throw "Warmup failed for $($command.Key): $($warmup.stderr)"
    }
}

$rows = foreach ($command in $commands.GetEnumerator()) {
    foreach ($payload in $payloads.GetEnumerator()) {
        foreach ($concurrency in 1, 3) {
            $invocations = @(
                1..$concurrency | ForEach-Object {
                    Start-HookInvocation -Arguments $command.Value -Payload $payload.Value
                }
            )
            $results = @($invocations | ForEach-Object { Complete-HookInvocation $_ })
            [pscustomobject]@{
                executable = $Executable
                command = $command.Key
                payload = $payload.Key
                concurrency = $concurrency
                timings_ms = @($results | ForEach-Object { $_.milliseconds })
                max_ms = [math]::Round(($results | Measure-Object milliseconds -Maximum).Maximum, 2)
                exit_codes = @($results | ForEach-Object { $_.exit_code })
                stdout = @($results | ForEach-Object { $_.stdout })
                stderr = @($results | ForEach-Object { $_.stderr })
            }
        }
    }
}

$rows | ConvertTo-Json -Depth 5
