"""Agent runtime for the agent gateway.

Runs secsdk-backed agents behind Azure API Management. APIM authenticates the user
and sends the user context as ``x-agw-*`` headers. Every model call goes back
through APIM, where per-user, per-team, per-agent and per-deployment token limits
apply.
"""

__version__ = "0.1.0"
