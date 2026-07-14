param(
    [string]$OutputRoot = $(Join-Path $PSScriptRoot ("corel_forensics_" + (Get-Date -Format "yyyyMMdd_HHmmss"))),
    [switch]$Cleanup,
    [switch]$WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log {
    param(
        [Parameter(Mandatory)] [string]$Message,
        [ValidateSet('INFO','WARN','ERROR','OK')] [string]$Level = 'INFO'
    )
    $line = "[{0}] {1}" -f $Level, $Message
    Write-Host $line
    Add-Content -LiteralPath $script:LogPath -Value $line
}

function Ensure-Dir {
    param([Parameter(Mandatory)] [string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Export-RegKey {
    param(
        [Parameter(Mandatory)] [string]$Key,
        [Parameter(Mandatory)] [string]$FileName
    )
    $dest = Join-Path $script:BackupDir $FileName
    & reg.exe export $Key $dest /y | Out-Null
    Write-Log "Exported $Key to $dest" 'OK'
}

function Test-CorelText {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $false }
    return ($Text -match 'Corel|Protexis|PaintShop|AfterShot|WinDVD|WordPerfect|MindManager')
}

function Get-UninstallEntries {
    $roots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    foreach ($root in $roots) {
        if (Test-Path $root) {
            Get-ChildItem -LiteralPath $root | ForEach-Object {
                try {
                    $p = Get-ItemProperty -LiteralPath $_.PSPath
                    if (Test-CorelText ($p.DisplayName + ' ' + $p.Publisher + ' ' + $p.InstallLocation + ' ' + $p.UninstallString)) {
                        [pscustomobject]@{
                            Source = 'Uninstall'
                            Hive = $root
                            KeyName = $_.PSChildName
                            DisplayName = $p.DisplayName
                            DisplayVersion = $p.DisplayVersion
                            Publisher = $p.Publisher
                            InstallLocation = $p.InstallLocation
                            UninstallString = $p.UninstallString
                            QuietUninstallString = $p.QuietUninstallString
                            WindowsInstaller = $p.WindowsInstaller
                            PSPath = $_.PSPath
                        }
                    }
                } catch {
                }
            }
        }
    }
}

function Get-InstallerUserDataMatches {
    $paths = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Installer\UserData'
    )
    foreach ($path in $paths) {
        if (Test-Path $path) {
            Get-ChildItem -LiteralPath $path -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
                try {
                    $name = $_.Name
                    $props = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
                    $text = $name + ' ' + ($props.PSObject.Properties.Value -join ' ')
                    if (Test-CorelText $text) {
                        [pscustomobject]@{
                            Source = 'InstallerUserData'
                            Path = $_.PSPath
                            Name = $name
                            PropertySummary = ($props.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join '; '
                        }
                    }
                } catch {
                }
            }
        }
    }
}

function Get-InstallerCacheMatches {
    $paths = @(
        (Join-Path $env:WINDIR 'Installer')
    )
    foreach ($path in $paths) {
        if (Test-Path $path) {
            Get-ChildItem -LiteralPath $path -File -ErrorAction SilentlyContinue | ForEach-Object {
                if ($_.Name -match '^\w+\.msi$|^\w+\.msp$') {
                    # File name alone isn't enough; keep as candidate only.
                    [pscustomobject]@{
                        Source = 'InstallerCache'
                        Path = $_.FullName
                        Length = $_.Length
                        LastWriteTime = $_.LastWriteTime
                    }
                }
            }
        }
    }
}

function Get-ServicingMatches {
    $svc = Get-CimInstance Win32_Service | Where-Object { Test-CorelText ($_.Name + ' ' + $_.DisplayName + ' ' + $_.PathName) }
    foreach ($s in $svc) {
        [pscustomobject]@{
            Source = 'Service'
            Name = $s.Name
            DisplayName = $s.DisplayName
            State = $s.State
            StartMode = $s.StartMode
            PathName = $s.PathName
        }
    }
}

function Get-FileSystemMatches {
    $roots = @(
        "$env:ProgramFiles\Common Files\Corel",
        "$env:ProgramFiles(x86)\Common Files\Corel",
        "$env:ProgramFiles\Corel",
        "$env:ProgramFiles(x86)\Corel",
        "$env:ProgramData\Corel",
        "$env:LOCALAPPDATA\Corel",
        "$env:APPDATA\Corel"
    ) | Where-Object { $_ -and (Test-Path $_) }

    foreach ($root in $roots) {
        $queue = New-Object 'System.Collections.Generic.Queue[object]'
        $queue.Enqueue([pscustomobject]@{ Path = $root; Depth = 0 })
        while ($queue.Count -gt 0) {
            $item = $queue.Dequeue()
            if (-not (Test-Path -LiteralPath $item.Path)) { continue }
            Get-ChildItem -LiteralPath $item.Path -Force -ErrorAction SilentlyContinue | ForEach-Object {
                [pscustomobject]@{
                    FullName = $_.FullName
                    Length = if ($_.PSIsContainer) { $null } else { $_.Length }
                    LastWriteTime = $_.LastWriteTime
                    Attributes = $_.Attributes
                }
                if ($_.PSIsContainer -and $item.Depth -lt 2) {
                    $queue.Enqueue([pscustomobject]@{ Path = $_.FullName; Depth = ($item.Depth + 1) })
                }
            }
        }
    }
}

function Get-CorelProcesses {
    Get-CimInstance Win32_Process | Where-Object { Test-CorelText ($_.Name + ' ' + $_.CommandLine) } | ForEach-Object {
        [pscustomobject]@{
            Source = 'Process'
            Name = $_.Name
            ProcessId = $_.ProcessId
            CommandLine = $_.CommandLine
        }
    }
}

function Remove-WithConfirmation {
    param(
        [Parameter(Mandatory)] [string]$Kind,
        [Parameter(Mandatory)] [string]$Target,
        [scriptblock]$Action
    )
    Write-Host ""
    Write-Host "$Kind candidate: $Target"
    $answer = Read-Host "Delete this item? Type YES to continue"
    if ($answer -eq 'YES') {
        & $Action
        Write-Log ("Deleted {0}: {1}" -f $Kind, $Target) 'OK'
    } else {
        Write-Log ("Skipped {0}: {1}" -f $Kind, $Target) 'WARN'
    }
}

Ensure-Dir $OutputRoot
$script:BackupDir = Join-Path $OutputRoot 'backups'
Ensure-Dir $script:BackupDir
$script:LogPath = Join-Path $OutputRoot 'corel_forensics.log'
New-Item -ItemType File -Path $script:LogPath -Force | Out-Null

Write-Log "Output root: $OutputRoot"
Write-Log "Cleanup mode: $Cleanup"
Write-Log "WhatIf mode: $WhatIf"

$inventory = [ordered]@{}

Write-Log 'Exporting registry backups...'
$regKeys = @(
    'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
    'HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
    'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Installer',
    'HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Installer',
    'HKLM\SOFTWARE\Classes\Installer',
    'HKLM\SOFTWARE\WOW6432Node\Classes\Installer',
    'HKLM\SYSTEM\CurrentControlSet\Services',
    'HKLM\SOFTWARE\Classes\CLSID',
    'HKLM\SOFTWARE\WOW6432Node\Classes\CLSID'
)
foreach ($k in $regKeys) {
    $safe = ($k -replace '[\\/:*?"<>|]', '_')
    try { Export-RegKey -Key $k -FileName "$safe.reg" } catch { Write-Log "Failed export: $k - $($_.Exception.Message)" 'WARN' }
}

$inventory.Uninstall = @(Get-UninstallEntries)
$inventory.InstallerUserData = @(Get-InstallerUserDataMatches)
$inventory.Services = @(Get-ServicingMatches)
$inventory.Processes = @(Get-CorelProcesses)

try {
    $inventory.InstallerCache = @(Get-InstallerCacheMatches | Where-Object { $_.Path -match '\\Corel|Corel' })
} catch {
    $inventory.InstallerCache = @()
}

try {
    $inventory.FileSystem = @(Get-FileSystemMatches | Where-Object { $_.FullName -match 'Corel|Protexis' })
} catch {
    $inventory.FileSystem = @()
}

$inventoryPath = Join-Path $OutputRoot 'inventory.json'
$inventory | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $inventoryPath -Encoding UTF8
Write-Log "Wrote inventory to $inventoryPath" 'OK'

foreach ($section in $inventory.Keys) {
    $count = @($inventory[$section]).Count
    Write-Log "$section count: $count"
}

if ($Cleanup) {
    Write-Log 'Cleanup mode enabled. Only confirmed actions will run.' 'WARN'
    Write-Log 'This script does not auto-delete registry or files without your explicit YES input.' 'WARN'
    foreach ($entry in $inventory.Uninstall) {
        if ($entry.DisplayName -match 'Corel Graphics - Windows Shell Extension' -and $entry.KeyName -match '^\{.*\}$') {
            Write-Log "Detected duplicate uninstall key candidate: $($entry.KeyName)" 'WARN'
        }
    }

    if ($WhatIf) {
        Write-Log 'WhatIf requested. No deletions will be performed.' 'WARN'
    }
}

Write-Log 'Done.'
