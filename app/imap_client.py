import email
import logging
import os
import threading
import time
from email.header import decode_header

from imapclient import IMAPClient

import re

from app.email_parser import ParsedTransaction, parse_tatra_banka_email

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = 300  # seconds before IDLE refresh (keep-alive)
RECONNECT_DELAY = 10  # seconds between reconnection attempts


def _get_imap_config() -> tuple[str, int, str, str]:
    host = os.environ.get("IMAP_HOST")
    port = int(os.environ.get("IMAP_PORT", "993"))
    username = os.environ.get("IMAP_USERNAME")
    password = os.environ.get("IMAP_PASSWORD")

    if not all([host, username, password]):
        raise RuntimeError(
            "IMAP_HOST, IMAP_USERNAME, and IMAP_PASSWORD environment variables must be set"
        )
    return host, port, username, password


def _decode_payload(part) -> str:
    """Decode an email part payload to string."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _extract_text_body(msg: email.message.Message) -> str:
    """Extract plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                return _decode_payload(part)
    else:
        if msg.get_content_type() == "text/plain":
            return _decode_payload(msg)
    return ""


def _decode_subject(msg: email.message.Message) -> str:
    """Decode email subject header."""
    subject = msg.get("Subject", "")
    decoded_parts = decode_header(subject)
    parts = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(part)
    return " ".join(parts)


def _extract_bank_transaction_id(subject: str) -> str | None:
    """Extract bank transaction ID from email subject like 'Debet na ucte (ID=110326/656-2)'."""
    match = re.search(r"ID=([^)]+)", subject)
    return match.group(1) if match else None


def _parse_email_message(raw_email: bytes) -> ParsedTransaction | None:
    """Parse a raw email into a ParsedTransaction."""
    msg = email.message_from_bytes(raw_email)
    subject = _decode_subject(msg)
    body = _extract_text_body(msg)

    logger.info("Processing email: %s", subject)

    if not body:
        logger.warning("No text body found in email: %s", subject)
        return None

    parsed = parse_tatra_banka_email(body)
    if parsed:
        parsed.bank_transaction_id = _extract_bank_transaction_id(subject)
        logger.info(
            "Parsed transaction: IBAN=%s, amount=%.2f, payee=%s, bank_id=%s",
            parsed.iban,
            parsed.amount,
            parsed.payee_name,
            parsed.bank_transaction_id,
        )
    else:
        logger.warning("Could not parse transaction from email: %s", subject)
    return parsed


def _fetch_and_process_unseen(client: IMAPClient, on_transaction) -> int:
    """Fetch all unseen messages, parse them, and call on_transaction for each."""
    msg_ids = client.search(["UNSEEN"])
    if not msg_ids:
        return 0

    logger.info("Found %d unread message(s)", len(msg_ids))
    count = 0

    for uid, data in client.fetch(msg_ids, ["RFC822"]).items():
        raw_email = data[b"RFC822"]
        parsed = _parse_email_message(raw_email)
        if parsed:
            success = on_transaction(parsed)
            if success:
                count += 1
            else:
                # Mark as unread so it gets retried
                client.remove_flags([uid], [b"\\Seen"])
                logger.warning("Marked message UID=%s as unread after failed processing", uid)
        else:
            # Unparseable email — mark as unread
            client.remove_flags([uid], [b"\\Seen"])
            logger.warning("Marked message UID=%s as unread (could not parse)", uid)

    return count


def _connect(host: str, port: int, username: str, password: str) -> IMAPClient:
    """Create a new IMAP connection and select INBOX."""
    logger.info("Connecting to IMAP server %s:%d", host, port)
    client = IMAPClient(host, port=port, ssl=True)
    client.login(username, password)
    logger.info("IMAP login successful")
    client.select_folder("INBOX")
    return client


class ImapWatcher:
    """Persistent IMAP watcher that uses IDLE to receive new emails in real-time."""

    def __init__(self, on_transaction):
        """
        Args:
            on_transaction: Callback called with a ParsedTransaction for each
                            successfully parsed email.
        """
        self._on_transaction = on_transaction
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        """Start the IMAP watcher in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="imap-watcher")
        self._thread.start()
        logger.info("IMAP watcher started")

    def stop(self):
        """Signal the IMAP watcher to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
            logger.info("IMAP watcher stopped")

    def _run(self):
        """Main loop: connect, process unseen, IDLE, reconnect on failure."""
        host, port, username, password = _get_imap_config()

        while not self._stop_event.is_set():
            client = None
            try:
                client = _connect(host, port, username, password)

                # Process any existing unread messages first
                count = _fetch_and_process_unseen(client, self._on_transaction)
                if count:
                    logger.info("Processed %d existing unread email(s)", count)

                # Enter IDLE loop
                while not self._stop_event.is_set():
                    client.idle()
                    responses = client.idle_check(timeout=IDLE_TIMEOUT)
                    client.idle_done()

                    if self._stop_event.is_set():
                        break

                    # Check if we got new mail notifications
                    has_new = any(
                        b"EXISTS" in resp if isinstance(resp, (bytes, bytearray))
                        else (len(resp) > 1 and resp[1] == b"EXISTS")
                        for resp in responses
                    )

                    if has_new:
                        logger.info("New email(s) detected via IDLE")
                        _fetch_and_process_unseen(client, self._on_transaction)

            except Exception as e:
                logger.error("IMAP connection error: %s", e)
            finally:
                if client:
                    try:
                        client.logout()
                    except Exception:
                        pass

            if not self._stop_event.is_set():
                logger.info("Reconnecting in %d seconds...", RECONNECT_DELAY)
                self._stop_event.wait(RECONNECT_DELAY)
