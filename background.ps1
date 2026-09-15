param(
    [ValidateSet('Install', 'Remove', 'Start', 'Stop', 'Status')]
    [string]$Action = 'Status'
)
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$digest = [System.Security.Cryptography.SHA256]::Create()
try {
    $hashBytes = $digest.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))
    $suffix = ([System.BitConverter]::ToString($hashBytes)).Replace('-', '').Substring(0, 8)
} finally { $digest.Dispose() }
$taskName = "TelegramForwardX-$suffix"
$pythonPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$runnerPath = Join-Path $projectRoot 'run_background.py'

switch ($Action) {
    'Install' {
        if (-not (Test-Path -LiteralPath $pythonPath)) {
            throw 'Create the .venv environment and install requirements.txt first.'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'meme_bot_session.session'))) {
            throw 'Complete Telegram login with load_Api_x.py before enabling background startup.'
        }
        $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $taskAction = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"' + $runnerPath + '"') -WorkingDirectory $projectRoot
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentIdentity
        $principal = New-ScheduledTaskPrincipal -UserId $currentIdentity -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger `
            -Principal $principal -Settings $settings -Description 'Resume saved TelegramForwardX deliveries after user login.' -Force | Out-Null
        Write-Output "Installed $taskName. It starts after your next login; use -Action Start to start now."
    }
    'Remove' {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output 'Removed startup task. Saved deliveries are retained.'
    }
    'Start' { Start-ScheduledTask -TaskName $taskName }
    'Stop' { Stop-ScheduledTask -TaskName $taskName }
    'Status' {
        Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
        Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime, LastTaskResult, NextRunTime
    }
}
