#!/bin/bash
set -euo pipefail
# shebang: запускает скрипт через bash

# SCRIPT_DIR: абсолютный путь к директории скрипта
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# запускает llama-server с заданными параметрами
exec llama-server \
    --host 127.0.0.1 \
    --port 9999 \
    --ui \
    --models-max 1 \
    --models-preset "$SCRIPT_DIR"/models.ini
