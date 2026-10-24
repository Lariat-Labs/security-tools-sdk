"""The Teams bot: Microsoft 365 Agents SDK wiring. Run with: python -m agw_teams.bot

Configuration (environment; parsed by the SDK's ``load_configuration_from_env``):

    CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE=FederatedCredentials
    CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<bot app registration id>
    CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<tenant id>
    CONNECTIONS__SERVICE_CONNECTION__SETTINGS__FEDERATEDCLIENTID=<bot managed identity client id>
    AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GATEWAY__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME=teams-sso
    AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GATEWAY__SETTINGS__OBOCONNECTIONNAME=SERVICE_CONNECTION
    AGW_GATEWAY_URL=https://<apim>.azure-api.net
    AGW_GATEWAY_APP_ID=<gateway app registration id>
    AGW_DEFAULT_AGENT=cve-triage            (optional)

How a message is handled:

1. The route requires the ``GATEWAY`` auth handler, so the SDK signs the user in (Teams
   SSO through the Azure Bot OAuth connection) before the handler runs.
2. The handler exchanges the SSO token on-behalf-of the user for a gateway token.
3. It acknowledges at once, then runs the agent in the background and posts the answer
   as a proactive message (runs take longer than a Teams turn should).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from os import environ
from typing import Any

from aiohttp import web
from microsoft_agents.activity import Activity, ActivityTypes, load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft_agents.hosting.core import (
    AgentApplication,
    Authorization,
    MemoryStorage,
    TurnContext,
    TurnState,
)

from .conversation import (
    HELP,
    ConversationStore,
    format_answer,
    format_error,
    format_profile,
    parse,
)
from .gateway import GatewayClient

log = logging.getLogger("agw_teams")

CONFIG = load_configuration_from_env(environ)
STORAGE = MemoryStorage()
CONNECTION_MANAGER = MsalConnectionManager(**CONFIG)
ADAPTER = CloudAdapter(connection_manager=CONNECTION_MANAGER)
AUTHORIZATION = Authorization(STORAGE, CONNECTION_MANAGER, **CONFIG)
AGENT_APP = AgentApplication[TurnState](
    storage=STORAGE, adapter=ADAPTER, authorization=AUTHORIZATION, **CONFIG
)

HANDLER = "GATEWAY"
BOT_APP_ID = environ.get("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", "")
GATEWAY_SCOPE = f"api://{environ.get('AGW_GATEWAY_APP_ID', '')}/.default"
DEFAULT_AGENT = environ.get("AGW_DEFAULT_AGENT", "")
GATEWAY = GatewayClient(environ.get("AGW_GATEWAY_URL", "http://localhost"))
CONVERSATIONS = ConversationStore()
_background: set[asyncio.Task[Any]] = set()  # keep references so tasks aren't collected


async def _gateway_token(context: TurnContext) -> str:
    response = await AGENT_APP.auth.exchange_token(context, [GATEWAY_SCOPE], HANDLER)
    if not response or not response.token:
        raise RuntimeError("No gateway token: sign in again with 'signout' then any message.")
    return str(response.token)


async def _reply_later(reference: Any, text: str) -> None:
    async def send(ctx: TurnContext) -> None:
        await ctx.send_activity(text)

    await ADAPTER.continue_conversation(BOT_APP_ID, reference.get_continuation_activity(), send)


async def _run_in_background(
    reference: Any, token: str, agent: str, question: str, convo_id: str
) -> None:
    convo = CONVERSATIONS.get(convo_id)
    try:
        status, body = await GATEWAY.run(token, agent, question, convo.history, convo.session_id)
        if status == 200:
            convo.remember(question, body.get("output", ""))
            text = format_answer(agent, body)
        else:
            text = format_error(status, body)
    except Exception as exc:  # never leave the user without an answer
        log.exception("agent run failed")
        text = f"⚠️ The run failed: {exc}"
    await _reply_later(reference, text)


@AGENT_APP.conversation_update("membersAdded")
async def welcome(context: TurnContext, _state: TurnState) -> None:
    bot_id = context.activity.recipient.id if context.activity.recipient else None
    if any(m.id != bot_id for m in context.activity.members_added or []):
        await context.send_activity(HELP)


@AGENT_APP.message("help")
async def on_help(context: TurnContext, _state: TurnState) -> None:
    await context.send_activity(HELP)


@AGENT_APP.message("signout")
async def on_signout(context: TurnContext, _state: TurnState) -> None:
    await AGENT_APP.auth.sign_out(context)
    await context.send_activity("Signed out.")


@AGENT_APP.message(re.compile(r".*", re.S), auth_handlers=[HANDLER])
async def on_message(context: TurnContext, _state: TurnState) -> None:
    command = parse(context.activity.text or "")
    convo = CONVERSATIONS.get(context.activity.conversation.id)

    if command.name == "help":
        await context.send_activity(HELP)
        return
    if command.name == "new":
        convo.reset()
        await context.send_activity("Started a new conversation.")
        return

    token = await _gateway_token(context)
    if command.name == "me":
        status, profile = await GATEWAY.me(token)
        await context.send_activity(
            format_profile(profile) if status == 200 else format_error(status, profile)
        )
        return
    if command.name in ("agents", "use") or not convo.agent:
        status, agents = await GATEWAY.agents(token)
        if status != 200:
            await context.send_activity(format_error(status, agents))
            return
        names = [a["name"] for a in agents]
        if not names:
            await context.send_activity("🔒 You don't have access to any agents yet.")
            return
        if command.name == "agents":
            listing = "\n".join(f"- **{a['name']}**: {a['description']}" for a in agents)
            await context.send_activity(
                f"Agents you can use:\n\n{listing}\n\nCurrent: **{convo.agent or names[0]}**"
            )
            return
        if command.name == "use":
            if command.arg not in names:
                await context.send_activity(
                    f"🔒 You can't use '{command.arg}'. Try: {', '.join(names)}"
                )
                return
            convo.agent = command.arg
            await context.send_activity(f"Using **{convo.agent}**.")
            return
        convo.agent = DEFAULT_AGENT if DEFAULT_AGENT in names else names[0]

    await context.send_activity(Activity(type=ActivityTypes.typing))
    await context.send_activity(f"Working on it with **{convo.agent}**… this can take a minute.")
    reference = context.activity.get_conversation_reference()
    task = asyncio.create_task(
        _run_in_background(reference, token, convo.agent, command.arg, convo.conversation_id)
    )
    _background.add(task)
    task.add_done_callback(_background.discard)


# ----- HTTP server ----------------------------------------------------------------------------


@web.middleware
async def _auth(request: web.Request, handler: Any) -> web.StreamResponse:
    if request.path == "/healthz":  # container probe: no Bot Service token
        return await handler(request)
    return await jwt_authorization_middleware(request, handler)


async def _messages(request: web.Request) -> web.StreamResponse:
    response = await start_agent_process(request, AGENT_APP, ADAPTER)
    return response or web.Response(status=202)


async def _healthz(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def create_server() -> web.Application:
    app = web.Application(middlewares=[_auth])
    app.router.add_post("/api/messages", _messages)
    app.router.add_get("/healthz", _healthz)
    app["agent_configuration"] = CONNECTION_MANAGER.get_default_connection_configuration()
    app["agent_app"] = AGENT_APP
    app["adapter"] = ADAPTER
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        from secsdk import telemetry

        telemetry.configure(service_name=os.environ.get("OTEL_SERVICE_NAME", "agent-gateway-teams"))
    web.run_app(create_server(), host="0.0.0.0", port=int(os.environ.get("PORT", "3978")))


if __name__ == "__main__":
    main()
