import csv
import io

import pandas as pd

MAX_ROWS_PARSED = 5000  # hard safety cap, independent of MAX_ROWS_IN_CONTEXT (chart_service.py)


class TableParseError(ValueError):
    """Raised when an uploaded chat attachment doesn't contain any parseable tabular
    data (e.g. a PDF of prose, an image, or a text file with no delimiter-separated
    rows at all)."""


class TableParserService:
    """Parses a chat-attached file into a plain {"columns": [...], "rows": [...]}
    shape, independent of document/services/parser_service.py (ParserService), which
    extracts free text for the RAG/embedding pipeline, not tabular data for charting.

    Deliberately file-format-driven by extension/content-type rather than reusing
    ParserService -- a CSV/XLSX upload here is never meant to become a Document or get
    embedded (per the user's explicit "no Document row for chart uploads" decision),
    so there's no shared code path with that pipeline.
    """

    @classmethod
    def parse(cls, django_file, filename):
        """`django_file` is any Django UploadedFile (already positioned at byte 0).
        Returns {"columns": [str, ...], "rows": [{col: value, ...}, ...]}.
        Raises TableParseError if no tabular structure could be found at all.
        """
        lower_name = (filename or '').lower()
        raw_bytes = django_file.read()

        if lower_name.endswith('.xlsx') or lower_name.endswith('.xls'):
            dataframe = cls._read_excel(raw_bytes)
        elif lower_name.endswith('.csv'):
            dataframe = cls._read_csv(raw_bytes)
        else:
            # Best-effort fallback: sniff for a delimiter in a plain-text file (e.g. a
            # .txt export, or a CSV uploaded with the wrong extension) rather than
            # refusing outright.
            dataframe = cls._sniff_delimited_text(raw_bytes)

        if dataframe is None or dataframe.empty:
            raise TableParseError(
                f'Could not find any tabular (row/column) data in "{filename}". '
                'Chart generation needs a CSV or Excel file with headers and rows.',
            )

        dataframe = dataframe.head(MAX_ROWS_PARSED)
        # NaN/NaT are not JSON-serializable by the stdlib json module used elsewhere
        # in this codebase (ChatService emits SSE via json.dumps) -- normalize to None.
        dataframe = dataframe.where(pd.notnull(dataframe), None)

        columns = [str(col) for col in dataframe.columns]
        rows = dataframe.to_dict(orient='records')

        return {'columns': columns, 'rows': rows}

    @classmethod
    def parse_text(cls, text):
        """Parses delimiter-separated text pasted into the chat (as opposed to an
        uploaded file). The LLM converts whatever the user typed (a markdown table, a
        list, etc.) into CSV text and passes it to build_chart_from_file's `inline_data`
        argument. Same return shape and error behavior as parse().
        """
        raw_bytes = (text or '').strip().encode('utf-8')

        dataframe = None
        if raw_bytes:
            try:
                dataframe = cls._read_csv(raw_bytes)
            except TableParseError:
                dataframe = None

        if dataframe is None or dataframe.empty or len(dataframe.columns) < 2:
            dataframe = cls._sniff_delimited_text(raw_bytes)

        if dataframe is None or dataframe.empty:
            raise TableParseError(
                'Could not find any tabular (row/column) data in the provided text. '
                'It needs a header row followed by data rows.',
            )

        dataframe = dataframe.head(MAX_ROWS_PARSED)
        dataframe = dataframe.where(pd.notnull(dataframe), None)

        return {
            'columns': [str(col) for col in dataframe.columns],
            'rows': dataframe.to_dict(orient='records'),
        }

    @classmethod
    def _read_excel(cls, raw_bytes):
        try:
            return pd.read_excel(io.BytesIO(raw_bytes))
        except Exception as exc:
            raise TableParseError(f'Could not read this file as an Excel spreadsheet: {exc}') from exc

    @classmethod
    def _read_csv(cls, raw_bytes):
        try:
            return pd.read_csv(io.BytesIO(raw_bytes))
        except Exception as exc:
            raise TableParseError(f'Could not read this file as a CSV: {exc}') from exc

    @classmethod
    def _sniff_delimited_text(cls, raw_bytes):
        try:
            text = raw_bytes.decode('utf-8', errors='ignore')
        except Exception:
            return None

        sample = text[:4096]
        if not sample.strip():
            return None

        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=',\t;|')
        except csv.Error:
            return None

        try:
            return pd.read_csv(io.StringIO(text), sep=dialect.delimiter)
        except Exception:
            return None
