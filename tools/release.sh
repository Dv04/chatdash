#!/bin/zsh
# Release DHI Orbit: tag, Homebrew tap, PyPI, GitHub release, with a check after every step.
#   tools/release.sh X.Y.Z            release origin/main, which must already be at version X.Y.Z (merge the bump PR first)
#   tools/release.sh X.Y.Z --dry-run  steps 1 to 4 only: nothing is pushed, tagged or uploaded
# Everything runs in a throwaway worktree of origin/main, never your checkout, so unpushed local commits and local tags
# cannot leak into a release. The script stops at the first failed check; PyPI (step 7) is the one step that cannot be undone,
# so it comes after everything that can be.
# Needs: git, gh (logged in), uv (uvx), brew, ~/.secrets/pypi-token (override with PYPI_TOKEN_FILE), the tap checked out at
# ../homebrew-dhi-orbit next to this repo.
set -euo pipefail
ver=${1:?usage: tools/release.sh X.Y.Z [--dry-run]}
dry=${2:-}
[[ -z $dry || $dry == --dry-run ]] || { echo "unknown option $dry"; exit 2; }
[[ $ver =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must look like 0.3.11"; exit 2; }

home=${0:A:h}/..
tap=${TAP:-$home/../homebrew-dhi-orbit}
token=${PYPI_TOKEN_FILE:-$HOME/.secrets/pypi-token}
EMPTY=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855   # sha256 of an empty file: a 404 page body hashes to this
work=$(mktemp -d)
step() { echo "== $*"; }
fail() { echo "FAIL: $*"; exit 1; }
cleanup() { git -C $home worktree remove --force $work/src >/dev/null 2>&1 || true; rm -rf $work; }
trap cleanup EXIT

step "1 source: origin/main must be at version $ver, tests pass"
git -C $home fetch -q origin
commit=$(git -C $home rev-parse origin/main)
git -C $home worktree add -q --detach $work/src $commit
cd $work/src
grep -q "^version = \"$ver\"" pyproject.toml || fail "pyproject.toml on origin/main is not at $ver (merge the version bump first)"
grep -q "__version__ = \"$ver\"" dhi_orbit/__init__.py || fail "dhi_orbit/__init__.py on origin/main is not at $ver"
python3 -m pytest -q 2>&1 | tail -1 | grep -q passed || fail "tests do not pass"
echo "origin/main $commit at $ver, tests pass"

step "2 tag v$ver must not exist locally, on GitHub or on PyPI"
if git -C $home rev-parse -q --verify "refs/tags/v$ver" >/dev/null; then fail "local tag v$ver already exists in this repo (tags are shared by every worktree): delete it or pick the next number"; fi
[[ -z $(git -C $home ls-remote --tags origin "v$ver") ]] || fail "tag v$ver is already on GitHub"
if curl -fsS "https://pypi.org/pypi/dhi-orbit/$ver/json" >/dev/null 2>&1; then fail "dhi-orbit $ver is already on PyPI"; fi
echo "v$ver is free"

step "3 build and check the PyPI files"
uvx --from build pyproject-build >/dev/null 2>&1
[[ -f dist/dhi_orbit-$ver-py3-none-any.whl && -f dist/dhi_orbit-$ver.tar.gz ]] || fail "build did not produce $ver files"
unzip -l dist/dhi_orbit-$ver-py3-none-any.whl | grep -q "ui/index.html" || fail "wheel has no UI files"
[[ $(uvx twine check dist/* 2>&1 | grep -c PASSED) == 2 ]] || fail "twine check failed"
echo "built and checked: $(ls dist | tr '\n' ' ')"

step "4 tap is clean, in sync and has a formula to update"
git -C $tap fetch -q origin
[[ $(git -C $tap rev-parse HEAD) == $(git -C $tap rev-parse origin/main) ]] || fail "tap is not at origin/main"
[[ -f $tap/Formula/dhi-orbit.rb ]] || fail "no formula in $tap"
[[ -r $token ]] || fail "no PyPI token at $token"
if [[ $dry == --dry-run ]]; then echo "DRY RUN OK: nothing was pushed, tagged or uploaded"; exit 0; fi

step "5 tag v$ver on $commit: push, read it back, tarball must be real"
git -C $home tag "v$ver" $commit
git -C $home push -q origin "refs/tags/v$ver"
[[ $(git -C $home ls-remote origin "refs/tags/v$ver" | cut -f1) == $(git -C $home rev-parse "v$ver") ]] || fail "tag did not reach GitHub"
url="https://github.com/Dv04/dhi-orbit/archive/refs/tags/v$ver.tar.gz"
curl -fsSL "$url" -o $work/orbit.tar.gz || fail "tarball $url is not downloadable"
[[ $(stat -f%z $work/orbit.tar.gz) -gt 10000 ]] || fail "tarball is suspiciously small"
sha=$(shasum -a 256 $work/orbit.tar.gz | cut -d' ' -f1)
[[ $sha != $EMPTY ]] || fail "tarball hash is the empty-file hash"
echo "tag on GitHub, tarball sha256 $sha"

step "6 tap formula: update, commit only the formula, push, install and check"
f=$tap/Formula/dhi-orbit.rb
sed -i '' -e "s#^  url \".*\"#  url \"$url\"#" -e "s#^  sha256 \".*\"#  sha256 \"$sha\"#" $f
grep -q "url \"$url\"" $f && grep -q "sha256 \"$sha\"" $f || fail "formula was not updated"
git -C $tap add Formula/dhi-orbit.rb
git -C $tap commit -qm "dhi-orbit $ver"
git -C $tap push -q origin main
[[ $(git -C $tap rev-parse HEAD) == $(git -C $tap ls-remote origin refs/heads/main | cut -f1) ]] || fail "tap push did not land"
export HOMEBREW_NO_INSTALL_FROM_API=1 HOMEBREW_NO_AUTO_UPDATE=1
brew update -q >/dev/null
brew fetch --force Dv04/dhi-orbit/dhi-orbit 2>&1 | tail -1
brew reinstall Dv04/dhi-orbit/dhi-orbit 2>&1 | tail -1
py=$(brew --prefix dhi-orbit)/libexec/bin/python
[[ $($py -c "import dhi_orbit; print(dhi_orbit.__version__)") == $ver ]] || fail "brew installed the wrong version"
$(brew --prefix)/bin/dhi-orbit --help | head -1
echo "brew install is $ver"

step "7 PyPI upload (cannot be undone), then read it back"
TWINE_USERNAME=__token__ TWINE_PASSWORD=$(cat $token) uvx twine upload dist/* 2>&1 | grep -E "View at|https://pypi" || true
v=
for i in 1 2 3 4 5 6; do
  v=$(curl -fsS https://pypi.org/pypi/dhi-orbit/json | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])" 2>/dev/null || true)
  [[ $v == $ver ]] && break
  sleep 5
done
[[ $v == $ver ]] || fail "PyPI does not show $ver (latest is '$v')"
echo "PyPI latest $v"

step "8 GitHub release"
gh release create "v$ver" -R Dv04/dhi-orbit --title "DHI Orbit $ver" --generate-notes dist/dhi_orbit-$ver-py3-none-any.whl dist/dhi_orbit-$ver.tar.gz | tail -1
echo "RELEASED dhi-orbit $ver"
