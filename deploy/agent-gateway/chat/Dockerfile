# Build from the repository root:
#   docker build --platform linux/amd64 -t agw-chat:0.1.0 -f deploy/agent-gateway/chat/Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# secsdk only for its telemetry helper (Application Insights setup).
COPY packages/secsdk /src/secsdk
RUN pip install --no-cache-dir "/src/secsdk[azure-monitor]" \
        "fastapi>=0.162" "uvicorn>=0.54" "httpx>=0.28" "azure-identity>=1.26" "pyjwt[crypto]>=2.10" \
    && rm -rf /src
COPY deploy/agent-gateway/chat/agw_chat /app/agw_chat

USER 10001
EXPOSE 8000
CMD ["uvicorn", "agw_chat.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
