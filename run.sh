#!/bin/bash
# Convenience script to run the transcription agent

cd "$(dirname "$0")"

# Activate virtual environment
if [ ! -d "venv" ]; then
    echo "Error: Virtual environment not found. Please run ./setup.sh first"
    exit 1
fi

source venv/bin/activate

# Load APP_URL from Laravel .env if it exists
if [ -f "../.env" ]; then
    source load_env.sh
fi

# Set default APP_URL if not set
if [ -z "$APP_URL" ]; then
    export APP_URL="http://localhost:8000"
    echo "Using default APP_URL: $APP_URL"
fi

# Run the agent
echo "Starting LiveKit Transcription Agent..."
echo "APP_URL: $APP_URL"
echo "Agent will fetch configuration from: ${APP_URL}/api/livekit/agent/config"
echo ""

python main.py dev
