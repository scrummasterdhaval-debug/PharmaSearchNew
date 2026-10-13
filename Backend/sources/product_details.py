from repository import save_product_detail


def save_product_details(
    substance,
    product,
    company,
    country,
    status,
    product_url="",
    atc_code="",
    registration_date="",
    smpc_url="",
    assessment_report_url=""
):
    return save_product_detail(
        {
            "substance": substance,
            "product": product,
            "company": company,
            "country": country,
            "status": status,
            "product_url": product_url,
            "atc_code": atc_code,
            "registration_date": registration_date,
            "smpc_url": smpc_url,
            "pil_url": assessment_report_url,
            "source_url": product_url,
        }
    )
