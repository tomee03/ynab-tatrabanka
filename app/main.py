import logging
import signal
import sys

from dotenv import load_dotenv

from app.email_parser import ParsedTransaction
from app.imap_client import ImapWatcher
from app.ynab_client import (
    account_cache,
    create_transaction,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _handle_savings_transfer(parsed: ParsedTransaction) -> bool:
    """Handle a savings account transfer notification. Returns True on success."""
    savings_name = parsed.savings_account_name
    if not savings_name:
        logger.warning("No savings account name found in email — skipping")
        return False

    # Find savings account by name
    savings_info = account_cache.find_by_name(savings_name)
    if not savings_info:
        logger.warning("No YNAB account found with name '%s' — skipping", savings_name)
        return False

    # Find source account by account number from Popis transakcie
    if not parsed.source_account_number:
        logger.warning("No source account number found in email — skipping")
        return False

    source_info = account_cache.find_by_iban(parsed.source_account_number)
    if not source_info:
        logger.warning(
            "No YNAB account found matching account number %s — skipping",
            parsed.source_account_number,
        )
        return False

    if not source_info.transfer_payee_id:
        logger.warning(
            "Account '%s' has no transfer_payee_id — skipping",
            source_info.account_name,
        )
        return False

    try:
        response = create_transaction(
            plan_id=savings_info.plan_id,
            account_id=savings_info.account_id,
            amount=parsed.amount,
            transaction_date=parsed.date,
            payee_id=source_info.transfer_payee_id,
            memo=parsed.memo,
            bank_transaction_id=parsed.bank_transaction_id,
        )
        created = response.data
        tx_ids = [str(t) for t in (created.transaction_ids or [])]
        logger.info(
            "Created YNAB savings transfer: %.2f from '%s' to '%s' (IDs: %s)",
            parsed.amount,
            source_info.account_name,
            savings_info.account_name,
            tx_ids,
        )
        return True
    except Exception as e:
        logger.error(
            "Failed to create YNAB savings transfer (%.2f): %s",
            parsed.amount,
            e,
        )
        return False


def _handle_transaction(parsed: ParsedTransaction) -> bool:
    """Called by ImapWatcher for each parsed email transaction. Returns True on success."""
    if parsed.is_savings_transfer:
        return _handle_savings_transfer(parsed)

    info = account_cache.find_by_iban(parsed.iban)
    if not info:
        logger.warning("No YNAB account found for IBAN %s — skipping", parsed.iban)
        return False

    # Check if this is a transfer from/to a known account (e.g. savings)
    payee_id = None
    payee_name = parsed.payee_name
    if parsed.source_account_number:
        counterpart = account_cache.find_by_iban(parsed.source_account_number)
        if counterpart and counterpart.transfer_payee_id:
            payee_id = counterpart.transfer_payee_id
            payee_name = None
            logger.info(
                "Matched transfer counterpart: '%s' (account number %s)",
                counterpart.account_name,
                parsed.source_account_number,
            )

    try:
        response = create_transaction(
            plan_id=info.plan_id,
            account_id=info.account_id,
            amount=parsed.amount,
            transaction_date=parsed.date,
            payee_name=payee_name,
            payee_id=payee_id,
            memo=parsed.memo,
            bank_transaction_id=parsed.bank_transaction_id,
        )
        created = response.data
        tx_ids = [str(t) for t in (created.transaction_ids or [])]
        logger.info(
            "Created YNAB transaction: %.2f %s in '%s' (IDs: %s)",
            parsed.amount,
            parsed.payee_name,
            info.account_name,
            tx_ids,
        )
        return True
    except Exception as e:
        logger.error(
            "Failed to create YNAB transaction for %s (%.2f): %s",
            parsed.payee_name,
            parsed.amount,
            e,
        )
        return False


def main():
    logger.info("Starting YNAB Transaction Service")

    logger.info("Warming up YNAB account cache...")
    account_cache.refresh()

    watcher = ImapWatcher(on_transaction=_handle_transaction)
    watcher.start()

    def _shutdown(signum, frame):
        logger.info("Shutting down...")
        watcher.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    signal.pause()


if __name__ == "__main__":
    main()
