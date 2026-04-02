# Release Process

Turnstone uses two parallel release tracks published from a single PyPI package.

## Release Tracks

| Track | Versions | Branch | Docker tags | PyPI install |
|-------|----------|--------|-------------|--------------|
| **Stable** | `1.0.0`, `1.0.1` | `stable/1.0` | `:1.0.1`, `:1.0`, `:stable`, `:latest` | `pip install turnstone` |
| **Experimental** | `1.1.0a1`, `1.1.0a2` | `main` | `:1.1.0a1`, `:experimental` | `pip install turnstone --pre` |

- **Stable** receives bugfixes only. Production-grade.
- **Experimental** receives new features. May be rough around the edges.
- When experimental matures, it is promoted to stable. The previous stable branch stops receiving patches.

## Version Scheme

[PEP 440](https://peps.python.org/pep-0440/) pre-release suffixes on a single package:

- `1.0.0` — stable release
- `1.1.0a1` — alpha (experimental)
- `1.1.0b1` — beta (experimental, more stable)
- `1.1.0rc1` — release candidate (experimental, nearly stable)
- `1.1.0` — promoted to stable

## Releasing an Experimental Version (from main)

```bash
scripts/release.sh 1.1.0a2 --push
```

This bumps `pyproject.toml` + `turnstone/__init__.py`, regenerates `uv.lock`, commits, tags `v1.1.0a2`, and pushes. CI runs, then publish + Docker workflows fire automatically.

## Releasing a Stable Patch (from stable/X.Y)

```bash
git checkout stable/1.0
git cherry-pick <commit-hash>    # bugfix from main
scripts/release.sh 1.0.2 --push
```

## Promoting Experimental to Stable

When `main` is ready for a stable release:

```bash
# 1. Tag the stable release on main
scripts/release.sh 1.1.0 --push

# 2. Create the stable maintenance branch from that tag
git branch stable/1.1 v1.1.0
git push origin stable/1.1

# 3. Start the next experimental cycle on main
scripts/release.sh 1.2.0a1 --push
```

The previous `stable/1.0` branch stops receiving patches at this point.

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
