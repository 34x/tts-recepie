#!/bin/sh
# Launches supervisord in the foreground.
# supervisord manages piper, supertonic, and the UI server (see /etc/supervisord.conf).
# The UI is gated behind wait_health.py so it only binds after backends are ready.
set -e

# ── HTTPS certificate (self-signed) ─────────────────────────────────────
# Generate once; kept on the persistent volume (/root/.cache mount) so the
# same cert survives container rebuilds/restarts. Non-fatal on failure:
# the UI server falls back to plain HTTP when cert/key are unreadable.
CERT_DIR="/root/.cache/certs"
CERT_FILE="$CERT_DIR/tts-ui.crt"
KEY_FILE="$CERT_DIR/tts-ui.key"

mkdir -p "$CERT_DIR" 2>/dev/null || true
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    python3 - <<'EOF' && echo "entrypoint: self-signed certificate generated ($CERT_FILE)" \
        || echo "entrypoint: WARNING: cert generation failed — UI will serve plain HTTP" >&2
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import datetime, ipaddress

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tts-ui")])
cert = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(subject)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
    .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
    .add_extension(
        x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]),
        critical=False,
    )
    .sign(key, hashes.SHA256())
)
with open("/root/.cache/certs/tts-ui.crt", "wb") as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM))
with open("/root/.cache/certs/tts-ui.key", "wb") as f:
    f.write(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
EOF
fi
chmod 644 "$CERT_FILE" "$KEY_FILE" 2>/dev/null || true

exec supervisord -n -c /etc/supervisord.conf