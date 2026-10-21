"""AA-925 (Quality System v1, Q-2): deterministic quality checks shared by the writers.

Modules here are pure and never import a writer module (graph, flag_fix_node, service, …) at load time —
those import them. Gate runners a check needs are passed in as callables.
"""
