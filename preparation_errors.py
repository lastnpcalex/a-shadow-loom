"""Narrow preparation-failure taxonomy.

Only documented recoverable preparation failures may be converted to a
retrieval fallback by the continuity engine. Everything else — cleanup,
teardown, configuration/programming errors, invariant violations, database
corruption — must surface and fail the turn visibly.
"""
from __future__ import annotations


class PreparationCapacityError(Exception):
    """The destination cannot admit a required preparation request; bounded."""


class PreparationTransportError(Exception):
    """The provider transport failed (launch, protocol, stream, handshake)."""


class PreparationValidationError(Exception):
    """Provider output failed schema/attestation/fidelity validation."""


class PreparationInvalidOutput(PreparationValidationError):
    """Provider output failed structured-state validation."""


class PreparationAttestationError(PreparationValidationError):
    """The provider's model attestation was missing, unverified or mismatched."""


class PreparationTimeout(Exception):
    """A bounded preparation call exceeded its time limit."""


class PreparationCleanupError(Exception):
    """The isolated preparation working directory could not be removed."""


class PreparationConfigurationError(Exception):
    """Programming/configuration/invariant failure; never recoverable."""
