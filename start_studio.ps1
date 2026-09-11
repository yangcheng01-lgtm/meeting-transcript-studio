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

# Safe local startup: token exists only in this process and is never written to disk.
if ([string]::IsNullOrWhiteSpace($Token) -and [string]::IsNullOrWhiteSpace($env:HF_TOKEN)) {
    $secure = Read-Host '粘贴 Hugging Face read token（输入不会回显）' -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:HF_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
if ([string]::IsNullOrWhiteSpace($env:HF_TOKEN) -and -not [string]::IsNullOrWhiteSpace($Token)) { $env:HF_TOKEN = $Token }

$python = if ($env:VOICEID_PYTHON) { $env:VOICEID_PYTHON } elseif (Test-Path "$PSScriptRoot\.venv\Scripts\python.exe") { "$PSScriptRoot\.venv\Scripts\python.exe" } else { "python" }
& $python "$PSScriptRoot\app.py"
