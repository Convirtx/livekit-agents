#!/bin/bash
# Load APP_URL from Laravel .env file
# The agent will fetch all other configuration from the Laravel API using this URL

if [ -f "../.env" ]; then
    echo "Loading APP_URL from Laravel .env..."

    # Extract APP_URL from Laravel .env
    APP_URL=$(grep "^APP_URL=" ../.env | cut -d '=' -f2 | tr -d '"' | tr -d "'")

    if [ -n "$APP_URL" ]; then
        export APP_URL="$APP_URL"
        echo "APP_URL loaded: $APP_URL"
        echo "Agent will fetch configuration from: ${APP_URL}/api/livekit/agent/config"
    else
        echo "Warning: APP_URL not found in .env, agent will use default: http://localhost:8000"
    fi
else
    echo "Warning: ../.env file not found."
    echo "Agent will use default APP_URL: http://localhost:8000"
fi
