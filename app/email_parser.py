from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


@dataclass
class ParsedTransaction:
    iban: str  # Account lookup identifier; historically this was always an IBAN.
    amount: float  # negative = outflow, positive = inflow
    date: date
    payee_name: str
    memo: str
    bank_transaction_id: str | None = None
    is_savings_transfer: bool = False
    savings_account_name: str | None = None
    source_account_number: str | None = None

    @property
    def account_identifier(self) -> str:
        """YNAB account note lookup key: IBAN, account number, or masked card number."""
        return self.iban


def _parse_amount(amount_str: str) -> float:
    """Parse amount strings like '12,04', '4 000,00', or '11.48' to float."""
    cleaned = amount_str.replace("\xa0", "").replace(" ", "").replace(",", ".")
    return float(cleaned)


def _parse_transaction_date(date_str: str) -> date:
    """Parse dates in Tatra Banka notifications, e.g. '9.3.2026'."""
    day, month, year = [p.strip() for p in date_str.split(".")]
    return date(int(year), int(month), int(day))


def _extract_memo(body: str) -> str:
    desc_match = re.search(r"Popis transakcie:\s*(.+?)(?:\n|$)", body)
    if desc_match:
        return desc_match.group(1).strip().rstrip(".")
    return ""


def _extract_source_account_number(memo: str) -> str | None:
    # e.g. "Platba 1100/000000-1238488000" -> "1238488000"
    account_match = re.search(r"\d{4}/\d+-([\d]+)", memo)
    if account_match:
        return account_match.group(1)
    return None


def parse_tatra_banka_email(body: str) -> ParsedTransaction | None:
    """Parse a Tatra Banka notification email body into a transaction.

    Expected format:
        9.3.2026 14:00 bol zostatok Vasho uctu SK7711... znizeny o 12,04 EUR.
        ...
        Popis transakcie: Platba kartou ...
        Ucet protistrany: Some Name (optional)
    """
    body = body.strip()
    if not body:
        return None

    # Match the main transaction line
    pattern = (
        r"(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\s+\d{1,2}:\d{2}\s+"
        r"bol zostatok Vasho uctu\s+([A-Z]{2}\d+)\s+"
        r"(znizeny|zvyseny)\s+o\s+([\d\s\xa0]+,\d{2})\s+EUR"
    )
    match = re.search(pattern, body)
    if not match:
        return _parse_savings_email(body) or _parse_credit_card_email(body)

    date_str = match.group(1)
    iban = match.group(2)
    direction = match.group(3)
    amount_str = match.group(4)

    transaction_date = _parse_transaction_date(date_str)

    # Parse amount
    amount = _parse_amount(amount_str)
    if direction == "znizeny":
        amount = -amount

    # Extract transaction description
    memo = _extract_memo(body)

    # Extract counterparty name (if present) — use as payee
    payee_name = ""
    counterparty_match = re.search(r"Ucet protistrany:\s*(.+?)(?:\n|$)", body)
    if counterparty_match:
        payee_name = counterparty_match.group(1).strip()

    # Fall back to transaction description as payee if no counterparty
    if not payee_name:
        # For card payments like "Platba kartou 4404**3080, SLOVNAFT-SK40120"
        # use only the part after the comma as payee
        card_match = re.match(r"Platba kartou\s+\S+,\s*(.+)", memo)
        if card_match:
            payee_name = card_match.group(1).strip()
        else:
            payee_name = memo if memo else "Unknown"

    source_account_number = _extract_source_account_number(memo)

    return ParsedTransaction(
        iban=iban,
        amount=amount,
        date=transaction_date,
        payee_name=payee_name,
        memo=memo,
        source_account_number=source_account_number,
    )


def _parse_savings_email(body: str) -> ParsedTransaction | None:
    """Parse a Tatra Banka savings account notification.

    Expected format:
        14.3.2026 10:37 bol zostatok Vasho sporenia Emergency fund zvyseny o 1 000,00 EUR.
        ...
        Popis transakcie: Platba 1100/000000-1238488000
    """
    pattern = (
        r"(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\s+\d{1,2}:\d{2}\s+"
        r"bol zostatok Vasho sporenia\s+(.+?)\s+zvyseny\s+o\s+([\d\s\xa0]+,\d{2})\s+EUR"
    )
    match = re.search(pattern, body)
    if not match:
        return None

    date_str = match.group(1)
    savings_name = match.group(2).strip()
    amount_str = match.group(3)

    transaction_date = _parse_transaction_date(date_str)

    amount = _parse_amount(amount_str)

    memo = _extract_memo(body)

    source_account_number = _extract_source_account_number(memo)

    return ParsedTransaction(
        iban="",
        amount=amount,
        date=transaction_date,
        payee_name="",
        memo=memo,
        is_savings_transfer=True,
        savings_account_name=savings_name,
        source_account_number=source_account_number,
    )


def _parse_credit_card_email(body: str) -> ParsedTransaction | None:
    """Parse a Tatra Banka credit card notification.

    Expected format:
        dna 30.6.2026 09:17 bol znizeny zostatok uveroveho ramca Vasej kreditnej karty
        o sumu transakcie vo vyske 11.48 EUR
        Cislo karty: 4330**3001
        Popis transakcie: OMV 2449, Bratislava SK, dom nakup - autorizovana
    """
    pattern = (
        r"dna\s+(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\s+\d{1,2}:\d{2}\s+"
        r"bol\s+(znizeny|zvyseny)\s+zostatok uveroveho ramca Vasej kreditnej karty\s+"
        r"o sumu transakcie vo vyske\s+([\d\s\xa0]+[,.]\d{2})\s+EUR"
    )
    match = re.search(pattern, body)
    if not match:
        return None

    card_match = re.search(r"Cislo karty:\s*(.+?)(?:\n|$)", body)
    if not card_match:
        return None

    transaction_date = _parse_transaction_date(match.group(1))
    direction = match.group(2)
    amount = _parse_amount(match.group(3))
    if direction == "znizeny":
        amount = -amount

    card_number = card_match.group(1).strip().upper().replace(" ", "").replace("\xa0", "")
    memo = _extract_memo(body)
    payee_name = memo.split(",", 1)[0].strip() if memo else "Unknown"

    return ParsedTransaction(
        iban=card_number,
        amount=amount,
        date=transaction_date,
        payee_name=payee_name,
        memo=memo,
    )
