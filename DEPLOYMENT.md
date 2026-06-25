# Deployment Guide for Render

## Environment Variables

Set these in your Render dashboard:

- `TELEGRAM_BOT_TOKEN`: Your Telegram bot token from BotFather
- `TELEGRAM_CHAT_IDS`: Comma-separated list of admin chat IDs (e.g., `123456789,987654321`)
- `ADMIN_KEY`: A secret key for admin API endpoints
- `WEBHOOK_URL`: Your Render app URL (e.g., `https://your-app.onrender.com`)
- `PYTHON_VERSION`: `3.13`

## Render Configuration

### 1. Create a Web Service

- **Name**: waakye-order-system
- **Region**: Choose nearest to your users
- **Branch**: `main`
- **Root Directory**: `.` (leave empty)
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `python flask_app.py`

### 2. Add Environment Variables

In Render dashboard → Settings → Environment Variables:

```
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_IDS=123456789,987654321
ADMIN_KEY=your_secret_key_here
WEBHOOK_URL=https://waakye-order-system.onrender.com
PYTHON_VERSION=3.13
```

### 3. Deploy

Push your code to GitHub, then connect the repository to Render. It will automatically deploy.

## How It Works

- **Development (local)**: Uses polling when `WEBHOOK_URL` is not set
- **Production (Render)**: Uses webhooks when `WEBHOOK_URL` is set
- The `/telegram-webhook` endpoint receives updates from Telegram
- Telegram sends updates to your app instead of your app polling Telegram

## Post-Deployment

1. After deployment, check the logs to verify the webhook was set up successfully
2. Test by sending a message to your Telegram bot
3. Place a test order and verify admin commands work

## Troubleshooting

- If webhook fails, check that `WEBHOOK_URL` is set correctly
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` are correct
- Check Render logs for any errors during startup
