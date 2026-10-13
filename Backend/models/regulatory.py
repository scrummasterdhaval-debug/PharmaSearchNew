from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RegulatoryDocument:
    document_type: str = ""
    title: str = ""
    url: str = ""
    effective_date: str = ""
    language: str = ""
    source_url: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "document_type": self.document_type,
            "title": self.title,
            "url": self.url,
            "effective_date": self.effective_date,
            "language": self.language,
            "source_url": self.source_url,
        }


@dataclass(slots=True)
class RegulatoryProduct:
    source: str
    active_substance: str
    product_name: str
    country: str
    region: str = ""
    authorisation_scope: str = ""
    marketing_authorisation_holder: str = ""
    applicant_sponsor: str = ""
    manufacturer_name: str = ""
    manufacturer_country: str = ""
    manufacturer_address: str = ""
    manufacturer_role: str = ""
    batch_release_manufacturer: str = ""
    strength: str = ""
    dosage_form: str = ""
    route: str = ""
    pack_size: str = ""
    status: str = ""
    registration_number: str = ""
    registration_date: str = ""
    expiry_date: str = ""
    product_url: str = ""
    source_url: str = ""
    smpc_url: str = ""
    pil_url: str = ""
    assessment_report_url: str = ""
    label_url: str = ""
    document_type: str = ""
    last_checked: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    manufacturers: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_source_record(self) -> dict[str, Any]:
        """Return the legacy source record shape used by search/export today."""
        return {
            "source": self.source,
            "substance": self.active_substance,
            "product": self.product_name,
            "company": self.marketing_authorisation_holder,
            "applicant_sponsor": self.applicant_sponsor,
            "country": self.country,
            "region": self.region,
            "authorisation_scope": self.authorisation_scope,
            "status": self.status,
            "strength": self.strength,
            "dosage_form": self.dosage_form,
            "route": self.route,
            "pack_size": self.pack_size,
            "registration_number": self.registration_number,
            "registration_date": self.registration_date,
            "expiry_date": self.expiry_date,
            "manufacturer_name": self.manufacturer_name,
            "manufacturer_country": self.manufacturer_country,
            "manufacturer_address": self.manufacturer_address,
            "manufacturer_role": self.manufacturer_role,
            "batch_release_manufacturer": self.batch_release_manufacturer,
            "manufacturers": self.manufacturers,
            "documents": self.documents,
            "evidence": self.evidence,
            "smpc_url": self.smpc_url,
            "pil_url": self.pil_url,
            "assessment_report_url": self.assessment_report_url,
            "label_url": self.label_url,
            "document_type": self.document_type,
            "source_url": self.source_url or self.product_url,
            "product_url": self.product_url,
            "url": self.product_url or self.source_url,
            "last_checked": self.last_checked,
        }
