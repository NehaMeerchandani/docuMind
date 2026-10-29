MAX_ROWS_IN_CONTEXT = 200  # how many rows get persisted into the conversation's CONTEXT
                            # message (chat_service.py) -- keeps the prompt/history
                            # bounded; unrelated to TableParserService.MAX_ROWS_PARSED,
                            # which caps what's parsed out of the file at all.
MAX_CHART_POINTS = 50       # how many rows actually get plotted -- a chart with
                            # thousands of x-axis labels is unreadable anyway.

SUPPORTED_CHART_TYPES = ('bar', 'line', 'pie', 'scatter')
DEFAULT_CHART_TYPE = 'bar'


class ChartBuildError(ValueError):
    """Raised when a table has no columns suitable for charting at all (e.g. every
    column is non-numeric, so there's nothing to plot on a value axis)."""


class ChartService:
    """Turns a parsed table ({"columns": [...], "rows": [...]}) into a Chart.js
    `data`+`type` config the frontend can hand straight to `new Chart(ctx, {...})`.

    Column selection is heuristic, not LLM-driven: the LLM only ever supplies an
    optional `chart_type` (see build_chart_from_file tool) -- it never sees or chooses
    individual column names, since doing that reliably would need the full row data
    inside the model's context on every call, which is unnecessary when a handful of
    pandas-dtype checks answers the same question deterministically and for free.
    """

    @classmethod
    def build_chart(cls, table, chart_type=None, title=None):
        columns = table['columns']
        rows = table['rows']

        if not rows:
            raise ChartBuildError('This table has no rows to chart.')

        numeric_columns, label_column = cls._classify_columns(columns, rows)

        if not numeric_columns:
            raise ChartBuildError(
                'None of the columns in this table contain numbers, so there is '
                "nothing to plot. Columns found: " + ', '.join(columns),
            )

        chart_type = (chart_type or '').strip().lower() or None
        if chart_type and chart_type not in SUPPORTED_CHART_TYPES:
            chart_type = None  # fall through to auto-pick rather than error -- the
                                # LLM may pass something slightly off (e.g. "pie chart")
        if chart_type is None:
            chart_type = cls._auto_pick_chart_type(numeric_columns, rows)

        plotted_rows = rows[:MAX_CHART_POINTS]
        truncated = len(rows) > MAX_CHART_POINTS

        if chart_type == 'pie':
            # Pie charts only make sense for a single series -- use the first numeric
            # column regardless of how many were found.
            value_column = numeric_columns[0]
            labels = [cls._label_for_row(row, label_column, i) for i, row in enumerate(plotted_rows)]
            values = [cls._to_number(row.get(value_column)) for row in plotted_rows]
            datasets = [{'label': value_column, 'data': values}]
        else:
            labels = [cls._label_for_row(row, label_column, i) for i, row in enumerate(plotted_rows)]
            datasets = [
                {
                    'label': column,
                    'data': [cls._to_number(row.get(column)) for row in plotted_rows],
                }
                for column in numeric_columns
            ]

        return {
            'chart': {
                'type': chart_type,
                'title': title or cls._default_title(label_column, numeric_columns),
                'labels': labels,
                'datasets': datasets,
                'truncated': truncated,
                'total_rows': len(rows),
            },
        }

    @classmethod
    def _classify_columns(cls, columns, rows):
        """Returns (numeric_columns, label_column). `label_column` is the first
        non-numeric column found (used for x-axis / pie-slice labels), or None if every
        column is numeric (in which case row position is used as the label instead)."""
        numeric_columns = []
        label_column = None

        for column in columns:
            if cls._column_is_numeric(column, rows):
                numeric_columns.append(column)
            elif label_column is None:
                label_column = column

        return numeric_columns, label_column

    @staticmethod
    def _column_is_numeric(column, rows):
        sample = [row.get(column) for row in rows[:50] if row.get(column) is not None]
        if not sample:
            return False
        for value in sample:
            if isinstance(value, bool):
                return False
            if isinstance(value, (int, float)):
                continue
            try:
                float(value)
            except (TypeError, ValueError):
                return False
        return True

    @staticmethod
    def _to_number(value):
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _label_for_row(row, label_column, index):
        if label_column is None:
            return str(index + 1)
        value = row.get(label_column)
        return str(value) if value is not None else str(index + 1)

    @classmethod
    def _auto_pick_chart_type(cls, numeric_columns, rows):
        # A single numeric column over a modest number of rows reads well as a pie
        # chart (proportions); more rows, or more than one series, read better as a
        # line/bar. This is intentionally simple -- the user can always ask for a
        # specific chart_type to override it.
        if len(numeric_columns) == 1 and len(rows) <= 8:
            return 'pie'
        return DEFAULT_CHART_TYPE

    @staticmethod
    def _default_title(label_column, numeric_columns):
        series = ', '.join(numeric_columns)
        if label_column:
            return f'{series} by {label_column}'
        return series
