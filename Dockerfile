FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/belgaied2/brother-scan-to-paperless"
LABEL org.opencontainers.image.description="Brother scanner Scan-to-Paperless daemon"

RUN apt-get update && apt-get install -y sane-utils snmp wget && \
    wget -q https://download.brother.com/welcome/dlf105200/brscan4-0.4.11-1.amd64.deb && \
    dpkg -i --force-all brscan4-0.4.11-1.amd64.deb && \
    rm brscan4*.deb && apt-get clean

COPY src/ /opt/brother-scan-to-paperless/src
WORKDIR /opt/brother-scan-to-paperless

CMD ["python", "-m", "src", "run"]
