"""File-based model store.

One JSON file per (layer, target metric). Writes are atomic (write + rename) so the
inference process can hot-reload while the trainer is running.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kizashi.models.baseline import BaselineModel


def _filename(metric: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", metric) + ".json"


@dataclass
class BaselineSet:
    metric: str
    version: str
    trained_at: float
    models: dict[str, BaselineModel]  # series key -> model


class ModelStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._cache: dict[str, tuple[float, BaselineSet]] = {}

    def _path(self, metric: str) -> Path:
        return self.root / "baseline" / _filename(metric)

    def save_baselines(self, metric: str, models: dict[str, BaselineModel]) -> BaselineSet:
        now = time.time()
        bset = BaselineSet(
            metric=metric,
            version=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)),
            trained_at=now,
            models=models,
        )
        path = self._path(metric)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "metric": metric,
            "version": bset.version,
            "trained_at": bset.trained_at,
            "series": {k: m.to_dict() for k, m in models.items()},
        }
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
        return bset

    def load_baselines(self, metric: str) -> BaselineSet | None:
        """Return the baselines for `metric`, re-reading the file only when it changed."""
        path = self._path(metric)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self._cache.pop(metric, None)
            return None
        cached = self._cache.get(metric)
        if cached and cached[0] == mtime:
            return cached[1]
        payload = json.loads(path.read_text())
        bset = BaselineSet(
            metric=payload["metric"],
            version=payload["version"],
            trained_at=float(payload["trained_at"]),
            models={k: BaselineModel.from_dict(v) for k, v in payload["series"].items()},
        )
        self._cache[metric] = (mtime, bset)
        return bset

    # ---------------------------------------------------------------- multivariate

    def _mv_paths(self, group_key: str) -> tuple[Path, Path]:
        """Return (metadata JSON path, classifier pickle path) for a group key."""
        name = _filename(group_key).removesuffix(".json")
        base = self.root / "multivariate" / name
        return base.with_suffix(".json"), base.with_suffix(".pkl")

    def save_multivariate(self, group_key: str, meta: Any, clf: Any) -> None:
        """Persist MultivariateModel metadata (JSON) and IsolationForest (joblib)."""
        try:
            import joblib  # noqa: PLC0415 — bundled with scikit-learn
        except ImportError as exc:
            raise RuntimeError("joblib is required (install scikit-learn)") from exc

        meta_path, clf_path = self._mv_paths(group_key)
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        # Write metadata JSON atomically
        fd, tmp = tempfile.mkstemp(dir=meta_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(meta.to_dict(), f)
            os.replace(tmp, meta_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        # Write classifier via joblib (atomic via temp file)
        fd2, tmp2 = tempfile.mkstemp(dir=clf_path.parent, suffix=".pkl.tmp")
        try:
            os.close(fd2)
            joblib.dump(clf, tmp2)
            os.replace(tmp2, clf_path)
        except BaseException:
            try:
                os.unlink(tmp2)
            except OSError:
                pass
            raise

    def load_multivariate(self, group_key: str) -> tuple[Any, Any] | None:
        """Load (MultivariateModel, IsolationForest). Returns None if not found."""
        try:
            import joblib  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("joblib is required (install scikit-learn)") from exc

        from kizashi.models.multivariate import MultivariateModel  # noqa: PLC0415

        meta_path, clf_path = self._mv_paths(group_key)
        if not meta_path.exists() or not clf_path.exists():
            return None
        meta = MultivariateModel.from_dict(json.loads(meta_path.read_text()))
        clf = joblib.load(clf_path)
        return meta, clf
