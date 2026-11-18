import io
import os

import fitz
import requests
from bs4 import BeautifulSoup
from docx import Document as DocxDocument

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
            return DocumentType.PDF, cls.clean_text(cls._parse_pdf(content))
        if 'html' in content_type:
            return DocumentType.WEBPAGE, cls.clean_text(cls._parse_html(content))
        return DocumentType.TEXT, cls.clean_text(content.decode('utf-8', errors='ignore'))

    @staticmethod
    def clean_text(text):
        """Drops NUL (0x00) characters. Some PDFs extract with them, and PostgreSQL cannot
        store them in a text column ("A string literal cannot contain NUL (0x00)
        characters"), which made the whole document fail when its chunks were saved."""
        return text.replace('\x00', '')

    @classmethod
    def parse_file(cls, filename, content):
        """Parse an uploaded file's bytes, choosing the parser by extension."""
        if len(content) > cls.MAX_CONTENT_BYTES:
            raise ValueError('Document is too large to process.')

        extension = os.path.splitext(filename)[1].lower()

        if extension == '.pdf':
            return DocumentType.PDF, cls.clean_text(cls._parse_pdf(content))
        if extension == '.docx':
            return DocumentType.DOCX, cls.clean_text(cls._parse_docx(content))
        if extension in ('.txt', '.md'):
            return DocumentType.TEXT, cls.clean_text(content.decode('utf-8', errors='ignore'))
        raise ValueError(f'Unsupported file type "{extension}". Supported: PDF, DOCX, TXT, MD.')

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

    @staticmethod
    def _parse_docx(content):
        document = DocxDocument(io.BytesIO(content))
        lines = [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                lines.append(' | '.join(cell.text for cell in row.cells))
        return '\n'.join(line for line in lines if line.strip())
