# Conversation Intelligence Monitor

A FastAPI service that receives webhook payloads from Chatrace (WhatsApp chatbot platform), analyzes completed conversations using NVIDIA NIM, detects anomalies against rolling baselines, and delivers real-time alerts and periodic digests via Telegram.

## Quick Start

```bash
pip install -r requirements.txt
uvicorn chatbot_monitor.main:app --host 0.0.0.0 --port 8000
```

See config.yaml and .env.example for configuration.
