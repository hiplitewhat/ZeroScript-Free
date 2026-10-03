#!/bin/sh
# -----------------------------------------------------------------------------
#  Land the ZeroScript patch on your PrivateAgent fork and let GitHub build it.
#
#  Run this on a machine (or in Termux) where YOU are signed in to GitHub -
#  the sandbox agent could not push, because its GitHub token is scoped to the
#  ZeroScript-Free repo only.
#
#    sh build-zeroscript-apk.sh [path/to/private-agent-zeroscript.patch]
#
#  What it does: clones your fork, applies the patch on a branch called
#  zeroscript-bridge, pushes it, and pushes a v* tag. The tag is what makes
#  GitHub Actions run, so you do NOT need the gh CLI.
# -----------------------------------------------------------------------------
set -e

REPO="hipliteidk-glitch/private-agent"
BRANCH="zeroscript-bridge"
PATCH="${1:-$(dirname "$0")/private-agent-zeroscript.patch}"
DIR="pa-zeroscript"
TAG="v1.0.3-zeroscript"

if [ ! -f "$PATCH" ]; then
    echo "patch not found: $PATCH" >&2
    echo "usage: sh build-zeroscript-apk.sh path/to/private-agent-zeroscript.patch" >&2
    exit 1
fi

# ── 1. a clone we can work in -------------------------------------------------
if [ -d "$DIR/.git" ]; then
    echo "[1/5] reusing $DIR"
    cd "$DIR"
    git fetch origin
    git checkout main
    git reset --hard origin/main
else
    echo "[1/5] cloning $REPO"
    git clone "https://github.com/$REPO" "$DIR"
    cd "$DIR"
fi

# ── 2. branch + patch ---------------------------------------------------------
echo "[2/5] applying the patch on $BRANCH"
git checkout -B "$BRANCH"
if ! git apply --check "$PATCH" 2>/dev/null; then
    echo "  the patch does not apply to this checkout - is it already applied?" >&2
    git apply --stat "$PATCH" >&2
    exit 1
fi
git apply "$PATCH"

# ── 3. commit ----------------------------------------------------------------
echo "[3/5] committing"
git add -A
git -c user.name="$(git config user.name || echo PrivateAgent)" \
    -c user.email="$(git config user.email || echo privateagent@localhost)" \
    commit -m "ZeroScript bridge: run out of the box against the on-device bridge

Loopback cleartext allowed, ZeroScript defaults (http://127.0.0.1:17614/v1,
'zeroscript'), no API key required for a loopback endpoint, ZeroScript preset in
Settings and onboarding, plus tests for the new rule." || echo "  (nothing new to commit)"

# ── 4. push the branch -------------------------------------------------------
echo "[4/5] pushing $BRANCH (you may be asked to sign in)"
git push -u origin "$BRANCH" --force-with-lease

# ── 5. trigger the APK build -------------------------------------------------
echo "[5/5] pushing tag $TAG to start the build"
git tag -f "$TAG"
git push -f origin "$TAG"

cat <<EOF

Done. GitHub Actions is now building the APK.

  Watch it:   https://github.com/$REPO/actions
  Download:   open the finished run -> Artifacts -> private-agent-android
              (it contains app-universal-release.apk + the per-ABI APKs)

If the Actions tab says workflows are disabled for this fork, open it once and
click "I understand my workflows, go ahead and enable them", then push the tag
again:   git push -f origin $TAG
EOF
