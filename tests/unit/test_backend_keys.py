from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sleepagent.backend_keys import BackendKeyError, BackendKeyProvider
from sleepagent.backend_settings import DeploymentMode


pytestmark = pytest.mark.unit


def test_test_key_material_is_reproducible_and_non_secret_manifest_safe() -> None:
    provider = BackendKeyProvider(DeploymentMode.TEST, environment={})

    first = provider.actor_key("test:actor-v1", key_id="primary")
    second = provider.actor_key("test:actor-v1", key_id="primary")

    assert first.private_key is not None
    assert first.public_key_pem == second.public_key_pem
    assert len(first.public_key_sha256) == 64
    assert provider.secret(
        "test:service-v1",
        purpose="service credential",
        minimum_bytes=32,
    ) == provider.secret(
        "test:service-v1",
        purpose="service credential",
        minimum_bytes=32,
    )
    assert len(provider.encryption_key("test:encryption-v1")) == 32


def test_production_rejects_deterministic_test_key_references() -> None:
    provider = BackendKeyProvider(DeploymentMode.PRODUCTION, environment={})

    with pytest.raises(BackendKeyError, match="forbidden in production"):
        provider.actor_key("test:actor", key_id="primary")
    with pytest.raises(BackendKeyError, match="forbidden in production"):
        provider.secret(
            "test:service",
            purpose="service credential",
            minimum_bytes=16,
        )


def test_api_verification_loader_never_derives_or_returns_a_private_key(
    tmp_path,
) -> None:
    provider = BackendKeyProvider(DeploymentMode.TEST, environment={})
    with pytest.raises(BackendKeyError, match="must not derive private key"):
        provider.actor_verification_key("test:actor", key_id="primary")

    public_path = tmp_path / "actor-public.pem"
    public_path.write_bytes(
        Ed25519PrivateKey.generate().public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    material = provider.actor_verification_key(
        f"file:{public_path}",
        key_id="primary",
    )

    assert material.private_key is None
    assert material.public_key_pem == public_path.read_bytes()


def test_environment_and_file_references_fail_closed(tmp_path) -> None:
    encoded_key = base64.b64encode(b"k" * 32).decode("ascii")
    provider = BackendKeyProvider(
        DeploymentMode.PRODUCTION,
        environment={"BACKEND_ENCRYPTION_KEY": encoded_key},
    )

    assert provider.encryption_key("env:BACKEND_ENCRYPTION_KEY") == b"k" * 32
    with pytest.raises(BackendKeyError, match="unavailable"):
        provider.secret(
            "env:MISSING",
            purpose="service credential",
            minimum_bytes=16,
        )

    key_file = tmp_path / "key"
    key_file.write_bytes(b"x" * 32)
    assert provider.secret(
        f"file:{key_file}",
        purpose="service credential",
        minimum_bytes=32,
    ) == b"x" * 32
    link = tmp_path / "link"
    link.symlink_to(key_file)
    with pytest.raises(BackendKeyError, match="not a symlink"):
        provider.secret(
            f"file:{link}",
            purpose="service credential",
            minimum_bytes=32,
        )
