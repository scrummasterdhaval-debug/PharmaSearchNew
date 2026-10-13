"""Regulatory PDF Parser - Extract manufacturer data from PDFs

Parses:
- EMA Product Information PDFs
- FDA Orange Book & Purple Book PDFs
- National regulatory documents
- Clinical trial protocols
- WHO prequalified medicines lists
"""

import logging
import re
import requests
from typing import Optional, List, Dict
from datetime import datetime
import io

logger = logging.getLogger(__name__)

class RegulatoryPDFParser:
    """Extract manufacturer information from regulatory PDFs."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        self.timeout = 20

    # ==================== EMA PRODUCT INFORMATION ====================

    def parse_ema_product_pdf(self, product_name: str) -> Optional[Dict]:
        """Download and parse EMA product information PDF for manufacturer info."""
        try:
            # Search EMA for product
            search_url = "https://www.ema.europa.eu/en/medicines"
            params = {'q': product_name, 'status': 'Authorised'}

            resp = self.session.get(search_url, params=params, timeout=self.timeout)

            # Look for product information link
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.content, 'html.parser')

            # Find product page link
            product_link = soup.find('a', class_='medicine-name')
            if not product_link:
                return None

            product_url = product_link.get('href', '')
            if not product_url.startswith('http'):
                product_url = 'https://www.ema.europa.eu' + product_url

            # Get product page
            product_resp = self.session.get(product_url, timeout=self.timeout)
            product_soup = BeautifulSoup(product_resp.content, 'html.parser')

            # Find Assessment Report or Product Information PDF
            pdf_links = product_soup.find_all('a', href=re.compile(r'\.pdf$', re.IGNORECASE))

            for pdf_link in pdf_links:
                pdf_url = pdf_link.get('href', '')
                if not pdf_url.startswith('http'):
                    pdf_url = 'https://www.ema.europa.eu' + pdf_url

                # Download PDF
                pdf_resp = self.session.get(pdf_url, timeout=self.timeout)
                if pdf_resp.status_code == 200:
                    # Parse PDF text (simple approach - look for keywords)
                    text = pdf_resp.text

                    # Extract marketing authorization holder
                    patterns = [
                        r'Marketing Authorisation Holder[:\s]+([A-Za-z0-9\s&.,\-()]+)',
                        r'MAH[:\s]+([A-Za-z0-9\s&.,\-()]+)',
                        r'Applicant[:\s]+([A-Za-z0-9\s&.,\-()]+)',
                        r'Manufacturer[:\s]+([A-Za-z0-9\s&.,\-()]+)',
                    ]

                    for pattern in patterns:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            manufacturer = match.group(1).strip()
                            if len(manufacturer) > 3:  # Reasonable name length
                                return {
                                    'manufacturer': manufacturer,
                                    'source': 'EMA PDF',
                                    'confidence': 0.90,
                                    'pdf_url': pdf_url,
                                    'method': 'pdf_parse',
                                }
        except Exception as e:
            logger.debug(f"EMA PDF parse error: {e}")

        return None

    # ==================== FDA ORANGE BOOK & PURPLE BOOK ====================

    def parse_fda_orange_book(self, product_name: str, active_ingredient: str = "") -> Optional[Dict]:
        """Extract manufacturer from FDA Orange Book (approved drugs)."""
        try:
            # FDA Orange Book is available as downloadable file
            url = "https://www.fda.gov/media/88305/download"  # Orange Book PDF URL

            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                return None

            # Search for product in text
            text = resp.text if hasattr(resp, 'text') else str(resp.content)

            # Pattern: Product Name | Applicant
            pattern = rf'{re.escape(product_name)}[^\n]*\|[^\n]*([A-Za-z0-9\s&.,\-()]+)'
            match = re.search(pattern, text, re.IGNORECASE)

            if match:
                return {
                    'manufacturer': match.group(1).strip(),
                    'source': 'FDA Orange Book',
                    'confidence': 0.88,
                    'pdf_url': url,
                    'method': 'fda_orange_book',
                }
        except Exception as e:
            logger.debug(f"FDA Orange Book error: {e}")

        return None

    def parse_fda_purple_book(self, product_name: str) -> Optional[Dict]:
        """Extract manufacturer from FDA Purple Book (biotech/biologics)."""
        try:
            url = "https://www.fda.gov/media/88302/download"  # Purple Book PDF

            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                return None

            text = resp.text if hasattr(resp, 'text') else str(resp.content)

            if product_name.lower() in text.lower():
                # Extract surrounding context
                idx = text.lower().find(product_name.lower())
                context = text[max(0, idx-200):min(len(text), idx+200)]

                # Look for manufacturer pattern
                mfg_pattern = r'(?:manufacturer|applicant)[:\s]+([A-Za-z0-9\s&.,\-()]+)'
                match = re.search(mfg_pattern, context, re.IGNORECASE)

                if match:
                    return {
                        'manufacturer': match.group(1).strip(),
                        'source': 'FDA Purple Book',
                        'confidence': 0.85,
                        'pdf_url': url,
                        'method': 'fda_purple_book',
                    }
        except Exception as e:
            logger.debug(f"FDA Purple Book error: {e}")

        return None

    # ==================== WHO PREQUALIFIED MEDICINES ====================

    def parse_who_prequalified_list(self, product_name: str) -> Optional[Dict]:
        """Download and parse WHO Prequalified Medicines list."""
        try:
            # WHO maintains prequalified medicines lists as PDFs/Excel
            url = "https://www.who.int/teams/prequalification-medicines"

            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                return None

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.content, 'html.parser')

            # Find list link
            list_links = soup.find_all('a', href=re.compile(r'prequalified.*list', re.IGNORECASE))

            if list_links:
                list_url = list_links[0].get('href', '')
                if not list_url.startswith('http'):
                    list_url = 'https://www.who.int' + list_url

                list_resp = self.session.get(list_url, timeout=self.timeout)
                text = list_resp.text if hasattr(list_resp, 'text') else str(list_resp.content)

                if product_name.lower() in text.lower():
                    return {
                        'manufacturer': 'WHO Prequalified',
                        'source': 'WHO Prequalified Medicines',
                        'confidence': 0.80,
                        'pdf_url': list_url,
                        'method': 'who_list',
                    }
        except Exception as e:
            logger.debug(f"WHO prequalified error: {e}")

        return None

    # ==================== NATIONAL REGULATORY PDFs ====================

    def parse_national_formulary(self, product_name: str, country: str) -> Optional[Dict]:
        """Parse national drug formularies for manufacturer info."""

        formulary_sources = {
            'UK': 'https://bnf.nice.org.uk',
            'DE': 'https://www.bfarm.de',
            'FR': 'https://ansm.sante.fr',
            'IT': 'https://www.aifa.gov.it',
            'ES': 'https://www.aemps.gob.es',
            'NL': 'https://www.cbg-meb.nl',
            'BE': 'https://www.afmps.be',
            'CH': 'https://www.swissmedic.ch',
            'AU': 'https://www.tga.gov.au',
            'CA': 'https://www.canada.ca/en/health-canada',
            'JP': 'https://www.pmda.go.jp',
            'KR': 'https://www.mfds.go.kr',
            'CN': 'https://www.nmpa.gov.cn',
            'IN': 'https://www.cdsco.gov.in',
            'BR': 'https://www.anvisa.gov.br',
        }

        if country not in formulary_sources:
            return None

        try:
            url = formulary_sources[country]

            resp = self.session.get(url, params={'q': product_name}, timeout=self.timeout)
            if resp.status_code == 200 and product_name.lower() in resp.text.lower():

                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.content, 'html.parser')

                # Look for manufacturer/MAH in page
                mfg_patterns = [
                    r'(?:Manufacturer|MAH|Applicant)[:\s]+([A-Za-z0-9\s&.,\-()]+)',
                ]

                for pattern in mfg_patterns:
                    match = re.search(pattern, resp.text, re.IGNORECASE)
                    if match:
                        return {
                            'manufacturer': match.group(1).strip(),
                            'source': f'{country} National Formulary',
                            'confidence': 0.82,
                            'url': url,
                            'method': 'national_formulary',
                        }
        except Exception as e:
            logger.debug(f"National formulary error for {country}: {e}")

        return None

    # ==================== BATCH PDF PROCESSING ====================

    def extract_from_all_regulatory_pdfs(self, product_name: str, country: str = "") -> Dict:
        """Try all regulatory PDF sources for manufacturer data."""

        results = []

        sources = [
            ('ema', self.parse_ema_product_pdf(product_name)),
            ('fda_orange', self.parse_fda_orange_book(product_name)),
            ('fda_purple', self.parse_fda_purple_book(product_name)),
            ('who_preq', self.parse_who_prequalified_list(product_name)),
        ]

        if country:
            sources.append(('national', self.parse_national_formulary(product_name, country)))

        # Filter valid results
        results = [r for _, r in sources if r is not None]

        if not results:
            return {
                'manufacturer': None,
                'confidence': 0.0,
                'pdf_sources_tried': len(sources),
                'timestamp': datetime.now().isoformat(),
            }

        # Sort by confidence
        results.sort(key=lambda x: x.get('confidence', 0), reverse=True)
        best = results[0]
        best['all_pdf_sources'] = results
        best['timestamp'] = datetime.now().isoformat()

        return best


# ==================== INTEGRATION ====================

def combine_web_and_pdf_results(web_result: Dict, pdf_result: Dict) -> Dict:
    """Combine web scraping and PDF parsing results."""

    web_conf = web_result.get('confidence', 0)
    pdf_conf = pdf_result.get('confidence', 0)

    # Use highest confidence result
    if pdf_conf > web_conf:
        best = pdf_result
        best['combined_from'] = 'PDF'
    else:
        best = web_result
        best['combined_from'] = 'Web'

    # Store both for reference
    best['web_result'] = web_result
    best['pdf_result'] = pdf_result
    best['highest_confidence'] = max(web_conf, pdf_conf)

    return best
