#!/bin/sh
set -e

echo "entrypoint: configuring MFC-L2710DN scanner at 192.168.1.32"
mkdir -p /etc/sane.d
echo "brother4" > /etc/sane.d/dll.conf
brsaneconfig4 -a name=BROTHER model=MFC-L2710DN ip=192.168.1.32 2>&1 || true

echo "entrypoint: testing scanner connection"
brsaneconfig4 -q | grep -q BROTHER && echo "scanner registered" || echo "scanner registration failed"

echo "entrypoint: starting brother-scan-to-paperless daemon"
exec /opt/brother-scan-to-paperless/brother_scan_daemon.py "$@"
