# Microsoft Teams Gateway

The bundled Teams platform plugin exposes Marlow to Microsoft Teams through the
official `microsoft-teams-apps==2.0.16` SDK.

Runtime configuration is read from `config.yaml` under `teams:` and from the
profile secrets `TEAMS_CLIENT_SECRET` and, when using a separate Graph/RSC
application, `TEAMS_GRAPH_CLIENT_SECRET`. The plugin is disabled by default and
supports Milestone 1 personal chats, group chats, and standard channel threads
for one configured tenant.

## Bot and Graph identities

Marlow uses `teams.client_id` and `TEAMS_CLIENT_SECRET` for Bot Framework
messaging. Thread-context hydration uses the effective Graph identity:
`teams.graph_client_id` when configured, otherwise `teams.client_id`. If the
Graph identity differs from the Bot identity and `thread_context.enabled` is
`true`, `TEAMS_GRAPH_CLIENT_SECRET` is required. This lets a Teams app use one
Entra application for Bot Framework and a different Entra application for the
Team RSC grant.

## Acknowledgement reactions

Operators can enable a best-effort acknowledgement reaction for supported Teams
turns:

```yaml
teams:
  reactions:
    enabled: true
```

When enabled, Marlow schedules a non-blocking `👀` reaction on the exact
triggering user message as processing starts. Reaction delivery is optional:
normal Teams ingress, duplicate suppression, dispatch, and final text delivery
continue even if the reaction is throttled, delayed, or rejected. The feature is
disabled by default and does not require a Teams manifest change or Graph
permissions. Operators may also set `TEAMS_REACTIONS=true` to enable it without
editing YAML.
