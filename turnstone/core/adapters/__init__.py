"""SessionKindAdapter implementations.

Each adapter bridges a concrete kind (interactive, coordinator) into
the kind-agnostic SessionManager by providing transport (how lifecycle
events fan out) and construction (what UI + ChatSession pair the kind
uses).
"""
