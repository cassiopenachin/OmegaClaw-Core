#!/usr/bin/env bash
set -euo pipefail

cd /PeTTa

su www-data -s /bin/sh -c "sh /opt/nginx/nginx.sh"

GATEWAY_URL="http://localhost:8080"
EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-Local}"

for arg in "$@"; do
  if [[ "$arg" == embeddingprovider=* ]]; then
    export EMBEDDING_PROVIDER="${arg#*=}"
  fi
done

# Optional knowledge-base import
if [[ "${IMPORT_KB_ON_START}" == "1" ]]; then
  su nobody -s /bin/sh -c "${OMEGACLAW_DIR}/scripts/import_knowledge.sh"
fi

# Scrub environment: only allowlisted vars survive.
SAFE_VARS="HOME USER PATH HOSTNAME TERM LANG LC_ALL \
  GATEWAY_URL PYTHONDONTWRITEBYTECODE PYTHONUNBUFFERED \
  HF_HOME SENTENCE_TRANSFORMERS_HOME HF_HUB_OFFLINE TRANSFORMERS_OFFLINE \
  OMEGACLAW_DIR MEMORY_DIR LLM_SERVER_LOCAL_URL TEST_SERVER_IP \
  OMEGAHIVE_DATABASE_URL"
# Note: the board channel runs in-process, so its Postgres DSN (OMEGAHIVE_DATABASE_URL) must
# reach the agent — an agent that can run `shell` can already read it. Limit the blast radius
# with a scoped DB role (deployment spec §4 reader/gateway split), not by scrubbing the DSN.

# Build the allowlisted env as a proper argv list (set --) so values with spaces or glob
# characters (e.g. a libpq keyword DSN, or a password with a space) survive unsplit.
run_args="$*"
set --
for var in $SAFE_VARS; do
  eval val=\${$var:-}
  if [ -n "$val" ]; then
    set -- "$@" "$var=$val"
  fi
done

# shellcheck disable=SC2086  # run_args is intentionally word-split into separate config args
exec env -i "$@" su nobody -s /bin/sh -c "sh run.sh run.metta $run_args"
