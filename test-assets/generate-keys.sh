#!/usr/bin/env bash
set -euo pipefail

ASSET_DIR="$(dirname "$0")"
PRIVATE_KEY="$ASSET_DIR/id_rsa_test"
PUBLIC_KEY="$ASSET_DIR/id_rsa_test.pub"
AUTHORIZED_KEYS="$ASSET_DIR/../authorized_keys"

if [ -f "$PRIVATE_KEY" ] && [ -f "$PUBLIC_KEY" ]; then
  echo "Test keys already exist: $PRIVATE_KEY"
  echo "Copying public key to authorized_keys (for image build)"
  install -m 600 "$PUBLIC_KEY" "$AUTHORIZED_KEYS"
  exit 0
fi

echo "Generating SSH test key pair in $ASSET_DIR"
ssh-keygen -t rsa -b 4096 -f "$PRIVATE_KEY" -N "" -C "debug-runner-test-key"

echo "Copying public key to authorized_keys (for image build)"
install -m 600 "$PUBLIC_KEY" "$AUTHORIZED_KEYS"

echo "Generated:
  private: $PRIVATE_KEY
  public:  $PUBLIC_KEY
  authorized_keys: $AUTHORIZED_KEYS"
