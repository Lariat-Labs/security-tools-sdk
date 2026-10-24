"""Web chat for the agent gateway: a sign-in page plus a backend that calls the gateway
on behalf of the signed-in user (OAuth 2.0 on-behalf-of).

The backend is the "middle tier" a Teams bot would also be: it receives a user token
for its own API, exchanges it for a gateway token, and calls ``/agents``. APIM still
sees the real user, so the same entitlements and token limits apply.
"""

__version__ = "0.1.0"
