# 02 · Identity: who you are, in a token

*Everything downstream trusts one thing: a signed Entra access token.*

An **app registration** describes an application to Entra. The gateway's registration
(`agent-gateway`) is an *API*: other things ask Entra for tokens *to* it. It defines two
kinds of permission:

- **Scope** `Agents.Invoke`: a *delegated* permission. A token with it means "this app acts
  for this signed-in person". It appears in the token as `scp`.
- **App roles**: `Agents.User` (given to groups) and `Gateway.Internal` (given to the APIM and
  runtime managed identities). They appear as `roles`.

Its **enterprise application** (the service principal) has *assignment required* on, so only
people in an assigned group can get a token at all. And it emits only the groups assigned to
it (`ApplicationGroup`), which keeps tokens small and private.

```mermaid
sequenceDiagram
    participant You
    participant CLI as Azure CLI
    participant E as Microsoft Entra ID
    You->>CLI: az account get-access-token --scope api://gateway/.default
    CLI->>E: you're signed in; Azure CLI is pre-authorised for Agents.Invoke
    E->>E: are you assigned (via a group)? which assigned groups are you in?
    E-->>CLI: JWT: aud=gateway, scp=Agents.Invoke, oid, groups
    CLI-->>You: the token (valid about an hour)
```

JWT
:   JSON Web Token: three base64 parts (header, claims, signature). Anyone can read the
    claims; only Entra can sign them.

aud
:   Audience: which API the token is for. APIM rejects tokens meant for anything else.

oid
:   Your object id, stable forever. The gateway counts your tokens under it.

azp
:   The app that asked for the token: the Azure CLI here, the chat app in module 07.

## Steps

!!! example "Run: get a gateway token and print its claims (never the token)"
    ```bash
    az account get-access-token --scope "api://$AGW_GATEWAY_APP_ID/.default" --query accessToken -o tsv \
    | python3 -c "import sys,json,base64; p=sys.stdin.read().split('.')[1]; p+='='*(-len(p)%4); c=json.loads(base64.urlsafe_b64decode(p)); print(json.dumps({k:c.get(k) for k in ('aud','scp','roles','groups','oid','name','azp')}, indent=2))"
    ```
    Expect `scp: Agents.Invoke`, `roles: [Agents.User]`, and one group id for each gateway
    group you're in: your tier, team and agent groups.

!!! info "Look: the gateway app registration"
    Entra ID → App registrations → `agent-gateway`. Open **Expose an API** (the scope and the
    pre-authorised Azure CLI), **App roles**, and **Token configuration** (groups claim). Or
    print the direct link:
    ```bash
    echo "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationMenuBlade/~/Overview/appId/$AGW_GATEWAY_APP_ID"
    ```

!!! info "Look: who is allowed in"
    Entra ID → Enterprise applications → `agent-gateway` → **Users and groups** lists the
    tier, team and agent groups (role Agents.User) and the two managed identities (role
    Gateway.Internal). **Properties** shows "Assignment required? Yes".

??? question "Check: your token says nothing about your tier or cost center. Where do those come from?"
    From your **group ids**, interpreted by the gateway using `gateway.yaml`. The token proves
    membership; the gateway decides what membership means. Changing a team's cost center is a
    config change, not a token change.

[Next: the APIM front door →](03-front-door.md)
