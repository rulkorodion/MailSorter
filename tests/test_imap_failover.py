"""Offline DNS/TLS failover tests. No corporate DNS or IMAP connection."""

from __future__ import annotations

import imaplib
import socket
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from imap_tools import MailboxLoginError

from mailsorter.config import ImapConfig
from mailsorter.imap_client import ImapClient, _PreconnectedIMAP4SSL, _PreconnectedMailBox, _open_tls_socket

HOST = "mail.example.invalid"
ADDRESSES = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 993)),
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 993)),
]


def config(**kwargs) -> ImapConfig:
    return ImapConfig(host=HOST, username="offline-user", password="offline-password",
                      timeout_seconds=15, **kwargs)


def fake_mailbox(login_error=None):
    mailbox = MagicMock()
    mailbox.folder.list.return_value = [SimpleNamespace(name="INBOX", delim="/")]
    if login_error is not None:
        mailbox.login.side_effect = login_error
    return mailbox


class ImapFailoverTests(unittest.TestCase):
    def test_connect_remembers_selected_inbox_uidvalidity(self):
        tls_socket = MagicMock()
        mailbox = fake_mailbox()
        mailbox.client.untagged_responses = {"UIDVALIDITY": [b"23"]}
        with patch("mailsorter.imap_client.socket.getaddrinfo", return_value=ADDRESSES[:1]), \
             patch("mailsorter.imap_client._open_tls_socket", return_value=tls_socket), \
             patch("mailsorter.imap_client._PreconnectedMailBox", return_value=mailbox):
            client = ImapClient(config())
            client.connect()
            self.assertEqual(client._selected_folder, "INBOX")
            self.assertEqual(client.selected_uidvalidity, 23)
            client.disconnect()
        self.assertIsNone(client.selected_uidvalidity)

    def test_first_ip_handshake_fails_second_succeeds(self):
        tls_socket = MagicMock()
        mailbox = fake_mailbox()
        with patch("mailsorter.imap_client.socket.getaddrinfo", return_value=ADDRESSES), \
             patch("mailsorter.imap_client._open_tls_socket",
                   side_effect=[ssl.SSLEOFError("offline EOF"), tls_socket]) as open_tls, \
             patch("mailsorter.imap_client._PreconnectedMailBox", return_value=mailbox) as adapter:
            client = ImapClient(config())
            client.connect()
            self.assertIs(client.mailbox, mailbox)
            client.disconnect()
        self.assertEqual(open_tls.call_count, 2)
        self.assertEqual(open_tls.call_args_list[0].args[0][0], "192.0.2.1")
        self.assertEqual(open_tls.call_args_list[1].args[0][0], "192.0.2.2")
        self.assertEqual([call.args[4] for call in open_tls.call_args_list], [HOST, HOST])
        self.assertIs(adapter.call_args.args[2], tls_socket)
        mailbox.login.assert_called_once_with("offline-user", "offline-password")

    def test_all_ips_unavailable_and_bounded(self):
        with patch("mailsorter.imap_client.socket.getaddrinfo", return_value=ADDRESSES), \
             patch("mailsorter.imap_client._open_tls_socket",
                   side_effect=[TimeoutError("offline timeout"), ssl.SSLEOFError("offline EOF")]) as open_tls, \
             patch("mailsorter.imap_client._PreconnectedMailBox") as adapter:
            with self.assertRaisesRegex(RuntimeError, "недоступен"):
                ImapClient(config()).connect()
        self.assertEqual(open_tls.call_count, 2)
        adapter.assert_not_called()

    def test_certificate_failure_stops_without_tls_downgrade(self):
        with patch("mailsorter.imap_client.socket.getaddrinfo", return_value=ADDRESSES), \
             patch("mailsorter.imap_client._open_tls_socket",
                   side_effect=ssl.SSLCertVerificationError("offline mismatch")) as open_tls:
            with self.assertRaisesRegex(RuntimeError, "сертификата"):
                ImapClient(config()).connect()
        self.assertEqual(open_tls.call_count, 1)
        context = open_tls.call_args.args[5]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_certificate_failure_at_adapter_also_stops_failover(self):
        tls_socket = MagicMock()
        with patch("mailsorter.imap_client.socket.getaddrinfo", return_value=ADDRESSES), \
             patch("mailsorter.imap_client._open_tls_socket", return_value=tls_socket) as open_tls, \
             patch("mailsorter.imap_client._PreconnectedMailBox",
                   side_effect=ssl.SSLCertVerificationError("offline mismatch")):
            with self.assertRaisesRegex(RuntimeError, "сертификата"):
                ImapClient(config()).connect()
        self.assertEqual(open_tls.call_count, 1)
        tls_socket.close.assert_called()

    def test_original_host_is_used_for_sni_on_every_ip(self):
        raw_sockets = [MagicMock(), MagicMock()]
        tls_sockets = [MagicMock(), MagicMock()]
        context = MagicMock()
        context.wrap_socket.side_effect = tls_sockets
        with patch("mailsorter.imap_client.socket.socket", side_effect=raw_sockets):
            for index, entry in enumerate(ADDRESSES):
                result = _open_tls_socket(
                    entry[4], entry[0], entry[1], entry[2], HOST, context, 4.0,
                )
                self.assertIs(result, tls_sockets[index])
                raw_sockets[index].connect.assert_called_once_with(entry[4])
        self.assertEqual([call.kwargs["server_hostname"]
                          for call in context.wrap_socket.call_args_list], [HOST, HOST])

    def test_imaplib_consumes_existing_tls_socket_without_new_dns(self):
        tls_socket = MagicMock()
        context = MagicMock()
        with patch.object(imaplib.IMAP4, "_connect"), \
             patch("mailsorter.imap_client.socket.create_connection") as connect:
            client = _PreconnectedIMAP4SSL(HOST, 993, tls_socket, context, 4.0)
        self.assertIs(client.sock, tls_socket)
        self.assertEqual(client.host, HOST)
        connect.assert_not_called()

    def test_imap_tools_adapter_preserves_existing_socket(self):
        tls_socket = MagicMock()
        context = MagicMock()
        with patch.object(imaplib.IMAP4, "_connect"), \
             patch("mailsorter.imap_client.socket.create_connection") as connect:
            mailbox = _PreconnectedMailBox(HOST, 993, tls_socket, context, 4.0)
        self.assertIs(mailbox.client.sock, tls_socket)
        self.assertEqual(mailbox.client.host, HOST)
        connect.assert_not_called()

    def test_login_failure_never_tries_second_ip(self):
        tls_socket = MagicMock()
        mailbox = fake_mailbox(MailboxLoginError(("NO", [b"offline auth"]), "OK"))
        with patch("mailsorter.imap_client.socket.getaddrinfo", return_value=ADDRESSES), \
             patch("mailsorter.imap_client._open_tls_socket", return_value=tls_socket) as open_tls, \
             patch("mailsorter.imap_client._PreconnectedMailBox", return_value=mailbox):
            with self.assertRaisesRegex(RuntimeError, "аутентификации"):
                ImapClient(config()).connect()
        self.assertEqual(open_tls.call_count, 1)
        mailbox.login.assert_called_once()

    def test_explicitly_disabled_verification_is_rejected(self):
        with patch("mailsorter.imap_client.socket.getaddrinfo") as dns:
            with self.assertRaisesRegex(RuntimeError, "обязательна"):
                ImapClient(config(ssl_verify=False)).connect()
        dns.assert_not_called()

    def test_dns_attempts_are_capped(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                      (f"192.0.2.{i}", 993)) for i in range(1, 20)]
        with patch("mailsorter.imap_client.socket.getaddrinfo", return_value=addresses), \
             patch("mailsorter.imap_client._open_tls_socket",
                   side_effect=TimeoutError("offline timeout")) as open_tls:
            with self.assertRaisesRegex(RuntimeError, "недоступен"):
                ImapClient(config()).connect()
        self.assertEqual(open_tls.call_count, 8)


if __name__ == "__main__":
    unittest.main()
