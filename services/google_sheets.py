import json
import socket
import time
from datetime import datetime

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    GOOGLE_SHEETS_BONUSES_SHEET,
    GOOGLE_SHEETS_CURRENT_SHEET,
    GOOGLE_SHEETS_PENALTIES_SHEET,
    GOOGLE_SHEETS_PROJECTS_SHEET,
    GOOGLE_SHEETS_RATES_SHEET,
    GOOGLE_SHEETS_SPREADSHEET_ID,
    GOOGLE_SERVICE_ACCOUNT_FILE,
    GOOGLE_SERVICE_ACCOUNT_JSON,
)
from services.db import (
    ensure_default_rates_for_active_workers,
    get_active_projects_with_stats,
    get_active_workers_with_rates,
    get_worker_by_id,
    replace_bonuses_from_rows,
    replace_penalties_from_rows,
    replace_rates_from_rows,
)
from services.reporting import build_monthly_report


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GOOGLE_RETRY_ATTEMPTS = 4
GOOGLE_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
RATE_WORK_TYPES = ("shift", "install")
MONTHLY_HEADER_BACKGROUND = {"red": 0.72, "green": 0.18, "blue": 0.18}
MONTHLY_HEADER_TEXT = {"red": 1, "green": 1, "blue": 1}
MONTHLY_ODD_ROW_BACKGROUND = {"red": 0.99, "green": 0.92, "blue": 0.92}
MONTHLY_EVEN_ROW_BACKGROUND = {"red": 0.96, "green": 0.84, "blue": 0.84}
HEADER_ALIASES = {
    "worker_id": ("worker_id", "id сотрудника", "id", "сотрудник id"),
    "full_name": ("full_name", "фио", "сотрудник", "имя сотрудника", "полное имя"),
    "shift_rate": ("shift_rate", "ставка смена", "ставка за смену", "смена ставка"),
    "install_rate": ("install_rate", "ставка монтаж", "ставка за монтаж", "монтаж ставка"),
    "work_type": ("work_type", "тип работ", "тип работы"),
    "rate_per_hour": ("rate_per_hour", "ставка в час", "ставка", "почасовая ставка"),
    "bonus_date": ("bonus_date", "дата премии"),
    "penalty_date": ("penalty_date", "дата штрафа"),
    "amount": ("amount", "сумма"),
    "description": ("description", "описание", "комментарий"),
    "project_id": ("project_id", "id проекта"),
    "project_name": ("project_name", "название проекта", "проект"),
    "participants_count": ("participants_count", "участников", "количество участников"),
    "total_hours": ("total_hours", "всего часов", "часы всего", "часы монтажей"),
}
DISPLAY_HEADERS = {
    "rates": ["ID сотрудника", "Сотрудник", "Ставка за смену", "Ставка за монтаж"],
    "projects": ["ID проекта", "Название проекта", "Участников", "Часы монтажей", "Подтверждённые расходы", "Ожидающие расходы", "Отклонённые расходы"],
    "bonuses": ["ID сотрудника", "Сотрудник", "Дата премии", "Сумма", "Описание"],
    "penalties": ["ID сотрудника", "Сотрудник", "Дата штрафа", "Сумма", "Описание"],
}
CURRENT_SHEET_RATE_HEADER_ALIASES = {
    "worker_id": ("worker_id", "id", "id сотрудника"),
    "shift": ("shift_rate", "rate_shift", "ставка смена", "ставка за смену"),
    "install": ("install_rate", "rate_install", "ставка монтаж", "ставка за монтаж"),
}


def _force_ipv4_for_googleapis():
    original_getaddrinfo = socket.getaddrinfo

    def ipv4_first_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        results = original_getaddrinfo(host, port, family, type, proto, flags)
        if isinstance(host, str) and host.endswith("googleapis.com"):
            ipv4_results = [item for item in results if item[0] == socket.AF_INET]
            if ipv4_results:
                return ipv4_results
        return results

    socket.getaddrinfo = ipv4_first_getaddrinfo


_force_ipv4_for_googleapis()


def _execute_google_request(request, *, attempts: int = GOOGLE_RETRY_ATTEMPTS):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return request.execute()
        except HttpError as exc:
            status_code = getattr(getattr(exc, "resp", None), "status", None)
            if status_code not in GOOGLE_RETRYABLE_STATUS_CODES or attempt == attempts:
                raise
            last_error = exc
        except (OSError, TimeoutError) as exc:
            if attempt == attempts:
                raise
            last_error = exc

        time.sleep(min(2 ** (attempt - 1), 8))

    if last_error:
        raise last_error


def _get_credentials():
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        return service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=SCOPES,
        )

    if GOOGLE_SERVICE_ACCOUNT_FILE:
        return service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=SCOPES,
        )

    raise ValueError(
        "Google service account credentials are not configured. "
        "Set GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON."
    )


def _get_sheets_service():
    credentials = _get_credentials()
    return build("sheets", "v4", credentials=credentials)


def _get_spreadsheet_id():
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        raise ValueError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured.")
    return GOOGLE_SHEETS_SPREADSHEET_ID


def _get_sheet_metadata(service, spreadsheet_id):
    return _execute_google_request(
        service.spreadsheets().get(spreadsheetId=spreadsheet_id)
    )


def _find_sheet_id(metadata, title):
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == title:
            return properties.get("sheetId")
    return None


def _normalize_header(value: str):
    normalized = (value or "").strip().lower()
    for canonical, aliases in HEADER_ALIASES.items():
        if normalized in aliases:
            return canonical
    return normalized


def _get_first_present(row: dict, aliases: tuple[str, ...]):
    for alias in aliases:
        if alias in row:
            return row.get(alias, "")
    return ""


def _parse_decimal(value: str, field_name: str, row_number: int, *, allow_zero: bool = False):
    raw = (value or "").strip()
    if not raw:
        if allow_zero:
            return 0.0
        raise ValueError(f"{field_name}: пустое значение в строке {row_number}")
    try:
        parsed = float(raw.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{field_name}: некорректное число в строке {row_number}: {raw}") from exc

    if parsed < 0 or (parsed == 0 and not allow_zero):
        comparator = "не меньше нуля" if allow_zero else "больше нуля"
        raise ValueError(f"{field_name}: значение должно быть {comparator} (строка {row_number})")
    return parsed


def _parse_int(value: str, field_name: str, row_number: int):
    raw = (value or "").strip()
    if not raw:
        raise ValueError(f"{field_name}: пустое значение в строке {row_number}")
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name}: некорректный ID в строке {row_number}: {raw}") from exc


def _parse_date(value: str, field_name: str, row_number: int):
    raw = (value or "").strip()
    if not raw:
        raise ValueError(f"{field_name}: пустая дата в строке {row_number}")

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{field_name}: неподдерживаемый формат даты в строке {row_number}: {raw}")


def _sheet_rows_to_dicts(values):
    if not values:
        return []
    headers = [_normalize_header(item) for item in values[0]]
    rows = []
    for row in values[1:]:
        if not any((cell or "").strip() for cell in row):
            continue
        row_map = {}
        for index, header in enumerate(headers):
            if not header:
                continue
            row_map[header] = row[index].strip() if index < len(row) and row[index] is not None else ""
        rows.append(row_map)
    return rows


def _require_columns(row_keys, columns, sheet_title):
    missing = [column for column in columns if column not in row_keys]
    if missing:
        raise ValueError(f"Лист {sheet_title}: отсутствуют обязательные колонки: {', '.join(missing)}")


def _validate_worker(worker_id: int, sheet_title: str, row_number: int):
    if not get_worker_by_id(worker_id):
        raise ValueError(f"Лист {sheet_title}: сотрудник с ID {worker_id} не найден (строка {row_number})")


def _rows_to_rate_map(rows: list[dict]):
    rate_map = {}
    for row in rows:
        worker_id = row["worker_id"]
        work_type = row["work_type"]
        rate_map.setdefault(worker_id, {})[work_type] = float(row["rate_per_hour"])
    return rate_map


def _db_rate_map():
    rate_map = {}
    for worker_id, _, shift_rate, install_rate in get_active_workers_with_rates():
        rate_map[worker_id] = {
            "shift": float(shift_rate or 0),
            "install": float(install_rate or 0),
        }
    return rate_map


def _rate_map_to_rows(rate_map: dict[int, dict[str, float]]):
    rows = []
    for worker_id in sorted(rate_map):
        values = rate_map[worker_id]
        for work_type in RATE_WORK_TYPES:
            rows.append(
                {
                    "worker_id": worker_id,
                    "work_type": work_type,
                    "rate_per_hour": float(values.get(work_type, 0) or 0),
                }
            )
    return rows


def _merge_rate_sources(
    db_rate_map: dict[int, dict[str, float]],
    rates_sheet_map: dict[int, dict[str, float]],
    current_sheet_map: dict[int, dict[str, float]],
):
    merged = {}
    worker_ids = set(db_rate_map) | set(rates_sheet_map) | set(current_sheet_map)
    for worker_id in worker_ids:
        merged[worker_id] = {}
        for work_type in RATE_WORK_TYPES:
            db_value = float(db_rate_map.get(worker_id, {}).get(work_type, 0) or 0)
            rates_value = float(rates_sheet_map.get(worker_id, {}).get(work_type, db_value) or 0)
            current_value = float(current_sheet_map.get(worker_id, {}).get(work_type, db_value) or 0)

            rates_changed = rates_value != db_value
            current_changed = current_value != db_value

            if rates_changed and not current_changed:
                chosen = rates_value
            elif current_changed and not rates_changed:
                chosen = current_value
            elif rates_changed and current_changed:
                chosen = rates_value if rates_value != current_value else current_value
            else:
                chosen = db_value

            merged[worker_id][work_type] = chosen
    return merged


def build_rates_sheet_rows():
    ensure_default_rates_for_active_workers()
    rows = [DISPLAY_HEADERS["rates"]]
    for worker_id, full_name, shift_rate, install_rate in get_active_workers_with_rates():
        rows.append(
            [
                worker_id,
                full_name,
                float(shift_rate or 0),
                float(install_rate or 0),
            ]
        )
    return rows


def build_projects_sheet_rows():
    rows = [DISPLAY_HEADERS["projects"]]
    for project_id, project_name, participants_count, total_hours, approved, pending, rejected in get_active_projects_with_stats():
        rows.append([project_id, project_name, participants_count, float(total_hours or 0), float(approved), float(pending), float(rejected)])
    return rows


def sync_projects_reference_sheet():
    spreadsheet_id = _get_spreadsheet_id()
    service = _get_sheets_service()
    rows = build_projects_sheet_rows()
    write_sheet_rows(service, spreadsheet_id, GOOGLE_SHEETS_PROJECTS_SHEET, rows)
    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_title": GOOGLE_SHEETS_PROJECTS_SHEET,
        "row_count": len(rows),
    }


def ensure_sheet_exists(service, spreadsheet_id, title):
    metadata = _get_sheet_metadata(service, spreadsheet_id)
    existing_sheet_id = _find_sheet_id(metadata, title)
    if existing_sheet_id is not None:
        return existing_sheet_id

    response = (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": title,
                            }
                        }
                    }
                ]
            },
        )
    )
    response = _execute_google_request(response)
    return response["replies"][0]["addSheet"]["properties"]["sheetId"]


def clear_sheet(service, spreadsheet_id, title):
    _execute_google_request(
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=title,
            body={},
        )
    )


def unmerge_sheet(service, spreadsheet_id, title):
    metadata = _get_sheet_metadata(service, spreadsheet_id)
    sheet_id = _find_sheet_id(metadata, title)
    if sheet_id is None:
        return

    _execute_google_request(
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "unmergeCells": {
                            "range": {
                                "sheetId": sheet_id,
                            }
                        }
                    }
                ]
            },
        )
    )


def write_sheet_rows(service, spreadsheet_id, title, rows):
    ensure_sheet_exists(service, spreadsheet_id, title)
    unmerge_sheet(service, spreadsheet_id, title)
    clear_sheet(service, spreadsheet_id, title)
    _execute_google_request(
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{title}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        )
    )


def write_sheet_headers(service, spreadsheet_id, title, headers):
    ensure_sheet_exists(service, spreadsheet_id, title)
    _execute_google_request(
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{title}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [headers]},
        )
    )


def read_sheet_rows(service, spreadsheet_id, title):
    response = _execute_google_request(
        service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=title,
        )
    )
    return response.get("values", [])


def format_monthly_report_sheet(service, spreadsheet_id, title, row_count: int, column_count: int, day_columns_count: int):
    if row_count <= 0 or column_count <= 0:
        return

    metadata = _get_sheet_metadata(service, spreadsheet_id)
    sheet_id = _find_sheet_id(metadata, title)
    if sheet_id is None:
        return

    day_start_index = 4
    day_end_index = day_start_index + day_columns_count
    summary_start_index = day_end_index
    summary_end_index = column_count

    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "frozenRowCount": 1,
                        "frozenColumnCount": 4,
                    },
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": MONTHLY_HEADER_BACKGROUND,
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": MONTHLY_HEADER_TEXT,
                            "fontSize": 12,
                        },
                        "wrapStrategy": "CLIP",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,wrapStrategy)",
            }
        },
    ]

    for row_index in range(1, row_count):
        worker_pair_index = (row_index - 1) // 2
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": MONTHLY_ODD_ROW_BACKGROUND if worker_pair_index % 2 == 0 else MONTHLY_EVEN_ROW_BACKGROUND,
                            "verticalAlignment": "MIDDLE",
                            "textFormat": {
                                "fontSize": 11,
                            },
                            "wrapStrategy": "WRAP",
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,verticalAlignment,textFormat,wrapStrategy)",
                }
            }
        )

    requests.extend(
        [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": row_count,
                        "startColumnIndex": day_start_index,
                        "endColumnIndex": day_end_index,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "LEFT",
                            "verticalAlignment": "TOP",
                        }
                    },
                    "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment)",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": row_count,
                        "startColumnIndex": summary_start_index,
                        "endColumnIndex": summary_end_index,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                            "textFormat": {
                                "bold": True,
                                "fontSize": 11,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,textFormat)",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": row_count,
                        "startColumnIndex": 0,
                        "endColumnIndex": 4,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "LEFT",
                            "textFormat": {
                                "bold": True,
                                "fontSize": 11,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(horizontalAlignment,textFormat)",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": 0,
                        "endIndex": row_count,
                    },
                    "properties": {"pixelSize": 34},
                    "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "properties": {"pixelSize": 95},
                    "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 1,
                        "endIndex": 2,
                    },
                    "properties": {"pixelSize": 190},
                    "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 2,
                        "endIndex": 4,
                    },
                    "properties": {"pixelSize": 105},
                    "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": day_start_index,
                        "endIndex": day_end_index,
                    },
                    "properties": {"pixelSize": 88},
                    "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": summary_start_index,
                        "endIndex": summary_end_index,
                    },
                    "properties": {"pixelSize": 98},
                    "fields": "pixelSize",
                }
            },
        ]
    )

    for row_index in range(1, row_count, 2):
        if row_index + 1 >= row_count:
            break
        for column_index in range(4):
            requests.append(
                {
                    "mergeCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 2,
                            "startColumnIndex": column_index,
                            "endColumnIndex": column_index + 1,
                        },
                        "mergeType": "MERGE_ALL",
                    }
                }
            )

    _execute_google_request(
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        )
    )


def parse_rates_sheet(values, sheet_title: str):
    rows = _sheet_rows_to_dicts(values)
    if not rows:
        return []

    row_keys = set(rows[0].keys())
    parsed = []

    if {"worker_id", "work_type", "rate_per_hour"}.issubset(row_keys):
        for offset, row in enumerate(rows, start=2):
            worker_id = _parse_int(row.get("worker_id", ""), "worker_id", offset)
            _validate_worker(worker_id, sheet_title, offset)
            work_type = (row.get("work_type", "") or "").strip().lower()
            if work_type not in RATE_WORK_TYPES:
                raise ValueError(f"Лист {sheet_title}: work_type должен быть shift или install (строка {offset})")
            rate_per_hour = _parse_decimal(row.get("rate_per_hour", ""), "rate_per_hour", offset, allow_zero=True)
            parsed.append(
                {
                    "worker_id": worker_id,
                    "work_type": work_type,
                    "rate_per_hour": rate_per_hour,
                }
            )
        return parsed

    _require_columns(row_keys, ["worker_id", "shift_rate", "install_rate"], sheet_title)
    for offset, row in enumerate(rows, start=2):
        worker_id = _parse_int(row.get("worker_id", ""), "worker_id", offset)
        _validate_worker(worker_id, sheet_title, offset)
        parsed.extend(
            [
                {
                    "worker_id": worker_id,
                    "work_type": "shift",
                    "rate_per_hour": _parse_decimal(row.get("shift_rate", ""), "shift_rate", offset, allow_zero=True),
                },
                {
                    "worker_id": worker_id,
                    "work_type": "install",
                    "rate_per_hour": _parse_decimal(row.get("install_rate", ""), "install_rate", offset, allow_zero=True),
                },
            ]
        )
    return parsed


def parse_current_sheet_rate_overrides(values, sheet_title: str):
    rows = _sheet_rows_to_dicts(values)
    if not rows:
        return {}

    row_keys = set(rows[0].keys())
    worker_id_aliases = CURRENT_SHEET_RATE_HEADER_ALIASES["worker_id"]
    shift_aliases = CURRENT_SHEET_RATE_HEADER_ALIASES["shift"]
    install_aliases = CURRENT_SHEET_RATE_HEADER_ALIASES["install"]

    if not any(alias in row_keys for alias in worker_id_aliases):
        return {}
    if not any(alias in row_keys for alias in shift_aliases):
        return {}
    if not any(alias in row_keys for alias in install_aliases):
        return {}

    parsed = {}
    for offset, row in enumerate(rows, start=2):
        worker_id_raw = _get_first_present(row, worker_id_aliases)
        if not (worker_id_raw or "").strip():
            continue
        worker_id = _parse_int(worker_id_raw, "worker_id", offset)
        _validate_worker(worker_id, sheet_title, offset)
        parsed[worker_id] = {
            "shift": _parse_decimal(_get_first_present(row, shift_aliases), "shift_rate", offset, allow_zero=True),
            "install": _parse_decimal(_get_first_present(row, install_aliases), "install_rate", offset, allow_zero=True),
        }
    return parsed


def parse_bonuses_sheet(values, sheet_title: str):
    rows = _sheet_rows_to_dicts(values)
    if not rows:
        return []
    _require_columns(set(rows[0].keys()), ["worker_id", "bonus_date", "amount", "description"], sheet_title)

    parsed = []
    for offset, row in enumerate(rows, start=2):
        worker_id = _parse_int(row.get("worker_id", ""), "worker_id", offset)
        _validate_worker(worker_id, sheet_title, offset)
        bonus_date = _parse_date(row.get("bonus_date", ""), "bonus_date", offset)
        amount = _parse_decimal(row.get("amount", ""), "amount", offset)
        description = (row.get("description", "") or "").strip()
        if not description:
            raise ValueError(f"Лист {sheet_title}: описание обязательно (строка {offset})")
        parsed.append(
            {
                "worker_id": worker_id,
                "bonus_date": bonus_date,
                "amount": amount,
                "description": description,
            }
        )
    return parsed


def parse_penalties_sheet(values, sheet_title: str):
    rows = _sheet_rows_to_dicts(values)
    if not rows:
        return []
    _require_columns(set(rows[0].keys()), ["worker_id", "penalty_date", "amount", "description"], sheet_title)

    parsed = []
    for offset, row in enumerate(rows, start=2):
        worker_id = _parse_int(row.get("worker_id", ""), "worker_id", offset)
        _validate_worker(worker_id, sheet_title, offset)
        penalty_date = _parse_date(row.get("penalty_date", ""), "penalty_date", offset)
        amount = _parse_decimal(row.get("amount", ""), "amount", offset)
        description = (row.get("description", "") or "").strip()
        if not description:
            raise ValueError(f"Лист {sheet_title}: описание обязательно (строка {offset})")
        parsed.append(
            {
                "worker_id": worker_id,
                "penalty_date": penalty_date,
                "amount": amount,
                "description": description,
            }
        )
    return parsed


def sync_admin_data_from_sheets():
    spreadsheet_id = _get_spreadsheet_id()
    service = _get_sheets_service()

    rates_values = read_sheet_rows(service, spreadsheet_id, GOOGLE_SHEETS_RATES_SHEET)
    current_values = read_sheet_rows(service, spreadsheet_id, GOOGLE_SHEETS_CURRENT_SHEET)
    bonuses_values = read_sheet_rows(service, spreadsheet_id, GOOGLE_SHEETS_BONUSES_SHEET)
    penalties_values = read_sheet_rows(service, spreadsheet_id, GOOGLE_SHEETS_PENALTIES_SHEET)

    parsed_rates = parse_rates_sheet(rates_values, GOOGLE_SHEETS_RATES_SHEET)
    parsed_current_rates = parse_current_sheet_rate_overrides(current_values, GOOGLE_SHEETS_CURRENT_SHEET)
    parsed_bonuses = parse_bonuses_sheet(bonuses_values, GOOGLE_SHEETS_BONUSES_SHEET)
    parsed_penalties = parse_penalties_sheet(penalties_values, GOOGLE_SHEETS_PENALTIES_SHEET)

    merged_rate_map = _merge_rate_sources(
        _db_rate_map(),
        _rows_to_rate_map(parsed_rates),
        parsed_current_rates,
    )
    merged_rate_rows = _rate_map_to_rows(merged_rate_map)

    replace_rates_from_rows(merged_rate_rows)
    replace_bonuses_from_rows(parsed_bonuses)
    replace_penalties_from_rows(parsed_penalties)

    write_sheet_rows(service, spreadsheet_id, GOOGLE_SHEETS_RATES_SHEET, build_rates_sheet_rows())
    write_sheet_rows(service, spreadsheet_id, GOOGLE_SHEETS_PROJECTS_SHEET, build_projects_sheet_rows())

    from datetime import date as current_date

    today = current_date.today()
    current_sheet_result = sync_monthly_report_to_current_sheet(today.year, today.month)

    return {
        "spreadsheet_id": spreadsheet_id,
        "rates_sheet": GOOGLE_SHEETS_RATES_SHEET,
        "bonuses_sheet": GOOGLE_SHEETS_BONUSES_SHEET,
        "penalties_sheet": GOOGLE_SHEETS_PENALTIES_SHEET,
        "rates_count": len(merged_rate_rows),
        "bonuses_count": len(parsed_bonuses),
        "penalties_count": len(parsed_penalties),
        "current_sheet": {
            "year": today.year,
            "month": today.month,
            "sheet_title": current_sheet_result["sheet_title"],
            "row_count": current_sheet_result["row_count"],
            "column_count": current_sheet_result["column_count"],
        },
    }


def ensure_admin_sheets():
    spreadsheet_id = _get_spreadsheet_id()
    service = _get_sheets_service()

    rates_rows = build_rates_sheet_rows()
    write_sheet_rows(service, spreadsheet_id, GOOGLE_SHEETS_RATES_SHEET, rates_rows)
    projects_rows = build_projects_sheet_rows()
    write_sheet_rows(service, spreadsheet_id, GOOGLE_SHEETS_PROJECTS_SHEET, projects_rows)
    write_sheet_headers(
        service,
        spreadsheet_id,
        GOOGLE_SHEETS_BONUSES_SHEET,
        DISPLAY_HEADERS["bonuses"],
    )
    write_sheet_headers(
        service,
        spreadsheet_id,
        GOOGLE_SHEETS_PENALTIES_SHEET,
        DISPLAY_HEADERS["penalties"],
    )

    return {
        "spreadsheet_id": spreadsheet_id,
        "sheets": [
            {
                "sheet_title": GOOGLE_SHEETS_RATES_SHEET,
                "headers": DISPLAY_HEADERS["rates"],
                "row_count": len(rates_rows),
            },
            {
                "sheet_title": GOOGLE_SHEETS_PROJECTS_SHEET,
                "headers": DISPLAY_HEADERS["projects"],
                "row_count": len(projects_rows),
            },
            {
                "sheet_title": GOOGLE_SHEETS_BONUSES_SHEET,
                "headers": DISPLAY_HEADERS["bonuses"],
                "row_count": 1,
            },
            {
                "sheet_title": GOOGLE_SHEETS_PENALTIES_SHEET,
                "headers": DISPLAY_HEADERS["penalties"],
                "row_count": 1,
            },
        ],
    }


def sync_monthly_report_to_sheet(year: int, month: int, sheet_title: str):
    report = build_monthly_report(year, month)
    spreadsheet_id = _get_spreadsheet_id()
    service = _get_sheets_service()

    ensure_sheet_exists(service, spreadsheet_id, sheet_title)
    write_sheet_rows(service, spreadsheet_id, sheet_title, report["sheet_rows"])
    format_monthly_report_sheet(
        service,
        spreadsheet_id,
        sheet_title,
        len(report["sheet_rows"]),
        len(report["sheet_rows"][0]) if report["sheet_rows"] else 0,
        report.get("day_columns_count", 0),
    )
    write_sheet_rows(service, spreadsheet_id, GOOGLE_SHEETS_PROJECTS_SHEET, build_projects_sheet_rows())

    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_title": sheet_title,
        "row_count": len(report["sheet_rows"]),
        "column_count": len(report["sheet_rows"][0]) if report["sheet_rows"] else 0,
        "report": report,
    }


def sync_monthly_report_to_current_sheet(year: int, month: int):
    return sync_monthly_report_to_sheet(year, month, GOOGLE_SHEETS_CURRENT_SHEET)


def archive_monthly_report(year: int, month: int):
    archive_title = f"{year}-{month:02d}"
    return sync_monthly_report_to_sheet(year, month, archive_title)


def _get_next_month(year: int, month: int):
    if month == 12:
        return year + 1, 1
    return year, month + 1


def close_month(year: int, month: int):
    archive_result = archive_monthly_report(year, month)
    next_year, next_month = _get_next_month(year, month)
    current_result = sync_monthly_report_to_current_sheet(next_year, next_month)

    return {
        "archived_month": {
            "year": year,
            "month": month,
            "sheet_title": archive_result["sheet_title"],
            "row_count": archive_result["row_count"],
            "column_count": archive_result["column_count"],
        },
        "current_month": {
            "year": next_year,
            "month": next_month,
            "sheet_title": current_result["sheet_title"],
            "row_count": current_result["row_count"],
            "column_count": current_result["column_count"],
        },
    }


def google_error_message(exc: Exception):
    if isinstance(exc, HttpError):
        return f"Google Sheets API error: {exc}"
    return str(exc)
