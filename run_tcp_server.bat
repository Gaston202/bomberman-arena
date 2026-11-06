@echo off
cd /d "%~dp0"
python tcp_version\server_tcp.py %*
pause
