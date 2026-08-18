@echo off
setlocal
cd /d "%~dp0"
call "%~dp0node\node.exe" "%~dp0node\node_modules\npm\bin\npm-cli.js" %*
