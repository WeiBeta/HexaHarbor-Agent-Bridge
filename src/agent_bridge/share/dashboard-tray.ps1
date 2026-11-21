# HexaHarbor Agent Bridge — Windows System Tray Controller
# Zero-dependency native WinForms NotifyIcon architecture inspired by
# @bitkyc08/opencodex (MIT License).

param(
  [Parameter(Mandatory = $true)][string]$BridgeExe,
  [string]$BridgePrefixJson = "",
  [Parameter(Mandatory = $true)][string]$BridgeHome,
  [string]$HostAddr = "127.0.0.1",
  [int]$Port = 8765,
  [ValidateSet("Run", "Stop")][string]$Mode = "Run"
)

$BridgePrefixArgs = @()
if ($BridgePrefixJson) {
  try {
    $decodedPrefix = $BridgePrefixJson | ConvertFrom-Json
    if ($decodedPrefix -is [System.Array]) {
      $BridgePrefixArgs = @($decodedPrefix | ForEach-Object { [string]$_ })
    } elseif ($decodedPrefix) {
      $BridgePrefixArgs = @([string]$decodedPrefix)
    }
  } catch { $null = $_ }
}

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
try { [System.Windows.Forms.Application]::EnableVisualStyles() } catch { $null = $_ }

function Normalize-HomePath([string]$Value) {
  $full = [System.IO.Path]::GetFullPath($Value)
  $root = [System.IO.Path]::GetPathRoot($full)
  if ($full -eq $root) { return $full }
  return $full.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
}

function Get-StableHash([string]$Value) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value.ToLowerInvariant())
    return [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace("-", "").Substring(0, 20)
  } finally {
    $sha.Dispose()
  }
}

$BridgeHome = Normalize-HomePath $BridgeHome
$BillingDir = Join-Path $BridgeHome "billing"
if (-not [System.IO.Directory]::Exists($BillingDir)) {
  [void][System.IO.Directory]::CreateDirectory($BillingDir)
}
$TrayPidPath = Join-Path $BillingDir "tray.pid"
$ServiceCfgPath = Join-Path $BillingDir "service.json"

$hash = Get-StableHash $BridgeHome
$stopCreated = $false
$stopEvent = New-Object System.Threading.EventWaitHandle(
  $false,
  [System.Threading.EventResetMode]::AutoReset,
  "Local\HexaHarborBridgeTrayStop-$hash",
  [ref]$stopCreated
)

if ($Mode -eq "Stop") {
  [void]$stopEvent.Set()
  $stopEvent.Dispose()
  exit 0
}

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\HexaHarborBridgeTray-$hash", [ref]$createdNew)
if (-not $createdNew) {
  $stopEvent.Dispose()
  $mutex.Dispose()
  exit 0
}
[void]$stopEvent.Reset()

# Record tray PID so the web dashboard can show live tray status.
try {
  $pidPayload = @{ pid = $PID; started_at = [DateTimeOffset]::UtcNow.ToString("o") } | ConvertTo-Json -Compress
  [System.IO.File]::WriteAllText($TrayPidPath, $pidPayload, (New-Object System.Text.UTF8Encoding($false)))
} catch { $null = $_ }

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class HexaTrayNative {
  [DllImport("user32.dll", CharSet = CharSet.Auto)]
  public static extern bool DestroyIcon(IntPtr handle);
}
"@

function New-HexIcon([bool]$Online) {
  $bmp = New-Object System.Drawing.Bitmap(32, 32)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  try {
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)
    $bgColor = if ($Online) { [System.Drawing.Color]::FromArgb(255, 18, 18, 20) } else { [System.Drawing.Color]::FromArgb(255, 63, 63, 70) }
    $fgColor = if ($Online) { [System.Drawing.Color]::FromArgb(255, 250, 250, 250) } else { [System.Drawing.Color]::FromArgb(255, 161, 161, 170) }
    $bgBrush = New-Object System.Drawing.SolidBrush($bgColor)
    $fgBrush = New-Object System.Drawing.SolidBrush($fgColor)
    $pen = New-Object System.Drawing.Pen($fgColor, 2.2)
    try {
      $pts = @(
        (New-Object System.Drawing.PointF(16, 3)),
        (New-Object System.Drawing.PointF(28, 9.5)),
        (New-Object System.Drawing.PointF(28, 22.5)),
        (New-Object System.Drawing.PointF(16, 29)),
        (New-Object System.Drawing.PointF(4, 22.5)),
        (New-Object System.Drawing.PointF(4, 9.5))
      )
      $g.FillPolygon($bgBrush, $pts)
      $g.DrawPolygon($pen, $pts)
      $g.FillEllipse($fgBrush, 12.2, 12.2, 7.6, 7.6)
    } finally {
      $bgBrush.Dispose()
      $fgBrush.Dispose()
      $pen.Dispose()
    }
    $hIcon = $bmp.GetHicon()
    try {
      $tmp = [System.Drawing.Icon]::FromHandle($hIcon)
      return [System.Drawing.Icon]$tmp.Clone()
    } finally {
      [void][HexaTrayNative]::DestroyIcon($hIcon)
    }
  } finally {
    $g.Dispose()
    $bmp.Dispose()
  }
}

$onlineIcon = New-HexIcon $true
$offlineIcon = New-HexIcon $false

function Get-CurrentEndpoint {
  $h = $HostAddr
  $p = $Port
  if ([System.IO.File]::Exists($ServiceCfgPath)) {
    try {
      $cfg = [System.IO.File]::ReadAllText($ServiceCfgPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
      if ($cfg.host) { $h = [string]$cfg.host }
      if ($cfg.port) { $p = [int]$cfg.port }
    } catch { $null = $_ }
  }
  return @{ Host = $h; Port = $p; Url = "http://$($h):$($p)" }
}

function Test-DashboardOnline([hashtable]$Ep) {
  try {
    $req = [System.Net.HttpWebRequest]::Create("$($Ep.Url)/api/health")
    $req.Timeout = 1500
    $req.ReadWriteTimeout = 1500
    $req.Method = "GET"
    $resp = $req.GetResponse()
    try {
      return ([int]$resp.StatusCode -eq 200)
    } finally {
      $resp.Close()
    }
  } catch {
    return $false
  }
}

function Quote-Arg([string]$Arg) {
  if ($Arg -match '[\s"]') {
    return '"' + ($Arg -replace '"', '\"') + '"'
  }
  return $Arg
}

function Invoke-BridgeSubcommand([string[]]$SubArgs, [switch]$CaptureOutput) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $BridgeExe
  $all = @()
  if ($BridgePrefixArgs) { $all += $BridgePrefixArgs }
  $all += $SubArgs
  $psi.Arguments = (($all | ForEach-Object { Quote-Arg $_ }) -join " ")
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
  $psi.EnvironmentVariables["AGENT_BRIDGE_HOME"] = $BridgeHome
  $psi.EnvironmentVariables["PYTHONUTF8"] = "1"
  $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
  if ($CaptureOutput) {
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
  }
  $proc = [System.Diagnostics.Process]::Start($psi)
  if ($null -eq $proc) { return @{ ExitCode = -1; Output = "" } }
  try {
    if ($CaptureOutput) {
      $errTask = $proc.StandardError.ReadToEndAsync()
      $out = $proc.StandardOutput.ReadToEnd()
      $proc.WaitForExit(30000) | Out-Null
      $err = if ($errTask.Wait(2000)) { $errTask.Result } else { "" }
      return @{ ExitCode = $proc.ExitCode; Output = ($out + "`n" + $err).Trim() }
    } else {
      $proc.WaitForExit(12000) | Out-Null
      return @{ ExitCode = $proc.ExitCode; Output = "" }
    }
  } finally {
    $proc.Dispose()
  }
}

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = $offlineIcon
$notify.Text = "六库仙贼/HexaHarbor 看板"
$notify.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$itemOpen = New-Object System.Windows.Forms.ToolStripMenuItem("打开面板")
$itemOpen.Font = New-Object System.Drawing.Font($itemOpen.Font, [System.Drawing.FontStyle]::Bold)
$itemStart = New-Object System.Windows.Forms.ToolStripMenuItem("启动服务")
$itemStop = New-Object System.Windows.Forms.ToolStripMenuItem("停止服务")
$itemRestart = New-Object System.Windows.Forms.ToolStripMenuItem("重启服务")
$itemUpdate = New-Object System.Windows.Forms.ToolStripMenuItem("更新提示")
$sep1 = New-Object System.Windows.Forms.ToolStripSeparator
$itemStatus = New-Object System.Windows.Forms.ToolStripMenuItem("服务: 检测中...")
$itemStatus.Enabled = $false
$sep2 = New-Object System.Windows.Forms.ToolStripSeparator
$itemFolder = New-Object System.Windows.Forms.ToolStripMenuItem("打开日志文件夹")
$sep3 = New-Object System.Windows.Forms.ToolStripSeparator
$itemQuit = New-Object System.Windows.Forms.ToolStripMenuItem("退出托盘")

[void]$menu.Items.AddRange(@(
  $itemOpen, $itemStart, $itemStop, $itemRestart, $itemUpdate,
  $sep1, $itemStatus, $sep2, $itemFolder, $sep3, $itemQuit
))
$notify.ContextMenuStrip = $menu

function Refresh-TrayStatus {
  $ep = Get-CurrentEndpoint
  $online = Test-DashboardOnline $ep
  if ($online) {
    $notify.Icon = $onlineIcon
    $notify.Text = "HexaHarbor 看板: 在线 ($($ep.Port))"
    $itemStatus.Text = "服务: 在线 (端口 $($ep.Port))"
    $itemStart.Enabled = $false
    $itemStop.Enabled = $true
  } else {
    $notify.Icon = $offlineIcon
    $notify.Text = "HexaHarbor 看板: 已停止"
    $itemStatus.Text = "服务: 已停止"
    $itemStart.Enabled = $true
    $itemStop.Enabled = $false
  }
}

$openAction = {
  $ep = Get-CurrentEndpoint
  if (-not (Test-DashboardOnline $ep)) {
    [void](Invoke-BridgeSubcommand @("dashboard", "start"))
  }
  Start-Process $ep.Url
  Refresh-TrayStatus
}

$itemOpen.Add_Click($openAction)
$notify.Add_DoubleClick($openAction)

$itemStart.Add_Click({
  [void](Invoke-BridgeSubcommand @("dashboard", "start"))
  Refresh-TrayStatus
})

$itemStop.Add_Click({
  [void](Invoke-BridgeSubcommand @("dashboard", "stop"))
  Refresh-TrayStatus
})

$itemRestart.Add_Click({
  [void](Invoke-BridgeSubcommand @("dashboard", "restart"))
  Refresh-TrayStatus
})

$itemUpdate.Add_Click({
  [void][System.Windows.Forms.MessageBox]::Show(
    "最新，别搞",
    "六库仙贼/HexaHarbor 看板",
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Information
  )
})

$itemFolder.Add_Click({
  Start-Process "explorer.exe" $BillingDir
})

$itemQuit.Add_Click({
  [System.Windows.Forms.Application]::ExitThread()
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.Add_Tick({
  if ($stopEvent.WaitOne(0)) {
    [System.Windows.Forms.Application]::ExitThread()
    return
  }
  Refresh-TrayStatus
})

$stopPoll = New-Object System.Windows.Forms.Timer
$stopPoll.Interval = 500
$stopPoll.Add_Tick({
  if ($stopEvent.WaitOne(0)) {
    [System.Windows.Forms.Application]::ExitThread()
  }
})

Refresh-TrayStatus
$timer.Start()
$stopPoll.Start()

try {
  [System.Windows.Forms.Application]::Run()
} finally {
  $timer.Stop()
  $stopPoll.Stop()
  $timer.Dispose()
  $stopPoll.Dispose()
  $notify.Visible = $false
  $notify.Dispose()
  $menu.Dispose()
  $onlineIcon.Dispose()
  $offlineIcon.Dispose()
  try {
    if ([System.IO.File]::Exists($TrayPidPath)) { [System.IO.File]::Delete($TrayPidPath) }
  } catch { $null = $_ }
  $stopEvent.Dispose()
  try { $mutex.ReleaseMutex() } catch { $null = $_ }
  $mutex.Dispose()
}
