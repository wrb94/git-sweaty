import json
import os
import subprocess
from typing import Any, Dict, List

from sync_strava import _activity_start_ts, _start_after_ts
from utils import ensure_dir, load_config, raw_activity_dir, read_json, write_json

RAW_DIR = raw_activity_dir("strava-kody")
SUMMARY_JSON = os.path.join("data", "last_sync_summary.json")
SUMMARY_TXT = os.path.join("data", "last_sync_summary.txt")
ATHLETE_PATH = os.path.join("data", "athletes_strava-kody.json")
EXPORT_MODULE_PATH = os.path.join("scripts", "kody_strava_export.mjs")
CLI_COMMAND = ["npx", "--yes", "@kodycodes/cli@latest", "execute", "--local", "--json", "--code"]


def _module_code(after: int) -> str:
    with open(EXPORT_MODULE_PATH, "r", encoding="utf-8") as handle:
        module = handle.read()
    marker = "const INPUT = null;"
    if module.count(marker) != 1:
        raise RuntimeError("Kody Strava export module is missing its input marker.")
    return module.replace(marker, f"const INPUT = {json.dumps({'after': after})};", 1)


def _find_export_payload(value: Any) -> Any:
    if isinstance(value, dict):
        if isinstance(value.get("activities"), list) and isinstance(value.get("athlete"), dict):
            return value
        for key in ("result", "output", "data", "value", "structuredContent"):
            if key in value:
                found = _find_export_payload(value[key])
                if found is not None:
                    return found
        for item in value.values():
            found = _find_export_payload(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_export_payload(item)
            if found is not None:
                return found
    elif isinstance(value, str):
        try:
            return _find_export_payload(json.loads(value))
        except (TypeError, ValueError):
            return None
    return None


def _parse_cli_result(stdout: str) -> Dict[str, Any]:
    candidates: List[Any] = []
    try:
        candidates.append(json.loads(stdout))
    except (TypeError, ValueError):
        pass

    for line in reversed((stdout or "").splitlines()):
        try:
            candidates.append(json.loads(line))
        except (TypeError, ValueError):
            continue

    for candidate in candidates:
        payload = _find_export_payload(candidate)
        if payload is not None:
            return payload
    raise RuntimeError("Kody CLI did not return the expected Strava export JSON.")


def _run_kody_export(after: int) -> Dict[str, Any]:
    if not str(os.environ.get("KODY_API_TOKEN", "")).strip():
        raise ValueError(
            "Missing KODY_API_TOKEN. Add a Kody API token with local-execute, account:read, "
            "integration:read, and integration:use scopes to GitHub Actions secrets."
        )

    command = [*CLI_COMMAND, _module_code(after)]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Node.js and npx are required for Strava via Kody sync.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Kody Strava export exceeded the one-hour workflow limit.") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Kody CLI failed without an error message.").strip()
        raise RuntimeError(f"Kody Strava export failed: {detail[-4000:]}")
    return _parse_cli_result(result.stdout)


def _reset_for_new_athlete(athlete_id: Any, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        previous = read_json(ATHLETE_PATH)
    except (OSError, ValueError):
        return
    if not isinstance(previous, dict) or str(previous.get("id")) == str(athlete_id):
        return

    print("Detected a different Kody-connected Strava athlete; resetting persisted activity data.")
    for path in (
        os.path.join("data", "activities_normalized.json"),
        os.path.join("data", "daily_aggregates.json"),
        os.path.join("site", "data.json"),
    ):
        if os.path.exists(path):
            os.remove(path)
    if os.path.isdir(RAW_DIR):
        import shutil

        shutil.rmtree(RAW_DIR)


def _write_summary(payload: Dict[str, Any], dry_run: bool, full_scan: bool) -> Dict[str, Any]:
    activities = payload.get("activities")
    athlete = payload.get("athlete")
    if not isinstance(activities, list) or not isinstance(athlete, dict):
        raise RuntimeError("Kody Strava export returned an invalid activity payload.")
    athlete_id = athlete.get("id")
    if athlete_id is None:
        raise RuntimeError("Kody Strava export did not return the athlete ID.")

    ensure_dir("data")
    ensure_dir(RAW_DIR)
    fetched_ids = set()
    changed = 0
    oldest_ts = None
    newest_ts = None
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        activity_id = activity.get("id")
        if activity_id is None:
            continue
        fetched_ids.add(str(activity_id))
        timestamp = _activity_start_ts(activity)
        if timestamp is not None:
            oldest_ts = timestamp if oldest_ts is None else min(oldest_ts, timestamp)
            newest_ts = timestamp if newest_ts is None else max(newest_ts, timestamp)
        if not dry_run:
            activity_id_text = str(activity_id).strip()
            if not activity_id_text or activity_id_text in {".", ".."} or "/" in activity_id_text or "\\" in activity_id_text or ".." in activity_id_text:
                continue
            path = os.path.join(RAW_DIR, f"{activity_id_text}.json")
            try:
                existing = read_json(path)
            except (OSError, ValueError):
                existing = None
            if existing != activity:
                write_json(path, activity)
                changed += 1

    if not dry_run:
        write_json(ATHLETE_PATH, {"id": athlete_id})

    summary = {
        "source": "strava-kody",
        "athlete_id": str(athlete_id),
        "fetched": len(fetched_ids),
        "new_or_updated": changed,
        "oldest_ts": oldest_ts,
        "newest_ts": newest_ts,
        "rate_limited": False,
        "rate_limit_message": "",
        "full_export": full_scan,
        "dry_run": dry_run,
    }
    if not dry_run:
        write_json(SUMMARY_JSON, summary)
        with open(SUMMARY_TXT, "w", encoding="utf-8") as handle:
            handle.write(
                f"Strava via Kody {'full' if full_scan else 'filtered'} export: "
                f"{summary['fetched']} activities fetched, "
                f"{summary['new_or_updated']} new or updated.\n"
            )
    return summary


def sync_strava_kody(dry_run: bool, prune_deleted: bool) -> Dict[str, Any]:
    config = load_config()
    after = _start_after_ts(config)
    payload = _run_kody_export(after)
    full_scan = after <= 0
    athlete = payload.get("athlete")
    if isinstance(athlete, dict) and athlete.get("id") is not None:
        _reset_for_new_athlete(athlete["id"], dry_run)
    summary = _write_summary(payload, dry_run, full_scan)

    if prune_deleted and not dry_run and full_scan:
        current_ids = {
            str(activity.get("id"))
            for activity in payload["activities"]
            if isinstance(activity, dict) and activity.get("id") is not None
        }
        for filename in os.listdir(RAW_DIR):
            if not filename.endswith(".json"):
                continue
            if filename[:-5] not in current_ids:
                os.remove(os.path.join(RAW_DIR, filename))
    elif prune_deleted and not dry_run:
        print("Skipping prune_deleted: Kody export used a start-after filter, so it was not a full-history scan.")

    print(
        "Strava via Kody sync complete: "
        f"{summary['fetched']} activities fetched, {summary['new_or_updated']} new or updated."
    )
    return summary
