from __future__ import annotations

from sleepagent.product_runtime.schemas import ContextPacket, EvidenceLedger


class GroundingCitationError(ValueError):
    pass


def grounded_citation_refs(
    context: ContextPacket,
    ledger: EvidenceLedger,
    *,
    role: str | None = None,
) -> list[str]:
    ledger_refs = [
        ref
        for ref in list(ledger.canonical_evidence_refs) + list(ledger.raw_evidence_refs)
        if not ref.startswith("seed:")
    ]
    ledger_refs.extend(ref for claim in ledger.claims for ref in claim.evidence_refs)
    seed_refs = _seed_refs_for_role(context, role)
    invalid_seed = [ref for ref in seed_refs if not ref.startswith("seed:")]
    if invalid_seed:
        raise GroundingCitationError(
            "RAG citations must use reviewed seed citation IDs: "
            + ", ".join(invalid_seed)
        )
    refs = list(dict.fromkeys([*ledger_refs, *seed_refs]))
    if not refs:
        raise GroundingCitationError(
            "Report and Chat output require Evidence Ledger or reviewed seed citations."
        )
    return refs


def _seed_refs_for_role(context: ContextPacket, role: str | None) -> list[str]:
    if role is None:
        return list(context.rag_context.citation_ids)
    allowed: list[str] = []
    for source in context.rag_context.source_metadata:
        citation_id = source.get("citation_id")
        roles = source.get("applicable_roles", [])
        if citation_id and role in roles:
            allowed.append(str(citation_id))
    return allowed


__all__ = ["GroundingCitationError", "grounded_citation_refs"]
