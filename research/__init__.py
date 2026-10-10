# research/__init__.py
"""
Offline development research. Nothing here is imported by the dashboard, Auto, the collector or
the forecast ledger; it reads the database read-only and writes only under data/research/ and
docs/reports/. Research-only dependencies (TabPFN, torch) live in their own environment,
.venv-research (research/requirements-tabpfn.txt), never in the production .venv.
"""
