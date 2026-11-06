@echo off
cd /d "%~dp0"
python udp_version\server_udp.py %*
pause
