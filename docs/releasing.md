# Release Process

All development integrates on `main`. Feature work and user escalations share
one development flow; selected fixes are backported to supported stable lines.

## Branch Roles

| Branch | Purpose | Release versions |
|--------|---------|------------------|
| `main` | Default branch, ongoing development, and source install path | Final releases and pre-releases |
| `stable/X.Y` | Maintenance of a supported release line | Matching final `X.Y.Z` releases |

The former `main` became `stable/1.8`, and the former `dev` became `main`.
The separate `dev` branch is retired and must not be recreated.

Source downloads and the installer target `main`. Users who need the 1.8 line
must select `stable/1.8` explicitly. Versioned release archives continue to refer
to their immutable release tags. Archived branches and artifacts may remain
available after support ends without being active release targets.

## Pull Requests and Backports

Features, refactors, dependency updates, and fixes target `main`. Backport a
focused fix to a supported `stable/X.Y` branch when that line needs it, preserving
its version metadata and recording the source commit with `git cherry-pick -x`.
Do not merge ongoing development wholesale into a stable maintenance branch.

Before backporting, check whether the fix is already present by inspecting the
code. Generated lockfile changes can make equivalent commits have different
patch IDs.

## Preparing a Release

1. Turn the development changelog into concise, user-facing release notes:
   group related changes, explain their effect, and call out upgrade actions.
2. Add the final release heading, such as `1.9.0`, and commit the changelog
   preparation before running the preflight go/no-go checks.
3. Run the release gates against that committed tree, check open work that may
   belong in the release, and review the exact commits that will be tagged.
4. After the go decision, use `scripts/release.sh` to create the version bump
   and tag. Changelog preparation alone does not publish or tag a release.

## Release Helper

Versions follow PEP 440: `1.9.0a1`, `1.9.0b1`, and `1.9.0rc1` are pre-releases;
`1.9.0` and `1.9.1` are final releases.

`scripts/release.sh` validates the version and branch, requires a clean worktree,
updates `pyproject.toml`, `turnstone/__init__.py`, and the chart's `appVersion`,
regenerates `uv.lock`, commits, and creates the version tag. Pre-releases must
come from `main`; final releases may come from `main` or the matching
`stable/X.Y` branch.

The chart defaults to a released stable image. A final release sets its branch's
`appVersion` to that release. A pre-release keeps `main`'s chart on the newest
final tag; fetch tags first. The helper rejects a backwards `appVersion` change
and bumps the chart's patch version whenever its default image changes. Inspect
the lockfile diff before publishing: unrelated lock-format changes do not belong
in a version bump.

### Pre-release from main

```bash
git switch main
git pull --ff-only origin main
git fetch --tags origin
scripts/release.sh 1.9.0rc2 --push
```

`--push` atomically pushes the branch and tag. Use it only for pre-releases:
their chart still points to an already-published stable image.

### Final release from main

After the committed changelog and preflight have passed:

```bash
git switch main
git pull --ff-only origin main
scripts/release.sh 1.9.0
git push origin v1.9.0
```

Wait for the tag's CI and both publish workflows to succeed before pushing
`main`. The release commit changes the chart's default image, so that image must
exist before the branch exposes it to installs and upgrades. Rerun failed jobs
without moving the tag.

```bash
git push origin main
```

Preserve the released line as `stable/X.Y` before `main` advances to the next
minor. Continue development on `main`; no new development branch is created.

### Stable maintenance release

After backporting and validating the selected fixes:

```bash
git switch stable/1.8
git pull --ff-only origin stable/1.8
scripts/release.sh 1.8.8
git push origin v1.8.8
```

Again, wait for CI and both publish workflows before pushing the branch:

```bash
git push origin stable/1.8
```

When a maintenance release becomes the newest stable image, update `main`'s
chart `appVersion` and increment its patch version in a focused change. Do not
merge the maintenance line's release metadata into ongoing development.

## CI and Publication

Pushes and pull requests to `main` and `stable/*` run CI. A `v*` tag runs the same
gates; only a successful same-repository tag run starts publication.

Pre-releases produce a pre-release package and release entry plus exact and
`:experimental` image tags. They never move the stable aliases.

Final releases produce a normal package and release entry plus exact and `:X.Y`
image tags. Only the newest final version tag, ordered numerically, is marked
latest and moves `:stable` and `:latest`. This decision does not use `main`'s
version: `main` may already be developing a future minor. Older final tags do
not move the global aliases backwards.

Check both publish workflows independently. Success from one does not imply
that the other completed.

## Dependency Updates

Dependency update automation targets `main`. Backport dependency and supply-chain fixes to supported
stable lines through the same focused backport process as other fixes.
