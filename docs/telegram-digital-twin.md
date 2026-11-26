# Telegram guest digital twin

Marlow can serve every authorized Telegram source outside the exact configured
super-admin conversation as a guest-facing digital twin of the profile owner.
Guest turns combine a global role prompt with the existing per-chat or per-topic
prompt.

This feature changes context, not authorization. It does not widen Telegram
allowlists or mention gates, grant admin permissions, expose personal memory,
or let guests capture or govern Work Experience.

## Configure the admin boundary

Configure the trusted administrator and destination locally:

```yaml
approvals:
  admin:
    enabled: true
    conversation_mode: super_admin
    platform: telegram
    user_id: "123456789"
    chat_id: "123456789"
    thread_id: null
```

Configure this block locally and restart the gateway. `user_id` remains the
privileged authorization boundary. The exact configured user, chat, and
optional thread form the super-admin conversation excluded from guest mode.

When `thread_id` is set, that administrator is elevated only in that topic.
When it is unset, the administrator is elevated throughout the configured
chat. Other users in the same chat remain guests.

## Enable the digital twin

```yaml
experience:
  telegram_digital_twin:
    enabled: true
    system_prompt: >-
      Represent the profile owner's established working style for guests.
      Be concise, practical, and transparent that you are an AI assistant.

telegram:
  channel_prompts:
    "-1001234567890": "Focus on release engineering for this group."
    "42": "Focus on customer support for this topic."
```

The prompt order is:

1. Normal Marlow and Telegram context.
2. `experience.telegram_digital_twin.system_prompt`.
3. The matching `telegram.channel_prompts` entry.
4. The gateway's existing global ephemeral system prompt, when configured.

The channel prompt specializes the global guest role. Existing tool policies,
identity checks, and approval routing govern each action.

## Conversation and file access

Guest turns use a separate session and file workspace. They receive no private
memory grant and cannot resume the owner's Codex Thread. Cross-session search
is unavailable in guest mode.

## Rollback

Set `experience.telegram_digital_twin.enabled: false`. Existing Telegram
message routing and per-channel prompts still apply.
