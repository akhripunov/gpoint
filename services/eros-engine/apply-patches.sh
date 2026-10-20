#!/bin/sh
# Compatibility guard: ME-36 OWNER decision requires stock upstream Eros.
set -eu
printf '%s\n' \
  'eros_downstream_patches_disabled: build the pinned, clean upstream source without patches' \
  'Historical patch files are retained for audit only; this command makes no changes.' >&2
exit 2
