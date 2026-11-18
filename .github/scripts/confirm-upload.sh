#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
# download-artifact parses IDs as JavaScript Numbers. Do not accept a rounded
# identifier or publish an empty ID when the action returned without uploading.
id=${TRANSFER_ID:-}
if [[ ! "$id" =~ ^[1-9][0-9]{0,15}$ ]] || (( id > 9007199254740991 )); then
  echo '::error::No valid artifact ID from a successful upload' >&2
  exit 1
fi
printf 'artifact-id=%s\n' "$id" >> "${GITHUB_OUTPUT:?}"
