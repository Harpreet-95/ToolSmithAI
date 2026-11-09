# Portable knowledge packages

ToolSmithAI can export sanitized, signed knowledge packages for reuse across
installations. Generated package files are runtime artifacts and are not
version controlled because they may contain source-specific schema and
business metadata.

Use the application export/import workflow to create or load a package. Keep
signing keys and source credentials outside this directory and out of Git.
