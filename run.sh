#!/bin/bash
# Run the captioner locally (hot reload). Needs a .env with LIVEKIT_* and DEEPGRAM_API_KEY.
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

source venv/bin/activate
set -a
[ -f .env ] && source .env
set +a

python main.py dev
