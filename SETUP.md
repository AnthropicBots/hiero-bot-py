# Local Setup

This guide walks you through setting up **Hiero Maintainer Bot** for local development and testing.

## Prerequisites

Before getting started, ensure you have the following installed:

- Python 3.12 or later
- Git
- A GitHub account
- A GitHub App (required for webhook events)
- ngrok or Cloudflare Tunnel (for local webhook testing)

---

## 1. Clone the Repository

```bash
git clone https://github.com/AnthropicBots/hiero-bot-py.git
cd hiero-bot-py
```

---

## 2. Create a Virtual Environment

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Windows

```powershell
python -m venv .venv
.venv\Scripts\activate
```

---

## 3. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 4. Configure Environment Variables

Copy the example environment file.

### Linux/macOS

```bash
cp .env.example .env
```

### Windows

```powershell
copy .env.example .env
```

Open `.env` and configure the required values:

```text
GITHUB_APP_ID=<your_github_app_id>
GITHUB_PRIVATE_KEY=<your_private_key>
GITHUB_WEBHOOK_SECRET=<your_webhook_secret>
```

Optional environment variables:

```text
ANTHROPIC_API_KEY=<your_anthropic_api_key>
DATABASE_URL=<postgres_or_sqlite_url>
LOG_LEVEL=info
ENVIRONMENT=development
```

> **Note:** If `DATABASE_URL` is not specified, the bot uses the default SQLite database (`hiero_bot.db`), which is recommended for local development.

---

## 5. Create a GitHub App

Create a new GitHub App from:

https://github.com/settings/apps

Configure the app with the required repository permissions and webhook events.

Then:

- Generate a private key (`.pem`)
- Copy the private key into `GITHUB_PRIVATE_KEY`
- Copy the App ID into `GITHUB_APP_ID`
- Generate a webhook secret and add it to `GITHUB_WEBHOOK_SECRET`
- Install the app on the repository where you want to test the bot

---

## 6. Configure the Repository

Create the following file in the target repository:

```text
.github/hiero-bot.yml
```

Use the example configuration provided in:

```text
templates/hiero-bot.yml
```

The bot will remain **inactive** until this configuration file exists.

---

## 7. Run the Bot

Start the development server:

```bash
uvicorn app.main:app --reload
```

The application will be available at:

```
http://localhost:8000
```

---

## 8. Expose Your Local Server

GitHub must be able to reach your local server to deliver webhook events.

### Using ngrok

```bash
ngrok http 8000
```

You'll receive a public URL similar to:

```
https://abcd1234.ngrok-free.app
```

Update your GitHub App webhook URL to:

```
https://abcd1234.ngrok-free.app/webhook
```

You can also use Cloudflare Tunnel if you prefer.

---

## 9. Verify the Setup

Open the dashboard:

```
http://localhost:8000
```

Verify the API is running:

```
http://localhost:8000/api/v1/health
```

Create a test issue or pull request in your configured repository and confirm that webhook deliveries succeed in your GitHub App settings.

---

# Development Commands

### Start the development server

```bash
uvicorn app.main:app --reload
```

### Run all tests

```bash
pytest
```

### Run tests with coverage

```bash
python -m pytest tests/ --cov=app --cov-report=term-missing
```

### Run Ruff

```bash
ruff check .
```

### Automatically fix lint issues

```bash
ruff check . --fix
```

---

# Troubleshooting

### The bot does not respond

- Verify the GitHub App is installed on the repository.
- Ensure `.github/hiero-bot.yml` exists.
- Confirm the application is running.
- Check webhook delivery logs in the GitHub App settings.

### Authentication errors

Verify the following environment variables:

- `GITHUB_APP_ID`
- `GITHUB_PRIVATE_KEY`
- `GITHUB_WEBHOOK_SECRET`

### Webhook deliveries fail

- Ensure your ngrok or Cloudflare Tunnel is running.
- Verify the webhook URL is correct.
- Confirm the webhook secret matches the value in `.env`.

### Database issues

The default SQLite database is created automatically.

If using PostgreSQL, verify that `DATABASE_URL` points to a running database and is correctly configured.