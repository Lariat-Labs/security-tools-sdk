# 12 · Wrap-up, Teams licensing, and teardown

*What you now know, how to finish Teams, and how to switch it all off.*

**The security model in one breath:** users prove who they are with Entra tokens; APIM turns
groups into tier, team and entitlement; the runtime only trusts APIM; the models only trust
APIM; every model call is counted under the user; no component holds a secret, because every
service-to-service hop uses a managed identity or a federated credential.

## Finish the Teams test

Teams needs a **member** work account in the gateway's tenant with a Teams licence.

1. **Use a member account for Microsoft 365 admin.** Guest accounts (people invited from
   another tenant or a personal Microsoft account) generally can't use the Microsoft 365 admin
   center, even as Global Administrator. Create a member user in the tenant and give it an
   admin role. It also needs a **usage location** before any licence can be assigned:
    ```bash
    az rest --method PATCH --url "https://graph.microsoft.com/v1.0/users/<user-object-id>" \
      --headers Content-Type=application/json --body '{"usageLocation": "US"}'
    ```
2. **Start a trial** as that user at [admin.microsoft.com](https://admin.microsoft.com):
   Billing → Purchase services → Microsoft 365 Business Basic → free trial.
3. **Assign the licence**: Users → Active users → the user → Licenses and apps. Teams can take
   15–30 minutes to provision.
4. **Give the user agent access**: add them to a tier, a team and an agent group.
5. **Upload the app** in the [Teams admin center](https://admin.teams.microsoft.com): Teams apps
   → Manage apps → Upload new app → `deploy/agent-gateway/build/agent-gateway-teams.zip`.
6. **Try it**: sign in to Teams as that user, open **Agent Gateway**, send `me`, then a CVE
   question.

## When you're done

!!! danger "Destructive: delete everything in Azure (irreversible)"
    APIM Standard v2, Event Hubs and the always-on runtime cost money every day the deployment
    exists. App registrations and groups in Entra are separate and stay until you delete them.
    ```bash
    az group delete -n $RG
    ```
    With Terraform, use `terraform destroy` instead: it also removes the app registrations.

??? question "Final check: trace one Teams message through every component you met."
    Teams → Azure Bot → bot container (Bot Service token) → Teams SSO token → OBO via Entra
    (bot identity's federated credential) → APIM front door (token, groups → tier, team, cost
    center) → runtime (APIM identity) → APIM model route (runtime identity, six token counters,
    metrics, usage event) → gpt-5-mini (APIM identity) → tools via secsdk → spans and span
    events → workbooks → proactive reply in Teams.

[Back to the field manual index](index.md)
