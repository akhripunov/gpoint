#!/bin/sh

# Return 0 with image metadata, 2 only for an explicit not-found response, and
# 1 for every ambiguous daemon/CLI failure. Cleanup must not equate errors with
# absence because doing so could report success while run-owned tags remain.
inspect_run_image(){
  inspect_error="$tmp/image-inspect.err"
  if inspect_output=$(docker_local image inspect "$1" --format '{{.Id}}|{{index .Config.Labels "com.gpoint.me36.run"}}|{{index .Config.Labels "com.gpoint.me36.service"}}' 2>"$inspect_error"); then
    if [ -z "$inspect_output" ]; then
      echo "LOCAL_E2E_IMAGE_INSPECT=FAIL tag=$1 reason=empty_result" >&2
      return 1
    fi
    printf '%s' "$inspect_output"
    return 0
  fi
  if grep -Fiq "No such image: $1" "$inspect_error"; then return 2; fi
  echo "LOCAL_E2E_IMAGE_INSPECT=FAIL tag=$1 reason=daemon_or_cli_error" >&2
  return 1
}

inspect_run_container(){
  inspect_error="$tmp/container-inspect.err"
  if inspect_output=$(docker_local inspect "$1" --format '{{.Id}}|{{index .Config.Labels "com.docker.compose.project"}}' 2>"$inspect_error"); then
    if [ -z "$inspect_output" ]; then
      echo "LOCAL_E2E_CONTAINER_INSPECT=FAIL name=$1 reason=empty_result" >&2
      return 1
    fi
    printf '%s' "$inspect_output"
    return 0
  fi
  if grep -Fiq "No such object: $1" "$inspect_error"; then return 2; fi
  echo "LOCAL_E2E_CONTAINER_INSPECT=FAIL name=$1 reason=daemon_or_cli_error" >&2
  return 1
}
