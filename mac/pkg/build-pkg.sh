#!/bin/bash
# Build the double-click installer (§v3 workstream 1, the last piece).
#
#   ./build-pkg.sh              build, sign and notarize
#   ./build-pkg.sh --unsigned   build only — for testing the install itself
#   ./build-pkg.sh --check      say what's missing and stop
#
# Why a package at all: the curl line works, and the person this product most
# needs to reach told us plainly that he does not want to open a terminal. A
# signed package is the difference between "paste this command" and "double
# click this file", and that difference ended one walkthrough already.
#
# Two certificates are involved and they are not interchangeable:
#   Developer ID Application  signs the app inside the payload
#   Developer ID Installer    signs the .pkg itself
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUILD="$HERE/build"
ROOTDIR="$BUILD/root"
PAYLOAD="$ROOTDIR/usr/local/loose-ends"
IDENTIFIER="dev.clyon.looseends.engine"
# shellcheck source=../version.sh
. "$ROOT/mac/version.sh"
VERSION="${VERSION:-$VERSION_FULL}"
PROFILE="${NOTARY_PROFILE:-looseends}"
PKG="$BUILD/LooseEnds-$VERSION.pkg"

say()  { printf '\033[1;32m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!\033[0m %s\n' "$*" >&2; }

MODE="${1:-}"

INSTALLER_ID="$(security find-identity -v 2>/dev/null \
                | grep "Developer ID Installer" | head -1 \
                | sed -E 's/.*"(.*)"/\1/' || true)"

if [ "$MODE" = "--check" ] || { [ -z "$INSTALLER_ID" ] && [ "$MODE" != "--unsigned" ]; }; then
  if [ -z "$INSTALLER_ID" ]; then
    warn "no Developer ID Installer certificate in the keychain."
    warn "  This is a *different* certificate from the one that signs the app."
    warn "  Xcode ▸ Settings ▸ Accounts ▸ (your team) ▸ Manage Certificates"
    warn "  ▸ + ▸ Developer ID Installer."
    warn ""
    warn "  Build an unsigned package to test the install itself:"
    warn "    $0 --unsigned"
    [ "$MODE" = "--check" ] && exit 1
    exit 1
  fi
  say "ready: $INSTALLER_ID"
  exit 0
fi

# ------------------------------------------------------------------ payload
say "version $VERSION (commit $VERSION_SHA)"
if [ "$VERSION_DIRTY" = "1" ]; then
  warn "the working tree has uncommitted changes."
  warn "  $VERSION_SHA does not describe what is about to be built, so this package"
  warn "  cannot be identified later. Fine for testing; commit before sharing."
fi
say "assembling the payload"
rm -rf "$BUILD"
mkdir -p "$PAYLOAD" "$BUILD"

# Tracked files only, so no database, no venv, no local mess — the same
# discipline publish/export.sh uses for the public repo.
cd "$ROOT"
git archive HEAD backend | tar -x -C "$PAYLOAD"

# The menu bar app rides along, already signed and stapled by mac/sign.sh.
if [ -d "$HERE/../build/Loose Ends.app" ]; then
  cp -R "$HERE/../build/Loose Ends.app" "$PAYLOAD/Loose Ends.app"
  say "including the menu bar app"
else
  warn "no signed menu bar app at mac/build — run mac/sign.sh first."
  warn "continuing without it; the engine will install, the icon won't appear."
fi

# What ships from deploy/ is stated positively: an allowlist cannot be
# outflanked by a new private note landing in that directory, the way naming
# each unwanted file can. Everything else there is ours, not the user's.
find "$PAYLOAD/backend/deploy" -type f \
     ! -name 'install.sh' ! -name 'uninstall.sh' -delete 2>/dev/null || true
rm -f "$PAYLOAD/backend"/*.db* 2>/dev/null || true

# ------------------------------------------------------------------ the pkg
say "building the package"

# pkgbuild sees an .app in the payload and, unasked, marks it relocatable.
# At install time the Installer then looks the bundle id up through Launch
# Services and writes the app wherever a copy already exists — ignoring the
# path declared right here. The first real install of this package proved it:
# the app went into a stale DerivedData folder inside the repo, so
# /Applications got nothing and postinstall found nothing to move.
#
# What makes it a trap rather than a bug is that it works on a clean Mac,
# where Spotlight knows of no other copy. It only misfires for people who
# have built the app before — which is to say, while testing it.
pkgbuild --analyze --root "$ROOTDIR" "$BUILD/component.plist" >/dev/null
i=0
while /usr/libexec/PlistBuddy -c "Print :$i:BundleIsRelocatable" \
        "$BUILD/component.plist" >/dev/null 2>&1; do
  /usr/libexec/PlistBuddy -c "Set :$i:BundleIsRelocatable false" \
        "$BUILD/component.plist" >/dev/null
  i=$((i + 1))
done
say "pinned $i bundle(s) to their declared path"

pkgbuild --root "$ROOTDIR" \
         --component-plist "$BUILD/component.plist" \
         --scripts "$HERE/scripts" \
         --identifier "$IDENTIFIER" \
         --version "$VERSION" \
         --install-location "/" \
         "$BUILD/component.pkg" >/dev/null

# A distribution package can state a minimum macOS and show a real title,
# where a bare component package shows the identifier and accepts anything.
cat > "$BUILD/distribution.xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
    <title>Loose Ends</title>
    <organization>dev.clyon</organization>
    <domains enable_localSystem="true"/>
    <options customize="never" require-scripts="true" hostArchitectures="arm64,x86_64"/>
    <volume-check>
        <!-- 14.0, matching the menu bar app's LSMinimumSystemVersion, not the
             engine's. The engine is Python and would run on 13; the app uses
             @Observable and would not, so a 13 install passed here and then
             quietly had no icon — an engine reading your mail with nothing in
             the menu bar to show it or stop it, which is the one outcome that
             app was written to prevent. Refusing up front beats half of it. -->
        <allowed-os-versions><os-version min="14.0"/></allowed-os-versions>
    </volume-check>
    <choices-outline><line choice="default"/></choices-outline>
    <choice id="default"><pkg-ref id="$IDENTIFIER"/></choice>
    <pkg-ref id="$IDENTIFIER" version="$VERSION" onConclusion="none">component.pkg</pkg-ref>
</installer-gui-script>
XML

if [ -n "$INSTALLER_ID" ] && [ "$MODE" != "--unsigned" ]; then
  productbuild --distribution "$BUILD/distribution.xml" \
               --package-path "$BUILD" \
               --sign "$INSTALLER_ID" --timestamp \
               "$PKG" >/dev/null
  say "signed with $INSTALLER_ID"
else
  productbuild --distribution "$BUILD/distribution.xml" \
               --package-path "$BUILD" \
               "$PKG" >/dev/null
  warn "unsigned — Gatekeeper will refuse this on any other Mac."
  say "built: $PKG"
  exit 0
fi

# ---------------------------------------------------------------- notarize
say "notarizing (Apple usually answers in a few minutes)…"
xcrun notarytool submit "$PKG" --keychain-profile "$PROFILE" --wait
say "stapling the ticket"
xcrun stapler staple "$PKG"
xcrun stapler validate "$PKG"

say "done — $PKG"
say "verify like Gatekeeper does:  spctl -a -vvv -t install \"$PKG\""
