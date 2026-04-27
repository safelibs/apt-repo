# SafeLibs Apt Repository

This repository assembles and publishes the static SafeLibs apt repository for
Ubuntu 24.04 on GitHub Pages.

The stable channel is generated dynamically at build time from the SafeLibs
validator at `https://safelibs.github.io/validator/site-data.json`
(configurable via the `validator` block in
[`repositories.yml`](./repositories.yml)). For each fully-passing library in
the configured proof mode (default `port-04-test`), `tools/build_site.py`
synthesizes a stable-channel entry — port repository, pinned tag/commit,
verify packages, and runtime subset — directly from the validator payload.
There is no checked-in list of ports to publish: as validator results change,
the emitted apt index follows automatically. Ports that regress drop off the
stable channel and ports that begin passing get picked up on the next site
rebuild.

The testing channel is discovered at build time from non-archived
`safelibs/port-*` repos. It resolves each repo's current default branch and
publishes the latest package artifacts available from that commit's GitHub
release, regardless of whether the repo has a `04-test` tag.

## Layout

- `repositories.yml`: archive metadata, validator source URL, testing-channel
  discovery, and per-port build-recipe overrides consumed only by the port-CI
  generator
- `tools/build_site.py`: synthesize stable entries from the validator,
  download release artifacts, index, sign, and render the Pages site
- `scripts/verify-in-ubuntu-docker.sh`: end-to-end `apt` verification in an
  Ubuntu 24.04 container
- `.github/workflows/ci.yml`: unit tests plus full build-and-verify
- `.github/workflows/pages.yml`: Pages deployment on `main`

## Config

`repositories.yml` defines:

- `archive`: signing, Pages metadata, the default Ubuntu 24.04 image, and
  the generated `apt` pin priority
- `validator`: the validator site-data URL and proof mode used to pick the
  stable port set
- `testing`: discovery rules for the testing channel plus per-port build
  overrides for testing-channel artifact production
- `port_build_overrides`: per-port build recipes consumed only by
  `tools/generate_port_ci.py` when rendering each `safelibs/port-*` repo's
  CI workflow. Any port not listed here uses the default safe-debian build.

The set of ports actually published in the stable apt index is derived
entirely from the validator — no port list lives in `repositories.yml`.

Package builds happen in the individual `safelibs/port-*` repositories. Their
generated `build-debs` workflow publishes a GitHub release named
`build-<12-char-sha>` for each pushed commit. During site generation,
`tools/build_site.py` resolves each configured tag or branch ref to a commit,
derives the matching release tag from that commit SHA, downloads the `.deb`
assets, and then publishes the signed apt indexes.

The generated site now publishes:

- `/all/`: the stable aggregate repository with every tagged SafeLibs package
- `/<library>/`: one stable repository per validating tagged library
  (the exact set follows the live validator)
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

## Pinning

The shipped `safelibs.pref` (or `safelibs-<repo>.pref`) installs an apt
preference of the form:

```
Package: <every package published by this repository>
Pin: release o=SafeLibs
Pin-Priority: 1001
```

Because the priority is `1001` (greater than `1000`), apt will prefer the
SafeLibs build for every listed package over the Ubuntu archive copy, even
when Ubuntu later ships a newer upstream version. This is what stops `apt
upgrade` or `apt install <something-that-depends-on-libfoo>` from silently
replacing a SafeLibs-installed library with the upstream Ubuntu package.

This is a *channel* pin, not a *version* pin: the rule matches anything
served from `o=SafeLibs`, so newer SafeLibs builds for the same package
remain upgrade candidates and `apt upgrade` will pull them in on the next
run. The Ubuntu copy stays out regardless of how its version number
compares, and SafeLibs releases continue to flow through normally.

If you ever remove `safelibs.pref` (or the SafeLibs source list), the next
`apt upgrade` is free to reinstall the Ubuntu version over the SafeLibs
build, so keep both files in place for as long as you want SafeLibs
packages to win.

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
