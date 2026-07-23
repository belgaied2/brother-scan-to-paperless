FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/belgaied2/brother-scan-to-paperless"
LABEL org.opencontainers.image.description="Brother scanner Scan-to-Paperless daemon"

RUN apt-get update && apt-get install -y sane-utils snmp wget && \
    wget -q https://download.brother.com/welcome/dlf105200/brscan4-0.4.11-1.amd64.deb && \
    dpkg -i --force-all brscan4-0.4.11-1.amd64.deb && \
    rm brscan4*.deb && apt-get clean && \
    mkdir -p /etc/sane.d && \
    echo "brother4" > /etc/sane.d/dll.conf && \
    brsaneconfig4 -a name=BROTHER model=MFC-L2710DN ip=192.168.1.32

COPY src/brother_scan_daemon.py /opt/brother-scan-to-paperless/brother_scan_daemon.py
COPY entrypoint.sh /opt/brother-scan-to-paperless/entrypoint.sh

RUN chmod +x /opt/brother-scan-to-paperless/brother_scan_daemon.py && \
    chmod +x /opt/brother-scan-to-paperless/entrypoint.sh && \
    ln -s /opt/brother-scan-to-paperless/brother_scan_daemon.py /usr/local/bin/brother-scan-to-paperless

ENTRYPOINT ["/opt/brother-scan-to-paperless/entrypoint.sh"]
CMD ["run"]
