# SafeLibs Apt Repository

This repository assembles and publishes the static SafeLibs apt repository for
Ubuntu 24.04 on GitHub Pages.

As of April 21, 2026, the checked-in stable channel in
[`repositories.yml`](./repositories.yml) tracks the latest release-backed
`build-<12-char-sha>` tag for each current `safelibs/port-*` repo that has
published SafeLibs `.deb` release assets:

- `safelibs/port-cjson` at `refs/tags/build-de29489668c1`
- `safelibs/port-giflib` at `refs/tags/build-8dd3019a8e99`
- `safelibs/port-libarchive` at `refs/tags/build-95a312cfb18f`
- `safelibs/port-libbz2` at `refs/tags/build-8c4b1bee6d25`
- `safelibs/port-libcsv` at `refs/tags/build-91e798441fdd`
- `safelibs/port-libexif` at `refs/tags/build-9f7fcf07e370`
- `safelibs/port-libjansson` at `refs/tags/build-32501acfb67e`
- `safelibs/port-libjpeg-turbo` at `refs/tags/build-27e232c22936`
- `safelibs/port-libjson` at `refs/tags/build-e25f4b433d01`
- `safelibs/port-liblzma` at `refs/tags/build-ebba850f8fae`
- `safelibs/port-libpng` at `refs/tags/build-57fa3d117156`
- `safelibs/port-libsdl` at `refs/tags/build-14609a9a5844`
- `safelibs/port-libsodium` at `refs/tags/build-d1d241340f2e`
- `safelibs/port-libtiff` at `refs/tags/build-9e34df3e07fc`
- `safelibs/port-libuv` at `refs/tags/build-a2d0955c60f5`
- `safelibs/port-libvips` at `refs/tags/build-12543f951c24`
- `safelibs/port-libwebp` at `refs/tags/build-d9437d8e3c87`
- `safelibs/port-libxml` at `refs/tags/build-8af9d2976d24`
- `safelibs/port-libyaml` at `refs/tags/build-17f439b2dab0`
- `safelibs/port-libzstd` at `refs/tags/build-64056ff8056f`

The testing channel is discovered at build time from non-archived
`safelibs/port-*` repos. It resolves each repo's current default branch and
publishes the latest package artifacts available from that commit's GitHub
release, regardless of whether the repo has a `04-test` tag.

## Layout

- `repositories.yml`: source of truth for which `safelibs/port-*` repos and refs
  to publish for stable, plus testing-channel discovery and port-CI build
  overrides
- `tools/build_site.py`: download release artifacts, index, sign, and render the
  Pages site
- `scripts/verify-in-ubuntu-docker.sh`: end-to-end `apt` verification in an
  Ubuntu 24.04 container
- `.github/workflows/ci.yml`: unit tests plus full build-and-verify
- `.github/workflows/pages.yml`: Pages deployment on `main`

## Config

Each repository entry defines:

- the GitHub repo and pinned ref
- optionally, which packages should be installed during Docker verification
- the per-port CI build configuration used by `tools/generate_port_ci.py`
- artifact globs to download from each per-commit GitHub release

The `archive` block also defines signing, Pages metadata, the default Ubuntu
24.04 image, and the generated `apt` pin priority.

Package builds happen in the individual `safelibs/port-*` repositories. Their
generated `build-debs` workflow publishes a GitHub release named
`build-<12-char-sha>` for each pushed commit. During site generation,
`tools/build_site.py` resolves each configured tag or branch ref to a commit,
derives the matching release tag from that commit SHA, downloads the `.deb`
assets, and then publishes the signed apt indexes.

The generated site now publishes:

- `/all/`: the stable aggregate repository with every tagged SafeLibs package
- `/<library>/`: one stable repository per tagged library, for example
  `/libjson/`, `/libpng/`, and `/libzstd/`
- `/testing/all/`: the testing aggregate repository with every latest
  default-branch SafeLibs package whose release artifacts downloaded
  successfully
- `/testing/<library>/`: one testing repository per latest buildable port
- `/`: a landing page that links to the split repositories; installs should use
  `/all/`, a library-specific subdirectory, `/testing/all/`, or a testing
  library-specific subdirectory

## Local Usage

Prerequisites:

- `gh` authenticated for access to the private `safelibs/port-*` repos
- `docker` for repository verification
- `gpg`
- `apt-ftparchive`
- `python3` with `PyYAML`

Run unit tests:

```bash
make test
```

Build the Pages site by downloading the per-port release artifacts:

```bash
make build-site
```

Verify the generated repository in Ubuntu 24.04 Docker:

```bash
make verify-docker
```

`make verify-docker` verifies the explicit stable `/all/` repository, each
stable per-library repository, and any generated testing repositories. The
`/all/` repository uses each entry's runtime-oriented `verify_all_packages`
when present, then falls back to `verify_packages`. When a manifest entry omits
configured verify packages, the verification script derives the package set
directly from the published `Packages` index for that repository. Stable
per-library `verify_packages` entries list every package asset published by
that repository, including runtime, development, tool, and documentation
packages.

The site output lands in `site/`, with installable repositories under
`site/all/`, `site/<library>/`, `site/testing/all/`, and
`site/testing/<library>/`. If `SAFEAPTREPO_GPG_PRIVATE_KEY` is not set, the
builder generates an ephemeral signing key for local/CI verification.

Ubuntu 24.04 already ships the distro packages that these SafeLibs ports
replace. The generated site therefore also publishes `safelibs.pref`, which
pins the published SafeLibs packages to priority `1001` so `apt` will select
them even if Ubuntu later carries a newer upstream version.

Example install for the aggregate `all` repository:

```bash
sudo install -d -m 0755 /etc/apt/keyrings /etc/apt/preferences.d
curl -fsSL https://safelibs.github.io/apt/all/safelibs.gpg | sudo tee /etc/apt/keyrings/safelibs.gpg > /dev/null
curl -fsSL https://safelibs.github.io/apt/all/safelibs-all.pref | sudo tee /etc/apt/preferences.d/safelibs-all.pref > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/safelibs.gpg] https://safelibs.github.io/apt/all noble main" | sudo tee /etc/apt/sources.list.d/safelibs-all.list > /dev/null
sudo apt-get update
```

To install just one port, swap `/all/` for the library-specific repository such
as `/libjson/`, and use a matching local filename like
`/etc/apt/preferences.d/safelibs-libjson.pref`.

To install the testing aggregate, use `/testing/all/` and the testing preference
file name:

```bash
curl -fsSL https://safelibs.github.io/apt/testing/all/safelibs.gpg | sudo tee /etc/apt/keyrings/safelibs.gpg > /dev/null
curl -fsSL https://safelibs.github.io/apt/testing/all/safelibs-testing-all.pref | sudo tee /etc/apt/preferences.d/safelibs-testing-all.pref > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/safelibs.gpg] https://safelibs.github.io/apt/testing/all noble main" | sudo tee /etc/apt/sources.list.d/safelibs-testing-all.list > /dev/null
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
