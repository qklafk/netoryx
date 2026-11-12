#!/bin/sh
# marznode runs this instead of xray. Every command goes to the newer Xray from the versioned volume; only
# `x25519` output is rewritten to the older "Private key: / Public key:" lines, which marznode parses to put the
# Reality public key into the links.
XRAY=/opt/xray/xray
if [ "$1" = "x25519" ]; then
  out="$("$XRAY" "$@")" || exit $?
  private="$(printf '%s\n' "$out" | sed -n -e 's/^PrivateKey: *//p' -e 's/^Private key: *//p' | head -n 1)"
  public="$(printf '%s\n' "$out" | sed -n -e 's/^Password (PublicKey): *//p' -e 's/^PublicKey: *//p' -e 's/^Public key: *//p' | head -n 1)"
  printf 'Private key: %s\nPublic key: %s\n' "$private" "$public"
  exit 0
fi
exec "$XRAY" "$@"
