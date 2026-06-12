"""Screen layer — pure rendering of game state into text frames.

No ANSI SGR escape sequences are emitted in v1; colour is carried as
metadata on cells for a future renderer but never written to output.
"""
