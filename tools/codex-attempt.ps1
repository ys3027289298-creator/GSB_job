param(
    [Parameter(Mandatory = $true)] [string] $TaskId,
    [Parameter(Mandatory = $true)] [ValidateSet('A', 'B')] [string] $Side,
    [Parameter(Mandatory = $true)] [string] $Workspace,
    [Parameter(Mandatory = $true)] [string] $PromptFile,
    [Parameter(Mandatory = $true)] [string] $CodexCmd,
    [Parameter(Mandatory = $true)] [ValidateRange(1, 65535)] [int] $Port
)

$ErrorActionPreference = 'Stop'
$env:TERM = $null
$env:GSB_PORT = [string]$Port
$env:PORT = [string]$Port
$env:VITE_PORT = [string]$Port
$env:SERVER_PORT = [string]$Port
$env:DEV_PORT = [string]$Port
Set-Location -LiteralPath $Workspace
try { $Host.UI.RawUI.WindowTitle = "$TaskId-$Side Codex" } catch {}

$prompt = Get-Content -LiteralPath $PromptFile -Encoding UTF8 -Raw
$runtimePortInstruction = "本次运行分配的独立开发端口是 $Port。禁止使用 3000 或其他任务正在使用的端口；请让 Vite/React 开发服务器读取 GSB_PORT，并确保启动命令实际使用该端口。"
$prompt = $prompt.TrimEnd() + "`n" + $runtimePortInstruction
& $CodexCmd --model 'auto_model/urm' --yolo $prompt
exit $LASTEXITCODE
