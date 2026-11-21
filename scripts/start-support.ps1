[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$appRoot = Join-Path $projectRoot 'app'
$runRoot = Join-Path $projectRoot '.run'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$viteScript = Join-Path $appRoot 'node_modules\vite\bin\vite.js'
$apiUrl = 'http://127.0.0.1:7101/api/bootstrap'
$frontendUrl = 'http://127.0.0.1:7100/'
$script:ownedProcesses = New-Object 'System.Collections.Generic.List[object]'

function Test-HttpEndpoint {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        $response = Invoke-WebRequest -Uri $Url -Method Get -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 400
    }
    catch {
        if ($null -ne $_.Exception.Response) {
            return $true
        }
        return $false
    }
}

function Get-FullBotProcesses {
    $processes = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'"
    return @($processes | Where-Object {
        $_.CommandLine -and $_.CommandLine -match '(?i)(?:^|\s)-m\s+support_bot(?:\s|$)'
    })
}

function Wait-CimProcess {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while ([DateTime]::UtcNow -lt $deadline) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
        if ($null -ne $process -and -not [string]::IsNullOrWhiteSpace($process.CommandLine)) {
            return $process
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Could not read CIM metadata for PID $ProcessId."
}

function Start-OwnedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Role,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $stdoutPath = Join-Path $runRoot "$Role.stdout.log"
    $stderrPath = Join-Path $runRoot "$Role.stderr.log"
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru

    $record = [PSCustomObject]@{
        Process = $process
        Metadata = $null
        MetadataPath = Join-Path $runRoot "$Role.json"
        StdoutPath = $stdoutPath
        StderrPath = $stderrPath
    }
    $script:ownedProcesses.Add($record)

    $cimProcess = Wait-CimProcess -ProcessId $process.Id
    $metadata = [ordered]@{
        Role = $Role
        ProcessId = $process.Id
        ExecutablePath = $cimProcess.ExecutablePath
        CommandLine = $cimProcess.CommandLine
        StartTime = $cimProcess.CreationDate.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
        StdoutPath = $stdoutPath
        StderrPath = $stderrPath
    }
    $metadata | ConvertTo-Json | Set-Content -LiteralPath $record.MetadataPath -Encoding UTF8
    $record.Metadata = [PSCustomObject]$metadata
    Write-Host "Started $Role with PID $($process.Id). Logs: $stdoutPath; $stderrPath"
    return $record
}

function Wait-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [object]$ProcessRecord = $null
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -ne $ProcessRecord -and $ProcessRecord.Process.HasExited) {
            throw "$Name exited during startup. See $($ProcessRecord.StderrPath)."
        }
        if (Test-HttpEndpoint -Url $Url) {
            Write-Host "$Name is ready: $Url"
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Timed out waiting for $Name at $Url."
}

function Stop-OwnedProcess {
    param([Parameter(Mandatory = $true)][object]$Record)

    $processId = [int]$Record.Process.Id
    if ($Record.Process.HasExited) {
        return
    }
    if ($null -eq $Record.Metadata) {
        Write-Warning "Not stopping PID ${processId}: launcher metadata is unavailable."
        return
    }

    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -eq $current) {
        return
    }

    $expectedPath = [string]$Record.Metadata.ExecutablePath
    $expectedCommandLine = [string]$Record.Metadata.CommandLine
    if ([string]::IsNullOrWhiteSpace($expectedPath) -or [string]::IsNullOrWhiteSpace($expectedCommandLine)) {
        Write-Warning "Not stopping PID ${processId}: incomplete launcher metadata."
        return
    }
    if (-not [string]::Equals($expectedPath, [string]$current.ExecutablePath, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Warning "Not stopping PID ${processId}: executable path changed."
        return
    }
    if (-not [string]::Equals($expectedCommandLine, [string]$current.CommandLine, [StringComparison]::Ordinal)) {
        Write-Warning "Not stopping PID ${processId}: command line changed."
        return
    }

    try {
        $expectedStart = [DateTime]::Parse(
            [string]$Record.Metadata.StartTime,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
        if ([Math]::Abs(($current.CreationDate - $expectedStart).TotalSeconds) -gt 2) {
            Write-Warning "Not stopping PID ${processId}: start time does not match."
            return
        }
    }
    catch {
        Write-Warning "Not stopping PID ${processId}: start time could not be verified."
        return
    }

    if ($Record.Metadata.Role -eq 'bot') {
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $processId" -ErrorAction SilentlyContinue
        foreach ($child in @($children)) {
            if ($child.Name -in @('python.exe', 'pythonw.exe') -and
                $child.CommandLine -match '(?i)(?:^|\s)-m\s+support_bot(?:\s|$)') {
                Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped launcher-owned $($Record.Metadata.Role) process (PID $processId)."
}

Set-Location -LiteralPath $projectRoot
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    [Console]::Error.WriteLine("Missing Python interpreter: $pythonPath")
    exit 1
}

$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if ($null -eq $nodeCommand) {
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
}
if ($null -eq $nodeCommand -or [string]::IsNullOrWhiteSpace($nodeCommand.Source)) {
    [Console]::Error.WriteLine('Node.js was not found in PATH.')
    exit 1
}
$nodePath = $nodeCommand.Source

if (-not (Test-Path -LiteralPath $viteScript -PathType Leaf)) {
    [Console]::Error.WriteLine("Missing Vite entry point: $viteScript. Run npm install in app first.")
    exit 1
}

$botRecord = $null
$viteRecord = $null
$exitCode = 0

try {
    $existingBots = @(Get-FullBotProcesses)
    if ($existingBots.Count -gt 0) {
        Write-Host "Full support bot is already running (PID $($existingBots[0].ProcessId)); not starting another one."
    }
    else {
        $botRecord = Start-OwnedProcess -Role 'bot' -FilePath $pythonPath `
            -Arguments @('-m', 'support_bot') -WorkingDirectory $projectRoot
    }

    Wait-HttpEndpoint -Name 'Mini App API' -Url $apiUrl -TimeoutSeconds 45 -ProcessRecord $botRecord

    if (Test-HttpEndpoint -Url $frontendUrl) {
        Write-Host 'Frontend is already running; not starting a second Vite process.'
    }
    else {
        $quotedViteScript = '"' + $viteScript + '"'
        $viteRecord = Start-OwnedProcess -Role 'vite' -FilePath $nodePath `
            -Arguments @($quotedViteScript, '--host', '127.0.0.1', '--port', '7100', '--strictPort') `
            -WorkingDirectory $appRoot
        Wait-HttpEndpoint -Name 'Vite' -Url $frontendUrl -TimeoutSeconds 30 -ProcessRecord $viteRecord
    }

    Start-Process 'http://localhost:7100/' | Out-Null
    Write-Host 'Opened http://localhost:7100/.'

    if ($null -ne $viteRecord) {
        Write-Host 'Press Ctrl+C to stop processes started by this launcher.'
        while (-not $viteRecord.Process.HasExited) {
            if ($null -ne $botRecord -and $botRecord.Process.HasExited) {
                throw "Support bot exited with code $($botRecord.Process.ExitCode). See $($botRecord.StderrPath)."
            }
            Start-Sleep -Seconds 1
            $viteRecord.Process.Refresh()
        }
        if ($viteRecord.Process.ExitCode -ne 0) {
            throw "Vite exited with code $($viteRecord.Process.ExitCode). See $($viteRecord.StderrPath)."
        }
    }
    elseif ($null -ne $botRecord) {
        Write-Host 'Press Ctrl+C to stop the bot started by this launcher.'
        while (Test-HttpEndpoint -Url $frontendUrl) {
            Start-Sleep -Seconds 1
        }
    }
}
catch {
    $exitCode = 1
    [Console]::Error.WriteLine("Launcher error: $($_.Exception.Message)")
}
finally {
    for ($index = $script:ownedProcesses.Count - 1; $index -ge 0; $index--) {
        Stop-OwnedProcess -Record $script:ownedProcesses[$index]
    }
}

exit $exitCode
