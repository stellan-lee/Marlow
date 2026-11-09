# Bundled web search providers — plugins/web/.
#
# Each subdirectory provides a web-search backend:
#   plugins/web/<name>/{plugin.yaml, __init__.py, provider.py}
#
# They auto-load via kind: backend and register via
# ctx.register_web_search_provider() into agent.web_search_registry.
