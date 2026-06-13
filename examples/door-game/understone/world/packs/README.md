# Bundled alternate worlds

This directory holds **bundled alternate worlds** — zero or more content packs
that ship with Understone alongside the default Vale of Understone (which lives
one level up, in `../data/`).

Each alternate is its own subdirectory containing a `world.json` (and the rest
of the pack's JSON files). The slug is the subdirectory name. `understone
worlds` discovers the Vale plus every pack here that carries a `world.json`,
loads each one, and reports whether it is sound.

This directory ships with one bundled alternate world — **The Cinder Wastes**
(`cinder-wastes/`), an ashen volcanic underworld authored against `AUTHORING.md`.
More are added here as they are written. To serve one, point the server at it:

```bash
UNDERSTONE_WORLD=understone/world/packs/<slug> understone
```

The default Vale needs no setting at all.
