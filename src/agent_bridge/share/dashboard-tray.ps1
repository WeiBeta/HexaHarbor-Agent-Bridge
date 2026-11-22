# HexaHarbor Agent Bridge — Windows System Tray Controller
# Zero-dependency native WinForms NotifyIcon architecture inspired by
# @bitkyc08/opencodex (MIT License).
# Keep this file encoded as UTF-8 with BOM for Windows PowerShell 5.1.

param(
  [string]$BridgeExe = "",
  [string]$BridgePrefixJson = "",
  [string]$BridgeHome = "",
  [string]$HostAddr = "127.0.0.1",
  [int]$Port = 8765,
  [ValidateSet("Run", "Stop")][string]$Mode = "Run"
)

$ErrorActionPreference = "Stop"

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

function Normalize-TrayLocale([object]$Value) {
  if ($null -eq $Value -or -not ($Value -is [string])) {
    return "zh-CN"
  }
  $cleaned = $Value.Trim().ToLowerInvariant()
  if ($cleaned -in @("en", "en-us", "en_us", "en-gb", "en_gb")) {
    return "en"
  }
  if ($cleaned -in @("zh-cn", "zh_cn", "zh", "cn", "zh-hans", "zh_hans")) {
    return "zh-CN"
  }
  return "zh-CN"
}

function Get-FallbackTrayCatalog {
  return @{
    "zh-CN" = @{
      tooltip_init    = "六库仙贼/HexaHarbor 看板"
      tooltip_online  = "HexaHarbor 看板: 在线 ({port})"
      tooltip_offline = "HexaHarbor 看板: 已停止"
      menu_open       = "打开面板"
      menu_start      = "启动服务"
      menu_stop       = "停止服务"
      menu_restart    = "重启服务"
      menu_update     = "更新提示"
      status_checking = "服务: 检测中..."
      status_online   = "服务: 在线 (端口 {port})"
      status_offline  = "服务: 已停止"
      menu_folder     = "打开日志文件夹"
      menu_quit       = "退出托盘"
      update_title    = "六库仙贼/HexaHarbor 看板"
      update_message  = "最新，别搞"
    }
    "en" = @{
      tooltip_init    = "HexaHarbor Dashboard"
      tooltip_online  = "HexaHarbor Dashboard: Online ({port})"
      tooltip_offline = "HexaHarbor Dashboard: Stopped"
      menu_open       = "Open Dashboard"
      menu_start      = "Start Service"
      menu_stop       = "Stop Service"
      menu_restart    = "Restart Service"
      menu_update     = "Update Notice"
      status_checking = "Service: Checking..."
      status_online   = "Service: Online (Port {port})"
      status_offline  = "Service: Stopped"
      menu_folder     = "Open Logs Folder"
      menu_quit       = "Quit Tray"
      update_title    = "HexaHarbor Dashboard"
      update_message  = "Already up to date — don't mess with it"
    }
  }
}

function Get-DefaultTrayLocaleDir {
  if ($PSScriptRoot) {
    $parent = Split-Path -Parent $PSScriptRoot
    if ($parent) {
      return Join-Path $parent "dashboard_assets\locales"
    }
  }
  return ""
}

function Get-TrayLocaleCatalog([string]$LocaleDir = "") {
  $catalog = Get-FallbackTrayCatalog
  $dir = if ($LocaleDir) { $LocaleDir } else { Get-DefaultTrayLocaleDir }
  if (-not $dir -or -not [System.IO.Directory]::Exists($dir)) {
    return $catalog
  }
  foreach ($loc in @("zh-CN", "en")) {
    $path = Join-Path $dir "$loc.json"
    if (-not [System.IO.File]::Exists($path)) { continue }
    try {
      $raw = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
      $parsed = $raw | ConvertFrom-Json
      if ($null -eq $parsed) { continue }
      $trayProp = $parsed.PSObject.Properties["tray"]
      if ($null -eq $trayProp -or $null -eq $trayProp.Value) { continue }
      foreach ($prop in $trayProp.Value.PSObject.Properties) {
        if ($prop.Value -is [string] -and $prop.Value.Length -gt 0) {
          $catalog[$loc][$prop.Name] = $prop.Value
        }
      }
    } catch {
      $null = $_
    }
  }
  return $catalog
}

function Get-CurrentTrayLocale([string]$PrefsPath, [string]$FallbackLocale = "zh-CN") {
  if (-not $PrefsPath -or -not [System.IO.File]::Exists($PrefsPath)) {
    return "zh-CN"
  }
  $raw = $null
  for ($attempt = 0; $attempt -lt 2; $attempt++) {
    try {
      $raw = [System.IO.File]::ReadAllText($PrefsPath, [System.Text.Encoding]::UTF8)
      break
    } catch [System.IO.IOException] {
      if ($attempt -eq 0) {
        Start-Sleep -Milliseconds 20
        continue
      }
      return (Normalize-TrayLocale $FallbackLocale)
    } catch {
      return "zh-CN"
    }
  }
  if ([string]::IsNullOrWhiteSpace($raw)) {
    return "zh-CN"
  }
  try {
    $parsed = $raw | ConvertFrom-Json
    if ($null -eq $parsed) { return "zh-CN" }
    $localeProp = $parsed.PSObject.Properties["locale"]
    if ($null -eq $localeProp) { return "zh-CN" }
    return (Normalize-TrayLocale $localeProp.Value)
  } catch {
    return "zh-CN"
  }
}

function Format-NotifyTooltip([string]$Text) {
  if ([string]::IsNullOrEmpty($Text)) {
    return "HexaHarbor"
  }
  # WinForms NotifyIcon.Text enforces a 63-character maximum on .NET Framework (PS 5.1).
  if ($Text.Length -gt 63) {
    return $Text.Substring(0, 63)
  }
  return $Text
}

function Ensure-TrayWinFormsLoaded {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  try { [System.Windows.Forms.Application]::EnableVisualStyles() } catch { $null = $_ }
  if (-not ("HexaTrayNative" -as [type])) {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class HexaTrayNative {
  [DllImport("user32.dll", CharSet = CharSet.Auto)]
  public static extern bool DestroyIcon(IntPtr handle);
}
"@
  }
}

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

function Get-CurrentEndpoint([string]$ServiceCfgPath, [string]$DefaultHost, [int]$DefaultPort) {
  $h = $DefaultHost
  $p = $DefaultPort
  if ($ServiceCfgPath -and [System.IO.File]::Exists($ServiceCfgPath)) {
    try {
      $cfg = [System.IO.File]::ReadAllText($ServiceCfgPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
      if ($null -ne $cfg) {
        $hostProp = $cfg.PSObject.Properties["host"]
        if ($null -ne $hostProp -and $hostProp.Value) { $h = [string]$hostProp.Value }
        $portProp = $cfg.PSObject.Properties["port"]
        if ($null -ne $portProp -and $portProp.Value) { $p = [int]$portProp.Value }
      }
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

function Invoke-BridgeSubcommand(
  [string]$Exe,
  [string[]]$PrefixArgs,
  [string]$HomeDir,
  [string[]]$SubArgs,
  [switch]$CaptureOutput
) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $Exe
  $all = @()
  if ($PrefixArgs) { $all += $PrefixArgs }
  $all += $SubArgs
  $psi.Arguments = (($all | ForEach-Object { Quote-Arg $_ }) -join " ")
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
  $psi.EnvironmentVariables["AGENT_BRIDGE_HOME"] = $HomeDir
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

function Apply-TrayLocale([hashtable]$State) {
  $loc = Get-CurrentTrayLocale $State.PrefsPath $State.Locale
  $State.Locale = $loc
  $tr = $State.Catalog[$loc]
  if ($null -eq $tr) {
    $tr = $State.Catalog["zh-CN"]
  }
  $State.ItemOpen.Text = $tr["menu_open"]
  $State.ItemStart.Text = $tr["menu_start"]
  $State.ItemStop.Text = $tr["menu_stop"]
  $State.ItemRestart.Text = $tr["menu_restart"]
  $State.ItemUpdate.Text = $tr["menu_update"]
  $State.ItemFolder.Text = $tr["menu_folder"]
  $State.ItemQuit.Text = $tr["menu_quit"]

  if ($State.OnlineState -eq "online") {
    $portStr = [string]$State.LastPort
    $State.Notify.Text = Format-NotifyTooltip ($tr["tooltip_online"].Replace("{port}", $portStr))
    $State.ItemStatus.Text = $tr["status_online"].Replace("{port}", $portStr)
  } elseif ($State.OnlineState -eq "offline") {
    $State.Notify.Text = Format-NotifyTooltip $tr["tooltip_offline"]
    $State.ItemStatus.Text = $tr["status_offline"]
  } else {
    $State.Notify.Text = Format-NotifyTooltip $tr["tooltip_init"]
    $State.ItemStatus.Text = $tr["status_checking"]
  }
}

function Refresh-TrayStatus([hashtable]$State) {
  $ep = Get-CurrentEndpoint $State.ServiceCfgPath $State.DefaultHost $State.DefaultPort
  $online = Test-DashboardOnline $ep
  $State.LastPort = $ep.Port
  if ($online) {
    $State.OnlineState = "online"
    $State.Notify.Icon = $State.OnlineIcon
    $State.ItemStart.Enabled = $false
    $State.ItemStop.Enabled = $true
  } else {
    $State.OnlineState = "offline"
    $State.Notify.Icon = $State.OfflineIcon
    $State.ItemStart.Enabled = $true
    $State.ItemStop.Enabled = $false
  }
  Apply-TrayLocale $State
}

function Get-TrayUpdateNotice([hashtable]$State) {
  Apply-TrayLocale $State
  $tr = $State.Catalog[$State.Locale]
  if ($null -eq $tr) { $tr = $State.Catalog["zh-CN"] }
  return @{
    Title   = $tr["update_title"]
    Message = $tr["update_message"]
  }
}

function New-TrayController(
  [string]$Exe,
  [string[]]$PrefixArgs,
  [string]$HomeDir,
  [string]$DefaultHost,
  [int]$DefaultPort,
  [string]$LocaleDir = "",
  [bool]$ShowNotifyIcon = $true
) {
  Ensure-TrayWinFormsLoaded
  $normHome = Normalize-HomePath $HomeDir
  $billing = Join-Path $normHome "billing"
  if (-not [System.IO.Directory]::Exists($billing)) {
    [void][System.IO.Directory]::CreateDirectory($billing)
  }

  $onlineIcon = New-HexIcon $true
  $offlineIcon = New-HexIcon $false

  $notify = New-Object System.Windows.Forms.NotifyIcon
  $notify.Icon = $offlineIcon
  $notify.Visible = $ShowNotifyIcon

  $menu = New-Object System.Windows.Forms.ContextMenuStrip
  $itemOpen = New-Object System.Windows.Forms.ToolStripMenuItem
  $openFont = New-Object System.Drawing.Font($itemOpen.Font, [System.Drawing.FontStyle]::Bold)
  $itemOpen.Font = $openFont
  $itemStart = New-Object System.Windows.Forms.ToolStripMenuItem
  $itemStop = New-Object System.Windows.Forms.ToolStripMenuItem
  $itemRestart = New-Object System.Windows.Forms.ToolStripMenuItem
  $itemUpdate = New-Object System.Windows.Forms.ToolStripMenuItem
  $sep1 = New-Object System.Windows.Forms.ToolStripSeparator
  $itemStatus = New-Object System.Windows.Forms.ToolStripMenuItem
  $itemStatus.Enabled = $false
  $sep2 = New-Object System.Windows.Forms.ToolStripSeparator
  $itemFolder = New-Object System.Windows.Forms.ToolStripMenuItem
  $sep3 = New-Object System.Windows.Forms.ToolStripSeparator
  $itemQuit = New-Object System.Windows.Forms.ToolStripMenuItem

  [void]$menu.Items.AddRange(@(
    $itemOpen, $itemStart, $itemStop, $itemRestart, $itemUpdate,
    $sep1, $itemStatus, $sep2, $itemFolder, $sep3, $itemQuit
  ))
  $notify.ContextMenuStrip = $menu

  $state = @{
    BridgeExe      = $Exe
    PrefixArgs     = $PrefixArgs
    BridgeHome     = $normHome
    BillingDir     = $billing
    ServiceCfgPath = (Join-Path $billing "service.json")
    PrefsPath      = (Join-Path $billing "preferences.json")
    DefaultHost    = $DefaultHost
    DefaultPort    = $DefaultPort
    LastPort       = $DefaultPort
    OnlineState    = "checking"
    Locale         = "zh-CN"
    Catalog        = (Get-TrayLocaleCatalog $LocaleDir)
    OnlineIcon     = $onlineIcon
    OfflineIcon    = $offlineIcon
    OpenFont       = $openFont
    Notify         = $notify
    Menu           = $menu
    ItemOpen       = $itemOpen
    ItemStart      = $itemStart
    ItemStop       = $itemStop
    ItemRestart    = $itemRestart
    ItemUpdate     = $itemUpdate
    ItemStatus     = $itemStatus
    ItemFolder     = $itemFolder
    ItemQuit       = $itemQuit
  }

  # Apply initial persisted locale immediately so "checking" state & tooltip match.
  Apply-TrayLocale $state

  $menu.Tag = $state
  $notify.Tag = $state
  $itemOpen.Tag = $state
  $itemStart.Tag = $state
  $itemStop.Tag = $state
  $itemRestart.Tag = $state
  $itemUpdate.Tag = $state
  $itemFolder.Tag = $state

  # Sync locale on every menu open without blocking on HTTP so offline switching is immediate.
  $menu.Add_Opening({
    param($sender, $eventArgs)
    $null = $eventArgs
    if ($sender -and $sender.Tag) {
      Apply-TrayLocale $sender.Tag
    }
  })

  $openAction = {
    param($sender, $eventArgs)
    $null = $eventArgs
    $st = if ($sender) { $sender.Tag } else { $null }
    if ($null -eq $st) { return }
    $ep = Get-CurrentEndpoint $st.ServiceCfgPath $st.DefaultHost $st.DefaultPort
    if (-not (Test-DashboardOnline $ep)) {
      [void](Invoke-BridgeSubcommand -Exe $st.BridgeExe -PrefixArgs $st.PrefixArgs -HomeDir $st.BridgeHome -SubArgs @("dashboard", "start"))
    }
    Start-Process $ep.Url
    Refresh-TrayStatus $st
  }

  $itemOpen.Add_Click($openAction)
  $notify.Add_DoubleClick($openAction)

  $itemStart.Add_Click({
    param($sender, $eventArgs)
    $null = $eventArgs
    $st = if ($sender) { $sender.Tag } else { $null }
    if ($null -eq $st) { return }
    [void](Invoke-BridgeSubcommand -Exe $st.BridgeExe -PrefixArgs $st.PrefixArgs -HomeDir $st.BridgeHome -SubArgs @("dashboard", "start"))
    Refresh-TrayStatus $st
  })

  $itemStop.Add_Click({
    param($sender, $eventArgs)
    $null = $eventArgs
    $st = if ($sender) { $sender.Tag } else { $null }
    if ($null -eq $st) { return }
    [void](Invoke-BridgeSubcommand -Exe $st.BridgeExe -PrefixArgs $st.PrefixArgs -HomeDir $st.BridgeHome -SubArgs @("dashboard", "stop"))
    Refresh-TrayStatus $st
  })

  $itemRestart.Add_Click({
    param($sender, $eventArgs)
    $null = $eventArgs
    $st = if ($sender) { $sender.Tag } else { $null }
    if ($null -eq $st) { return }
    [void](Invoke-BridgeSubcommand -Exe $st.BridgeExe -PrefixArgs $st.PrefixArgs -HomeDir $st.BridgeHome -SubArgs @("dashboard", "restart"))
    Refresh-TrayStatus $st
  })

  $itemUpdate.Add_Click({
    param($sender, $eventArgs)
    $null = $eventArgs
    $st = if ($sender) { $sender.Tag } else { $null }
    if ($null -eq $st) { return }
    $notice = Get-TrayUpdateNotice $st
    [void][System.Windows.Forms.MessageBox]::Show(
      $notice.Message,
      $notice.Title,
      [System.Windows.Forms.MessageBoxButtons]::OK,
      [System.Windows.Forms.MessageBoxIcon]::Information
    )
  })

  $itemFolder.Add_Click({
    param($sender, $eventArgs)
    $null = $eventArgs
    $st = if ($sender) { $sender.Tag } else { $null }
    if ($null -eq $st) { return }
    Start-Process "explorer.exe" $st.BillingDir
  })

  $itemQuit.Add_Click({
    [System.Windows.Forms.Application]::ExitThread()
  })

  return $state
}

function Dispose-TrayController([hashtable]$State) {
  if ($null -eq $State) { return }
  try { $State.Notify.Visible = $false } catch { $null = $_ }
  try { $State.Notify.Dispose() } catch { $null = $_ }
  try { $State.Menu.Dispose() } catch { $null = $_ }
  try { $State.OpenFont.Dispose() } catch { $null = $_ }
  try { $State.OnlineIcon.Dispose() } catch { $null = $_ }
  try { $State.OfflineIcon.Dispose() } catch { $null = $_ }
}

function Invoke-TrayMain {
  if (-not $BridgeExe -or -not $BridgeHome) {
    throw "BridgeExe and BridgeHome parameters are required."
  }

  $prefixArgs = @()
  if ($BridgePrefixJson) {
    try {
      $decodedPrefix = $BridgePrefixJson | ConvertFrom-Json
      if ($decodedPrefix -is [System.Array]) {
        $prefixArgs = @($decodedPrefix | ForEach-Object { [string]$_ })
      } elseif ($decodedPrefix) {
        $prefixArgs = @([string]$decodedPrefix)
      }
    } catch { $null = $_ }
  }

  $normHome = Normalize-HomePath $BridgeHome
  $billingDir = Join-Path $normHome "billing"
  if (-not [System.IO.Directory]::Exists($billingDir)) {
    [void][System.IO.Directory]::CreateDirectory($billingDir)
  }
  $trayPidPath = Join-Path $billingDir "tray.pid"

  $hash = Get-StableHash $normHome
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
    [System.IO.File]::WriteAllText($trayPidPath, $pidPayload, (New-Object System.Text.UTF8Encoding($false)))
  } catch { $null = $_ }

  $controller = New-TrayController -Exe $BridgeExe -PrefixArgs $prefixArgs -HomeDir $normHome -DefaultHost $HostAddr -DefaultPort $Port

  $timer = New-Object System.Windows.Forms.Timer
  $timer.Interval = 5000
  $timer.Tag = @{ Controller = $controller; StopEvent = $stopEvent }
  $timer.Add_Tick({
    param($sender, $eventArgs)
    $null = $eventArgs
    $ctx = if ($sender) { $sender.Tag } else { $null }
    if ($null -eq $ctx) { return }
    if ($ctx.StopEvent.WaitOne(0)) {
      [System.Windows.Forms.Application]::ExitThread()
      return
    }
    Refresh-TrayStatus $ctx.Controller
  })

  $stopPoll = New-Object System.Windows.Forms.Timer
  $stopPoll.Interval = 500
  $stopPoll.Tag = $stopEvent
  $stopPoll.Add_Tick({
    param($sender, $eventArgs)
    $null = $eventArgs
    $evt = if ($sender) { $sender.Tag } else { $null }
    if ($evt -and $evt.WaitOne(0)) {
      [System.Windows.Forms.Application]::ExitThread()
    }
  })

  Refresh-TrayStatus $controller
  $timer.Start()
  $stopPoll.Start()

  try {
    [System.Windows.Forms.Application]::Run()
  } finally {
    $timer.Stop()
    $stopPoll.Stop()
    $timer.Dispose()
    $stopPoll.Dispose()
    Dispose-TrayController $controller
    try {
      if ([System.IO.File]::Exists($trayPidPath)) { [System.IO.File]::Delete($trayPidPath) }
    } catch { $null = $_ }
    $stopEvent.Dispose()
    try { $mutex.ReleaseMutex() } catch { $null = $_ }
    $mutex.Dispose()
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  Invoke-TrayMain
}
