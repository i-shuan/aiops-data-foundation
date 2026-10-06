@echo off
rem One-command deploy to a LOCAL Kubernetes (Rancher Desktop, containerd).
rem This file must stay pure ASCII: cmd.exe misreads non-ASCII text in batch files.
rem The real script, with usage notes, is deploy.ps1 next to this file.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
exit /b %ERRORLEVEL%
