# Build from the repository root:
#   docker build --platform linux/amd64 -t agw-teams:0.1.0 -f deploy/agent-gateway/teams/Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# secsdk only for its telemetry helper (Application Insights setup).
COPY packages/secsdk /src/secsdk
RUN pip install --no-cache-dir "/src/secsdk[azure-monitor]" "httpx>=0.28" "aiohttp>=3.10" \
        "microsoft-agents-hosting-aiohttp==1.8.0" "microsoft-agents-authentication-msal==1.8.0" \
        "microsoft-agents-hosting-core==1.8.0" "microsoft-agents-activity==1.8.0" \
    && rm -rf /src
COPY deploy/agent-gateway/teams/agw_teams /app/agw_teams

USER 10001
EXPOSE 3978
CMD ["python", "-m", "agw_teams.bot"]
