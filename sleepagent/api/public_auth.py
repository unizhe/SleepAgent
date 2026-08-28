# 本模块负责唯一 ASGI 服务的接口契约或请求编排，不承载领域状态。
"""Fail-closed service and actor authentication for the public sleep API."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import (
    ec,
    ed25519,
    padding,
    rsa,
    utils,
)
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sleepagent.api.public_contracts import PublicActorRole, PublicErrorCode


UTC = timezone.utc


class SleepApiSecurityError(RuntimeError):
    def __init__(
        self,
        code: PublicErrorCode,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class ServicePrincipal:
    principal_id: str
    credential_id: str
    allowed_actor_issuers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RotatingServiceCredential:
    credential_id: str
    principal_id: str
    secret_sha256: str
    not_before: datetime
    not_after: datetime
    allowed_actor_issuers: frozenset[str] = frozenset()

    @classmethod
    def from_secret(
        cls,
        *,
        credential_id: str,
        principal_id: str,
        secret: str,
        not_before: datetime,
        not_after: datetime,
        allowed_actor_issuers: frozenset[str] = frozenset(),
    ) -> "RotatingServiceCredential":
        if not secret:
            raise ValueError("service credential secret is required")
        return cls(
            credential_id=credential_id,
            principal_id=principal_id,
            secret_sha256=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            not_before=not_before,
            not_after=not_after,
            allowed_actor_issuers=allowed_actor_issuers,
        )


class ServicePrincipalVerifier(Protocol):
    def verify(self, request: Request, *, now: datetime) -> ServicePrincipal: ...


class HttpsBearerServicePrincipalVerifier:
    """Reference verifier for bounded, overlapping Bearer credential rotation."""

    def __init__(
        self,
        credentials: tuple[RotatingServiceCredential, ...],
        *,
        require_https: bool = True,
    ) -> None:
        if not credentials:
            raise ValueError("at least one service credential is required")
        self._credentials = credentials
        self._require_https = require_https

    def verify(self, request: Request, *, now: datetime) -> ServicePrincipal:
        if self._require_https and request.url.scheme.lower() != "https":
            raise SleepApiSecurityError(
                PublicErrorCode.INVALID_SERVICE_CREDENTIAL,
                "HTTPS is required for service authentication.",
                status_code=401,
            )
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token.strip():
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHENTICATION_REQUIRED,
                "A Bearer service credential is required.",
                status_code=401,
            )
        provided_hash = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
        selected: RotatingServiceCredential | None = None
        for candidate in self._credentials:
            if not (candidate.not_before <= now <= candidate.not_after):
                continue
            if secrets.compare_digest(candidate.secret_sha256, provided_hash):
                selected = candidate
        if selected is None:
            raise SleepApiSecurityError(
                PublicErrorCode.INVALID_SERVICE_CREDENTIAL,
                "The service credential is invalid or outside its rotation window.",
                status_code=401,
            )
        return ServicePrincipal(
            principal_id=selected.principal_id,
            credential_id=selected.credential_id,
            allowed_actor_issuers=selected.allowed_actor_issuers,
        )


@dataclass(frozen=True)
class ActorVerificationKey:
    issuer: str
    key_id: str
    algorithm: str
    public_key_pem: bytes
    not_before: datetime
    not_after: datetime

    def __post_init__(self) -> None:
        if self.algorithm not in {"EdDSA", "ES256", "RS256"}:
            raise ValueError("actor key algorithm must be asymmetric")
        if self.not_after <= self.not_before:
            raise ValueError("actor key rotation window is invalid")


class ActorAssertionClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    iss: str = Field(..., min_length=1)
    aud: str = Field(..., min_length=1)
    jti: str = Field(..., min_length=1, max_length=200)
    actor_id: str = Field(..., min_length=1, max_length=200)
    subject_id: str = Field(..., min_length=1, max_length=200)
    role: PublicActorRole
    scope: tuple[str, ...] = Field(..., min_length=1)
    iat: int
    exp: int
    nonce: str = Field(..., min_length=16, max_length=300)
    method: str = Field(..., min_length=1, max_length=20)
    path: str = Field(..., min_length=1, max_length=1000)
    body_sha256: str
    authorization_epoch: int = Field(ge=1)
    privacy_epoch: int = Field(ge=1)
    retrieval_policy_epoch: int = Field(ge=1)

    @field_validator("scope")
    @classmethod
    def scopes_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("scope values must be unique and non-empty")
        return value

    @field_validator("body_sha256")
    @classmethod
    def body_hash_is_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("body_sha256 must be lowercase SHA-256")
        return value


class AssertionReplayStore(Protocol):
    def consume(
        self,
        *,
        issuer: str,
        assertion_id: str,
        nonce: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool: ...


class InMemoryAssertionReplayStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._assertions: dict[tuple[str, str], datetime] = {}
        self._nonces: dict[tuple[str, str], datetime] = {}

    def consume(
        self,
        *,
        issuer: str,
        assertion_id: str,
        nonce: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        with self._lock:
            self._assertions = {
                key: expiry for key, expiry in self._assertions.items() if expiry >= now
            }
            self._nonces = {
                key: expiry for key, expiry in self._nonces.items() if expiry >= now
            }
            assertion_key = (issuer, assertion_id)
            nonce_key = (issuer, nonce)
            if assertion_key in self._assertions or nonce_key in self._nonces:
                return False
            self._assertions[assertion_key] = expires_at
            self._nonces[nonce_key] = expires_at
            return True


@dataclass(frozen=True)
class AuthoritativeRoleBinding:
    authorization_id: str
    actor_id: str
    subject_id: str
    role: PublicActorRole
    scopes: frozenset[str]
    authorization_epoch: int
    active: bool = True
    expires_at: datetime | None = None


class AuthoritativeRoleBindingResolver(Protocol):
    def resolve(
        self,
        *,
        actor_id: str,
        subject_id: str,
        role: PublicActorRole,
        now: datetime,
    ) -> AuthoritativeRoleBinding | None: ...


class InMemoryAuthoritativeRoleBindingResolver:
    """Mutable test/dev authority; every resolve observes the current binding."""

    def __init__(
        self,
        bindings: tuple[AuthoritativeRoleBinding, ...] = (),
    ) -> None:
        self._lock = RLock()
        self._bindings = {
            (binding.actor_id, binding.subject_id, binding.role): binding
            for binding in bindings
        }
        self.resolve_count = 0

    def put(self, binding: AuthoritativeRoleBinding) -> None:
        with self._lock:
            self._bindings[
                (binding.actor_id, binding.subject_id, binding.role)
            ] = binding

    def resolve(
        self,
        *,
        actor_id: str,
        subject_id: str,
        role: PublicActorRole,
        now: datetime,
    ) -> AuthoritativeRoleBinding | None:
        with self._lock:
            self.resolve_count += 1
            binding = self._bindings.get((actor_id, subject_id, role))
            if binding is None or not binding.active:
                return None
            if binding.expires_at is not None and binding.expires_at < now:
                return None
            return binding


class FailClosedRoleBindingResolver:
    def resolve(
        self,
        *,
        actor_id: str,
        subject_id: str,
        role: PublicActorRole,
        now: datetime,
    ) -> AuthoritativeRoleBinding | None:
        raise SleepApiSecurityError(
            PublicErrorCode.AUTHORIZATION_UNAVAILABLE,
            "The authoritative role-binding service is unavailable.",
            status_code=503,
            retryable=True,
        )


class AuthorizationEpochRoleCache:
    """Caches only an already rechecked binding at its exact authority epoch."""

    def __init__(self, *, maximum_entries: int = 4096) -> None:
        self._maximum_entries = maximum_entries
        self._lock = RLock()
        self._entries: dict[
            tuple[str, str, PublicActorRole, int], AuthoritativeRoleBinding
        ] = {}

    def remember(
        self, binding: AuthoritativeRoleBinding
    ) -> AuthoritativeRoleBinding:
        key = (
            binding.actor_id,
            binding.subject_id,
            binding.role,
            binding.authorization_epoch,
        )
        with self._lock:
            stale = [
                item
                for item in self._entries
                if item[:3] == key[:3] and item[3] != key[3]
            ]
            for item in stale:
                self._entries.pop(item, None)
            if len(self._entries) >= self._maximum_entries:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = binding
            return self._entries[key]


@dataclass(frozen=True)
class AuthenticatedActorContext:
    service_principal: ServicePrincipal
    claims: ActorAssertionClaims
    binding: AuthoritativeRoleBinding
    correlation_id: str


@dataclass(frozen=True)
class VerifiedActorIdentity:
    """Request-bound service and actor identity before authority resolution."""

    service_principal: ServicePrincipal
    claims: ActorAssertionClaims
    correlation_id: str


class ActorAssertionVerifier:
    def __init__(
        self,
        keys: tuple[ActorVerificationKey, ...],
        *,
        audience: str,
        replay_store: AssertionReplayStore,
        maximum_lifetime: timedelta = timedelta(minutes=5),
        clock_skew: timedelta = timedelta(seconds=30),
    ) -> None:
        if not keys:
            raise ValueError("at least one actor verification key is required")
        self._keys = {(key.issuer, key.key_id): key for key in keys}
        if len(self._keys) != len(keys):
            raise ValueError("actor issuer/key ids must be unique")
        self._audience = audience
        self._replay_store = replay_store
        self._maximum_lifetime = maximum_lifetime
        self._clock_skew = clock_skew

    def verify(
        self,
        *,
        compact_jws: str,
        service_principal: ServicePrincipal,
        method: str,
        path: str,
        body: bytes,
        now: datetime,
    ) -> ActorAssertionClaims:
        return self._verify(
            compact_jws=compact_jws,
            service_principal=service_principal,
            method=method,
            path=path,
            body=body,
            now=now,
            replay_store=self._replay_store,
        )

    def verify_idempotent_read(
        self,
        *,
        compact_jws: str,
        service_principal: ServicePrincipal,
        method: str,
        path: str,
        body: bytes,
        now: datetime,
    ) -> ActorAssertionClaims:
        """Verify a signed side-effect-free GET without consuming its nonce."""

        if (
            method.upper() != "GET"
            or body
            or not _is_product_report_read_path(path)
        ):
            raise _invalid_assertion(
                "Stateless actor verification is restricted to Product report "
                "empty-body GETs."
            )
        return self._verify(
            compact_jws=compact_jws,
            service_principal=service_principal,
            method=method,
            path=path,
            body=body,
            now=now,
            replay_store=None,
        )

    def _verify(
        self,
        *,
        compact_jws: str,
        service_principal: ServicePrincipal,
        method: str,
        path: str,
        body: bytes,
        now: datetime,
        replay_store: AssertionReplayStore | None,
    ) -> ActorAssertionClaims:
        try:
            encoded_header, encoded_payload, encoded_signature = compact_jws.split(".")
            header = json.loads(_b64url_decode(encoded_header))
            payload = json.loads(_b64url_decode(encoded_payload))
            signature = _b64url_decode(encoded_signature)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise _invalid_assertion("The actor assertion is malformed.") from exc
        if not isinstance(header, dict) or set(header) - {"alg", "kid", "typ"}:
            raise _invalid_assertion("The actor assertion header is invalid.")
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if not isinstance(algorithm, str) or not isinstance(key_id, str):
            raise _invalid_assertion("The actor assertion key metadata is missing.")
        try:
            claims = ActorAssertionClaims.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_assertion("The actor assertion claims are invalid.") from exc
        key = self._keys.get((claims.iss, key_id))
        if key is None or key.algorithm != algorithm:
            raise _invalid_assertion("The actor assertion key is not approved.")
        if service_principal.allowed_actor_issuers and (
            claims.iss not in service_principal.allowed_actor_issuers
        ):
            raise _invalid_assertion(
                "The actor assertion issuer is not allowed for this service."
            )
        if not (key.not_before <= now <= key.not_after):
            raise _invalid_assertion(
                "The actor assertion key is outside its rotation window."
            )
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        self._verify_signature(key, signing_input, signature)
        issued_at = datetime.fromtimestamp(claims.iat, tz=UTC)
        expires_at = datetime.fromtimestamp(claims.exp, tz=UTC)
        if claims.aud != self._audience:
            raise _invalid_assertion("The actor assertion audience is invalid.")
        if issued_at > now + self._clock_skew:
            raise _invalid_assertion("The actor assertion was issued in the future.")
        if expires_at < now - self._clock_skew:
            raise _invalid_assertion("The actor assertion has expired.")
        if expires_at <= issued_at or expires_at - issued_at > self._maximum_lifetime:
            raise _invalid_assertion("The actor assertion lifetime is invalid.")
        if claims.method.upper() != method.upper() or claims.path != path:
            raise _invalid_assertion(
                "The actor assertion is not bound to this method and path."
            )
        body_sha256 = hashlib.sha256(body).hexdigest()
        if not secrets.compare_digest(claims.body_sha256, body_sha256):
            raise _invalid_assertion(
                "The actor assertion is not bound to this request body."
            )
        if replay_store is not None:
            consumed = replay_store.consume(
                issuer=claims.iss,
                assertion_id=claims.jti,
                nonce=claims.nonce,
                expires_at=expires_at + self._clock_skew,
                now=now,
            )
            if not consumed:
                raise SleepApiSecurityError(
                    PublicErrorCode.ACTOR_ASSERTION_REPLAYED,
                    "The actor assertion id or nonce has already been used.",
                    status_code=401,
                )
        return claims

    @staticmethod
    def _verify_signature(
        key: ActorVerificationKey,
        signing_input: bytes,
        signature: bytes,
    ) -> None:
        try:
            public_key = serialization.load_pem_public_key(key.public_key_pem)
            if key.algorithm == "EdDSA":
                if not isinstance(public_key, ed25519.Ed25519PublicKey):
                    raise TypeError("EdDSA key type mismatch")
                public_key.verify(signature, signing_input)
            elif key.algorithm == "ES256":
                if not isinstance(public_key, ec.EllipticCurvePublicKey):
                    raise TypeError("ES256 key type mismatch")
                if len(signature) != 64:
                    raise ValueError("ES256 JWS signatures must be raw r||s")
                signature = utils.encode_dss_signature(
                    int.from_bytes(signature[:32], "big"),
                    int.from_bytes(signature[32:], "big"),
                )
                public_key.verify(
                    signature,
                    signing_input,
                    ec.ECDSA(hashes.SHA256()),
                )
            elif key.algorithm == "RS256":
                if not isinstance(public_key, rsa.RSAPublicKey):
                    raise TypeError("RS256 key type mismatch")
                public_key.verify(
                    signature,
                    signing_input,
                    padding.PKCS1v15(),
                    hashes.SHA256(),
                )
            else:  # pragma: no cover - guarded by ActorVerificationKey.
                raise TypeError("unsupported asymmetric algorithm")
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise _invalid_assertion(
                "The actor assertion signature is invalid."
            ) from exc


class SleepApiAuthenticator:
    def __init__(
        self,
        *,
        service_verifier: ServicePrincipalVerifier,
        actor_verifier: ActorAssertionVerifier,
        role_binding_resolver: AuthoritativeRoleBindingResolver,
        role_cache: AuthorizationEpochRoleCache | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self.service_verifier = service_verifier
        self.actor_verifier = actor_verifier
        self.role_binding_resolver = role_binding_resolver
        self.role_cache = role_cache or AuthorizationEpochRoleCache()
        self.now_factory = now_factory

    def authenticate(
        self,
        request: Request,
        *,
        body: bytes,
        required_scopes: frozenset[str],
        allowed_roles: frozenset[PublicActorRole] | None = None,
    ) -> AuthenticatedActorContext:
        identity = self.verify_identity(
            request,
            body=body,
            required_scopes=required_scopes,
            allowed_roles=allowed_roles,
        )
        return self.authorize_identity(identity, required_scopes=required_scopes)

    def verify_identity(
        self,
        request: Request,
        *,
        body: bytes,
        required_scopes: frozenset[str],
        allowed_roles: frozenset[PublicActorRole] | None = None,
    ) -> VerifiedActorIdentity:
        return self._verify_identity(
            request,
            body=body,
            required_scopes=required_scopes,
            allowed_roles=allowed_roles,
            assertion_verifier=self.actor_verifier.verify,
        )

    def verify_read_identity(
        self,
        request: Request,
        *,
        body: bytes,
        required_scopes: frozenset[str],
        allowed_roles: frozenset[PublicActorRole] | None = None,
    ) -> VerifiedActorIdentity:
        return self._verify_identity(
            request,
            body=body,
            required_scopes=required_scopes,
            allowed_roles=allowed_roles,
            assertion_verifier=self.actor_verifier.verify_idempotent_read,
        )

    def _verify_identity(
        self,
        request: Request,
        *,
        body: bytes,
        required_scopes: frozenset[str],
        allowed_roles: frozenset[PublicActorRole] | None,
        assertion_verifier: Callable[..., ActorAssertionClaims],
    ) -> VerifiedActorIdentity:
        now = self.now_factory()
        service_principal = self.service_verifier.verify(request, now=now)
        assertion = request.headers.get("x-sleep-actor-assertion")
        if not assertion:
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHENTICATION_REQUIRED,
                "An actor assertion is required.",
                status_code=401,
            )
        claims = assertion_verifier(
            compact_jws=assertion,
            service_principal=service_principal,
            method=request.method,
            path=str(request.scope.get("path") or request.url.path),
            body=body,
            now=now,
        )
        if allowed_roles is not None and claims.role not in allowed_roles:
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "The authenticated role cannot access this resource.",
                status_code=403,
            )
        asserted_scopes = frozenset(claims.scope)
        if not required_scopes.issubset(asserted_scopes):
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "The actor assertion does not include the required scope.",
                status_code=403,
            )
        correlation_id = str(
            getattr(request.state, "correlation_id", "") or uuid4()
        )
        request.state.correlation_id = correlation_id
        return VerifiedActorIdentity(
            service_principal=service_principal,
            claims=claims,
            correlation_id=correlation_id[:128],
        )

    def authorize_identity(
        self,
        identity: VerifiedActorIdentity,
        *,
        required_scopes: frozenset[str],
    ) -> AuthenticatedActorContext:
        now = self.now_factory()
        claims = identity.claims
        binding = self.role_binding_resolver.resolve(
            actor_id=claims.actor_id,
            subject_id=claims.subject_id,
            role=claims.role,
            now=now,
        )
        if binding is None:
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "No active authoritative actor/subject role binding exists.",
                status_code=403,
            )
        if not required_scopes.issubset(binding.scopes):
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "The authoritative role binding does not grant the required scope.",
                status_code=403,
            )
        if claims.authorization_epoch != binding.authorization_epoch:
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "The actor assertion authorization epoch is stale.",
                status_code=403,
            )
        binding = self.role_cache.remember(binding)
        return AuthenticatedActorContext(
            service_principal=identity.service_principal,
            claims=claims,
            binding=binding,
            correlation_id=identity.correlation_id,
        )


def _invalid_assertion(message: str) -> SleepApiSecurityError:
    return SleepApiSecurityError(
        PublicErrorCode.INVALID_ACTOR_ASSERTION,
        message,
        status_code=401,
    )


def _is_product_report_read_path(path: str) -> bool:
    root = "/product/sleep/reports"
    if path == root:
        return True
    if not path.startswith(root + "/"):
        return False
    candidate = path[len(root) + 1 :]
    try:
        parsed = datetime.strptime(candidate, "%Y-%m-%d").date()
    except ValueError:
        return False
    return candidate == parsed.isoformat()


def _b64url_decode(value: str) -> bytes:
    if (
        not value
        or "=" in value
        or any(character.isspace() for character in value)
    ):
        raise ValueError("invalid base64url value")
    padding_length = (-len(value)) % 4
    decoded = base64.b64decode(
        value + ("=" * padding_length),
        altchars=b"-_",
        validate=True,
    )
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != value:
        raise ValueError("non-canonical base64url value")
    return decoded


__all__ = [
    "ActorAssertionClaims",
    "ActorAssertionVerifier",
    "ActorVerificationKey",
    "AuthenticatedActorContext",
    "AuthoritativeRoleBinding",
    "AuthoritativeRoleBindingResolver",
    "AuthorizationEpochRoleCache",
    "FailClosedRoleBindingResolver",
    "HttpsBearerServicePrincipalVerifier",
    "InMemoryAssertionReplayStore",
    "InMemoryAuthoritativeRoleBindingResolver",
    "RotatingServiceCredential",
    "ServicePrincipal",
    "ServicePrincipalVerifier",
    "SleepApiAuthenticator",
    "SleepApiSecurityError",
    "VerifiedActorIdentity",
]
