Set-Location -LiteralPath 'C:\Users\yang.cheng01\Documents\??&????\speaker_transcript_studio'
$env:MEETING_DATA_ROOT = Join-Path 'D:\' ([string][char]0x591A + [string][char]0x542C + [string][char]0x5DE5 + [string][char]0x4F5C + [string][char]0x53F0)
$env:MEETING_PROJECTS_DIR = Join-Path $env:MEETING_DATA_ROOT 'data\projects'
& .\start_studio.ps1
