@echo off
rem usage: run_client.bat udp 192.168.1.20      (or tcp)
cd /d "%~dp0"
python client\pygame_client.py --proto %1 --host %2
pause
