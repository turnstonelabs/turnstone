# Bundled alternate worlds

This directory holds **bundled alternate worlds** — zero or more content packs
that ship with Understone alongside the default Vale of Understone (which lives
one level up, in `../data/`).

Each alternate is its own subdirectory containing a `world.json` (and the rest
of the pack's JSON files). The slug is the subdirectory name. `understone
worlds` discovers the Vale plus every pack here that carries a `world.json`,
loads each one, and reports whether it is sound.

The directory ships effectively empty (this README is the placeholder that keeps
it under version control); alternate worlds are added here as they are authored.
To serve one, point the server at it:

```bash
UNDERSTONE_WORLD=understone/world/packs/<slug> understone
```

The default Vale needs no setting at all.
