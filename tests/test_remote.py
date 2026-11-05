"""HTTPS on your LAN. Certificates are judged by a real TLS client, and the limits are tested over real sockets, because
a handshake happens below anything a test client reaches."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import ssl
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient
from mcp import Client

from mia import cli, doctor, mcp_server, paths, remote, server, service, tokens
from mia.engine import MiaError

NAME = "my-mac.lan"
ADDRESS = "192.168.1.20"
HANDSHAKE = 0.4
ARRIVAL = 0.4


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIA_HOME", str(tmp_path / "mia"))
    monkeypatch.setenv("MIA_ENGINE", "fake")
    monkeypatch.setattr(server, "HANDSHAKE_SECONDS", HANDSHAKE)
    monkeypatch.setattr(server, "ARRIVAL_SECONDS", ARRIVAL)


def floored(context: ssl.SSLContext) -> ssl.SSLContext:
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def client_context(authority: Path | None = None) -> ssl.SSLContext:
    return floored(ssl.create_default_context(cafile=str(authority or remote.authority_file())))


def judged(client: ssl.SSLContext, certificate: Path, key: Path, host: str) -> None:
    """A real TLS handshake in memory. A client that doesn't accept the certificate raises."""
    answering = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    answering.load_cert_chain(certificate, key)
    to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    asking = client.wrap_bio(to_client, to_server, server_hostname=host)
    answers = answering.wrap_bio(to_server, to_client, server_side=True)
    for _ in range(10):
        for side in (asking, answers):
            with contextlib.suppress(ssl.SSLWantReadError):
                side.do_handshake()
    assert asking.version() == "TLSv1.3"


def forged(tmp_path: Path, name: str | None = None, address: str | None = None) -> tuple[Path, Path]:
    """What someone holding the authority's key could make for a name of their choice."""
    signer = serialization.load_pem_private_key((remote.authority_file().parent / "authority.key").read_bytes(), password=None)
    assert isinstance(signer, ec.EllipticCurvePrivateKey)
    authority = x509.load_pem_x509_certificate(remote.authority_file().read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    alternative: x509.GeneralName = x509.DNSName(name) if name else x509.IPAddress(IPv4Address(address))
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "forged")]))
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([alternative]), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(signer, hashes.SHA256())
    )
    certificate_file, key_file = tmp_path / "forged.pem", tmp_path / "forged.key"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return certificate_file, key_file


def judge_real(host: str) -> None:
    judged(client_context(), remote.certificate_file(), remote.certificate_file().with_name("certificate.key"), host)


# The certificates.


def test_the_certificate_is_accepted_for_its_names_and_addresses() -> None:
    remote.turn_on([NAME, "my-mac.local"], [ADDRESS])
    for host in (NAME, "my-mac.local", ADDRESS):
        judge_real(host)


def test_an_address_alone_works() -> None:
    # A client's name constraints check would fail on an address in the certificate's common name.
    remote.turn_on([], [ADDRESS])
    judge_real(ADDRESS)


@pytest.mark.parametrize("host", ["other.example", "192.168.1.21"])
def test_the_certificate_is_refused_for_anything_else(host: str) -> None:
    remote.turn_on([NAME], [ADDRESS])
    with pytest.raises(ssl.SSLCertVerificationError):
        judge_real(host)


def test_a_client_that_doesnt_trust_the_authority_refuses_it(tmp_path: Path) -> None:
    remote.turn_on([NAME], [])
    stranger = tmp_path / "other.pem"
    stranger.write_bytes(remote.authority_file().read_bytes())
    remote.turn_on([NAME], [], new_authority=True)
    with pytest.raises(ssl.SSLCertVerificationError):
        judged(client_context(stranger), remote.certificate_file(), remote.certificate_file().with_name("certificate.key"), NAME)


@pytest.mark.parametrize("name", ["bank.example", "my-mac.lan.bank.example", "local.bank.example"])
def test_the_authoritys_stolen_key_cant_pose_as_another_sites_name(tmp_path: Path, name: str) -> None:
    remote.turn_on([NAME], [ADDRESS])
    certificate, key = forged(tmp_path, name)
    # The reason matters: a forgery refused for some other fault would prove nothing about the limits on the authority.
    with pytest.raises(ssl.SSLCertVerificationError, match="permitted subtree violation"):
        judged(client_context(), certificate, key, name)


def test_the_authority_limits_names_and_not_addresses() -> None:
    # A limit on addresses makes Claude Code, on BoringSSL, refuse every certificate holding one, which tests here can't
    # see, because OpenSSL accepts it. It was found by connecting a real client. Don't add one without trying that again.
    remote.turn_on([NAME], [ADDRESS])
    authority = x509.load_pem_x509_certificate(remote.authority_file().read_bytes())
    constraints = authority.extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical and constraints.value.excluded_subtrees is None
    assert [type(item) for item in constraints.value.permitted_subtrees or []] == [x509.DNSName, x509.DNSName]


@pytest.mark.parametrize(("name", "address"), [("x.my-mac.local", None), (NAME, None), (None, "192.168.1.30")])
def test_the_authority_covers_what_its_meant_to(tmp_path: Path, name: str | None, address: str | None) -> None:
    remote.turn_on([NAME], [ADDRESS])
    certificate, key = forged(tmp_path, name, address)
    judged(client_context(), certificate, key, name or str(address))


def test_secrets_are_private_from_their_first_moment() -> None:
    remote.turn_on([NAME], [])
    folder = remote.authority_file().parent
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert {stat.S_IMODE(item.stat().st_mode) for item in folder.iterdir()} == {0o600}
    assert sorted(item.name for item in folder.iterdir()) == ["authority.key", "authority.pem", "certificate.key", "certificate.pem", "remote.json"]


def test_a_new_address_or_local_name_keeps_the_authority() -> None:
    remote.turn_on([NAME], [ADDRESS])
    before = remote.fingerprint()
    settings = remote.turn_on([NAME, "my-mac.local"], ["192.168.1.31"])
    assert remote.fingerprint() == before and remote.load() == settings
    judge_real("192.168.1.31")


def test_a_name_the_authority_doesnt_cover_asks_before_replacing_it() -> None:
    remote.turn_on([NAME], [])
    before, saved = remote.fingerprint(), remote.load()
    with pytest.raises(MiaError, match=r"doesn't cover other\.example.*--new-authority"):
        remote.turn_on(["other.example"], [])
    assert remote.fingerprint() == before and remote.load() == saved
    remote.turn_on(["other.example"], [], new_authority=True)
    assert remote.fingerprint() != before
    judge_real("other.example")


def test_a_name_that_only_ends_the_same_isnt_covered() -> None:
    remote.turn_on([NAME], [])
    with pytest.raises(MiaError, match="doesn't cover"):
        remote.turn_on([f"x{NAME}"], [])


@pytest.mark.parametrize(
    ("names", "addresses", "port", "message"),
    [
        ([], [], 8766, "needs a name or address"),
        (["localhost"], [], 8766, "isn't a name"),
        (["my mac.example"], [], 8766, "isn't a name"),
        (["*.local"], [], 8766, "isn't a name"),
        (["192.168.1.20"], [], 8766, "isn't a name"),
        (["evil.localhost"], [], 8766, "isn't a name"),
        ([], ["my-mac.local"], 8766, "isn't an address"),
        ([], ["8.8.8.8"], 8766, "isn't a private address"),
        ([], ["127.0.0.1"], 8766, "isn't a private address"),
        ([], ["0.0.0.0"], 8766, "isn't a private address"),  # noqa: S104
        ([], ["fe80::1"], 8766, "IPv6"),
        ([NAME], [], 80, "isn't a port"),
        ([NAME], [], 70000, "isn't a port"),
    ],
)
def test_bad_settings_are_refused_and_nothing_is_saved(names: list[str], addresses: list[str], port: int, message: str) -> None:
    with pytest.raises(MiaError, match=message):
        remote.turn_on(names, addresses, port)
    assert remote.load() is None


def test_a_name_is_kept_in_lower_case_once() -> None:
    assert remote.turn_on(["My-Mac.Local", "my-mac.local."], []).names == ("my-mac.local",)


def test_turning_off_keeps_the_authority() -> None:
    assert remote.turn_off() is False
    remote.turn_on([NAME], [])
    before = remote.fingerprint()
    assert remote.turn_off() is True
    assert remote.load() is None and remote.state() is None
    remote.turn_on([NAME], [])
    assert remote.fingerprint() == before


@pytest.mark.parametrize(
    "saved",
    [
        '{"names": [1], "addresses": [], "port": 8766}',
        '{"names": "my-mac.lan", "addresses": [], "port": 8766}',
        '{"names": [], "addresses": "192.168.1.20", "port": 8766}',
        '{"names": [], "addresses": ["nonsense"], "port": 8766}',
        '{"names": [], "addresses": [], "port": 70000}',
        '{"names": [], "addresses": [], "port": "8766"}',
        '{"names": []}',
        "[]",
    ],
)
def test_settings_of_the_wrong_shape_are_called_damaged(saved: str) -> None:
    remote.turn_on([NAME], [])
    (remote.authority_file().parent / "remote.json").write_text(saved)
    with pytest.raises(MiaError, match="run mia remote on again"):
        remote.load()


def test_a_port_that_cant_be_says_so() -> None:
    with pytest.raises(MiaError, match="isn't a port"):
        server._bound("127.0.0.1", 70000)


def test_a_damaged_settings_file_says_so(tmp_path: Path) -> None:
    remote.turn_on([NAME], [])
    (remote.authority_file().parent / "remote.json").write_text("{")
    with pytest.raises(MiaError, match="run mia remote on again"):
        remote.load()


# Some are split because the privacy test looks for addresses that could be real, and the boundaries need the real ranges.
@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("127.0.0.1", True),
        ("192.168.1.20", True),
        ("10" + ".1.2.3", True),
        ("172" + ".16.0.1", True),
        ("169.254.1.1", True),
        ("172.32.0.1", False),
        ("8.8.8.8", False),
        ("100" + ".64.0.1", False),
        ("::1", True),
        ("2001:4860:4860::8888", False),
    ],
)
def test_what_counts_as_nearby(address: str, expected: bool) -> None:
    assert remote.nearby(address) is expected


# Renewing.


def serial() -> int:
    return x509.load_pem_x509_certificate(remote.certificate_file().read_bytes()).serial_number


def test_a_certificate_with_time_left_stays() -> None:
    settings = remote.turn_on([NAME], [ADDRESS])
    before = serial()
    assert remote.renew(settings) is False
    assert serial() == before


def test_a_certificate_near_its_end_is_replaced_and_still_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [ADDRESS])
    before, fingerprint = serial(), remote.fingerprint()
    monkeypatch.setattr(remote, "RENEW_DAYS", remote.CERTIFICATE_DAYS + 1)
    assert remote.renew(settings) is True
    assert serial() != before and remote.fingerprint() == fingerprint
    judge_real(NAME)


def test_a_certificate_for_other_names_or_with_no_key_is_replaced() -> None:
    settings = remote.turn_on([NAME], [ADDRESS])
    assert remote.renew(remote.Settings((NAME,), ())) is True
    assert remote.renew(settings) is True
    judge_real(ADDRESS)
    remote.certificate_file().with_name("certificate.key").unlink()
    assert remote.renew(settings) is True
    judge_real(NAME)


def test_a_certificate_never_outlasts_its_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "AUTHORITY_DAYS", 10)
    remote.turn_on([NAME], [])
    ends = x509.load_pem_x509_certificate(remote.certificate_file().read_bytes()).not_valid_after_utc
    assert ends <= x509.load_pem_x509_certificate(remote.authority_file().read_bytes()).not_valid_after_utc


def test_an_authority_that_has_run_out_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [])

    class Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Later:
            return cls.fromtimestamp(datetime.now(tz).timestamp() + (remote.AUTHORITY_DAYS + 1) * 86400, tz)

    monkeypatch.setattr(remote, "datetime", Later)
    with pytest.raises(MiaError, match="--new-authority"):
        remote.renew(settings)


# Who it answers to.


@pytest.fixture
def named() -> Iterator[TestClient]:
    with TestClient(server.create_app(names=(NAME, ADDRESS)), base_url=f"https://{NAME}:8766") as client:
        yield client


@pytest.mark.parametrize("host", [NAME, f"{NAME}:8766", ADDRESS, f"{ADDRESS}:8766", "MY-MAC.LAN", "127.0.0.1:8766"])
def test_https_answers_to_the_chosen_names(named: TestClient, host: str) -> None:
    assert named.get("/v1/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", f"{NAME}.evil.example", f"evil.{NAME}", f"{NAME}:x" + "@evil.example", "my-macxlan", "192.168.1.200", f"{NAME}:123456"])
def test_https_refuses_other_names(named: TestClient, host: str) -> None:
    assert named.get("/v1/health", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("origin", [f"https://{NAME}:8766", f"https://{NAME}", f"HTTPS://{ADDRESS}:8766", "http://127.0.0.1:8765"])
def test_pages_from_the_chosen_names_are_allowed_over_https(named: TestClient, origin: str) -> None:
    assert named.get("/v1/health", headers={"Origin": origin}).status_code == 200


@pytest.mark.parametrize("origin", [f"http://{NAME}:8766", f"https://{NAME}.evil.example", "https://evil.example", "null", f"https://{NAME}/x", f"ftp://{NAME}", f"http://_{NAME}", f"evil://_{NAME}"])
def test_other_pages_are_refused(named: TestClient, origin: str) -> None:
    assert named.get("/v1/health", headers={"Origin": origin}).status_code == 403


def test_the_chosen_names_count_only_over_https() -> None:
    with TestClient(server.create_app(names=(NAME,)), base_url="http://127.0.0.1:8765") as plain:
        assert plain.get("/v1/health").status_code == 200
        assert plain.get("/v1/health", headers={"Host": NAME}).status_code == 403


def test_without_chosen_names_nothing_else_is_allowed_even_over_https() -> None:
    with TestClient(server.create_app(), base_url=f"https://{NAME}:8766") as nothing:
        assert nothing.get("/v1/health").status_code == 403


def test_a_token_is_still_needed_over_https(named: TestClient) -> None:
    assert named.get("/v1/status").status_code == 401
    assert named.get("/v1/status", headers={"Authorization": f"Bearer {tokens.create('laptop')}"}).status_code == 200


# Over real sockets.


@dataclass
class Running:
    port: int
    plain_port: int
    server: server.Server


@pytest.fixture
def running() -> Iterator[Running]:
    settings = remote.turn_on([NAME], [ADDRESS])
    plain, listener = socket.socket(), socket.socket()
    plain.bind(("127.0.0.1", 0))
    listener.bind(("127.0.0.1", 0))
    secure = server.Secure(listener, remote.context(), settings)
    made = server.Server(server._config(server.create_app(names=settings.hosts)), secure)
    thread = threading.Thread(target=made.run, kwargs={"sockets": [plain]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not made._background and time.monotonic() < deadline:
        time.sleep(0.01)
    assert made._background
    try:
        yield Running(int(listener.getsockname()[1]), int(plain.getsockname()[1]), made)
    finally:
        made.should_exit = True
        thread.join(10)
        assert not thread.is_alive(), "something held up stopping the server"
        plain.close()


def connected(port: int) -> socket.socket:
    return socket.create_connection(("127.0.0.1", port), timeout=5)


def secure_socket(port: int, context: ssl.SSLContext | None = None, host: str = NAME) -> ssl.SSLSocket:
    return (context or client_context()).wrap_socket(connected(port), server_hostname=host)


def ended_by_server(held: socket.socket) -> bool:
    try:
        return held.recv(4096) == b""
    except OSError as problem:
        return not isinstance(problem, TimeoutError)


def ask(held: socket.socket, path: str = "/v1/health", host: str = NAME, extra: str = "") -> tuple[int, bytes]:
    held.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{extra}\r\n".encode())
    seen = b""
    while b"\r\n\r\n" not in seen:
        seen += held.recv(4096)
    head, _, body = seen.partition(b"\r\n\r\n")
    length = next(int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length"))
    while len(body) < length:
        body += held.recv(4096)
    return int(head.split()[1]), body


def test_a_trusting_client_gets_answers_over_tls_1_3(running: Running) -> None:
    with secure_socket(running.port) as held:
        assert held.version() == "TLSv1.3"
        assert held.selected_alpn_protocol() is None  # the client didn't ask for one
        assert ask(held)[0] == 200


def test_it_answers_a_token_over_https_and_speaks_http_1_1_to_those_who_ask(running: Running) -> None:
    context = client_context()
    context.set_alpn_protocols(["h2", "http/1.1"])
    with secure_socket(running.port, context) as held:
        assert held.selected_alpn_protocol() == "http/1.1"
        issued = tokens.create("laptop")
        assert ask(held, "/v1/status")[0] == 401
        assert ask(held, "/v1/status", extra=f"Authorization: Bearer {issued}\r\n")[0] == 200


def test_the_wrong_host_gets_403_even_through_tls(running: Running) -> None:
    with secure_socket(running.port) as held:
        assert ask(held, host="evil.example")[0] == 403


def test_plain_http_on_the_https_port_gets_no_answer(running: Running) -> None:
    with connected(running.port) as held:
        held.sendall(f"GET /v1/health HTTP/1.1\r\nHost: {NAME}\r\n\r\n".encode())
        began = time.monotonic()
        assert ended_by_server(held)
        assert time.monotonic() - began < HANDSHAKE * 3


def test_the_plain_port_keeps_listening_on_this_mac_only(running: Running) -> None:
    assert running.server.servers[0].sockets[0].getsockname()[0] == "127.0.0.1"
    with connected(running.plain_port) as held:
        assert ask(held, host="127.0.0.1")[0] == 200
    with connected(running.plain_port) as held:
        assert ask(held, host=NAME)[0] == 403


def test_tls_1_2_is_refused(running: Running) -> None:
    old = client_context()
    old.maximum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises(ssl.SSLError):
        secure_socket(running.port, old)


def test_a_client_that_doesnt_trust_it_ends_the_connection_and_mia_carries_on(running: Running) -> None:
    with pytest.raises(ssl.SSLCertVerificationError):
        secure_socket(running.port, floored(ssl.create_default_context()))
    with secure_socket(running.port) as held:
        assert ask(held)[0] == 200


def test_a_connection_that_never_starts_a_handshake_is_dropped_in_time(running: Running) -> None:
    with connected(running.port) as held:
        began = time.monotonic()
        assert ended_by_server(held)
        assert HANDSHAKE * 0.8 <= time.monotonic() - began < HANDSHAKE * 5
    deadline = time.monotonic() + 5
    while running.server._handshakes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not running.server._handshakes


def test_half_a_handshake_is_dropped_too(running: Running) -> None:
    with connected(running.port) as held:
        held.sendall(b"\x16\x03\x01\x02\x00\x01")  # the start of a real TLS record, which then stops
        assert ended_by_server(held)


def test_a_finished_handshake_still_has_to_send_a_request_in_time(running: Running) -> None:
    with secure_socket(running.port) as held:
        began = time.monotonic()
        assert ended_by_server(held)
        assert time.monotonic() - began >= ARRIVAL * 0.8


@pytest.mark.parametrize("limit", ["MAX_HANDSHAKES", "MAX_HANDSHAKES_PER_ADDRESS"])
def test_handshakes_are_capped_and_a_slot_comes_back(running: Running, monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    monkeypatch.setattr(server, "HANDSHAKE_SECONDS", 30.0)
    monkeypatch.setattr(server, limit, 2)
    first, second = connected(running.port), connected(running.port)
    deadline = time.monotonic() + 5
    while len(running.server._handshakes) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(running.server._handshakes) == 2
    with connected(running.port) as third:
        assert ended_by_server(third)
    assert len(running.server._handshakes) == 2, "the first two should still be waiting"
    first.close()
    second.close()
    deadline = time.monotonic() + 5
    while running.server._handshakes and time.monotonic() < deadline:
        time.sleep(0.01)
    with secure_socket(running.port) as held:
        assert ask(held)[0] == 200


def test_connection_caps_count_https_connections_too(running: Running, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "ARRIVAL_SECONDS", 30.0)
    monkeypatch.setattr(server, "MAX_CONNECTIONS_PER_ADDRESS", 1)
    with secure_socket(running.port) as first:
        assert ask(first)[0] == 200
        with secure_socket(running.port) as second:
            assert ended_by_server(second)


def test_an_address_from_outside_your_network_is_dropped_before_a_handshake(running: Running, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "nearby", lambda _address: False)
    with connected(running.port) as held:
        began = time.monotonic()
        assert ended_by_server(held)
        assert time.monotonic() - began < HANDSHAKE * 0.8
    assert not running.server._handshakes


def test_stopping_doesnt_wait_for_a_handshake(running: Running, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "HANDSHAKE_SECONDS", 30.0)
    held = connected(running.port)
    deadline = time.monotonic() + 5
    while not running.server._handshakes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert running.server._handshakes
    began = time.monotonic()
    running.server.should_exit = True
    assert ended_by_server(held)
    assert time.monotonic() - began < 5
    held.close()


def test_stopping_cancels_handshakes_before_it_waits_for_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    # One that finished during the wait would be a new connection that nothing had asked to close.
    settings = remote.turn_on([NAME], [])
    seen: list[int] = []

    async def main() -> None:
        made = server.Server(server._config(server.create_app()), server.Secure(socket.socket(), remote.context(), settings))
        waiting = asyncio.create_task(asyncio.sleep(30))
        made._handshakes[waiting] = "192.168.1.30"

        async def rest_of_stopping(_self: uvicorn.Server, sockets: Any = None) -> None:
            seen.append(waiting.cancelling())

        monkeypatch.setattr(uvicorn.Server, "shutdown", rest_of_stopping)
        await made.shutdown()
        assert made.secure is not None and made.secure.listener.fileno() == -1

    asyncio.run(main())
    assert seen == [1]


def test_a_renewed_certificate_is_used_without_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [])
    made = remote.context()
    monkeypatch.setattr(server, "RENEW_CHECK_SECONDS", 0.01)

    monkeypatch.setattr(remote, "RENEW_DAYS", remote.CERTIFICATE_DAYS + 1)
    reloaded: list[ssl.SSLContext] = []
    real_reload = remote.reload

    async def main() -> None:
        done = asyncio.Event()

        def reload(context: ssl.SSLContext) -> None:
            real_reload(context)
            reloaded.append(context)
            done.set()

        monkeypatch.setattr(remote, "reload", reload)
        secure = server.Secure(socket.socket(), made, settings)
        task = asyncio.create_task(server.Server(server._config(server.create_app()), secure)._renew(secure))
        await asyncio.wait_for(done.wait(), 10)
        task.cancel()
        secure.listener.close()

    before = serial()
    asyncio.run(main())
    assert serial() != before and reloaded[0] is made


def run_renewals(monkeypatch: pytest.MonkeyPatch, secure: server.Secure, then: Any, until: Any) -> None:
    """Runs the renewal loop quickly, with `then` happening once it has started, until `until` holds."""
    monkeypatch.setattr(server, "RENEW_CHECK_SECONDS", 0.01)

    async def main() -> None:
        task = asyncio.create_task(server.Server(server._config(server.create_app()), secure)._renew(secure))
        await asyncio.sleep(0.02)
        then()
        for _ in range(500):
            if until():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        secure.listener.close()

    asyncio.run(main())
    assert until()


def test_a_certificate_replaced_by_mia_remote_on_is_loaded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [])
    made = remote.context()
    reloaded: list[ssl.SSLContext] = []
    real = remote.reload

    def counted(context: ssl.SSLContext) -> None:
        real(context)
        reloaded.append(context)

    checks: list[int] = []
    real_renew = remote.renew

    def renewing(chosen: remote.Settings) -> bool:
        checks.append(1)
        return real_renew(chosen)

    monkeypatch.setattr(remote, "reload", counted)
    monkeypatch.setattr(remote, "renew", renewing)

    def replace_it() -> None:
        remote.turn_on([NAME], [])
        checks.clear()

    run_renewals(monkeypatch, server.Secure(socket.socket(), made, settings), replace_it, lambda: len(checks) >= 5)
    assert reloaded == [made]  # once: what's loaded is now what's in the file


def test_a_reload_that_fails_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [])
    made = remote.context()
    tries: list[int] = []

    def failing_once(_context: ssl.SSLContext) -> None:
        tries.append(1)
        if len(tries) == 1:
            raise ssl.SSLError("not this time")

    monkeypatch.setattr(remote, "reload", failing_once)
    run_renewals(monkeypatch, server.Secure(socket.socket(), made, settings), lambda: remote.turn_on([NAME], []), lambda: len(tries) >= 2)


def test_a_name_added_while_running_isnt_taken_away_by_a_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [])
    made = remote.context()
    checks: list[int] = []
    real = remote.renew

    def counted(chosen: remote.Settings) -> bool:
        checks.append(1)
        return real(chosen)

    monkeypatch.setattr(remote, "renew", counted)
    run_renewals(monkeypatch, server.Secure(socket.socket(), made, settings), lambda: remote.turn_on([NAME, "my-mac.local"], []), lambda: len(checks) >= 5)
    judge_real("my-mac.local")


def test_an_unexpected_error_doesnt_end_renewing(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [])
    tries: list[int] = []

    def strange(_settings: remote.Settings) -> bool:
        tries.append(1)
        raise TypeError("an encrypted key, say")

    monkeypatch.setattr(remote, "renew", strange)
    run_renewals(monkeypatch, server.Secure(socket.socket(), remote.context(), settings), lambda: None, lambda: len(tries) >= 2)


def test_turning_it_off_stops_renewing(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [])
    calls: list[int] = []

    def renewing(_settings: remote.Settings) -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(remote, "renew", renewing)
    made = remote.context()
    remote.turn_off()
    ticks: list[int] = []

    def off() -> None:
        ticks.append(1)

    monkeypatch.setattr(remote, "load", off)
    run_renewals(monkeypatch, server.Secure(socket.socket(), made, settings), lambda: None, lambda: len(ticks) >= 3)
    assert calls == []


def test_an_error_taking_a_connection_doesnt_end_accepting(running: Running, monkeypatch: pytest.MonkeyPatch) -> None:
    real = remote.nearby
    faults = [ValueError("a strange address")]

    def faulty(address: str) -> bool:
        if faults:
            raise faults.pop()
        return real(address)

    monkeypatch.setattr(remote, "nearby", faulty)
    with connected(running.port) as first:
        assert ended_by_server(first)
    with secure_socket(running.port) as held:
        assert ask(held)[0] == 200


def test_a_connection_with_no_address_is_closed_and_accepting_carries_on(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [])

    async def main() -> None:
        made = server.Server(server._config(server.create_app()), None)
        one, other = socket.socketpair()
        made._admit(one, None, server.Secure(socket.socket(), remote.context(), settings))
        assert one.fileno() == -1 and not made._handshakes
        other.close()

    asyncio.run(main())


def test_an_aborted_connection_doesnt_make_accepting_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [])

    async def main() -> float:
        secure = server.Secure(socket.socket(), remote.context(), settings)
        made = server.Server(server._config(server.create_app()), secure)
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        aborted = [ConnectionAbortedError() for _ in range(5)]

        async def accepting(_listener: socket.socket) -> tuple[socket.socket, Any]:
            if aborted:
                raise aborted.pop()
            finished.set()
            await asyncio.sleep(30)
            raise AssertionError

        monkeypatch.setattr(loop, "sock_accept", accepting)
        began = time.monotonic()
        task = asyncio.create_task(made._accept(secure))
        await asyncio.wait_for(finished.wait(), 10)
        took = time.monotonic() - began
        task.cancel()
        secure.listener.close()
        return took

    assert asyncio.run(main()) < 1


def stalled_tls(port: int, requests: int = 3000) -> socket.socket:
    context = client_context()
    raw = socket.socket()
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    raw.connect(("127.0.0.1", port))
    raw.settimeout(10)
    held = context.wrap_socket(raw, server_hostname=NAME)
    with contextlib.suppress(OSError, TimeoutError):
        held.sendall(f"GET /openapi.json HTTP/1.1\r\nHost: {NAME}\r\n\r\n".encode() * requests)
    return held


def test_requests_that_are_never_read_dont_hold_an_https_connection_for_good(running: Running, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "ARRIVAL_SECONDS", 30.0)
    monkeypatch.setattr(server, "WRITE_STALL_SECONDS", 0.5)
    held = stalled_tls(running.port)
    deadline = time.monotonic() + 10
    while running.server.server_state.connections and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not running.server.server_state.connections, "the unread connection kept its slot"
    held.close()


def test_connections_get_the_smaller_send_buffer_so_a_slow_reader_doesnt_look_stalled(running: Running, monkeypatch: pytest.MonkeyPatch) -> None:
    # Linux doubles what it's given, and neither system starts with this size by itself.
    monkeypatch.setattr(server, "SEND_BUFFER_BYTES", 40_000)
    for port, wrap in ((running.port, secure_socket), (running.plain_port, connected)):
        with wrap(port) as held:
            assert ask(held, host=NAME if wrap is secure_socket else "127.0.0.1")[0] == 200
            connection = next(iter(running.server.server_state.connections))
            assert 40_000 <= connection.transport.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) <= 80_000


def test_a_renewal_that_fails_is_logged_and_tried_again(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    settings = remote.turn_on([NAME], [])
    monkeypatch.setattr(server, "RENEW_CHECK_SECONDS", 0.01)
    tries: list[int] = []

    async def main() -> None:
        again = asyncio.Event()
        loop = asyncio.get_running_loop()

        def failing(_settings: remote.Settings) -> bool:
            tries.append(1)
            if len(tries) == 2:
                loop.call_soon_threadsafe(again.set)
            raise MiaError("nope")

        monkeypatch.setattr(remote, "renew", failing)
        secure = server.Secure(socket.socket(), remote.context(), settings)
        task = asyncio.create_task(server.Server(server._config(server.create_app()), secure)._renew(secure))
        await asyncio.wait_for(again.wait(), 10)
        assert not task.done()
        task.cancel()
        secure.listener.close()

    asyncio.run(main())
    assert "couldn't renew" in capsys.readouterr().err


# Starting it.


def test_with_remote_access_off_nothing_listens_beyond_this_mac(capsys: pytest.CaptureFixture[str]) -> None:
    assert server._secure(8765) is None
    assert capsys.readouterr().err == ""


def test_https_cant_share_the_plain_port_and_this_mac_keeps_its_api(capsys: pytest.CaptureFixture[str]) -> None:
    remote.turn_on([NAME], [], port=8765)
    assert server._secure(8765) is None
    assert "can't share port 8765" in capsys.readouterr().err


def test_a_taken_https_port_says_so_and_this_mac_keeps_its_api(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(server, "ANYWHERE", "127.0.0.1")
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        remote.turn_on([NAME], [], port=int(taken.getsockname()[1]))
        assert server._secure(8765) is None
    assert "already in use" in capsys.readouterr().err


def free_https_port(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(server, "ANYWHERE", "127.0.0.1")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def damage(name: str, data: bytes = b"damaged") -> None:
    (remote.authority_file().parent / name).write_bytes(data)


@pytest.mark.parametrize(
    "broken",
    [
        {"authority.pem": b"damaged", "certificate.pem": b"damaged"},
        {"authority.key": b"damaged", "certificate.pem": b"damaged"},
        {"remote.json": b"{"},
        {"remote.json": b'{"names": [1], "addresses": [], "port": 8766}'},
        {"remote.json": b'{"names": "my-mac.lan", "addresses": [], "port": 8766}'},
        {"remote.json": b'{"names": [], "addresses": ["nonsense"], "port": 8766}'},
        {"remote.json": b'{"names": [], "addresses": [], "port": 70000}'},
    ],
)
def test_damaged_files_leave_this_macs_api_running_and_say_why(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], broken: dict[str, bytes]) -> None:
    remote.turn_on([NAME], [], port=free_https_port(monkeypatch))
    for name, data in broken.items():
        damage(name, data)
    capsys.readouterr()
    assert server._secure(8765) is None
    assert "HTTPS didn't start" in capsys.readouterr().err


@pytest.mark.parametrize("broken", ["certificate.key", "certificate.pem"])
def test_a_damaged_certificate_is_simply_made_again(monkeypatch: pytest.MonkeyPatch, broken: str) -> None:
    remote.turn_on([NAME], [], port=free_https_port(monkeypatch))
    damage(broken)
    secure = server._secure(8765)
    assert secure is not None
    secure.listener.close()
    judge_real(NAME)


def test_a_key_from_another_certificate_is_noticed_and_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = remote.turn_on([NAME], [])
    key = remote.certificate_file().with_name("certificate.key")
    other = key.read_bytes()
    remote.turn_on([NAME], [])
    key.write_bytes(other)  # what a crash between writing the two files leaves
    assert remote.renew(settings) is True
    judge_real(NAME)


@pytest.mark.parametrize("broken", ["authority.pem", "authority.key"])
def test_a_damaged_authority_says_what_to_do_and_new_authority_repairs_it(capsys: pytest.CaptureFixture[str], broken: str) -> None:
    remote.turn_on([NAME], [])
    damage(broken)
    assert cli.main(["remote", "on", "--name", NAME]) == 1
    assert "--new-authority" in capsys.readouterr().err
    assert cli.main(["remote", "on", "--name", NAME, "--new-authority"]) == 0
    judge_real(NAME)


def test_doctor_says_when_remote_access_is_on_but_not_listening() -> None:
    settings = remote.Settings((NAME,), ())
    later = datetime.now(UTC) + timedelta(days=9)
    finding = remote_finding(remote.State(settings, later, listening=False))
    assert finding.status == doctor.WARN and "not listening" in finding.detail


def test_running_on_again_keeps_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.main(["remote", "on", "--name", NAME, "--port", "18999"]) == 0
    assert cli.main(["remote", "on", "--name", NAME]) == 0
    saved = remote.load()
    assert saved is not None and saved.port == 18999
    assert cli.main(["remote", "on", "--name", NAME, "--port", "19000"]) == 0
    saved = remote.load()
    assert saved is not None and saved.port == 19000


def test_on_refuses_the_port_the_service_uses(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(service, "installed_port", lambda: 18999)
    assert cli.main(["remote", "on", "--name", NAME, "--port", "18999"]) == 1
    assert "already uses port 18999" in capsys.readouterr().err
    assert remote.load() is None


def test_starting_up_replaces_a_missing_certificate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "ANYWHERE", "127.0.0.1")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    remote.turn_on([NAME], [], port=port)
    remote.certificate_file().unlink()
    secure = server._secure(8765)
    assert secure is not None
    secure.listener.close()
    assert remote.certificate_file().exists()


# The command.


def test_on_uses_the_macs_own_name_and_address_when_given_none(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(remote, "suggested_names", lambda: ["my-mac.local"])
    monkeypatch.setattr(remote, "suggested_addresses", lambda: [ADDRESS])
    assert cli.main(["remote", "on"]) == 0
    printed = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert printed[1:3] == ["https://my-mac.local:8766", f"https://{ADDRESS}:8766"]
    assert remote.fingerprint() in printed


@pytest.mark.parametrize(("arguments", "hosts"), [(["--name", "my-mac.lan"], ("my-mac.lan",)), (["--address", ADDRESS], (ADDRESS,))])
def test_on_uses_only_what_its_given(monkeypatch: pytest.MonkeyPatch, arguments: list[str], hosts: tuple[str, ...]) -> None:
    monkeypatch.setattr(remote, "suggested_names", lambda: ["my-mac.local"])
    monkeypatch.setattr(remote, "suggested_addresses", lambda: ["192.168.1.31"])
    assert cli.main(["remote", "on", *arguments]) == 0
    saved = remote.load()
    assert saved is not None and saved.hosts == hosts


def test_on_with_nothing_to_go_on_says_what_to_add(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(remote, "suggested_names", list)
    monkeypatch.setattr(remote, "suggested_addresses", list)
    assert cli.main(["remote", "on"]) == 1
    assert "--name" in capsys.readouterr().err


def test_status_says_off_and_on(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["remote"]) == 0
    assert capsys.readouterr().out.startswith("off")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
        remote.turn_on([NAME], [], port=port)
        capsys.readouterr()
        assert cli.main(["remote", "status"]) == 0
        printed = capsys.readouterr().out.splitlines()
        assert printed[0] == f"on, at https://{NAME}:{port}" and printed[1].startswith("not listening") and remote.fingerprint() in printed[-1]
        probe.listen()
        assert cli.main(["remote", "status"]) == 0
        assert "listening now" in capsys.readouterr().out


def test_authority_prints_the_certificate_and_never_a_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["remote", "authority"]) == 1
    assert "mia remote on" in capsys.readouterr().err
    remote.turn_on([NAME], [])
    capsys.readouterr()
    assert cli.main(["remote", "authority"]) == 0
    printed = capsys.readouterr().out
    assert printed == remote.authority_file().read_text()
    assert printed.startswith("-----BEGIN CERTIFICATE-----") and "PRIVATE" not in printed


def test_off_says_when_it_takes_effect(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["remote", "off"]) == 0
    assert "already off" in capsys.readouterr().out
    remote.turn_on([NAME], [])
    assert cli.main(["remote", "off"]) == 0
    assert "next time mia serve starts" in capsys.readouterr().out


def test_suggested_name_comes_from_the_mac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "scutil"
    fake.write_text('#!/bin/sh\n[ "$1 $2" = "--get LocalHostName" ] && echo My-Mac\n')
    fake.chmod(0o755)
    monkeypatch.setattr(remote, "SCUTIL", str(fake))
    assert remote.suggested_names() == ["my-mac.local"]
    monkeypatch.setattr(remote, "SCUTIL", str(tmp_path / "missing"))
    assert remote.suggested_names() == []


@pytest.mark.parametrize(("found", "expected"), [(ADDRESS, [ADDRESS]), ("8.8.8.8", []), ("127.0.0.1", [])])
def test_a_suggested_address_has_to_be_a_private_one(monkeypatch: pytest.MonkeyPatch, found: str, expected: list[str]) -> None:
    class Probe:
        def __init__(self, *_arguments: Any) -> None:
            pass

        def __enter__(self) -> Probe:
            return self

        def __exit__(self, *_arguments: Any) -> None:
            pass

        def connect(self, address: tuple[str, int]) -> None:
            assert address[0] == "192.0.2.1"  # it never reaches anything: UDP sends nothing until asked

        def getsockname(self) -> tuple[str, int]:
            return found, 1

    monkeypatch.setattr(socket, "socket", Probe)
    assert remote.suggested_addresses() == expected


# What doctor says.


FACTS = doctor.Facts(system="Darwin", machine="arm64", python=(3, 13), memory_gb=32.0, folder=Path("/nowhere/mia"), disk_free_gb=100.0, engines={}, models=0)


def remote_finding(state: remote.State | None) -> doctor.Finding:
    return next(item for item in doctor.findings(FACTS.__class__(**{**FACTS.__dict__, "remote": state})) if item.name == "Remote access")


def test_doctor_describes_remote_access() -> None:
    settings = remote.Settings((NAME,), (ADDRESS,))
    later, earlier = datetime.now(UTC) + timedelta(days=9), datetime.now(UTC) - timedelta(days=1)
    assert remote_finding(None).status == doctor.INFO
    on = remote_finding(remote.State(settings, later))
    assert on.status == doctor.PASS and f"https://{NAME}:8766" in on.detail
    assert remote_finding(remote.State(settings, earlier)).status == doctor.WARN
    assert remote_finding(remote.State(settings, None)).status == doctor.WARN


def test_doctor_changes_nothing_and_survives_damage() -> None:
    assert doctor._remote_state() is None
    assert not remote.authority_file().parent.exists()
    remote.turn_on([NAME], [])
    (remote.authority_file().parent / "remote.json").write_text("{")
    state = doctor._remote_state()
    assert state is not None and state.certificate_ends is None


# An assistant's bridge, over real TLS. localhost stands in for a name, since tests stay on this machine: mia itself
# refuses to choose it, so these make its certificate directly.


def localhost_certificate() -> remote.Settings:
    settings = remote.Settings(("localhost",), ())
    paths.make_private(paths.home_folder())
    paths.make_private(remote.authority_file().parent)
    remote._make_authority(settings.names)
    remote._issue(settings)
    return settings


@pytest.fixture
def localhost_port() -> Iterator[int]:
    settings = localhost_certificate()
    plain, listener = socket.socket(), socket.socket()
    plain.bind(("127.0.0.1", 0))
    listener.bind(("127.0.0.1", 0))
    made = server.Server(server._config(server.create_app(names=settings.hosts)), server.Secure(listener, remote.context(), settings))
    thread = threading.Thread(target=made.run, kwargs={"sockets": [plain]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not made._background and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        yield int(listener.getsockname()[1])
    finally:
        made.should_exit = True
        thread.join(10)
        plain.close()


def status_through_bridge(port: int, authority: str | None) -> tuple[bool, str]:
    async def main() -> tuple[bool, str]:
        bridge = mcp_server.bridge(f"https://localhost:{port}", tokens.create(f"bridge-{time.monotonic_ns()}"), authority)
        async with Client(bridge) as client:
            result = await client.call_tool("mia_status", {})
        text = result.content[0].text  # type: ignore[union-attr]
        return result.is_error, text

    return asyncio.run(main())


def test_the_bridge_reaches_mia_over_https_trusting_only_the_authority(localhost_port: int) -> None:
    failed, text = status_through_bridge(localhost_port, str(remote.authority_file()))
    assert not failed
    assert "jobs" in json.loads(text)


def test_the_bridge_refuses_a_server_it_doesnt_trust(localhost_port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    failed, text = status_through_bridge(localhost_port, None)
    assert failed and "can't be reached" in text


def test_a_bad_authority_file_stops_the_bridge_with_a_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("MIA_TOKEN", "mia_x")
    assert cli.main(["mcp", "--url", "https://my-mac.lan:8766", "--ca", str(tmp_path / "missing.pem")]) == 1
    assert "missing.pem" in capsys.readouterr().err
