from typing import Any

from sources.connectors.base import (
    FunctionSourceConnector,
    SourceConnector,
    SourceMetadata,
)
from sources.belgium_famhp import run_belgium_famhp_search
from sources.ema import run_ema_search
from sources.eu_mri import run_eu_mri_search
from sources.fda_ndc import run_fda_ndc_search
from sources.france_bdpm import run_france_bdpm_search
from sources.health_canada_dpd import run_health_canada_dpd_search
from sources.ireland_medicines_ie import run_ireland_medicines_search
from sources.medsafe import run_medsafe_search
from sources.mhra import run_mhra_search
from sources.cdsco_india import run_cdsco_india_search
from sources.regional_live import (
    run_bpom_indonesia_search,
    run_dav_vietnam_search,
    run_cyprus_pharmaceutical_services_search,
    run_fda_orange_book_search,
    run_fda_purple_book_search,
    run_fda_ghana_search,
    run_fda_philippines_search,
    run_hong_kong_drug_office_search,
    run_israel_drug_registry_search,
    run_mfds_south_korea_search,
    run_npra_malaysia_search,
    run_pmda_japan_search,
    run_sahpra_search,
    run_thai_fda_search,
)
from sources.open_registers import run_aifa_italy_search, run_anvisa_brazil_search
from sources.romania_anmdmr import run_romania_anmdmr_search
from sources.lebanon_moph import run_lebanon_moph_search
from sources.eof_greece import run_eof_greece_search
from sources.germany_bfarm import run_germany_bfarm_search
from sources.japan_nhi import run_mhlw_japan_search
from sources.fda_drugsfda import run_drugs_at_fda_search
from sources.taiwan_fda import run_tfda_taiwan_search
from sources.grls_russia import run_grls_register_search
from sources.open_data_registers import (
    run_hpra_ireland_search,
    run_hsa_singapore_search,
    run_ukraine_drlz_search,
)
from sources.national_registers import (
    run_cbg_netherlands_search,
    run_dkma_denmark_search,
    run_sukl_czech_search,
    run_urpl_poland_search,
)
from sources.americas_registers import run_invima_colombia_search
from sources.asia_registers import run_ndda_kazakhstan_search
from sources.europe_registers import (
    run_alims_serbia_search,
    run_bda_bulgaria_search,
    run_dmp_norway_search,
    run_malta_medicines_search,
    run_sukl_slovakia_search,
    run_titck_turkey_search,
    run_zva_latvia_search,
)
from sources.sfda_saudi import run_sfda_saudi_search
from sources.spain_cima import run_spain_cima_search
from sources.swissmedic import run_swissmedic_search
from sources.tga import run_tga_search
from sources.pbs_australia import run_pbs_australia_search
from sources.nmpa_china import run_nmpa_china_search


CONNECTORS: list[SourceConnector] = [
    FunctionSourceConnector(
        SourceMetadata(
            name="FDA",
            region="US",
            countries=("United States",),
            supports_documents=True,
        ),
        # The NDC directory, searched locally: every listing, not the first 100 labels.
        run_fda_ndc_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="TFDA Taiwan",
            region="AS",
            countries=("Taiwan",),
            supports_documents=False,
        ),
        # TFDA's licence register, with English names from the trade register.
        run_tfda_taiwan_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="HPRA Ireland",
            region="EU",
            countries=("Ireland",),
            supports_documents=False,
        ),
        # HPRA's daily list of every authorised human medicine.
        run_hpra_ireland_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="CBG Netherlands",
            region="EU",
            countries=("Netherlands",),
            supports_documents=True,
        ),
        # CBG's Medicines Information Bank, published whole each week.
        run_cbg_netherlands_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="URPL Poland",
            region="EU",
            countries=("Poland",),
            supports_documents=True,
        ),
        # The Register of Medicinal Products, published whole each day.
        run_urpl_poland_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="SUKL Czech Republic",
            region="EU",
            countries=("Czech Republic",),
            supports_documents=False,
        ),
        # SUKL's medicinal products database (DLP), published monthly.
        run_sukl_czech_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="DKMA Denmark",
            region="EU",
            countries=("Denmark",),
            supports_documents=False,
        ),
        # DKMA's daily list of authorised medicines.
        run_dkma_denmark_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="DMP Norway",
            region="EU",
            countries=("Norway",),
            supports_documents=True,
        ),
        # DMP's FEST prescribing catalogue, published every two weeks.
        run_dmp_norway_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="SUKL Slovakia",
            region="EU",
            countries=("Slovakia",),
            supports_documents=False,
        ),
        # SUKL's daily list of medicines with a valid registration.
        run_sukl_slovakia_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="ZVA Latvia",
            region="EU",
            countries=("Latvia",),
            supports_documents=True,
        ),
        # ZVA's Medicines Register, exported daily.
        run_zva_latvia_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="TITCK Turkey",
            region="ME",
            countries=("Turkey",),
            supports_documents=False,
        ),
        # TITCK's weekly list of licensed human medicinal products.
        run_titck_turkey_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="ALIMS Serbia",
            region="EU",
            countries=("Serbia",),
            supports_documents=False,
        ),
        # ALIMS's register of medicines for human use, published daily.
        run_alims_serbia_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Malta Medicines Authority",
            region="EU",
            countries=("Malta",),
            supports_documents=False,
        ),
        # The Medicines Authority's list of authorised medicines.
        run_malta_medicines_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="BDA Bulgaria",
            region="EU",
            countries=("Bulgaria",),
            supports_documents=False,
        ),
        # BDA's register of medicinal products authorised for use, monthly.
        run_bda_bulgaria_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="INVIMA Colombia",
            region="LA",
            countries=("Colombia",),
            supports_documents=False,
        ),
        # INVIMA's current marketing authorisations (CUM), on datos.gov.co.
        run_invima_colombia_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="NDDA Kazakhstan",
            region="AS",
            countries=("Kazakhstan",),
            supports_documents=False,
        ),
        # NDDA's State Register of Medicinal Products, from its public JSON service.
        run_ndda_kazakhstan_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Drugs@FDA",
            region="US",
            countries=("United States",),
            supports_documents=False,
        ),
        # Every US application and its holder, discontinued ones included.
        run_drugs_at_fda_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="FDA Orange Book",
            region="US",
            countries=("United States",),
            supports_documents=False,
        ),
        run_fda_orange_book_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="FDA Purple Book",
            region="US",
            countries=("United States",),
            supports_documents=False,
        ),
        run_fda_purple_book_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="MHRA",
            region="UK",
            countries=("United Kingdom",),
            supports_documents=True,
        ),
        run_mhra_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="EMA",
            region="EU",
            countries=(
                "Austria",
                "Belgium",
                "Bulgaria",
                "Croatia",
                "Cyprus",
                "Czech Republic",
                "Denmark",
                "Estonia",
                "Finland",
                "France",
                "Germany",
                "Greece",
                "Hungary",
                "Ireland",
                "Italy",
                "Latvia",
                "Lithuania",
                "Luxembourg",
                "Malta",
                "Netherlands",
                "Poland",
                "Portugal",
                "Romania",
                "Slovakia",
                "Slovenia",
                "Spain",
                "Sweden",
            ),
            supports_documents=True,
        ),
        run_ema_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="EU MRI Product Index",
            region="EU",
            countries=(
                "Austria",
                "Belgium",
                "Bulgaria",
                "Croatia",
                "Cyprus",
                "Czech Republic",
                "Denmark",
                "Estonia",
                "Finland",
                "France",
                "Germany",
                "Greece",
                "Hungary",
                "Ireland",
                "Italy",
                "Latvia",
                "Lithuania",
                "Luxembourg",
                "Malta",
                "Netherlands",
                "Poland",
                "Portugal",
                "Romania",
                "Slovakia",
                "Slovenia",
                "Spain",
                "Sweden",
            ),
            supports_documents=True,
        ),
        run_eu_mri_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Health Canada",
            region="CA",
            countries=("Canada",),
            supports_documents=False,
        ),
        # The DPD, fetched whole and searched locally: every product, not the first 50.
        run_health_canada_dpd_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="TGA Australia",
            region="AU",
            countries=("Australia",),
            supports_documents=True,
        ),
        run_tga_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="PBS Australia",
            region="AU",
            countries=("Australia",),
            supports_documents=False,
        ),
        # PBS Schedule, monthly CSV tables: subsidised brands with their sponsor.
        run_pbs_australia_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Medsafe New Zealand",
            region="NZ",
            countries=("New Zealand",),
            supports_documents=True,
        ),
        run_medsafe_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="SAHPRA South Africa",
            region="AF",
            countries=("South Africa",),
            supports_documents=False,
        ),
        run_sahpra_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="FDA Ghana",
            region="AF",
            countries=("Ghana",),
            supports_documents=False,
        ),
        run_fda_ghana_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="MoPH Lebanon",
            region="ME",
            countries=("Lebanon",),
            supports_documents=False,
        ),
        run_lebanon_moph_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="SFDA Saudi Arabia",
            region="ME",
            countries=("Saudi Arabia",),
            supports_documents=False,
        ),
        run_sfda_saudi_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Israel Drug Registry",
            region="ME",
            countries=("Israel",),
            supports_documents=False,
        ),
        run_israel_drug_registry_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="CDSCO India",
            region="AS",
            countries=("India",),
            supports_documents=False,
        ),
        run_cdsco_india_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="NMPA China",
            region="AS",
            countries=("China",),
            supports_documents=False,
        ),
        # NMPA's quarterly National Drug Code list; its database blocks scripts.
        run_nmpa_china_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="BPOM Indonesia",
            region="AS",
            countries=("Indonesia",),
            supports_documents=False,
        ),
        run_bpom_indonesia_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="NPRA Malaysia",
            region="AS",
            countries=("Malaysia",),
            supports_documents=False,
        ),
        run_npra_malaysia_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="FDA Philippines",
            region="AS",
            countries=("Philippines",),
            supports_documents=False,
        ),
        run_fda_philippines_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="HSA Singapore",
            region="AS",
            countries=("Singapore",),
            supports_documents=False,
        ),
        run_hsa_singapore_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="MFDS South Korea",
            region="AS",
            countries=("South Korea",),
            supports_documents=False,
        ),
        run_mfds_south_korea_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Thai FDA",
            region="AS",
            countries=("Thailand",),
            supports_documents=False,
        ),
        run_thai_fda_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="DAV Vietnam",
            region="AS",
            countries=("Vietnam",),
            supports_documents=False,
        ),
        run_dav_vietnam_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Swissmedic",
            region="CH",
            countries=("Switzerland",),
            supports_documents=False,
            enabled=False,
        ),
        run_swissmedic_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="MHLW Japan",
            region="JP",
            countries=("Japan",),
            supports_documents=False,
        ),
        # The Ministry's NHI drug price list, downloaded whole and searched
        # locally. PMDA's own search turns away scripted clients.
        run_mhlw_japan_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="EOF Greece",
            region="EU",
            countries=("Greece",),
            supports_documents=True,
        ),
        run_eof_greece_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="BfArM Germany",
            region="EU",
            countries=("Germany",),
            supports_documents=False,
            # BfArM holds its public database to 200 sessions at a time and
            # says its searches are slow; one question a second is plenty.
            rate_limit_per_minute=60,
        ),
        run_germany_bfarm_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="PMDA Japan",
            region="JP",
            countries=("Japan",),
            supports_documents=False,
        ),
        run_pmda_japan_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Hong Kong Drug Office",
            region="AS",
            countries=("Hong Kong",),
            supports_documents=False,
        ),
        run_hong_kong_drug_office_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Belgium FAMHP",
            region="EU",
            countries=("Belgium",),
            supports_documents=True,
        ),
        run_belgium_famhp_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="France BDPM",
            region="EU",
            countries=("France",),
            supports_documents=False,
        ),
        run_france_bdpm_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Ireland medicines.ie",
            region="EU",
            countries=("Ireland",),
            supports_documents=True,
        ),
        run_ireland_medicines_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Spain CIMA",
            region="EU",
            countries=("Spain",),
            supports_documents=True,
        ),
        run_spain_cima_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="ANMDMR Romania",
            region="EU",
            countries=("Romania",),
            supports_documents=True,
        ),
        run_romania_anmdmr_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="AIFA Italy",
            region="EU",
            countries=("Italy",),
            supports_documents=False,
        ),
        run_aifa_italy_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="ANVISA Brazil",
            region="BR",
            countries=("Brazil",),
            supports_documents=False,
        ),
        run_anvisa_brazil_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Cyprus Pharmaceutical Services",
            region="EU",
            countries=("Cyprus",),
            supports_documents=False,
        ),
        run_cyprus_pharmaceutical_services_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="Ukraine DRLZ",
            region="EU",
            countries=("Ukraine",),
            supports_documents=False,
        ),
        run_ukraine_drlz_search,
    ),
    FunctionSourceConnector(
        SourceMetadata(
            name="GRLS Russia",
            region="RU",
            countries=("Russia",),
            supports_documents=False,
        ),
        # The whole register, downloaded daily from GRLS's own export link.
        run_grls_register_search,
    ),
]


def enabled_connectors() -> list[SourceConnector]:
    return [connector for connector in CONNECTORS if connector.metadata.enabled]


def connector_metadata() -> list[dict[str, Any]]:
    return [connector.metadata.to_dict() for connector in CONNECTORS]


def connector_by_name(name: str) -> SourceConnector | None:
    normalized = name.strip().lower()
    for connector in CONNECTORS:
        if connector.metadata.name.lower() == normalized:
            return connector
    return None


# Backward-compatible registry consumed by the current search engine.
SOURCES = [
    {"name": connector.metadata.name, "function": connector.search}
    for connector in enabled_connectors()
]
