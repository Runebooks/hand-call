FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ common/
COPY agents/ agents/

ENV PYTHONUNBUFFERED=1
ENV A2A_PORT=8082
ENV A2A_HOST=0.0.0.0

EXPOSE 8082

CMD ["python", "-m", "agents.kubernetes.server"]
