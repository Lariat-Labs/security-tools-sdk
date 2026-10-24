"""Teams bot for the agent gateway (Microsoft 365 Agents SDK).

Teams SSO gives the bot a token for its own API (``api://botid-<app-id>``); the bot
exchanges it on-behalf-of the user for a gateway token and calls ``/agents`` exactly
like the web chat. The bot has no secrets: it authenticates with a federated credential
from its managed identity, and the Azure Bot OAuth connection uses a federated
credential too.
"""

__version__ = "0.1.0"
