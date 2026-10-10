# Release Process

Beginning with 1.8.0, Turnstone separates the stable checkout from next-release
development while preserving real Git ancestry between supported lines.

## Branch Roles

| Branch | Versions | Purpose | Docker aliases |
|--------|----------|---------|----------------|
| `main` | final `X.Y.Z` | Current stable release and install source | exact, `:X.Y`, `:stable`, `:latest` |
| `dev` | `X.Y.0aN`, `bN`, or `rcN` | Next-release integration and GitHub default | exact, `:experimental` |
| `stable/X.Y` | final `X.Y.Z` | Supported prior minor after `main` moves on | exact, `:X.Y` |

`main` is the only writable authority for the current stable minor. Do not
maintain a same-minor `stable/X.Y` branch beside it. Immediately before `main`
advances to a new minor, preserve its current tip as `stable/X.Y`; that branch
then becomes the maintenance line for the prior minor.

Archived stable branches and their released artifacts may remain available even
after support ends, but they are not active release targets.

## Pull Request Targets and Forward Merges

- Features, refactors, and fixes needed only by the next release target `dev`.
- Fixes needed by the current stable release target `main`.
- Fixes needed by a supported prior minor start on the oldest affected
  `stable/X.Y` branch.

Move stable fixes forward in order:

```text
stable/X.Y  →  main  →  dev
```

Use a merge commit or a true fast-forward for branch-to-branch synchronization.
Do not squash, rebase, or cherry-pick synchronization PRs: the shared ancestry
is what lets Git recognize the same fix the next time the branches meet. This
restriction applies to synchronization PRs; ordinary focused PRs may follow the
repository's normal merge policy.

Release-only metadata belongs to the destination branch. When a forward merge
touches `pyproject.toml`, `turnstone/__init__.py`, `uv.lock`, or the changelog
header, keep the destination line's version and release state while accepting
the source branch's code and documentation changes. The Helm chart's
`appVersion` is the exception: keep whichever side names the newer stable
release, because every branch's chart defaults to a stable image, and if that
changes the destination's `appVersion`, bump the chart's patch `version` too.
The release commit itself should still become an ancestor of newer branches.

Never merge `dev` backward into a stable branch outside the deliberate promotion
process.

## Version Scheme

Turnstone uses [PEP 440](https://peps.python.org/pep-0440/) versions in one PyPI
package:

- `1.8.1` — stable release
- `1.9.0a1` — alpha pre-release
- `1.9.0b1` — beta pre-release
- `1.9.0rc1` — release candidate
- `1.9.0` — promoted stable release

## Release Helper

`scripts/release.sh` validates the version and branch, requires a clean worktree,
updates `pyproject.toml`, `turnstone/__init__.py` and the Helm chart's
`appVersion`, regenerates `uv.lock`, commits, and creates the `vX.Y.Z` tag. With
`--push`, it pushes the branch and tag atomically so CI never observes only half
of a release. Use `--push` only for pre-releases. A stable release points its
branch's chart at its own image, which exists only after the tag's CI passes and
the Docker publish finishes, so push a stable release's tag first and its branch
once both publish runs succeed.

The chart's `appVersion` is its default image tag. A stable release sets it to
that release on its own branch (`main` or `stable/X.Y`). A pre-release sets
`dev`'s to the newest stable `vX.Y.Z` tag instead, so installing the chart from
`dev`, the default branch, never selects a pre-release image; fetch tags before
cutting one. The helper refuses to move `appVersion` backwards, which catches a
clone that is missing the newest stable tag. `dev`'s default can trail `main`'s
newest patch until the next pre-release, forward merge, or `dev` chart update
(see the stable patch steps below). Pin `image.tag` to deploy anything else.

When a release changes `appVersion`, it also bumps the chart's patch `version`,
because a deployment that follows the chart from Git may rebuild it only when
that version changes. Chart changes between releases should bump the chart
`version` for the same reason.

The helper enforces these release locations:

- pre-releases: `dev` only
- current stable releases: `main`
- prior-line patches: the matching `stable/X.Y` branch

### Next-release pre-release

```bash
git switch dev
git pull --ff-only origin dev
git fetch --tags origin
scripts/release.sh 1.9.0rc1 --push
```

### Current stable patch

```bash
git switch main
git pull --ff-only origin main
scripts/release.sh 1.8.1
git push origin v1.8.1
```

The tag's CI starts the PyPI and Docker publish workflows. Push `main` only
after both succeed: the release commit moves the chart's default image to
`1.8.1`, and until that image is published, an install or upgrade that follows
`main`'s chart cannot pull it. If the tag's CI fails, rerun its failed jobs
rather than pushing `main`.

```bash
git push origin main
```

Then bring the new default image to `dev`. While stable fixes land on `main`,
forward-merge the release commit from `main` into `dev`, retaining the
development version in the release metadata files and taking `main`'s newer
chart `appVersion`. While fixes land on `dev` and reach `main` as backports,
nothing merges `main` into `dev`, so open a `dev` pull request that sets the
chart's `appVersion` to the release and bumps its patch `version` instead.

### Prior stable patch

```bash
git switch stable/1.8
git pull --ff-only origin stable/1.8
scripts/release.sh 1.8.2
git push origin v1.8.2
```

Push the branch once both publish runs succeed, for the same reason:

```bash
git push origin stable/1.8
```

Then merge `stable/1.8` into `main` and `main` into `dev`, retaining each
destination branch's version metadata. This replaces the old repeated
cherry-pick workflow.

## Promoting `dev` to Stable

For a 1.8 to 1.9 promotion:

1. Freeze `dev` and forward-merge the latest `main` into it.
2. Finalize the changelog and run the full release gates on `dev`.
3. Create `stable/1.8` from the current `main` tip. It becomes the prior-line
   maintenance branch only when `main` advances.
4. Merge `dev` into `main` with ancestry preserved.
5. From `main`, run `scripts/release.sh 1.9.0`, push the `v1.9.0` tag, and push
   `main` once both publish runs succeed. The publish workflows compare `main`'s
   minor version with the tag's to decide whether 1.9.0 is marked the latest
   release and moves the `stable` and `latest` image tags, so step 4 must leave
   `main` on `dev`'s 1.9 version.
6. Fast-forward `dev` to the 1.9.0 release commit, then begin the next
   pre-release cycle there.

Do not create `stable/1.9` at the 1.9.0 release. `main` already owns that line;
create the maintenance branch only when `main` is about to advance again.

## 1.8.0 Cutover

The initial transition has no existing `dev` branch:

1. Merge the 1.8.0 release preparation into `main` and run the full gates.
2. From `main`, run `scripts/release.sh 1.8.0` to create the release commit and
   tag locally.
3. Create `dev` from `v1.8.0`, then atomically push `main`, `dev`, and the tag.
4. Make `dev` the GitHub default branch.
5. Start the 1.9 line on `dev`; publish `1.9.0a1` when that first alpha is ready.
6. Leave the 1.7 and earlier stable branches frozen.

GitHub's default branch is `dev`, so normal clones and pull requests begin on
the development line. Stable install paths select `main` explicitly, and
Renovate is configured to target `dev` directly.

## CI and Publication

Pushes and pull requests to `main`, `dev`, and `stable/*` run CI. A `v*` tag runs
the same gates; only a successful same-repository tag run can start the PyPI,
GitHub Release, and Docker publication jobs.

Pre-release tags produce:

- a PyPI pre-release
- a GitHub pre-release that is never marked latest
- exact and `:experimental` Docker tags

Final tags on the current `main` minor produce:

- a normal PyPI and GitHub release, marked latest on GitHub
- exact, `:X.Y`, `:stable`, and `:latest` Docker tags

Final tags on a prior `stable/X.Y` line produce a normal PyPI/GitHub maintenance
release plus exact and `:X.Y` Docker tags. They do not move GitHub's latest
release or the global Docker aliases backward.

## Dependency Updates

Renovate targets `dev`. A dependency or supply-chain fix needed on a stable line
starts on the oldest affected supported branch and follows the same forward-merge
path as any other stable fix.
