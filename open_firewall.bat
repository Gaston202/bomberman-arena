@echo off
rem Run ONCE as Administrator on the SERVER PC (right click > Run as administrator)
netsh advfirewall firewall add rule name="Arena TCP 8000" dir=in action=allow protocol=TCP localport=8000
netsh advfirewall firewall add rule name="Arena UDP 9000" dir=in action=allow protocol=UDP localport=9000
echo Done. Ports TCP 8000 and UDP 9000 are open for incoming connections.
pause
