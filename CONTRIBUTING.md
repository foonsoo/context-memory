# Contributing

Use Python 3.11 or newer. Create an isolated environment, install with
`python -m pip install -e '.[dev]'`, and run:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
ruff check .
ruff format --check .
python -m compileall -q src benchmarks scripts
```

Keep changes small and independently reviewable. Add a regression that fails for
the original behavior, explain any deliberate contract change, and use only
temporary databases and synthetic fixture data. A useful bug report includes the
package/commit version, Python and OS versions, exact command or MCP request,
expected and actual result, and a minimal synthetic reproduction.

Never attach a real memory database, credentials, raw environment output, or
personal memory content to an issue. Redact paths and identifiers. Hosted files
are prototypes and are not the supported local product; proposals must keep that
boundary explicit. Do not report a client as verified without recording the
client version, OS, date, method, and observed result.
