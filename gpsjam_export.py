
import csv
import io
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h3
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("gpsjam")

# Northern Europe and Baltic Sea bounding box
MIN_LAT, MAX_LAT = 53.0, 71.5
MIN_LON, MAX_LON = 9.0, 33.0

# Minimum number of aircraft per H3 cell
MIN_AIRCRAFT = int(os.getenv("MIN_AIRCRAFT", "5"))

# Do not silently fall back to older dates.
# A missing daily file should fail visibly.
OUTPUT = Path("public")
OUTPUT.mkdir(parents=True, exist_ok=True)


def download_csv(day):
    url = (
        "https://gpsjam.org/data/"
        f"{day.isoformat()}-h3_4.csv"
    )
    LOG.info("Downloading %s", url)

    response = requests.get(
        url,
        timeout=90,
        headers={
            "User-Agent": "BalticGNSSMonitor/1.0"
        },
    )
    response.raise_for_status()

    reader = csv.DictReader(
        io.StringIO(response.text)
    )

    required = {
        "hex",
        "count_good_aircraft",
        "count_bad_aircraft",
    }
    if not reader.fieldnames or not required.issubset(
        set(reader.fieldnames)
    ):
        raise ValueError(
            f"Unexpected CSV columns: {reader.fieldnames}"
        )

    return list(reader)


def classify(ratio):
    if ratio > 0.10:
        return "high"
    if ratio >= 0.02:
        return "medium"
    return "low"


def process(rows, day):
    features = []
    totals = {
        "low": 0,
        "medium": 0,
        "high": 0,
    }
    good_total = 0
    bad_total = 0

    for row in rows:
        cell = row["hex"].strip()
        if not h3.is_valid_cell(cell):
            continue

        lat, lon = h3.cell_to_latlng(cell)
        if not (
            MIN_LAT <= lat <= MAX_LAT
            and MIN_LON <= lon <= MAX_LON
        ):
            continue

        good = int(row["count_good_aircraft"])
        bad = int(row["count_bad_aircraft"])
        total = good + bad

        if total < MIN_AIRCRAFT:
            continue

        ratio = bad / total
        level = classify(ratio)
        totals[level] += 1
        good_total += good
        bad_total += bad

        boundary = h3.cell_to_boundary(cell)
        ring = [[lng, latitude] for latitude, lng in boundary]
        ring.append(ring[0])

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [ring],
            },
            "properties": {
                "hex": cell,
                "date": day.isoformat(),
                "level": level,
                "bad_percent": round(ratio * 100, 2),
                "good_aircraft": good,
                "bad_aircraft": bad,
                "total_aircraft": total,
            },
        })

    count = sum(totals.values())
    affected = totals["medium"] + totals["high"]
    aircraft_total = good_total + bad_total

    stats = {
        "date": day.isoformat(),
        "total_cells": count,
        "low_cells": totals["low"],
        "medium_cells": totals["medium"],
        "high_cells": totals["high"],
        "affected_cells": affected,
        "affected_cell_percent": (
            round(100 * affected / count, 2)
            if count else 0
        ),
        "low_accuracy_aircraft_percent": (
            round(100 * bad_total / aircraft_total, 2)
            if aircraft_total else 0
        ),
    }

    if count == 0:
        raise RuntimeError(
            "No valid GPSJam cells found in the region"
        )

    return features, stats


def write_files(features, stats):
    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    (OUTPUT / "gpsjam.geojson").write_text(
        json.dumps(geojson, separators=(",", ":")),
        encoding="utf-8",
    )

    (OUTPUT / "stats.json").write_text(
        json.dumps(stats, indent=2),
        encoding="utf-8",
    )

    LOG.info("Statistics: %s", json.dumps(stats))


def export_metrics(stats):
    endpoint = os.environ.get(
        "GRAFANA_OTLP_ENDPOINT", ""
    ).rstrip("/")
    user = os.environ.get("GRAFANA_USER", "")
    token = os.environ.get("GRAFANA_API_KEY", "")

    if not all([endpoint, user, token]):
        raise RuntimeError(
            "Missing Grafana Cloud configuration"
        )

    if endpoint.endswith("/v1/metrics"):
        url = endpoint
    else:
        url = endpoint + "/v1/metrics"

    # Timestamp represents the collection day.
    # Push a fresh sample on each successful run.
    timestamp_ns = int(
        datetime.now(timezone.utc).timestamp()
        * 1_000_000_000
    )

    values = {
        "gpsjam_total_cells": stats["total_cells"],
        "gpsjam_low_cells": stats["low_cells"],
        "gpsjam_medium_cells": stats["medium_cells"],
        "gpsjam_high_cells": stats["high_cells"],
        "gpsjam_affected_cells": stats["affected_cells"],
        "gpsjam_affected_cell_percent":
            stats["affected_cell_percent"],
        "gpsjam_low_accuracy_aircraft_percent":
            stats["low_accuracy_aircraft_percent"],
    }

    metrics = []
    for name, value in values.items():
        metrics.append({
            "name": name,
            "description": "GPSJam Baltic region daily metric",
            "unit": "1",
            "gauge": {
                "dataPoints": [{
                    "attributes": [{
                        "key": "region",
                        "value": {"stringValue": "baltic"}
                    }],
                    "timeUnixNano": str(timestamp_ns),
                    "asDouble": float(value),
                }]
            },
        })

    payload = {
        "resourceMetrics": [{
            "resource": {
                "attributes": [{
                    "key": "service.name",
                    "value": {
                        "stringValue": "gpsjam-monitor"
                    },
                }]
            },
            "scopeMetrics": [{
                "scope": {"name": "gpsjam-exporter"},
                "metrics": metrics,
            }],
        }]
    }

    response = requests.post(
        url,
        auth=(user, token),
        headers={
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    LOG.info(
        "Grafana OTLP accepted: HTTP %s",
        response.status_code,
    )


def main():
    now = datetime.now(timezone.utc)
    day = (now - timedelta(days=1)).date()

    override = os.getenv("GPSJAM_DATE", "").strip()
    if override:
        day = datetime.strptime(
            override, "%Y-%m-%d"
        ).date()

    rows = download_csv(day)
    LOG.info("Downloaded %d CSV rows", len(rows))

    features, stats = process(rows, day)
    write_files(features, stats)
    export_metrics(stats)

    LOG.info(
        "Success: %d geographic cells processed",
        len(features),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("GPSJam export failed")
        sys.exit(1)
