"""AA-1002 (gateway slice S2) — provider adapters reached through the connection registry (``connections.py``).

An adapter sends one request on one connection and maps the provider's reply back to plain values; ``LLMClient``
(client.py) decides when an adapter is used (``vendor_route.py``), checks the reply against the request's schema,
prices it and builds the ``LLMResponse``. Adapters never log ``llm_call_log`` rows themselves.
"""
