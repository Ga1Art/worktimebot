from bot.shared_text import format_pending_expenses, format_pending_users


def paginate_rows(rows, page: int = 0, page_size: int = 5):
    if not rows:
        return {
            "rows": [],
            "page": 0,
            "page_size": page_size,
            "total_pages": 0,
            "page_rows": [],
        }

    total_pages = (len(rows) - 1) // page_size + 1
    page = min(max(0, page), total_pages - 1)
    start = page * page_size
    return {
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "page_rows": rows[start:start + page_size],
    }


def build_pending_users_overview_text(rows, page: int = 0, page_size: int = 5):
    if not rows:
        return format_pending_users(rows)

    view = paginate_rows(rows, page=page, page_size=page_size)
    lines = [f"Заявки на регистрацию, страница {view['page'] + 1}/{view['total_pages']}:"]
    for worker_id, full_name, chat_id in view["page_rows"]:
        lines.append(f"{worker_id}. {full_name} (chat_id: {chat_id})")
    return "\n".join(lines), view


def build_pending_expenses_overview_text(rows, page: int = 0, page_size: int = 5):
    if not rows:
        return format_pending_expenses(rows)

    view = paginate_rows(rows, page=page, page_size=page_size)
    lines = [f"Расходы на подтверждение, страница {view['page'] + 1}/{view['total_pages']}:"]
    for expense_id, full_name, amount, description, expense_date in view["page_rows"]:
        lines.append(f"{expense_id}. {full_name} | {amount} | {description} | {expense_date}")
    return "\n".join(lines), view
