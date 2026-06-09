import io
import json
import math
import os
import uuid
from datetime import datetime, timezone

import pandas as pd

from core.config import DATASET_UPLOADS_DIR
from core.intelligence.semantic_classifier import classify_columns
from core.intelligence.segmentation_engine import compute_segmentation_profile_from_df
from data.db import get_connection
from data.dataset_replacement_service import (
    create_replacement_record,
    complete_replacement,
    fail_replacement,
)


def _compute_histogram(series, n_bins: int = 10) -> list:
    try:
        if len(series) < 2:
            return []
        min_v = float(series.min())
        max_v = float(series.max())
        if not (math.isfinite(min_v) and math.isfinite(max_v)):
            return []
        if min_v == max_v:
            return [{"min": min_v, "max": max_v, "count": int(len(series))}]
        step = (max_v - min_v) / n_bins
        bins = []
        for i in range(n_bins):
            low  = min_v + i * step
            high = min_v + (i + 1) * step
            if i < n_bins - 1:
                mask = (series >= low) & (series < high)
            else:
                mask = (series >= low) & (series <= high)
            bins.append({"min": round(low, 4), "max": round(high, 4), "count": int(mask.sum())})
        return bins
    except Exception:
        return []


def _compute_date_profile(df: pd.DataFrame, numeric_cols: list, categorical_cols: list) -> dict:
    DATE_THRESHOLD = 0.70
    date_columns: list[dict] = []

    for col in categorical_cols:
        try:
            raw_series = df[col]
            series     = raw_series.dropna()
            if len(series) == 0:
                continue
            parsed = pd.to_datetime(series, errors="coerce")
            valid  = int(parsed.notna().sum())
            if valid == 0 or valid / len(series) < DATE_THRESHOLD:
                continue
            valid_dates = parsed.dropna().sort_values()
            earliest    = valid_dates.iloc[0]
            latest      = valid_dates.iloc[-1]
            null_count  = int(raw_series.isnull().sum())

            granularity = "unknown"
            try:
                deltas = valid_dates.diff().dropna().dt.days
                median_delta = float(deltas.median())
                if median_delta <= 1.5:
                    granularity = "daily"
                elif median_delta <= 8:
                    granularity = "weekly"
                elif median_delta <= 32:
                    granularity = "monthly"
            except Exception:
                pass

            monthly_counts: list = []
            try:
                mc = valid_dates.dt.to_period("M").value_counts().sort_index()
                monthly_counts = [{"month": str(m), "count": int(c)} for m, c in mc.items()][:24]
            except Exception:
                pass

            date_columns.append({
                "column":               col,
                "earliest":             earliest.isoformat(),
                "latest":               latest.isoformat(),
                "min_date":             earliest.isoformat()[:10],
                "max_date":             latest.isoformat()[:10],
                "valid_count":          valid,
                "null_count":           null_count,
                "range_days":           int((latest - earliest).days),
                "inferred_granularity": granularity,
                "monthly_counts":       monthly_counts,
            })
        except Exception:
            continue

    trend_insights: list[dict] = []
    if date_columns and numeric_cols:
        primary_col = date_columns[0]["column"]
        try:
            df_s = df.copy()
            df_s["__date__"] = pd.to_datetime(df_s[primary_col], errors="coerce")
            df_s = df_s.dropna(subset=["__date__"]).sort_values("__date__").reset_index(drop=True)
            n = len(df_s)
            if n >= 4:
                half = n // 2
                for num_col in numeric_cols[:3]:
                    try:
                        f_mean = df_s.iloc[:half][num_col].dropna().mean()
                        s_mean = df_s.iloc[half:][num_col].dropna().mean()
                        if pd.isna(f_mean) or pd.isna(s_mean) or f_mean == 0:
                            continue
                        pct = round(((s_mean - f_mean) / abs(f_mean)) * 100, 1)
                        if abs(pct) < 5:
                            trend, symbol = "stable", "→"
                        elif pct > 0:
                            trend, symbol = "increasing", "↑"
                        else:
                            trend, symbol = "decreasing", "↓"
                        trend_insights.append({
                            "column":           num_col,
                            "trend":            trend,
                            "pct_change":       pct,
                            "symbol":           symbol,
                            "first_half_mean":  round(float(f_mean), 4),
                            "second_half_mean": round(float(s_mean), 4),
                        })
                    except Exception:
                        continue
        except Exception:
            pass

    return {"date_columns": date_columns, "trend_insights": trend_insights}


def create_dataset_summary(
    user_id: str,
    filename: str,
    row_count: int,
    column_count: int,
    columns: list,
    numeric_profile: dict,
    missing_values: dict,
    categorical_profile: dict,
    date_profile: dict | None = None,
    correlation_profile: list | None = None,
    categorical_meta: dict | None = None,
    semantic_profile: list | None = None,
    segmentation_profile: dict | None = None,
    file_path: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO datasets
              (user_id, filename, uploaded_at, row_count, column_count,
               columns_json, numeric_profile_json, missing_values_json,
               categorical_profile_json, date_profile_json,
               correlation_profile_json, categorical_meta_json,
               semantic_profile_json, segmentation_profile_json,
               file_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                filename,
                now,
                row_count,
                column_count,
                json.dumps(columns),
                json.dumps(numeric_profile),
                json.dumps(missing_values),
                json.dumps(categorical_profile),
                json.dumps(date_profile) if date_profile is not None else None,
                json.dumps(correlation_profile) if correlation_profile is not None else None,
                json.dumps(categorical_meta) if categorical_meta is not None else None,
                json.dumps(semantic_profile) if semantic_profile is not None else None,
                json.dumps(segmentation_profile) if segmentation_profile is not None else None,
                file_path,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_latest_dataset_for_user(user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM datasets WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_dataset_by_id(dataset_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM datasets WHERE id = ?",
            (dataset_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_datasets_for_user(user_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, filename, uploaded_at, row_count, column_count"
            " FROM datasets WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def rename_dataset(dataset_id: int, user_id: str, new_filename: str) -> bool:
    """Rename a dataset's display filename. Returns True if updated, False if not found or not owned."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE datasets SET filename = ? WHERE id = ? AND user_id = ?",
            (new_filename, dataset_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_dataset(dataset_id: int, user_id: str) -> bool:
    """Delete a dataset owned by user_id. Returns True if deleted, False if not found or not owned."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT file_path FROM datasets WHERE id = ? AND user_id = ?",
            (dataset_id, user_id),
        ).fetchone()
        cursor = conn.execute(
            "DELETE FROM datasets WHERE id = ? AND user_id = ?",
            (dataset_id, user_id),
        )
        conn.commit()
        if cursor.rowcount > 0 and row and row[0]:
            try:
                os.remove(row[0])
            except OSError:
                pass
        return cursor.rowcount > 0
    finally:
        conn.close()


def reprofile_dataset(dataset_id: int, user_id: str) -> dict | None:
    """Recompute all profiles for an existing dataset from its stored source file.

    Returns the updated dataset dict, or None if the dataset is not found,
    not owned by user_id, or the stored file is missing/unreadable.
    """
    row = get_dataset_by_id(dataset_id)
    if row is None or str(row.get("user_id", "")) != str(user_id):
        return None

    file_path = row.get("file_path")
    if not file_path or not os.path.exists(file_path):
        return None

    fp_lower = file_path.lower()
    try:
        with open(file_path, "rb") as fh:
            contents = fh.read()
        if fp_lower.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(contents))
        elif fp_lower.endswith(".xlsx"):
            df = pd.read_excel(io.BytesIO(contents), engine="openpyxl")
        else:
            df = pd.read_excel(io.BytesIO(contents), engine="xlrd")
    except Exception:
        return None

    def _safe_float(val):
        try:
            v = float(val)
            return None if not math.isfinite(v) else round(v, 4)
        except (TypeError, ValueError, OverflowError):
            return None

    numeric_cols     = df.select_dtypes(include="number").columns.tolist()
    categorical_cols = df.select_dtypes(exclude="number").columns.tolist()

    numeric_profile: dict = {}
    for col in numeric_cols:
        raw    = df[col]
        series = raw.dropna()
        n_tot  = len(raw)
        n_val  = len(series)
        if n_val == 0:
            numeric_profile[col] = {
                "min": None, "max": None, "mean": None, "sum": None,
                "std": None, "median": None,
                "p25": None, "p75": None, "p90": None,
                "non_null_count": 0, "null_count": n_tot,
                "zero_count": 0, "negative_count": 0,
                "outlier_count_iqr": 0, "histogram_bins": [],
            }
        else:
            try:
                q25 = _safe_float(series.quantile(0.25))
                q75 = _safe_float(series.quantile(0.75))
                outlier_count = 0
                if q25 is not None and q75 is not None:
                    iqr_v  = q75 - q25
                    lower  = q25 - 1.5 * iqr_v
                    upper  = q75 + 1.5 * iqr_v
                    outlier_count = int(((series < lower) | (series > upper)).sum())
            except Exception:
                q25 = q75 = None
                outlier_count = 0

            numeric_profile[col] = {
                "min":               _safe_float(series.min()),
                "max":               _safe_float(series.max()),
                "mean":              _safe_float(series.mean()),
                "sum":               _safe_float(series.sum()),
                "std":               _safe_float(series.std()),
                "median":            _safe_float(series.median()),
                "p25":               q25,
                "p75":               q75,
                "p90":               _safe_float(series.quantile(0.90)),
                "non_null_count":    n_val,
                "null_count":        n_tot - n_val,
                "zero_count":        int((series == 0).sum()),
                "negative_count":    int((series < 0).sum()),
                "outlier_count_iqr": outlier_count,
                "histogram_bins":    _compute_histogram(series, 10),
            }

    missing_values = {col: int(count) for col, count in df.isnull().sum().items()}

    categorical_profile: dict = {}
    for col in categorical_cols:
        top = df[col].value_counts().head(5)
        categorical_profile[col] = [
            {"value": str(v), "count": int(c)}
            for v, c in zip(top.index, top.values)
        ]

    categorical_meta: dict = {}
    for col in categorical_cols:
        try:
            raw    = df[col]
            n_tot  = len(raw)
            n_null = int(raw.isnull().sum())
            n_val  = n_tot - n_null
            unique_count = int(raw.nunique())
            top_vals     = raw.value_counts()
            top_share    = round(float(top_vals.iloc[0]) / n_tot, 4) if len(top_vals) > 0 and n_tot > 0 else 0.0

            entropy_approx = None
            try:
                total = top_vals.sum()
                if total > 0:
                    props = top_vals / total
                    entropy_approx = round(
                        float(-sum(p * math.log(float(p)) for p in props if p > 0)), 4
                    )
            except Exception:
                pass

            categorical_meta[col] = {
                "unique_count":    unique_count,
                "top_value_share": top_share,
                "null_count":      n_null,
                "non_null_count":  n_val,
                "entropy_approx":  entropy_approx,
            }
        except Exception:
            continue

    correlation_profile: list = []
    if len(numeric_cols) >= 2:
        try:
            corr_mx = df[numeric_cols].corr()
            pairs: list = []
            for i in range(len(numeric_cols)):
                for j in range(i + 1, len(numeric_cols)):
                    try:
                        c = float(corr_mx.iloc[i, j])
                        if not math.isfinite(c):
                            continue
                        abs_c    = abs(c)
                        strength = "strong" if abs_c >= 0.7 else ("moderate" if abs_c >= 0.4 else "weak")
                        pairs.append({
                            "column_a":    numeric_cols[i],
                            "column_b":    numeric_cols[j],
                            "correlation": round(c, 4),
                            "strength":    strength,
                        })
                    except Exception:
                        continue
            pairs.sort(key=lambda x: abs(x["correlation"]), reverse=True)
            correlation_profile = pairs[:20]
        except Exception:
            pass

    date_profile = _compute_date_profile(df, numeric_cols, categorical_cols)

    semantic_profile = classify_columns(
        columns=df.columns.tolist(),
        numeric_profile=numeric_profile,
        categorical_meta=categorical_meta,
        date_profile=date_profile,
        missing_values=missing_values,
        row_count=len(df),
    )

    segmentation_profile = compute_segmentation_profile_from_df(
        df=df,
        semantic_profile=semantic_profile,
        numeric_profile=numeric_profile,
        row_count=len(df),
    )
    if not segmentation_profile.get("computed_pairs", 0):
        segmentation_profile = None

    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE datasets SET
              row_count                = ?,
              column_count             = ?,
              columns_json             = ?,
              numeric_profile_json     = ?,
              missing_values_json      = ?,
              categorical_profile_json = ?,
              date_profile_json        = ?,
              correlation_profile_json = ?,
              categorical_meta_json    = ?,
              semantic_profile_json    = ?,
              segmentation_profile_json = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                len(df),
                len(df.columns),
                json.dumps(df.columns.tolist()),
                json.dumps(numeric_profile),
                json.dumps(missing_values),
                json.dumps(categorical_profile),
                json.dumps(date_profile),
                json.dumps(correlation_profile) if correlation_profile else None,
                json.dumps(categorical_meta) if categorical_meta else None,
                json.dumps(semantic_profile) if semantic_profile else None,
                json.dumps(segmentation_profile) if segmentation_profile else None,
                dataset_id,
                user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return get_dataset_by_id(dataset_id)


def replace_dataset_source_file(
    dataset_id: int,
    user_id: str,
    file_bytes: bytes,
    filename: str,
) -> dict | None:
    """Replace the stored source file for an existing dataset and immediately reprofile it.

    Returns the updated dataset dict, or None if the dataset is not found or not owned
    by user_id. The dataset_id is preserved; all profiles are recomputed from the new file.

    Operation order (failure-safe):
      1. Verify ownership.
      2. Write new file to disk.
      3. Open audit record (status=pending).
      4. Commit new file_path to DB.
      5. Reprofile from new file.
      6. Mark audit record success.
      7. Delete old file (best-effort; never blocks success).
    """
    row = get_dataset_by_id(dataset_id)
    if row is None or str(row.get("user_id", "")) != str(user_id):
        return None

    old_path = row.get("file_path")
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "bin"
    os.makedirs(DATASET_UPLOADS_DIR, exist_ok=True)
    new_file_path = os.path.join(DATASET_UPLOADS_DIR, f"{uuid.uuid4().hex}.{ext}")

    # Step 1 — write new file; fail loudly before any DB or audit state is mutated.
    try:
        with open(new_file_path, "wb") as fh:
            fh.write(file_bytes)
    except OSError as exc:
        raise RuntimeError(f"Failed to write replacement file: {exc}") from exc

    # Step 2 — open audit record now that we have both paths.
    record_id = create_replacement_record(
        dataset_id=dataset_id,
        user_id=user_id,
        old_file_path=old_path,
        new_file_path=new_file_path,
        original_filename=filename,
    )

    # Step 3 — commit new file_path to DB.
    try:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE datasets SET file_path = ? WHERE id = ? AND user_id = ?",
                (new_file_path, dataset_id, user_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        fail_replacement(record_id, f"DB update failed: {exc}")
        try:
            os.remove(new_file_path)
        except OSError:
            pass
        raise

    # Step 4 — reprofile from new file.
    try:
        result = reprofile_dataset(dataset_id, user_id)
    except Exception as exc:
        fail_replacement(record_id, f"Reprofile raised: {exc}")
        raise

    if result is None:
        fail_replacement(record_id, "reprofile_dataset returned None")
        return None

    # Step 5 — mark success, then best-effort delete old file.
    complete_replacement(record_id)

    if old_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass

    return result


def get_user_email(user_id: str) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT email FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return row["email"] if row and row["email"] else None
    finally:
        conn.close()
