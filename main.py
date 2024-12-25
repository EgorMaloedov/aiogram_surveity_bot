import asyncio
import logging
import sys
import jwt
import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session import aiohttp
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('TOKEN')
SECRET_KEY = os.getenv('SECRET_KEY')
BASE_URL = os.getenv('BASE_URL')


# Инициализация диспетчера
dp = Dispatcher()

# Состояния пользователя
class UserState(StatesGroup):
    user_inputs = State()
    user_token = State()
    answer_text = State()

# Функции HTTP запросов
async def get(path, params=None, headers=None):
    headers = headers or {}
    headers.update({'Content-Type': 'application/json'})
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(BASE_URL + path, params=params) as resp:
            return await resp.json()

async def post(path, data=None, headers=None):
    headers = headers or {}
    headers.update({'Content-Type': 'application/json'})
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(BASE_URL + path, json=data) as resp:
            return await resp.json()

# JWT генерация
def generate_jwt(test_id):
    payload = {
        'exp': datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
        'iat': datetime.datetime.now(datetime.UTC),
        'test_id': test_id,
        'sub': 'telegramSurveityBot'
    }
    return jwt.encode(payload, SECRET_KEY, algorithm='HS256')

# Функция для отправки вопроса
async def ask(call: CallbackQuery, question: dict, state: FSMContext):
    if question['type'] == 'text':
        await call.message.edit_text(f"{question['text']}\n\nВведите ответ")
        await state.set_state(UserState.answer_text)
    else:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"{'✅' if answer.get('isChecked') else ''} {answer['text']}",
                    callback_data=f"answer {i} {answer.get('isChecked', 0)}"
                )] for i, answer in enumerate(question['answers'])
            ]
        )
        if any(answer.get('isChecked') for answer in question['answers']):
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text='Отправить ответ', callback_data='question')]
            )
        await call.message.edit_text(f'{question['text']} ({"один ответ" if question['type'] != "multiple" else "несколько ответов" })', reply_markup=kb)

# Команда /start
@dp.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext, command: CommandStart):
    args = command.args
    if args:
        test_id = args
        token = generate_jwt(test_id)
        await state.update_data(user_token=token)
        survey = await get("tests/current/summary", {'token': token})
        current_input = 0
        await state.update_data(survey=survey, token=token, current_input=current_input, user_inputs=[])
        await message.answer(
            f"Добро пожаловать на тест: {survey['title']}\n"
            f"Количество вопросов в тесте: {survey['question_count']}"
        )
        if survey["user_inputs"]:
            await message.answer("Перед началом заполним необходимые поля:")
            await state.set_state(UserState.user_inputs)
            await message.answer(survey["user_inputs"][current_input]['title'])
        else:
            await message.answer(
                "Можно начинать тест",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text='Начать тест', callback_data='start')]]
                )
            )
    else:
        await message.answer("Убедитесь, что используете правильную ссылку.")

# Обработчики
@dp.callback_query(F.data == 'start')
async def start(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    jwt_token = data['user_token']
    session = await post("sessions", {'userInputs': data['user_inputs']}, headers={'Authorization': f'Bearer {jwt_token}'})
    session_id = session['session_id'][0]
    await state.update_data(session_id=session_id)
    question = await post(f'/sessions/{session_id}/start', {}, {'Authorization': f'Bearer {jwt_token}'})
    await state.update_data(question=question)
    await ask(call, question, state)

@dp.message(StateFilter(UserState.user_inputs))
async def user_inputs(message: Message, state: FSMContext):
    data = await state.get_data()
    current_input = data['current_input']
    data['user_inputs'].append(message.text)
    current_input += 1
    survey = data['survey']
    await state.update_data(user_inputs=data['user_inputs'], current_input=current_input)
    if len(survey['user_inputs']) == current_input:
        await state.set_state()
        await message.answer(
            "Можно начинать тест",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='Начать тест', callback_data='start')]]
            )
        )
    else:
        await message.answer(survey["user_inputs"][current_input]['title'])

@dp.message(StateFilter(UserState.answer_text))
async def answer_text(message: Message, state: FSMContext):
    await state.update_data(answer=message.text)
    await message.answer(
        f"Ваш ответ: {message.text}\n"
        f"Если неправильно, то напишите еще раз",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='Отправить ответ', callback_data='question')]]
        )
    )

@dp.callback_query(F.data.startswith('answer'))
async def answer_call(call: CallbackQuery, state: FSMContext):
    answer_idx, checked = map(int, call.data.split()[1:])
    data = await state.get_data()
    question = data['question']
    if question['type'] == 'single' and not checked:
        for answer in question['answers']:
            if 'isChecked' in answer:
                del answer['isChecked']
    question['answers'][answer_idx]['isChecked'] = 0 if checked else 1
    await state.update_data(question=question)
    await ask(call, question, state)


@dp.callback_query(F.data == 'question')
async def question_call(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    jwt_token = data['user_token']

    # Извлечение ответа в зависимости от типа вопроса
    if data['question']['type'] == 'text':
        answer = data['answer']
    elif data['question']['type'] == 'multiple':
        answer = [a['id'] for a in data['question']['answers'] if a.get('isChecked')]
    elif data['question']['type'] == 'single':
        checked_answers = [a for a in data['question']['answers'] if a.get('isChecked')]
        answer = checked_answers[0]['id'] if checked_answers else None

    await post(
        f"sessions/{data['session_id']}/submit_answer",
        {'question_id': data['question']['question_id'], 'answer_id': answer},
        {'Authorization': f'Bearer {jwt_token}'}
    )

    if data['question']['isLast']:
        await post(f"sessions/{data['session_id']}/complete")
        await call.message.edit_text('Опрос пройден')
    else:
        question = await get(
            f"sessions/{data['session_id']}/next",
            {},
            headers={'Authorization': f'Bearer {jwt_token}'}
        )
        await state.update_data(question=question)
        await ask(call, question, state)


@dp.message()
async def echo_handler(message: Message):
    await message.answer('Перейди по ссылке с опросом')

# Главная функция
async def main():
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
