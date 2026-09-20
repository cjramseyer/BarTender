import base64
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_app_module(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    import importlib

    import bartender.app as app_module

    return importlib.reload(app_module)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@pytest.mark.parametrize("key_format", ("raw", "der", "pem"))
def test_load_license_public_key_accepts_supported_formats(tmp_path, key_format):
    app_module = _load_app_module(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    if key_format == "raw":
        value = _base64url(
            public_key.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )
    elif key_format == "der":
        value = base64.b64encode(
            public_key.public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii")
    else:
        value = public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    loaded_key = app_module._load_license_public_key(value)

    message = b"license"
    loaded_key.verify(private_key.sign(message), message)


def test_load_license_public_key_rejects_invalid_value(tmp_path):
    app_module = _load_app_module(tmp_path)

    with pytest.raises(ValueError, match="License verification key is invalid"):
        app_module._load_license_public_key("not-a-public-key")