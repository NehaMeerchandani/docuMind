import fitz
import requests
from bs4 import BeautifulSoup

from document.models import DocumentType


class ParserService:
    REQUEST_TIMEOUT_SECONDS = 15
    MAX_CONTENT_BYTES = 20 * 1024 * 1024

    @classmethod
    def fetch_and_parse(cls, url):
        response = requests.get(
            url,
            timeout=cls.REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )

        if response.is_redirect:
            raise ValueError('The URL redirected to another address, which is not allowed for security reasons.')

        response.raise_for_status()

        content = cls._read_capped(response)
        content_type = response.headers.get('Content-Type', '').lower()

        if 'pdf' in content_type or url.lower().endswith('.pdf'):
            return DocumentType.PDF, cls._parse_pdf(content)
        if 'html' in content_type:
            return DocumentType.WEBPAGE, cls._parse_html(content)
        return DocumentType.TEXT, content.decode('utf-8', errors='ignore')

    @classmethod
    def _read_capped(cls, response):
        chunks = []
        total_bytes = 0

        for chunk in response.iter_content(chunk_size=8192):
            total_bytes += len(chunk)
            if total_bytes > cls.MAX_CONTENT_BYTES:
                raise ValueError('Document is too large to process.')
            chunks.append(chunk)

        return b''.join(chunks)

    @staticmethod
    def _parse_html(content):
        soup = BeautifulSoup(content, 'html.parser')
        for tag in soup(['script', 'style']):
            tag.decompose()
        return soup.get_text(separator='\n', strip=True)

    @staticmethod
    def _parse_pdf(content):
        with fitz.open(stream=content, filetype='pdf') as pdf:
            return '\n'.join(page.get_text() for page in pdf)
