# SafeLibs Apt Repository

This repository assembles and publishes the static SafeLibs apt repository for
Ubuntu 24.04 on GitHub Pages.

As of April 20, 2026, the checked-in stable channel in
[`repositories.yml`](./repositories.yml) tracks every current
`safelibs/port-*` repo that exposes a `04-test` tag:

- `safelibs/port-cjson` at `refs/tags/cjson/04-test`
- `safelibs/port-giflib` at `refs/tags/giflib/04-test`
- `safelibs/port-libarchive` at `refs/tags/libarchive/04-test`
- `safelibs/port-libbz2` at `refs/tags/libbz2/04-test`
- `safelibs/port-libcsv` at `refs/tags/libcsv/04-test`
- `safelibs/port-libjpeg-turbo` at `refs/tags/libjpeg-turbo/04-test`
- `safelibs/port-libjson` at `refs/tags/libjson/04-test`
- `safelibs/port-liblzma` at `refs/tags/liblzma/04-test`
- `safelibs/port-libpng` at `refs/tags/libpng/04-test`
- `safelibs/port-libsdl` at `refs/tags/libsdl/04-test`
- `safelibs/port-libsodium` at `refs/tags/libsodium/04-test`
- `safelibs/port-libtiff` at `refs/tags/libtiff/04-test`
- `safelibs/port-libuv` at `refs/tags/libuv/04-test`
- `safelibs/port-libvips` at `refs/tags/libvips/04-test`
- `safelibs/port-libwebp` at `refs/tags/libwebp/04-test`
- `safelibs/port-libxml` at `refs/tags/libxml/04-test`
- `safelibs/port-libyaml` at `refs/tags/libyaml/04-test`
- `safelibs/port-libzstd` at `refs/tags/libzstd/04-test`

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
stable per-library repository, and any generated testing repositories. When a
manifest entry omits
`verify_packages`, the verification script derives the package set directly
from the published `Packages` index for that repository.

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
curl -fsSL https://safelibs.github.io/apt-repo/all/safelibs.gpg | sudo tee /etc/apt/keyrings/safelibs.gpg > /dev/null
curl -fsSL https://safelibs.github.io/apt-repo/all/safelibs-all.pref | sudo tee /etc/apt/preferences.d/safelibs-all.pref > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/safelibs.gpg] https://safelibs.github.io/apt-repo/all noble main" | sudo tee /etc/apt/sources.list.d/safelibs-all.list > /dev/null
sudo apt-get update
```

To install just one port, swap `/all/` for the library-specific repository such
as `/libjson/`, and use a matching local filename like
`/etc/apt/preferences.d/safelibs-libjson.pref`.

To install the testing aggregate, use `/testing/all/` and the testing preference
file name:

```bash
curl -fsSL https://safelibs.github.io/apt-repo/testing/all/safelibs.gpg | sudo tee /etc/apt/keyrings/safelibs.gpg > /dev/null
curl -fsSL https://safelibs.github.io/apt-repo/testing/all/safelibs-testing-all.pref | sudo tee /etc/apt/preferences.d/safelibs-testing-all.pref > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/safelibs.gpg] https://safelibs.github.io/apt-repo/testing/all noble main" | sudo tee /etc/apt/sources.list.d/safelibs-testing-all.list > /dev/null
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
