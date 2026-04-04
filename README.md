# SafeLibs Apt Repository

This repository builds and publishes the static SafeLibs apt repository for
Ubuntu 24.04 on GitHub Pages.

As of April 3, 2026, the checked-in [`repositories.yml`](./repositories.yml)
tracks the SafeLibs repos that currently expose a `04-test` tag:

- `safelibs/port-libjson` at `refs/tags/libjson/04-test`
- `safelibs/port-libpng` at `refs/tags/libpng/04-test`
- `safelibs/port-libzstd` at `refs/tags/libzstd/04-test`

## Layout

- `repositories.yml`: source of truth for which `safelibs/port-*` repos to pull
  and how to build them
- `tools/build_site.py`: clone, build, index, sign, and render the Pages site
- `scripts/verify-in-ubuntu-docker.sh`: end-to-end `apt` verification in an
  Ubuntu 24.04 container
- `.github/workflows/ci.yml`: unit tests plus full build-and-verify
- `.github/workflows/pages.yml`: Pages deployment on `main`

## Config

Each repository entry defines:

- the GitHub repo and pinned ref
- which packages should be installed during Docker verification
- either an Ubuntu 24.04 container build command or checked-in package artifact mode
- artifact globs to publish

The `archive` block also defines signing, Pages metadata, the default Ubuntu
24.04 image, and the generated `apt` pin priority.

The build commands are repo-specific on purpose. The current SafeLibs package
repos do not share one packaging entrypoint yet, and at least one (`libpng`)
currently publishes the package artifacts directly at its `04-test` tag. The
builder also supports repo-specific Rust toolchain overrides for tags that
outgrow the Rust version shipped by the base Ubuntu 24.04 image.

## Local Usage

Prerequisites:

- `gh` authenticated for access to the private `safelibs/port-*` repos
- `docker`
- `gpg`
- `apt-ftparchive`
- `python3` with `PyYAML`

Run unit tests:

```bash
make test
```

Build the Pages site:

```bash
make build-site
```

Verify the generated repository in Ubuntu 24.04 Docker:

```bash
make verify-docker
```

The site output lands in `site/`. If `SAFEAPTREPO_GPG_PRIVATE_KEY` is not set,
the builder generates an ephemeral signing key for local/CI verification.

Ubuntu 24.04 already ships the distro packages that these SafeLibs ports
replace. The generated site therefore also publishes `safelibs.pref`, which
pins the published SafeLibs packages to priority `1001` so `apt` will select
them even if Ubuntu later carries a newer upstream version.

Example install:

```bash
sudo install -d -m 0755 /etc/apt/keyrings /etc/apt/preferences.d
curl -fsSL https://safelibs.github.io/apt-repo/safelibs.gpg | sudo tee /etc/apt/keyrings/safelibs.gpg > /dev/null
curl -fsSL https://safelibs.github.io/apt-repo/safelibs.pref | sudo tee /etc/apt/preferences.d/safelibs.pref > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/safelibs.gpg] https://safelibs.github.io/apt-repo noble main" | sudo tee /etc/apt/sources.list.d/safelibs.list
sudo apt-get update
```

## Signing

`tools/build_site.py` accepts the signing key from the environment:

- `SAFEAPTREPO_GPG_PRIVATE_KEY`: armored private key material
- `SAFEAPTREPO_GPG_PASSPHRASE`: optional passphrase for that key

Without those variables, the tool generates a throwaway key and exports the
matching `safelibs.asc` and `safelibs.gpg` into the site.

Legacy `SAFEDEBREPO_GPG_PRIVATE_KEY` and `SAFEDEBREPO_GPG_PASSPHRASE` names are
still accepted during the rename.

## CI Notes

The `ci` workflow always runs unit tests. Its full site build plus Docker
verification run only when `SAFELIBS_REPO_TOKEN` is configured with read access
to the private `safelibs/port-*` repos.

The `pages` workflow only builds and deploys when both of the following are
available:

- `SAFELIBS_REPO_TOKEN`
- `SAFEAPTREPO_GPG_PRIVATE_KEY`, or the legacy
  `SAFEDEBREPO_GPG_PRIVATE_KEY` while the rename is in flight

`SAFEAPTREPO_GPG_PASSPHRASE` remains optional and is only needed when the
deployment key is passphrase-protected.
