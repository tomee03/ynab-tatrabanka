# YNAB Tatra Banka

Automatically creates [YNAB](https://www.ynab.com/) transactions from Tatra Banka email notifications.

The service connects to an IMAP mailbox, listens for new emails using IDLE (push), parses Tatra Banka transaction notifications, matches the account by IBAN, and creates transactions in YNAB.

## Features

- **Real-time processing** — uses IMAP IDLE for instant email detection, no polling
- **Multi-plan support** — scans all YNAB plans/budgets to find accounts by IBAN
- **Savings account transfers** — automatically records transfers between personal and savings accounts
- **Account caching** — IBAN-to-account mappings are cached and refreshed every 6 hours
- **Retry on failure** — failed emails are marked as unread so they get reprocessed
- **Auto-reconnect** — recovers automatically from IMAP connection failures
- **Graceful shutdown** — handles SIGINT/SIGTERM for clean container stops

## How it works

1. Connects to the configured IMAP mailbox and selects INBOX
2. Processes any existing unread emails
3. Enters IDLE mode and waits for new emails
4. When a new email arrives, parses the Tatra Banka notification to extract:
   - IBAN, amount, date, payee, memo, and bank transaction ID
5. Looks up the IBAN in the cached YNAB account mappings
6. Creates a transaction in the matching YNAB account
7. If processing fails, the email is marked as unread for retry

### Supported email types

- **Regular transactions** — card payments, incoming/outgoing transfers on your main account
- **Savings deposit** — money moved from personal to savings account (`sporenia ... zvyseny`)
- **Savings withdrawal** — money moved from savings back to personal account (matched via account number in `Popis transakcie`)

### IBAN matching

Store the IBAN in the **Notes** field of your YNAB account. The service scans all plans and accounts on startup and caches the mappings.

### Savings accounts

The service automatically handles transfers between your personal and savings accounts in Tatra Banka.

**Setup in YNAB:**

1. Create a savings account in YNAB with the **exact same name** as in Tatra Banka (e.g. `Emergency fund`)
2. Store the IBAN of your personal account in its **Notes** field (as with any other account)

**How it works:**

- **Deposit to savings** — When you transfer money to a savings account, Tatra Banka sends an email like _"bol zostatok Vasho sporenia Emergency fund zvyseny o 1 000,00 EUR"_. The service extracts the savings account name (`Emergency fund`) from the email, finds the matching YNAB account by name, extracts the source account number from `Popis transakcie`, and creates a transfer transaction using the source account's `transfer_payee_id`.

- **Withdrawal from savings** — When you transfer money from savings back to your personal account, you receive a normal credit notification on your personal account. The `Popis transakcie` contains the savings account number. The service matches it to your YNAB savings account and records it as a transfer using the savings account's `transfer_payee_id`.

Both directions are recorded as proper YNAB transfers between accounts, not regular payee transactions.

## Setup

### Prerequisites

- A [YNAB Personal Access Token](https://api.ynab.com/#personal-access-tokens)
- An IMAP-enabled email account that receives Tatra Banka notifications
- IBAN set in the Notes field of the corresponding YNAB account(s)

### Configuration

Create a `.env` file based on `.env.example`:

```env
YNAB_ACCESS_TOKEN=your-ynab-token

IMAP_HOST=imap.server.com
IMAP_PORT=993
IMAP_USERNAME=bank@example.com
IMAP_PASSWORD=your-password
```

### Run with Docker Compose

```bash
docker compose up -d
```

### Run with Docker

```bash
docker build -t ynab-tatrabanka .
docker run --env-file .env ynab-tatrabanka
```

### Run locally

```bash
pip install -r requirements.txt
export $(cat .env | xargs)
python -m app.main
```

## Release

Docker images are published to [GitHub Container Registry](https://ghcr.io) when a version tag is pushed:

```bash
git tag v1.0.0
git push origin v1.0.0
```

This produces image tags: `1.0.0`, `1.0`, and `latest`.

```bash
docker pull ghcr.io/<owner>/ynab-tatrabanka:latest
```
