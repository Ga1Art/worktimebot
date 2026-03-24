import gspread
from oauth2client.service_account import ServiceAccountCredentials

SPREADSHEET_ID = "1i_wJrc2SnWBthDsYMN8KMNhA9pKJvaanrFzgbF6cGo4"


def get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "credentials.json",
        scope
    )

    client = gspread.authorize(creds)

    return client.open_by_key(SPREADSHEET_ID).sheet1


def get_client():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "credentials.json",
        scope
    )

    return gspread.authorize(creds)


def find_first_empty_row(sheet):
    col = sheet.col_values(1)

    for i, value in enumerate(col, start=1):
        if value == "":
            return i

    return len(col) + 1


def save_to_sheets(data: dict, name: str):
    client = get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    date = data.get("date")

    if data.get("place") == "Монтаж":
        sheet = spreadsheet.worksheet("МОНТАЖ")
        row_data = [name, date, data.get("hours"), data.get("project")]

    elif data.get("place") == "Смена":
        sheet = spreadsheet.worksheet("СМЕНА")
        row_data = [name, date, data.get("hours")]

    elif data.get("place") == "Расходы":
        sheet = spreadsheet.worksheet("ЛИЧНЫЕ РАСХОДЫ")
        row_data = [name, date, data.get("amount"), data.get("expense_type")]

    else:
        return

    # 🔥 ищем строку
    row_index = find_first_empty_row(sheet)

    print("WRITE TO ROW:", row_index)
    print("DATA:", row_data)

    # 🔥 записываем
    for col_index, value in enumerate(row_data, start=1):
        sheet.update_cell(row_index, col_index, value)

    row_index = find_first_empty_row(sheet)

    for col_index, value in enumerate(row_data, start=1):
        sheet.update_cell(row_index, col_index, value)