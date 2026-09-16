<#
.SYNOPSIS
    Registers (or re-registers) the three Windows Task Scheduler entries
    that start and stop Jarvis automatically.

.DESCRIPTION
    These three tasks are the entire implementation of the "starts at
    login, quits on sleep, comes back on wake" behaviour that
    USER_GUIDE.md section 5 draws as a state diagram. Nothing in the
    Python code observes power events at all. They lived only in the live
    Task Scheduler database until now, which meant the documented
    behaviour had no backing artifact anywhere in the repo -- not
    reviewable, not reproducible on another machine, and not obviously
    the place to look when a wake-from-sleep left nothing running.

    The XML templates alongside this script carry {{USER}},
    {{JARVIS_DIR}} and {{WT_EXE}} placeholders so they aren't pinned to
    one machine's SID and paths. schtasks wants its XML as UTF-16, so
    each filled-in template is written to a temp file in that encoding
    before being handed over; the templates themselves stay UTF-8 so they
    diff cleanly.

.EXAMPLE
    .\scheduler\register_tasks.ps1
    .\scheduler\register_tasks.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Where Jarvis lives. Defaults to this script's parent directory, so
    # running it from a clone just works.
    [string] $JarvisDir = (Split-Path -Parent $PSScriptRoot),

    # Windows Terminal. The tasks launch `wt.exe -F -p Jarvis` rather than
    # python.exe directly, so the console comes up in its own theme.
    [string] $WtExe = "$env:LOCALAPPDATA\Microsoft\WindowsApps\wt.exe"
)

$ErrorActionPreference = 'Stop'

$tasks = [ordered]@{
    'Jarvis Voice Control'   = 'jarvis-voice-control.xml'
    'Jarvis Quit On Sleep'   = 'jarvis-quit-on-sleep.xml'
    'Jarvis Restart On Wake' = 'jarvis-restart-on-wake.xml'
}

$user = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $WtExe)) {
    Write-Warning "Windows Terminal not found at $WtExe -- registering anyway, but the logon/wake tasks will fail to launch until that path is right."
}
if (-not (Test-Path (Join-Path $JarvisDir '.venv\Scripts\python.exe'))) {
    Write-Warning "No venv interpreter under $JarvisDir -- the quit-on-sleep task will fail until there is one."
}

foreach ($name in $tasks.Keys) {
    $template = Join-Path $PSScriptRoot $tasks[$name]
    if (-not (Test-Path $template)) { throw "Missing template: $template" }

    $xml = (Get-Content -Raw -Encoding UTF8 $template).
        Replace('{{USER}}', $user).
        Replace('{{JARVIS_DIR}}', $JarvisDir).
        Replace('{{WT_EXE}}', $WtExe)

    if (-not $PSCmdlet.ShouldProcess($name, 'Register scheduled task')) { continue }

    # Unicode, not UTF8: schtasks /create /xml rejects anything else.
    # The declaration has to be rewritten to match, or schtasks fails with
    # "unable to switch the encoding" before it looks at anything else --
    # it trusts the declaration over the actual byte-order mark. The
    # templates stay UTF-8 on disk, which is what they genuinely are.
    $xml = $xml -replace '(?i)encoding="utf-8"', 'encoding="UTF-16"'
    $temp = [System.IO.Path]::GetTempFileName()
    try {
        Set-Content -Path $temp -Value $xml -Encoding Unicode
        # /F replaces an existing task of the same name, so this script is
        # safe to re-run after editing a template.
        schtasks /create /tn $name /xml $temp /f | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "schtasks failed for '$name' (exit $LASTEXITCODE)" }
        Write-Host "registered: $name"
    }
    finally {
        Remove-Item $temp -ErrorAction SilentlyContinue
    }
}

Write-Host ''
Write-Host 'Done. Check them with:  schtasks /query /tn "\Jarvis Voice Control"'
