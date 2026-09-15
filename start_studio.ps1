param(
    [string]$Token
)

# Local data can live outside the source repository, for example on D:.
# The migration script creates this directory and the setting is process-local.
if ([string]::IsNullOrWhiteSpace($env:MEETING_DATA_ROOT)) {
    if (Test-Path 'D:\多听工作台\data\projects') {
        $env:MEETING_DATA_ROOT = 'D:\多听工作台'
    } else {
        $env:MEETING_DATA_ROOT = $PSScriptRoot
    }
}
if ([string]::IsNullOrWhiteSpace($env:MEETING_PROJECTS_DIR)) {
    $env:MEETING_PROJECTS_DIR = Join-Path $env:MEETING_DATA_ROOT 'data\projects'
}
if ([string]::IsNullOrWhiteSpace($env:HF_HOME) -and (Test-Path (Join-Path $env:MEETING_DATA_ROOT 'model-cache\huggingface'))) {
    $env:HF_HOME = Join-Path $env:MEETING_DATA_ROOT 'model-cache\huggingface'
}
if ([string]::IsNullOrWhiteSpace($env:TORCH_HOME) -and (Test-Path (Join-Path $env:MEETING_DATA_ROOT 'model-cache\torch'))) {
    $env:TORCH_HOME = Join-Path $env:MEETING_DATA_ROOT 'model-cache\torch'
}
if ([string]::IsNullOrWhiteSpace($env:PYANNOTE_CACHE) -and (Test-Path (Join-Path $env:MEETING_DATA_ROOT 'model-cache\torch\pyannote'))) {
    $env:PYANNOTE_CACHE = Join-Path $env:MEETING_DATA_ROOT 'model-cache\torch\pyannote'
}

# Reuse the Windows user proxy for yt-dlp when one is enabled.
if ([string]::IsNullOrWhiteSpace($env:YTDLP_PROXY)) {
    $internet = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    if ($internet.ProxyEnable -eq 1 -and -not [string]::IsNullOrWhiteSpace($internet.ProxyServer)) {
        $env:YTDLP_PROXY = if ($internet.ProxyServer -match '^https?://') { $internet.ProxyServer } else { "http://$($internet.ProxyServer)" }
    }
}

# Cached pyannote models run offline without asking ordinary users for an HF token.
$hasOfflineModel = -not [string]::IsNullOrWhiteSpace($env:PYANNOTE_CACHE) -and (Test-Path $env:PYANNOTE_CACHE)
if ($hasOfflineModel) {
    $env:HF_HUB_OFFLINE = '1'
} elseif ([string]::IsNullOrWhiteSpace($Token) -and [string]::IsNullOrWhiteSpace($env:HF_TOKEN)) {
    $secure = Read-Host '首次下载模型时粘贴 Hugging Face read token（输入不会回显）' -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:HF_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
if ([string]::IsNullOrWhiteSpace($env:HF_TOKEN) -and -not [string]::IsNullOrWhiteSpace($Token)) { $env:HF_TOKEN = $Token }

$python = if ($env:VOICEID_PYTHON) { $env:VOICEID_PYTHON } elseif (Test-Path "$PSScriptRoot\.venv\Scripts\python.exe") { "$PSScriptRoot\.venv\Scripts\python.exe" } else { "python" }
# Speaker 分离必须使用装有 pyannote 的 D:\voiceid31，而不是 Web 应用自己的 Python。
if ([string]::IsNullOrWhiteSpace($env:VOICEID_PYTHON) -and (Test-Path 'D:\voiceid31\Scripts\python.exe')) {
  $env:VOICEID_PYTHON = 'D:\voiceid31\Scripts\python.exe'
}
& $python "$PSScriptRoot\app.py"
