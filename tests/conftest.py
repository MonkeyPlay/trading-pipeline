# tests/conftest.py
"""Shared test setup: no test reads the deployment's trained models (data/models/nq_ml) - the whole session sees an
empty model directory; tests/test_ml.py points it at the models it trains, for its module only - and no test reads or
writes the full-session RTH cache (data/rth_cache): each session gets an empty one."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _no_deployed_models(tmp_path_factory):
    from forecaster import ml_model, rth_analogues
    mp = pytest.MonkeyPatch()
    mp.setattr(ml_model, "MODELS_DIR", str(tmp_path_factory.mktemp("no_models")))
    mp.setattr(rth_analogues, "CACHE_DIR", str(tmp_path_factory.mktemp("rth_cache")))   # never data/rth_cache
    yield
    mp.undo()
