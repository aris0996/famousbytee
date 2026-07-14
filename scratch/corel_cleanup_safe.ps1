param(
    [string]$OutputRoot = $(Join-Path $PSScriptRoot ("corel_cleanup_" + (Get-Date -Format "yyyyMMdd_HHmmss"))),
    [switch]$DryRun
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

function Ensure-Dir([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Backup-RegKey([string]$Key, [string]$Name) {
    $dest = Join-Path $script:BackupDir $Name
    & reg.exe export $Key $dest /y | Out-Null
    Write-Log "Backed up $Key -> $dest" 'OK'
}

function Remove-RegPath([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
        Write-Log "Removed registry path $Path" 'OK'
    } else {
        Write-Log "Registry path not found: $Path" 'WARN'
    }
}

function Remove-FolderSafe([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $full = (Resolve-Path -LiteralPath $Path).Path
        Write-Host ""
        Write-Host "Folder candidate: $full"
        $answer = Read-Host "Type DELETE to remove this folder"
        if ($answer -eq 'DELETE') {
            Remove-Item -LiteralPath $full -Recurse -Force
            Write-Log "Removed folder $full" 'OK'
        } else {
            Write-Log "Skipped folder $full" 'WARN'
        }
    } else {
        Write-Log "Folder not found: $Path" 'WARN'
    }
}

Ensure-Dir $OutputRoot
$script:BackupDir = Join-Path $OutputRoot 'backups'
Ensure-Dir $script:BackupDir
$script:LogPath = Join-Path $OutputRoot 'cleanup.log'
New-Item -ItemType File -Path $script:LogPath -Force | Out-Null

Write-Log "Output root: $OutputRoot"
Write-Log "DryRun: $DryRun"

$targets = @(
    @{
        Hive = 'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
        Key  = '_{AF87FFD3-1D24-4940-99AE-F0CBAB8EDEAC}'
    },
    @{
        Hive = 'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
        Key  = '{AF87FFD3-1D24-4940-99AE-F0CBAB8EDEAC}'
    },
    @{
        Hive = 'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
        Key  = '{34C7ED8D-9DB4-43B3-B0EF-0B15A06BD3E8}'
    }
)

Backup-RegKey 'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall' 'HKLM_SOFTWARE_Microsoft_Windows_CurrentVersion_Uninstall.reg'
Backup-RegKey 'HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall' 'HKLM_SOFTWARE_WOW6432Node_Microsoft_Windows_CurrentVersion_Uninstall.reg'
Backup-RegKey 'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Installer' 'HKLM_SOFTWARE_Microsoft_Windows_CurrentVersion_Installer.reg'
Backup-RegKey 'HKLM\SOFTWARE\Classes\Installer' 'HKLM_SOFTWARE_Classes_Installer.reg'

foreach ($t in $targets) {
    $path = Join-Path ("Registry::" + $t.Hive) $t.Key
    if (Test-Path -LiteralPath $path) {
        $props = Get-ItemProperty -LiteralPath $path -ErrorAction SilentlyContinue
        Write-Log ("Target: {0} | DisplayName={1} | DisplayVersion={2} | UninstallString={3}" -f $path, $props.DisplayName, $props.DisplayVersion, $props.UninstallString)
        if (-not $DryRun) {
            $answer = Read-Host "Type YES to remove this registry key"
            if ($answer -eq 'YES') {
                Remove-RegPath $path
            } else {
                Write-Log "Skipped registry key $path" 'WARN'
            }
        }
    } else {
        Write-Log "Registry key not found: $path" 'WARN'
    }
}

$shellDir = 'C:\Program Files\Common Files\Corel\Shared\Shell Extension'
if (-not $DryRun) {
    Remove-FolderSafe $shellDir
} else {
    Write-Log "DryRun enabled, folder deletion skipped: $shellDir" 'WARN'
}

Write-Log 'Cleanup pass finished. Reboot recommended before reinstall.' 'OK'
