from abc import ABC, abstractmethod


class ProfilingQueryBuilder(ABC):
    """Dialect-neutral interface for all SQL emitted during profiling.

    Each method returns a complete, ready-to-execute SQL string.
    No database access is performed here — pure query generation only.
    """

    @abstractmethod
    def build_row_count_query(self, table_fqn: str) -> str:
        """Return a query that counts all rows in a table."""
        ...

    @abstractmethod
    def build_date_range_query(self, table_fqn: str, column_name: str) -> str:
        """Return a query that finds the earliest and latest value in a date column."""
        ...

    @abstractmethod
    def build_column_stats_query(
        self, table_fqn: str, column_name: str, data_type: str
    ) -> str:
        """Return a column-level statistics query for the given normalized data_type.

        data_type is one of: TEXT | INTEGER | DECIMAL | DATETIME | BOOLEAN | BINARY | JSON | OTHER
        Must always return columns: total_rows, populated_count, null_count, distinct_count,
        min_value, max_value, plus type-specific columns matching the MSSQL contract.
        """
        ...

    @abstractmethod
    def build_top_values_query(
        self, table_fqn: str, column_name: str, limit: int = 20
    ) -> str:
        """Return a query for the top-N most frequent non-null values.

        Returns: value (string-cast), row_count.
        """
        ...

    @abstractmethod
    def build_sample_values_query(
        self,
        table_fqn: str,
        column_name: str,
        limit: int = 10,
        sample_percent: int = 5,
    ) -> str:
        """Return a query for a random sample of non-null values.

        sample_percent: 1–100. Dialects without page-level sampling should ignore it
        and fall back to a full-scan LIMIT/TOP approach.
        """
        ...

    @abstractmethod
    def build_null_count_query(self, table_fqn: str, column_name: str) -> str:
        """Return a standalone query that counts NULL values in a column."""
        ...

    @abstractmethod
    def build_distinct_count_query(self, table_fqn: str, column_name: str) -> str:
        """Return a standalone query that counts distinct non-null values in a column."""
        ...

    @abstractmethod
    def build_percentile_query(self, table_fqn: str, column_name: str) -> str:
        """Return a query computing P5/P25/P50/P75/P95 for a numeric column.

        Must return a single row with columns: p5_value, p25_value, p50_value,
        p75_value, p95_value.  All values must be string-cast.
        Called for INTEGER and DECIMAL columns only; NULL rows must be excluded.
        """
        ...

    @abstractmethod
    def build_histogram_query(
        self,
        table_fqn: str,
        column_name: str,
        min_val: float,
        max_val: float,
        n_buckets: int = 10,
    ) -> str:
        """Return a single-scan query computing equal-width histogram buckets.

        Called for INTEGER and DECIMAL columns only, after column statistics have
        already established min/max so they can be injected as safe numeric literals.

        Must return rows of (bucket_idx INTEGER, row_count INTEGER) for every
        non-empty bucket, ordered by bucket_idx ascending.  Empty buckets are
        omitted — the caller reconstructs the full bucket list.
        min_val and max_val must be pre-validated finite floats.
        """
        ...

    @abstractmethod
    def quote_identifier(self, identifier: str) -> str:
        """Return the identifier wrapped in dialect-appropriate quotes."""
        ...
