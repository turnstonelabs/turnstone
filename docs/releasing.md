# Release Process

Turnstone ships several parallel release tracks from a single PyPI package.

## Release Tracks

| Track | Versions | Branch | Docker tags | PyPI install |
|-------|----------|--------|-------------|--------------|
| **Stable 1.5** | `1.5.x` | `stable/1.5` | `:1.5.x`, `:1.5` | `pip install 'turnstone==1.5.*'` |
| **Stable 1.6** | `1.6.x` | `stable/1.6` | `:1.6.x`, `:1.6`, `:stable`, `:latest` | `pip install turnstone` |
| **Experimental** | `1.7.0aN` | `main` | `:1.7.0aN`, `:experimental` | `pip install turnstone --pre` |

- **Stable** tracks receive bugfixes only.  The most-recent stable minor
  owns the `:stable` / `:latest` Docker tags and the default PyPI
  install.
- **Experimental** (always on `main`) receives new features. May be
  rough around the edges.
- When experimental matures, it is promoted to a new stable minor via
  a `stable/X.Y` branch. One prior stable track is maintained alongside
  the current one; at each promotion the oldest track is retired — its
  branch is deleted, while its tags and released artifacts remain
  available.

## Version Scheme

[PEP 440](https://peps.python.org/pep-0440/) pre-release suffixes on a single package:

- `1.0.0` — stable release
- `1.1.0a1` — alpha (experimental)
- `1.1.0b1` — beta (experimental, more stable)
- `1.1.0rc1` — release candidate (experimental, nearly stable)
- `1.1.0` — promoted to stable

## Releasing an Experimental Version (from main)

```bash
scripts/release.sh 1.7.0a2 --push
```

This bumps `pyproject.toml` + `turnstone/__init__.py`, regenerates `uv.lock`, commits, tags `v1.7.0a2`, and pushes. CI runs, then publish + Docker workflows fire automatically.

## Releasing a Stable Patch (from stable/X.Y)

```bash
git checkout stable/1.6
git cherry-pick <commit-hash>    # bugfix from main
scripts/release.sh 1.6.1 --push
```

## Promoting Experimental to Stable

When `main` is ready for a stable release:

```bash
# 1. Tag the stable release on main
scripts/release.sh 1.6.0 --push

# 2. Create the stable maintenance branch from that tag
git branch stable/1.6 v1.6.0
git push origin stable/1.6

# 3. Start the next experimental cycle on main
scripts/release.sh 1.7.0a1 --push
```

The previous stable branch continues to receive security-only patches;
the track before it is retired at each promotion (at 1.6.0:
`stable/1.5` stays maintained, `stable/1.4` is retired).

## CI/CD Pipeline

All releases are gated on CI success:

1. `git push` with `v*` tag triggers **CI** (lint, typecheck, test, test-postgres, lock-check, security audit)
2. On CI success, **Publish to PyPI** fires via `workflow_run`
3. On CI success, **Publish Docker Image** fires via `workflow_run`

Pre-release tags (`a`, `b`, `rc` suffixes) produce:
- PyPI: pre-release version (not installed by default)
- GitHub Release: marked as pre-release
- Docker: `:experimental` alias + exact version tag

Stable tags produce:
- PyPI: stable version (default `pip install`)
- GitHub Release: full release
- Docker: `:stable`, `:latest`, `:X.Y`, `:X.Y.Z` tags

## Dependency Updates

Renovate targets `main` (experimental) only. Stable branches receive manual dependency updates via cherry-pick when security-relevant.
