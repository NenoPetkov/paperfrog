import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from paperfrog.domain.types import SafeError
from paperfrog.features.engineering import CORE_FEATURES, SCHEMA_HASH
from paperfrog.models.training import Ensemble, digest, train_member


def synthetic_samples(rows, start_ms):
    rng = np.random.default_rng(start_ms)
    x = rng.normal(size=(rows, len(CORE_FEATURES)))
    data = pd.DataFrame(x, columns=CORE_FEATURES)
    data["success"] = (x[:, 0] + rng.normal(size=rows) > 0.0).astype(int)
    data["net_return"] = x[:, 0] * 0.002 + rng.normal(0, 0.002, rows)
    data["timestamp_ms"] = np.arange(rows) * 86400000 + start_ms
    data["label_end_ms"] = data.timestamp_ms + 14400000
    return data


def test_real_xgboost_training_and_integrity(tmp_path):
    train, val = synthetic_samples(140, 1), synthetic_samples(100, 150 * 86400000)
    train_member(train, val, tmp_path / "member")
    manifest = {
        "schema_hash": SCHEMA_HASH,
        "created_at": datetime.now(UTC).isoformat(),
        "files": {str(p.relative_to(tmp_path)): digest(p) for p in tmp_path.rglob("*.json")},
        "members": ["member"],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    model = Ensemble(tmp_path)
    p = model.predict(val.tail(2))
    assert p["probabilities"].shape == (2, 1)
    assert np.isfinite(p["returns"]).all()
    manifest["schema_hash"] = "different"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SafeError, match="schema"):
        Ensemble(tmp_path)
    manifest["schema_hash"] = SCHEMA_HASH
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "member" / "classifier.json").write_text("{}")
    with pytest.raises(SafeError, match="integrity"):
        Ensemble(tmp_path)
