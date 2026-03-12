#!/bin/bash
# Setup script for LiveKit Transcription Agents

set -e

echo "Setting up LiveKit Transcription Agents..."

# Check if python3-venv is installed
if ! dpkg -l | grep -q python3.*-venv; then
    echo "python3-venv is not installed. Installing..."
    sudo apt install python3.12-venv -y
fi

# Create virtual environment
echo "Creating virtual environment..."
python3 -m venv venv

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

echo "Setup complete!"
echo ""
echo "To activate the virtual environment in the future, run:"
echo "  source venv/bin/activate"
echo ""
echo "To run the agent:"
echo "  source venv/bin/activate"
echo "  python main.py dev"
