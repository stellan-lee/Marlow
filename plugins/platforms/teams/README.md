# Microsoft Teams Gateway

The bundled Teams platform plugin exposes Marlow to Microsoft Teams through the
official `microsoft-teams-apps==2.0.16` SDK.

Runtime configuration is read from `config.yaml` under `teams:` and from the
profile secret `TEAMS_CLIENT_SECRET`. The plugin is disabled by default and
supports Milestone 1 personal chats, group chats, and standard channel threads
for one configured tenant.
