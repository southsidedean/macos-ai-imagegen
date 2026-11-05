"""HTTPS on your LAN: a certificate authority mia makes once, which each device trusts, and the certificate it gives
mia serve.

The authority can vouch only for names on your own network, so someone who stole its key couldn't use it to pose as
another site's name to a device that trusts it. Nothing here runs unless you turn it on with mia remote on.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import ssl
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mia import paths
from mia.engine import MiaError

DEFAULT_PORT = 8766
AUTHORITY_DAYS = 3650
# Apple's devices refuse a longer one, even from an authority you added yourself.
CERTIFICATE_DAYS = 397
RENEW_DAYS = 30
SCUTIL = "/usr/sbin/scutil"
LOCAL_SUFFIX = "local"
NAME = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")


@dataclass(frozen=True)
class Settings:
    names: tuple[str, ...]
    addresses: tuple[str, ...]
    port: int = DEFAULT_PORT

    @property
    def hosts(self) -> tuple[str, ...]:
        return (*self.names, *self.addresses)

    @property
    def urls(self) -> list[str]:
        return [f"https://{host}:{self.port}" for host in self.hosts]


@dataclass(frozen=True)
class State:
    settings: Settings
    certificate_ends: datetime | None  # None if it hasn't been made, or can't be read
    listening: bool = True


def _folder() -> Path:
    return paths.home_folder() / "remote"


def _settings_file() -> Path:
    return _folder() / "remote.json"


def authority_file() -> Path:
    return _folder() / "authority.pem"


def _authority_key() -> Path:
    return _folder() / "authority.key"


def certificate_file() -> Path:
    return _folder() / "certificate.pem"


def _certificate_key() -> Path:
    return _folder() / "certificate.key"


def _write_private(path: Path, data: bytes) -> None:
    """Only you can read it, from its first moment, and a crash can't leave half a file."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
    temporary.replace(path)


def nearby(address: str) -> bool:
    """Whether a connection comes from this Mac or your own network. A port forwarded on a router would show a public address."""
    return ip_address(address).is_private


def _check_name(name: str) -> str:
    name = name.lower().rstrip(".")
    if not NAME.fullmatch(name) or name.endswith(".localhost") or name.split(".")[-1].isdigit():
        raise MiaError(f"{name} isn't a name mia can use: it should look like my-mac.local")
    return name


def _check_address(address: str) -> str:
    try:
        parsed = ip_address(address)
    except ValueError:
        raise MiaError(f"{address} isn't an address: it should look like 192.168.1.20") from None
    if not isinstance(parsed, IPv4Address):
        raise MiaError(f"{address} is an IPv6 address, and mia listens only on IPv4 for now")
    if parsed.is_loopback or parsed.is_unspecified or not parsed.is_private:
        raise MiaError(f"{address} isn't a private address on your own network, so mia won't listen for it")
    return address


def suggested_names() -> list[str]:
    """The Mac's Bonjour name, the one you see in System Settings under Sharing."""
    try:
        found = subprocess.run([SCUTIL, "--get", "LocalHostName"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    return [f"{found.lower()}.{LOCAL_SUFFIX}"] if found else []


def suggested_addresses() -> list[str]:
    """The address this Mac uses on its network. Connecting a UDP socket picks a route and sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            found = str(probe.getsockname()[0])
        return [_check_address(found)]
    except (OSError, MiaError):
        return []


def load() -> Settings | None:
    """The settings, or None if remote access is off."""
    try:
        saved = json.loads(_settings_file().read_text())
        names, addresses, port = saved["names"], saved["addresses"], saved["port"]
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names) or not isinstance(addresses, list):
            raise TypeError
        for address in addresses:
            ip_address(address)
        if not isinstance(port, int) or not 0 < port <= 65535:
            raise ValueError
        return Settings(tuple(names), tuple(addresses), port)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as problem:
        raise MiaError(f"mia can't read {_settings_file()}: run mia remote on again ({type(problem).__name__})") from None


def _new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _key_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _make_authority(names: tuple[str, ...]) -> None:
    """Covers .local and the other names you gave. A name it doesn't cover needs a new authority.

    Addresses can't be limited the same way. Clients built on BoringSSL, which include Claude Code, refuse a certificate
    holding an address if the authority limits addresses at all, whichever name they connected to. So the authority
    could vouch for any address, which matters far less than names: sites are reached by name.
    """
    others = [name for name in names if not name.endswith(f".{LOCAL_SUFFIX}")]
    permitted: list[x509.GeneralName] = [x509.DNSName(name) for name in (LOCAL_SUFFIX, *others)]
    key = _new_key()
    # The random part tells one Mac's authority from another's in a list of trusted certificates.
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"mia certificate authority {secrets.token_hex(3)}")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=AUTHORITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
        .add_extension(_usage(key_cert_sign=True, crl_sign=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private(_authority_key(), _key_bytes(key))
    _write_private(authority_file(), certificate.public_bytes(serialization.Encoding.PEM))


def _usage(digital_signature: bool = False, key_cert_sign: bool = False, crl_sign: bool = False) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=digital_signature,
        key_cert_sign=key_cert_sign,
        crl_sign=crl_sign,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        encipher_only=False,
        decipher_only=False,
    )


def _read_certificate(path: Path) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except ValueError:
        raise MiaError(f"{path} is damaged. If it's the authority, mia remote on --new-authority replaces it, and every device has to trust the new one") from None


def _uncovered(names: tuple[str, ...]) -> list[str]:
    """The names the authority can't vouch for."""
    try:
        constraints = _read_certificate(authority_file()).extensions.get_extension_for_class(x509.NameConstraints).value
    except x509.ExtensionNotFound:
        raise MiaError("mia's certificate authority has no limits, so mia didn't make it: run mia remote on --new-authority") from None
    allowed = [item.value for item in constraints.permitted_subtrees or [] if isinstance(item, x509.DNSName)]
    return [name for name in names if not any(name == root or name.endswith(f".{root}") for root in allowed)]


def _issue(settings: Settings) -> None:
    authority = _read_certificate(authority_file())
    try:
        signer = serialization.load_pem_private_key(_authority_key().read_bytes(), password=None)
    except (ValueError, TypeError):
        raise MiaError("mia's certificate authority has a damaged key: run mia remote on --new-authority, and trust the new one on each device") from None
    if not isinstance(signer, ec.EllipticCurvePrivateKey):
        raise MiaError("mia's certificate authority has a kind of key mia didn't make: run mia remote on --new-authority")
    now = datetime.now(UTC)
    if authority.not_valid_after_utc <= now:
        raise MiaError("mia's certificate authority has run out: run mia remote on --new-authority, and trust the new one on each device")
    key = _new_key()
    alternatives: list[x509.GeneralName] = [x509.DNSName(name) for name in settings.names]
    alternatives += [x509.IPAddress(IPv4Address(address)) for address in settings.addresses]
    certificate = (
        x509.CertificateBuilder()
        # Clients read the names below. A name here that looks like a host would be checked as one, and an address fails that.
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mia")]))
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(min(now + timedelta(days=CERTIFICATE_DAYS), authority.not_valid_after_utc))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=True)
        .add_extension(_usage(digital_signature=True), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
        .sign(signer, hashes.SHA256())
    )
    _write_private(_certificate_key(), _key_bytes(key))
    _write_private(certificate_file(), certificate.public_bytes(serialization.Encoding.PEM))


def turn_on(names: list[str], addresses: list[str], port: int = DEFAULT_PORT, new_authority: bool = False) -> Settings:
    """Saves the settings and makes what's missing. mia serve starts listening the next time it starts."""
    settings = Settings(
        tuple(dict.fromkeys(_check_name(name) for name in names)),
        tuple(dict.fromkeys(_check_address(address) for address in addresses)),
        port,
    )
    if not settings.hosts:
        raise MiaError("mia needs a name or address to answer to: add --name my-mac.local or --address 192.168.1.20")
    if not 1024 <= port <= 65535:
        raise MiaError(f"{port} isn't a port mia can use: choose one from 1024 to 65535")
    paths.make_private(paths.home_folder())
    paths.make_private(_folder())
    if new_authority or not authority_file().exists():
        _make_authority(settings.names)
    uncovered = _uncovered(settings.names)
    if uncovered:
        raise MiaError(
            f"mia's certificate authority doesn't cover {', '.join(uncovered)}. Add --new-authority to replace it, and every device will have to trust the new one"
        )
    _issue(settings)
    _write_private(_settings_file(), json.dumps(asdict(settings)).encode())
    return settings


def turn_off() -> bool:
    """Keeps the authority, so turning it on again doesn't mean trusting a new one on every device."""
    try:
        _settings_file().unlink()
    except FileNotFoundError:
        return False
    return True


def renew(settings: Settings) -> bool:
    """Makes a new certificate if it's near its end, missing, or for other names. Devices trust the authority, so they notice nothing."""
    try:
        certificate = _read_certificate(certificate_file())
        covers = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        current = {*covers.get_values_for_type(x509.DNSName), *(str(item) for item in covers.get_values_for_type(x509.IPAddress))}
        due = certificate.not_valid_after_utc - datetime.now(UTC) < timedelta(days=RENEW_DAYS)
        # Loading them also catches a key that doesn't match, from a crash between writing the two files.
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certificate_file(), _certificate_key())
    except (MiaError, OSError, ValueError, x509.ExtensionNotFound):
        current, due = set(), True
    if not due and current == set(settings.hosts):
        return False
    _issue(settings)
    return True


def context() -> ssl.SSLContext:
    """TLS 1.3 only: every current client has it, and it leaves out renegotiation and the weak ciphers."""
    made = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    made.minimum_version = ssl.TLSVersion.TLSv1_3
    made.set_alpn_protocols(["http/1.1"])  # the only HTTP version mia's server speaks
    reload(made)
    return made


def reload(made: ssl.SSLContext) -> None:
    """Only new connections see the change, and open ones carry on."""
    made.load_cert_chain(certificate_file(), _certificate_key())


def certificate_serial() -> int | None:
    try:
        return _read_certificate(certificate_file()).serial_number
    except (MiaError, OSError):
        return None


def saved_port() -> int:
    """The port to keep when mia remote on is run again without one."""
    try:
        settings = load()
    except MiaError:
        return DEFAULT_PORT
    return settings.port if settings else DEFAULT_PORT


def listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except OSError:
        return False
    return True


def fingerprint() -> str:
    """What to compare on the other device, to know the file you copied is the one this Mac made."""
    return _read_certificate(authority_file()).fingerprint(hashes.SHA256()).hex(":").upper()


def state() -> State | None:
    """Only reads. None if remote access is off."""
    settings = load()
    if settings is None:
        return None
    is_listening = listening(settings.port)
    try:
        return State(settings, _read_certificate(certificate_file()).not_valid_after_utc, is_listening)
    except (MiaError, OSError):
        return State(settings, None, is_listening)
