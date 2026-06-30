from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

import ynab
from ynab.models.new_transaction import NewTransaction
from ynab.models.post_transactions_wrapper import PostTransactionsWrapper

logger = logging.getLogger(__name__)

CACHE_TTL = 6 * 60 * 60  # 6 hours in seconds


def _get_configuration() -> ynab.Configuration:
    token = os.environ.get("YNAB_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("YNAB_ACCESS_TOKEN environment variable is not set")
    return ynab.Configuration(access_token=token)


@contextmanager
def _api_client():
    configuration = _get_configuration()
    with ynab.ApiClient(configuration) as client:
        yield client


def get_plans() -> list:
    with _api_client() as client:
        api = ynab.PlansApi(client)
        response = api.get_plans()
        return response.data.plans


def get_accounts(plan_id: str) -> list:
    with _api_client() as client:
        api = ynab.AccountsApi(client)
        response = api.get_accounts(plan_id)
        return response.data.accounts


@dataclass
class AccountInfo:
    plan_id: str
    account_id: str
    account_name: str
    account_identifier: str
    transfer_payee_id: str | None = None

    @property
    def iban(self) -> str:
        """Backward-compatible alias for older IBAN-only callers."""
        return self.account_identifier


class AccountCache:
    """Cache mapping account note identifiers to YNAB accounts across all plans.

    Identifiers can be IBANs, domestic account numbers, or masked card numbers.
    Refreshes automatically every CACHE_TTL seconds."""

    def __init__(self):
        self._cache: dict[str, AccountInfo] = {}
        self._by_name: dict[str, AccountInfo] = {}
        self._lock = threading.Lock()
        self._last_refresh: float = 0

    def _needs_refresh(self) -> bool:
        return time.time() - self._last_refresh > CACHE_TTL

    def refresh(self):
        """Reload all accounts from all plans."""
        logger.info("Refreshing YNAB account cache...")
        new_cache: dict[str, AccountInfo] = {}
        new_by_name: dict[str, AccountInfo] = {}

        plans = get_plans()
        for plan in plans:
            plan_id = str(plan.id)
            try:
                accounts = get_accounts(plan_id)
            except Exception as e:
                logger.warning("Failed to fetch accounts for plan '%s': %s", plan.name, e)
                continue

            for account in accounts:
                if account.closed or account.deleted:
                    continue

                info = AccountInfo(
                    plan_id=plan_id,
                    account_id=str(account.id),
                    account_name=account.name,
                    account_identifier="",
                    transfer_payee_id=str(account.transfer_payee_id) if account.transfer_payee_id else None,
                )

                # Index by name
                new_by_name[account.name] = info

                # Index by account identifier if present in notes
                if account.note:
                    note_normalized = account.note.strip().upper().replace(" ", "")
                    info = AccountInfo(
                        plan_id=plan_id,
                        account_id=str(account.id),
                        account_name=account.name,
                        account_identifier=note_normalized,
                        transfer_payee_id=str(account.transfer_payee_id) if account.transfer_payee_id else None,
                    )
                    new_cache[note_normalized] = info
                    logger.info(
                        "Cached: account_identifier=%s → plan=%s, account='%s'",
                        note_normalized,
                        plan.name,
                        account.name,
                    )

        with self._lock:
            self._cache = new_cache
            self._by_name = new_by_name
            self._last_refresh = time.time()

        logger.info(
            "Account cache refreshed: %d account identifier(s), %d name(s) mapped",
            len(new_cache),
            len(new_by_name),
        )

    def find_by_account_identifier(self, identifier: str) -> AccountInfo | None:
        """Look up an account by account note identifier. Auto-refreshes if cache is stale."""
        if self._needs_refresh():
            self.refresh()

        identifier_normalized = identifier.strip().upper().replace(" ", "")
        with self._lock:
            # Exact match first
            if identifier_normalized in self._cache:
                return self._cache[identifier_normalized]
            # Substring match (identifier contained in note)
            for key, info in self._cache.items():
                if identifier_normalized in key or key in identifier_normalized:
                    return info
        return None

    def find_by_iban(self, iban: str) -> AccountInfo | None:
        """Look up an account by IBAN. Kept for compatibility with existing callers."""
        return self.find_by_account_identifier(iban)

    def find_by_name(self, name: str) -> AccountInfo | None:
        """Look up an account by name. Auto-refreshes if cache is stale."""
        if self._needs_refresh():
            self.refresh()
        with self._lock:
            return self._by_name.get(name)


# Global singleton cache
account_cache = AccountCache()


def create_transaction(
    plan_id: str,
    account_id: str,
    amount: float,
    transaction_date: date,
    payee_name: str | None = None,
    payee_id: str | None = None,
    memo: str | None = None,
    bank_transaction_id: str | None = None,
):
    milliunit_amount = int(amount * 1000)

    if bank_transaction_id:
        import_id = f"TB:{bank_transaction_id}:{uuid.uuid4().hex[:8]}"
    else:
        import_id = f"YNAB:{milliunit_amount}:{transaction_date.isoformat()}:{uuid.uuid4().hex[:8]}"

    transaction = NewTransaction(
        account_id=account_id,
        var_date=transaction_date,
        amount=milliunit_amount,
        payee_name=payee_name,
        payee_id=payee_id,
        memo=memo,
        cleared="cleared",
        import_id=import_id,
    )

    data = PostTransactionsWrapper(transaction=transaction)

    with _api_client() as client:
        api = ynab.TransactionsApi(client)
        response = api.create_transaction(plan_id, data)
        return response
