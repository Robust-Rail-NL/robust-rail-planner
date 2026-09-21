# Contributing

## Git hooks

Tracked hooks live under [`.githooks/`](.githooks/). They aren't active
until you point git at them once per clone:

```
git config core.hooksPath .githooks
```

Two hooks live here:

- `pre-commit` refuses a direct commit to `edge` (see the branch flow rules
  below) while still allowing a `git merge --no-ff` completion.
- `pre-push` refuses a direct push to `main`'s `refs/heads/main` ref —
  push a feature branch and open a PR instead.

Like any hook, `--no-verify` skips either one — they're a backstop, not a
guarantee. Note that `core.hooksPath` replaces git's hook directory
wholesale: it won't fall back to `.git/hooks/` for hook types that aren't
tracked here, so any hook you want enforced needs to actually live under
`.githooks/`.

## Image channels: `stable` and `edge`

The planner image is published under two channels:

- **`stable`** — built from `main` via [`docker-push.sh`](docker-push.sh),
  tagged `planner:$VERSION` (and `planner:latest` for a non-prerelease
  version). Every change lands here through its own feature branch and a
  reviewed PR into `main` — `stable` means "last reviewed, released
  version."
- **`edge`** — a fast, lower-ceremony channel for running a not-yet-reviewed
  fix without waiting for its PR into `main`. Built and pushed via
  [`docker-push-edge.sh`](docker-push-edge.sh) under the floating
  `planner:edge` tag, always overwritten by the newest push to the `edge`
  branch.

### Branch flow

- **Every change still goes through its own feature branch and PR into
  `main`.** `edge` does not replace that — it runs alongside it. Never
  commit directly to `edge`.
- **To get a fix onto `edge` early**, merge its feature branch into `edge`
  (`git merge --no-ff <branch>`) in addition to opening the normal PR into
  `main`. Once that PR is reviewed and merged, `edge` already has the
  content — nothing needs to be cherry-picked or re-applied.
- **`edge` only ever advances via merge commits** (`git merge --no-ff`),
  whether merging in a feature branch or catching up with `main`. Never
  rebase `edge`, and never fast-forward it — the point is for its history to
  show what was merged in and when, as a readable sequence of merge commits,
  not a flattened line that hides which branch each change came from.
- **Flow between `edge` and `main` is one-directional**: `main → edge`
  only, via periodic `git merge --no-ff main`. Nothing flows from `edge`
  back into `main` directly, since nothing should ever exist on `edge` that
  doesn't also exist on some reviewed feature branch (see above).
- One accepted gap: if `edge` is ever force-pushed or rebased despite the
  above, a dropped commit won't show up in `git log` — only `git reflog` or
  GitHub's Actions run history would have it. Treated as a reasonable
  tradeoff for a fast-moving channel; revisit only if that actually causes a
  real problem.

### Publishing `edge`

Currently manual only — run [`docker-push-edge.sh`](docker-push-edge.sh)
yourself from the `edge` branch (it checks and refuses to run from anywhere
else). There is no CI automation building or pushing `edge` on every push to
the branch. This was a deliberate choice, not an oversight: wiring that up
means a new category of CI workflow (deploy, not just build/test) and
storing GHCR push credentials as Actions secrets — a real increase in attack
surface for a channel that exists to move fast, not to be hardened.

### Versioning `edge`

`edge` images don't reuse the `VERSION` file's contents as-is (that's what
distinguishes a `stable` build). Instead:
`<release>-edge+<date>.<short-sha>`, e.g. `0.4.0-edge+20260911.a1b2c3d`.

- `edge` is its own semver prerelease identifier, not chained onto whatever
  prerelease suffix (if any) `VERSION` currently holds — the release portion
  is always the bare `X.Y.Z`.
- The date and short SHA are semver build metadata (after the `+`), not part
  of the prerelease identifier — build metadata doesn't affect version
  precedence/sorting, which is correct here: an edge build should never be
  compared as if it were an ordered release, but a human or a bug report
  should still be able to trace a running image back to an exact commit and
  day.
- No git tag is created per edge build — since every push to `edge` is meant
  to become a new image, the branch history itself is the record of what
  produced what.

Whatever version string a build embeds (stable or edge) also becomes
`PLANNER_VERSION` in the image, which `main.py` prints on startup — see
`planner_version()` there.

### Test workflow on `edge`

`edge` is listed under `push` (not `pull_request`) in
[`.github/workflows/schema.yml`](.github/workflows/schema.yml), since it
only ever gains work via direct `git merge --no-ff`, never a GitHub PR
targeting it.
