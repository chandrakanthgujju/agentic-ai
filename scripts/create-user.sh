#!/usr/bin/env bash
# Create a Cognito user for the deployed app and optionally make it an admin.
#
#   scripts/create-user.sh you@example.com            # chat user
#   scripts/create-user.sh you@example.com --admin    # can also manage the knowledge base
#
# Cognito emails a temporary password; you set a new one at first sign-in.
set -euo pipefail

EMAIL="${1:?usage: $0 <email> [--admin]}"
ADMIN="${2:-}"
STACK="${AUTH_STACK:-AgenticAi-Auth}"

POOL_ID=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
if [[ -z "$POOL_ID" || "$POOL_ID" == "None" ]]; then
  echo "Could not read UserPoolId from stack $STACK. Is it deployed in this region?" >&2
  exit 1
fi

aws cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" \
  --username "$EMAIL" \
  --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL >/dev/null
echo "Created $EMAIL in $POOL_ID (temporary password sent by email)."

if [[ "$ADMIN" == "--admin" ]]; then
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" --username "$EMAIL" --group-name admin
  echo "Added $EMAIL to the admin group."
fi
