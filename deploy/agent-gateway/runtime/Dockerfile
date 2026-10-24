# Build from the repository root so secsdk can be installed from source:
#   az acr build -r <registry> -t agw-runtime:0.1.0 -f deploy/agent-gateway/runtime/Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SECSDK_CACHE_DIR=/tmp/secsdk

WORKDIR /app
COPY packages/secsdk /src/secsdk
RUN pip install --no-cache-dir "/src/secsdk[azure-monitor]" \
        "fastapi>=0.162" "uvicorn>=0.54" "openai>=3.37" "azure-identity>=1.26" "pyyaml>=6.0" \
    && rm -rf /src

COPY deploy/agent-gateway/runtime/agw_runtime /app/agw_runtime

USER 10001
EXPOSE 8000
CMD ["uvicorn", "agw_runtime.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
