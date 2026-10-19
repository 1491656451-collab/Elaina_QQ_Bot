# 伊蕾娜语音服务：一键开启 / 关闭 / 查看状态（10/20）
# 用法：双击同目录的 开启语音服务.bat / 关闭语音服务.bat / 语音服务状态.bat，它们都调用这个脚本
param([ValidateSet("start", "stop", "status")][string]$Action = "status")

$Here = $PSScriptRoot
$Gsv = "D:\GPT-SoVITS\GPT-SoVITS-v2pro-20250604"       # GPT-SoVITS 整合包的位置，挪了就改这里
$Py = Join-Path $Gsv "runtime\python.exe"
$Script = Join-Path $Here "elaina_tts.py"
$PidFile = Join-Path $Here "service.pid"
$Log = Join-Path $Here "语音服务.log"

function Test-Port([int]$Port) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $c.Close(); return $true } catch { return $false }
}

function Get-ServiceProcs {
    # 语音服务本身、它启动的 GPT-SoVITS（api_v2）、连服务器的 ssh 隧道
    Get-CimInstance Win32_Process | Where-Object {
        $cl = $_.CommandLine
        $cl -and ($cl -like "*elaina_tts.py*" -or ($cl -like "*api_v2.py*" -and $cl -like "*-p 9880*") -or ($_.Name -eq "ssh.exe" -and $cl -like "*127.0.0.1:9881:127.0.0.1:9881*"))
    }
}

function Show-LogTail([int]$N = 6) {
    if (Test-Path $Log) { Get-Content $Log -Encoding UTF8 -Tail $N | ForEach-Object { "    $_" } }
}

switch ($Action) {
    "start" {
        if (Test-Port 9881) { Write-Host "语音服务已经在运行了，不用再开。"; Show-LogTail 3; break }
        if (-not (Test-Path $Py)) { Write-Host "没找到 $Py ，用记事本打开 voice_ctl.ps1 改第 6 行的路径"; break }
        $skip = 0; if (Test-Path $Log) { $skip = @(Get-Content $Log -Encoding UTF8).Count }     # 只看这次启动以后的日志
        $p = Start-Process -FilePath $Py -ArgumentList "`"$Script`"" -WorkingDirectory $Here -WindowStyle Hidden -PassThru
        $p.Id | Set-Content $PidFile
        Write-Host "正在后台启动语音服务（载入模型大约要半分钟到一分钟）……"
        $ok = $false
        for ($i = 0; $i -lt 90; $i++) {
            Start-Sleep -Seconds 2
            if ($p.HasExited) { break }
            if (Test-Port 9881) { $ok = $true; break }
        }
        if (-not $ok) {
            Write-Host "启动失败。日志最后几行："; Show-LogTail 10
            Write-Host "（GPT-SoVITS 自己的输出在 voice_service\api_v2.log）"
            break
        }
        Write-Host "合成服务已经起来了，等隧道连上服务器……"
        $tunnel = ""
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Seconds 2
            $lines = Get-Content $Log -Encoding UTF8 | Select-Object -Skip $skip | Where-Object { $_ -match "隧道" }
            if ($lines | Where-Object { $_ -match "隧道已连上" }) { $tunnel = "ok"; break }
            if ($lines | Where-Object { $_ -match "隧道断了|开不了" }) { $tunnel = "bad" }
        }
        if ($tunnel -eq "ok") { Write-Host "好了：语音服务在后台运行，她现在可以发语音了。打游戏前双击 关闭语音服务.bat。" }
        else { Write-Host "合成服务开着，但隧道还没连上（会自己一直重试）。日志最后几行："; Show-LogTail 6 }
    }
    "stop" {
        $procs = @(Get-ServiceProcs)
        if ($procs.Count -eq 0 -and -not (Test-Port 9881) -and -not (Test-Port 9880)) {
            Write-Host "语音服务本来就没在运行。"
            Remove-Item $PidFile -ErrorAction SilentlyContinue
            break
        }
        if (Test-Path $PidFile) {
            $id = Get-Content $PidFile
            & taskkill /PID $id /T /F 2>$null | Out-Null       # 连同它启动的 GPT-SoVITS 和 ssh 一起关
        }
        foreach ($x in @(Get-ServiceProcs)) { Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        if ((Test-Port 9881) -or (Test-Port 9880)) { Write-Host "还有残留没关掉，再双击一次试试；还不行就重启电脑。" }
        else {
            Write-Host "已关闭语音服务，显存已经释放，可以放心打游戏了。她现在只发文字。"
            Add-Content $Log -Encoding UTF8 -Value ("{0:MM-dd HH:mm:ss} 用 关闭语音服务.bat 关掉了" -f (Get-Date))
        }
    }
    "status" {
        $api = Test-Port 9880; $svc = Test-Port 9881
        if ($svc) { Write-Host "语音服务：运行中" } elseif ($api) { Write-Host "语音服务：GPT-SoVITS 开着，但服务没起来（可以先关闭再开启）" } else { Write-Host "语音服务：没在运行（她只发文字）" }
        Write-Host "最近的日志："; Show-LogTail 8
    }
}
