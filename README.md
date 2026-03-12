# LiveKit Transcription Agents

Python service for real-time transcription of LiveKit live streams using Deepgram STT.

## Setup

1. Install Python venv package (if not already installed):
```bash
sudo apt install python3.12-venv
```

2. Create a virtual environment:
```bash
python3 -m venv venv
```

3. Activate the virtual environment:
```bash
source venv/bin/activate
```

4. Upgrade pip:
```bash
pip install --upgrade pip
```

5. Install dependencies:
```bash
pip install -r requirements.txt
```

**Note:** Always activate the virtual environment before running the agent:
```bash
source venv/bin/activate
```

2. Configure Laravel Settings:
   
   All agent configuration is managed through the Laravel dashboard. Navigate to:
   **Dashboard → Settings → Third Parties**
   
   Configure the following settings:
   - **Deepgram API Key**: Your Deepgram API key for speech-to-text
   - **Deepgram Model**: Choose `nova-2` or `nova-3` (recommended for Arabic)
   - **LiveKit WSS Host**: Your LiveKit server WebSocket URL
   - **LiveKit API Key**: Your LiveKit API key
   - **LiveKit API Secret**: Your LiveKit API secret
   - **Agents Webhook URL**: URL where transcriptions will be sent (defaults to `{APP_URL}/api/livekit/transcription/webhook`)
   - **Default Language**: Default transcription language (e.g., `en`, `ar`, `ar-EG`)
   - **Default Transcription Language**: Fallback language if event doesn't specify one

3. Set APP_URL (optional):
   
   The agent needs to know where your Laravel application is running to fetch configuration.
   If your Laravel `.env` file is in the parent directory, the agent will automatically read `APP_URL` from it.
   
   Otherwise, you can set it manually:
   ```bash
   export APP_URL="http://localhost:8000"  # Or your production URL
   ```
   
   **Note:** If `APP_URL` is not set, the agent defaults to `http://localhost:8000`.

4. Load APP_URL from Laravel .env (optional):
```bash
source load_env.sh
```

5. Run the agent:
   
   **Option 1: Use the run script (easiest):**
   ```bash
   ./run.sh
   ```
   
   **Option 2: Manual run:**
   ```bash
   # Make sure virtual environment is activated
   source venv/bin/activate
   
   # Load APP_URL from Laravel .env (optional)
   source load_env.sh
   
   # Run in development mode
   python main.py dev
   ```

**Quick Start (all in one):**
```bash
./run.sh
```

The `run.sh` script will:
- Activate the virtual environment
- Load `APP_URL` from Laravel `.env` if available (or use default `http://localhost:8000`)
- Fetch all configuration from Laravel API (`{APP_URL}/api/livekit/agent/config`)
- Start the agent in development mode

**How Configuration Works:**
1. Agent reads `APP_URL` from environment (or uses default `http://localhost:8000`)
2. Agent calls `GET {APP_URL}/api/livekit/agent/config` to fetch all settings
3. Settings are loaded from Laravel's Third-Party Settings (configured in dashboard)
4. If API call fails, agent falls back to environment variables (backward compatibility)

## Deployment

The agent can be deployed as a service using systemd, supervisor, or Docker.

### Systemd Service

Create `/etc/systemd/system/livekit-transcription.service`:

```ini
[Unit]
Description=LiveKit Transcription Agent
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/livekit-agents
Environment="APP_URL=https://your-laravel-app.com"
ExecStart=/path/to/livekit-agents/venv/bin/python main.py start
Restart=always

[Install]
WantedBy=multi-user.target
```

**Note:** The agent will automatically fetch all configuration (LiveKit credentials, Deepgram API key, etc.) from the Laravel API using the `APP_URL`. Make sure your Laravel Third-Party Settings are configured in the dashboard.

Then:
```bash
sudo systemctl enable livekit-transcription
sudo systemctl start livekit-transcription
```

**Note:** Make sure to use the Python interpreter from the virtual environment (`venv/bin/python`) in the `ExecStart` path.

## Configuration

### API-Based Configuration

The agent fetches all configuration from the Laravel API endpoint: `GET {APP_URL}/api/livekit/agent/config`

**Required Environment Variable:**
- `APP_URL`: URL of your Laravel application (defaults to `http://localhost:8000` if not set)

**Configuration Source:**
All settings are managed in the Laravel dashboard:
- Navigate to **Dashboard → Settings → Third Parties**
- Configure:
  - **Deepgram API Key**: Your Deepgram API key
  - **Deepgram Model**: `nova-2` or `nova-3` (recommended for Arabic)
  - **LiveKit WSS Host**: Your LiveKit server WebSocket URL
  - **LiveKit API Key**: Your LiveKit API key
  - **LiveKit API Secret**: Your LiveKit API secret
  - **Agents Webhook URL**: Where transcriptions are sent
  - **Default Language**: Default transcription language
  - **Default Transcription Language**: Fallback language

**Fallback Behavior:**
If the API call fails, the agent falls back to environment variables for backward compatibility:
- `DEEPGRAM_API_KEY`
- `DEEPGRAM_MODEL`
- `LIVEKIT_WSS_HOST` / `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `LIVEKIT_AGENTS_WEBHOOK_URL`

### Language Configuration

The agent supports multiple languages for transcription. Language is configured per-event in the Laravel dashboard:

**Supported Languages:**
- `en` - English (mapped to `en-US` for Deepgram)
- `ar` - Arabic (mapped to `ar` for Deepgram)
- `ar-EG` - Egyptian Arabic
- `ar-SA` - Saudi Arabic
- `ar-AE` - UAE Arabic
- `ar-KW` - Kuwaiti Arabic
- `ar-QA` - Qatari Arabic

**Setting Language:**

1. **Via Laravel Dashboard** (recommended):
   - Navigate to **Dashboard → Event Agenda → Create/Edit Event**
   - Set the **Transcription Language** field
   - The agent will automatically use this language for that event

2. **Via Default Settings**:
   - Set **Default Transcription Language** in **Dashboard → Settings → Third Parties**
   - This is used as fallback if an event doesn't specify a language

3. **Via Environment Variable** (fallback only, if API unavailable):
   ```bash
   export DEEPGRAM_LANGUAGE=ar
   ```

**Language Code Mapping:**
- Laravel/App format: `en`, `ar`, `ar-SA`, `ar-EG`, etc.
- Deepgram format: `en-US`, `ar`, `ar-SA`, `ar-EG`, etc.
- The agent automatically converts between formats
- For Arabic, try generic `ar` first. If that doesn't work, try specific variants like `ar-SA` or `ar-EG`

**Troubleshooting:**

1. **Check agent logs** - Look for configuration and language messages:
   ```
   [main] Fetching agent configuration from: http://localhost:8000/api/livekit/agent/config
   [main] Successfully fetched configuration from API
   [TranscriptionAgent] Language configuration:
     - Room name: event-5
     - Resolved language_code: ar-EG
     - Mapped to Deepgram format: ar-EG
     - Deepgram STT initialized with explicit language: ar-EG
   ```

2. **Verify API connectivity** - Ensure the agent can reach Laravel:
   ```bash
   curl http://localhost:8000/api/livekit/agent/config
   ```
   Should return JSON with configuration values.

3. **Check Laravel settings** - Verify Third-Party Settings are configured:
   - Navigate to **Dashboard → Settings → Third Parties**
   - Ensure all required fields are filled (Deepgram API key, LiveKit credentials, etc.)

4. **Verify APP_URL** - Ensure the agent knows where Laravel is:
   ```bash
   echo $APP_URL
   ```
   Should show your Laravel application URL.

5. **Try different Arabic variants**:
   - Start with generic `ar`
   - If that fails, try `ar-SA` (Saudi), `ar-EG` (Egyptian), or other variants
   - Check Deepgram documentation for supported Arabic variants

6. **Restart the agent** - Configuration changes require agent restart:
   ```bash
   # Stop the agent, then restart
   ./run.sh
   ```

6. **Check detected language in logs** - Look for messages like:
   ```
   [TranscriptionAgent] Detected language: ar (expected: ar, confidence: 0.95, text: ...)
   ```
   - If you see "WARNING: Language mismatch!", the language parameter isn't being respected
   - Verify the language is correctly set in agent startup logs

7. **Deepgram Account Configuration**:
   - Ensure your Deepgram account supports Arabic transcription
   - Some Deepgram plans may have language restrictions
   - Check your Deepgram dashboard for supported languages

## Features

- Real-time transcription using Deepgram Nova-2
- Supports English and Arabic (configurable via environment variables)
- Auto-detection support for mixed-language streams
- Language code mapping (Laravel format → Deepgram format)
- Publishes transcriptions to LiveKit text stream for real-time display
- Sends transcriptions to Laravel webhook for storage
- Handles multiple participants in a room
