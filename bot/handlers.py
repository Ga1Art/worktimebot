from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from services.db import save_to_db

from bot.states import WorkState

router = Router()

async def show_summary(message: Message, state: FSMContext):
    data = await state.get_data()

    text = f"Проверь данные:\n\nДата: {data.get('date')}\nТип: {data.get('place')}"

    if data.get("place") == "Монтаж":
        text += f"\nПроект: {data.get('project')}\nЧасы: {data.get('hours')}"

    elif data.get("place") == "Смена":
        text += f"\nЧасы: {data.get('hours')}"

    elif data.get("place") == "Расходы":
        text += f"\nТип расходов: {data.get('expense_type')}\nСумма: {data.get('amount')}"

    text += "\n\nПодтвердить? (да/нет)"

    await state.set_state(WorkState.confirm)
    await message.answer(text)


@router.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.set_state(WorkState.waiting_date)
    await message.answer("Введите дату (1-31):")


@router.message(WorkState.waiting_date)
async def handle_date(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число от 1 до 31")
        return

    date = int(message.text)

    if not (1 <= date <= 31):
        await message.answer("Дата должна быть от 1 до 31")
        return

    await state.update_data(date=date)
    await state.set_state(WorkState.waiting_place)

    await message.answer(
        "Выберите тип работы:\n"
        "1. Монтаж\n"
        "2. Смена\n"
        "3. Расходы"
    )


@router.message(WorkState.waiting_place)
async def handle_place(message: Message, state: FSMContext):
    text = message.text

    if text not in ["1", "2", "3"]:
        await message.answer("Выберите 1, 2 или 3")
        return

    if text == "1":
        await state.update_data(place="Монтаж")
        await state.set_state(WorkState.waiting_project)
        await message.answer("Введите название проекта:")

    elif text == "2":
        await state.update_data(place="Смена")
        await state.set_state(WorkState.waiting_hours)
        await message.answer("Введите количество часов (1-24):")

    elif text == "3":
        await state.update_data(place="Расходы")
        await state.set_state(WorkState.waiting_expense_type)
        await message.answer("Введите тип расходов (например: бензин):")


@router.message(WorkState.waiting_project)
async def handle_project(message: Message, state: FSMContext):
    await state.update_data(project=message.text)
    await state.set_state(WorkState.waiting_hours)

    await message.answer("Введите количество часов (1-24):")


@router.message(WorkState.waiting_hours)
async def handle_hours(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число от 1 до 24")
        return

    hours = int(message.text)

    if not (1 <= hours <= 24):
        await message.answer("Часы должны быть от 1 до 24")
        return

    await state.update_data(hours=hours)
    await show_summary(message, state)


@router.message(WorkState.waiting_expense_type)
async def handle_expense_type(message: Message, state: FSMContext):
    await state.update_data(expense_type=message.text)
    await state.set_state(WorkState.waiting_expense_amount)

    await message.answer("Введите сумму расходов:")


@router.message(WorkState.waiting_expense_amount)
async def handle_expense_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("Введите корректную сумму (> 0)")
        return

    await state.update_data(amount=amount)
    await show_summary(message, state)


@router.message(WorkState.confirm)
async def handle_confirm(message: Message, state: FSMContext):
    text = message.text.lower()
    data = await state.get_data()

    name = message.from_user.full_name
    chat_id = message.from_user.id

    if text == "да":
        save_to_db(data, name, chat_id)

        await message.answer("Сохранено в Google Sheets ✅")

        await state.clear()

    elif text == "нет":
        await message.answer("Отменено ❌ Начните заново: /start")
        await state.clear()

    else:
        await message.answer("Введите 'да' или 'нет'")