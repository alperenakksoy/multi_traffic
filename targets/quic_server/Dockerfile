FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl libssl-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
# Generate self-signed TLS certificate for QUIC (TLS 1.3 required)
RUN openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
    -days 365 -nodes -subj "/CN=target-quic" \
    -addext "subjectAltName=DNS:target-quic,DNS:localhost,IP:127.0.0.1"
EXPOSE 4433/udp
CMD ["python", "server.py"]
