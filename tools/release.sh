#!/bin/zsh
# Publish a tagged DHI Orbit release and point the Homebrew tap at it.
#   tools/release.sh 0.3.0
# Pushes main and the tag vX.Y.Z to github.com/Dv04/dhi-orbit, hashes GitHub's tarball for that tag, rewrites
# url/sha256 in ../homebrew-dhi-orbit/Formula/dhi-orbit.rb, commits and pushes the tap, then installs from the tap
# as a user would and checks `dhi-orbit --help`.
set -euo pipefail
ver=${1:?usage: tools/release.sh X.Y.Z}
cd "${0:A:h}/.."
grep -q "version = \"$ver\"" pyproject.toml || { echo "pyproject.toml is not at $ver"; exit 1; }
git rev-parse -q --verify "refs/tags/v$ver" >/dev/null || { echo "no local tag v$ver"; exit 1; }
git push origin main "v$ver"

url="https://github.com/Dv04/dhi-orbit/archive/refs/tags/v$ver.tar.gz"
sha=$(curl -fsSL "$url" | shasum -a 256 | cut -d' ' -f1)
echo "tarball sha256 $sha"

tap="${0:A:h}/../../homebrew-dhi-orbit"
f="$tap/Formula/dhi-orbit.rb"
sed -i '' -e "s#^  url \".*\"#  url \"$url\"#" -e "s#^  sha256 \".*\"#  sha256 \"$sha\"#" "$f"
git -C "$tap" commit -qam "dhi-orbit $ver"
git -C "$tap" push -q origin main

export HOMEBREW_NO_INSTALL_FROM_API=1
brew update -q >/dev/null
brew reinstall Dv04/dhi-orbit/dhi-orbit || brew install Dv04/dhi-orbit/dhi-orbit
dhi-orbit --help | head -1
echo "released dhi-orbit $ver"
