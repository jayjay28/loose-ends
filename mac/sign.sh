#!/bin/bash
# Sign and notarize the menu bar app for distribution (§v3 workstream 7).
#
#   ./sign.sh                 build, sign, notarize, staple, zip
#   ./sign.sh --check         say what's missing and stop
#   ./sign.sh --sign-only     build and sign, skip Apple's notary
#
# Gatekeeper refuses an app signed with an "Apple Development" certificate on
# any Mac but the one that built it, so shipping needs three things Apple
# gates: a Developer ID Application certificate, a notarytool credential, and
# a round trip to Apple's notary service. This script does the parts that can
# be automated and names the parts that can't.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$HERE/Loose Ends Menu Bar.xcodeproj"
SCHEME="Loose Ends Menu Bar"
PROFILE="${NOTARY_PROFILE:-looseends}"
BUILD="$HERE/build"
APP="$BUILD/Loose Ends.app"

say()  { printf '\033[1;32m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!\033[0m %s\n' "$*" >&2; }

# ------------------------------------------------------------- the two gates
# `|| true`: with `set -e` and `pipefail`, a grep that matches nothing kills
# the script here — before it can print the one message it exists to print.
IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
            | grep "Developer ID Application" | head -1 \
            | sed -E 's/.*"(.*)"/\1/' || true)"

missing=0
if [ -z "$IDENTITY" ]; then
  missing=1
  warn "no Developer ID Application certificate in the keychain."
  warn "  Xcode ▸ Settings ▸ Accounts ▸ (your team) ▸ Manage Certificates"
  warn "  ▸ + ▸ Developer ID Application.  Needs the Account Holder role."
fi

if ! xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
  missing=1
  warn "no stored notary credential named '$PROFILE'."
  warn "  Create an App Store Connect API key (Users and Access ▸ Integrations"
  warn "  ▸ Keys ▸ +, role: Developer), download the .p8, then run:"
  warn ""
  warn "    xcrun notarytool store-credentials $PROFILE \\"
  warn "      --key ~/Downloads/AuthKey_XXXXXXXXXX.p8 \\"
  warn "      --key-id XXXXXXXXXX --issuer <issuer-uuid>"
  warn ""
  warn "  (An Apple ID + app-specific password also works, but a revocable"
  warn "   API key beats putting an account password in a keychain item.)"
fi

if [ "${1:-}" = "--check" ]; then
  [ "$missing" -eq 0 ] && say "ready to sign and notarize."
  exit "$missing"
fi
SIGN_ONLY=0
if [ "${1:-}" = "--sign-only" ]; then
  SIGN_ONLY=1
  # Signing needs only the certificate; the notary credential is the gate on
  # the *second* half. A Developer ID signature without a notarization ticket
  # still opens on another Mac — behind a right-click and a warning, which is
  # exactly the friction this project exists to remove, so it is a way point
  # rather than a destination.
  [ -z "$IDENTITY" ] && { warn "nothing was signed."; exit 1; }
elif [ "$missing" -eq 1 ]; then
  warn "nothing was signed."
  exit 1
fi

# ------------------------------------------------------------------- build
say "building Release with $IDENTITY"
rm -rf "$BUILD"
xcodebuild -project "$PROJECT" -scheme "$SCHEME" -configuration Release \
  -derivedDataPath "$BUILD/dd" \
  CODE_SIGN_IDENTITY="$IDENTITY" CODE_SIGN_STYLE=Manual \
  OTHER_CODE_SIGN_FLAGS="--timestamp" \
  >/dev/null
mkdir -p "$BUILD"
cp -R "$BUILD/dd/Build/Products/Release/Loose Ends.app" "$APP"

# Hardened runtime is required for notarization; --timestamp is required for
# the signature to outlive the certificate.
say "signing"
codesign --force --deep --timestamp --options runtime \
  --sign "$IDENTITY" "$APP"
codesign --verify --strict --verbose=2 "$APP" 2>&1 | tail -2

if [ "$SIGN_ONLY" -eq 1 ]; then
  say "signed, not notarized — $APP"
  say "on another Mac this still needs right-click ▸ Open the first time."
  exit 0
fi

# ---------------------------------------------------------------- notarize
say "notarizing (Apple usually answers in a few minutes)…"
ZIP="$BUILD/LooseEnds.zip"
ditto -c -k --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait

# The ticket has to be stapled into the app so Gatekeeper can find it on a
# Mac that is offline the first time it opens this.
say "stapling the ticket"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"

rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$BUILD/LooseEnds-signed.zip"
say "done — $BUILD/LooseEnds-signed.zip"
say "verify like Gatekeeper does:  spctl -a -vvv -t install \"$APP\""
