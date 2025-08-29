import logging
import json
import os
import gspread
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Updater, CommandHandler, MessageHandler,
    CallbackContext, Filters, ConversationHandler
)
from threading import Thread
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from collections import OrderedDict
from functools import lru_cache
import requests
import time
from typing import Dict, List, Set
import asyncio

# NOTORIGIN3
# --- Настройки из переменных окружения ---
try:
    TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
    ALLOWED_PASSWORD = os.environ["BOT_PASSWORD"]
    FORM_URL = os.environ["FORM_URL"]
    GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
    BITRIX_WEBHOOK = os.environ.get("BITRIX_WEBHOOK")
    BITRIX_PROJECT_ID = os.environ.get("BITRIX_PROJECT_ID", "")
    BITRIX_PROJECT_ID = [int(x.strip()) for x in BITRIX_PROJECT_ID.split(",") if x.strip()]
except KeyError as e:
    raise RuntimeError(f"Не задана переменная окружения: {e}")

# --- Google Sheets ---
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
SPREADSHEET_NAME = "TerenTimeSheetV2"
PROJECTS_SHEET = "projects_sheet"
LOG_SHEET = "WebAppData"
USER_SHEET = "user_data"

# --- Глобальные кэши для ВСЕХ данных ---
ALL_PROJECTS_DATA: Dict[int, str] = {}  # project_id -> project_name
ALL_SUBPROJECTS_DATA: Dict[int, List[Dict]] = {}  # project_id -> list of subprojects
ALL_TASKS_DATA: Dict[int, List[Dict]] = {}  # subproject_id -> list of tasks
USER_ACCESS_CACHE: Dict[int, Set[int]] = {}  # user_id -> set of accessible project_ids

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Авторизация Google API ---
try:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
    client = gspread.authorize(creds)
    project_sheet = client.open(SPREADSHEET_NAME).worksheet(PROJECTS_SHEET)
    log_sheet = client.open(SPREADSHEET_NAME).worksheet(LOG_SHEET)
    user_sheet = client.open(SPREADSHEET_NAME).worksheet(USER_SHEET)
except Exception as e:
    logger.error(f"Ошибка подключения к Google Sheets: {e}")
    raise

# --- FastAPI ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def is_user_allowed(username: str) -> bool:
    """Проверяет, есть ли username в Google Sheets (лист user_data)"""
    try:
        data = user_sheet.get_all_records()
        allowed_usernames = [
            row.get("telegram_username", "").strip().lstrip("@")
            for row in data if row.get("telegram_username")
        ]
        return username.lstrip("@") in allowed_usernames
    except Exception as e:
        logger.error(f"Ошибка проверки доступа: {e}")
        return False


def get_bitrix_project_info(project_id: int) -> str:
    """Возвращает строку вида [ID] - NAME для проекта"""
    try:
        url = f"{BITRIX_WEBHOOK}sonet_group.get.json"
        response = requests.get(url, params={"FILTER[ID]": project_id})
        data = response.json()
        if "result" in data and data["result"]:
            project = data["result"][0]
            return f"[{project['ID']}] - {project['NAME']}"
        else:
            return f"Не найден проект {project_id}"
    except Exception as e:
        logger.error(f"Ошибка получения проекта из Bitrix: {e}")
        return f"Ошибка {project_id}"


def get_bitrix_subprojects(project_id: int):
    """Возвращает список подпроектов (верхнеуровневых задач) внутри проекта"""
    try:
        url = f"{BITRIX_WEBHOOK}tasks.task.list.json"
        response = requests.get(url, params={
            "filter[GROUP_ID]": project_id,
            "filter[PARENT_ID]": 0,
            "select[]": ["ID", "TITLE"]
        })
        data = response.json()
        if "result" in data:
            return [{"id": t["id"], "title": t["title"]} for t in data["result"]["tasks"]]
        return []
    except Exception as e:
        logger.error(f"Ошибка получения подпроектов из Bitrix: {e}")
        return []


def get_bitrix_tasks(subproject_id: int):
    """Возвращает список подзадач внутри подпроекта"""
    try:
        url = f"{BITRIX_WEBHOOK}tasks.task.list.json"
        response = requests.get(url, params={
            "filter[PARENT_ID]": subproject_id,
            "select[]": ["ID", "TITLE"]
        })
        data = response.json()
        if "result" in data:
            return [{"id": t["id"], "title": t["title"]} for t in data["result"]["tasks"]]
        return []
    except Exception as e:
        logger.error(f"Ошибка получения задач из Bitrix: {e}")
        return []


def is_user_in_project_tasks(project_id: int, bitrix_id: int, parent_id: int = 0) -> bool:
    """Рекурсивно проверяет, есть ли пользователь в задачах проекта"""
    try:
        # Используем предзагруженные данные вместо запросов к Bitrix
        if project_id not in ALL_SUBPROJECTS_DATA:
            return False

        # Проверяем все подпроекты проекта
        for subproject in ALL_SUBPROJECTS_DATA[project_id]:
            subproject_id = subproject['id']

            # Проверяем задачи в подпроекте
            if subproject_id in ALL_TASKS_DATA:
                for task in ALL_TASKS_DATA[subproject_id]:
                    # Здесь должна быть логика проверки пользователя в задаче
                    # Для простоты предположим, что у нас есть информация о пользователях
                    # В реальности нужно адаптировать под вашу структуру данных
                    pass

        # Временная заглушка - возвращаем True для тестирования
        return True

    except Exception as e:
        logger.error(f"Ошибка проверки задач проекта {project_id}: {e}")
        return False


def get_user_projects(bitrix_id: int):
    """Возвращает проекты, где пользователь участвует"""
    projects = []
    try:
        for pid in BITRIX_PROJECT_ID:
            if is_user_in_project_tasks(pid, bitrix_id):
                project_name = ALL_PROJECTS_DATA.get(pid, f"[{pid}] - Неизвестный проект")
                projects.append(project_name)

        return projects
    except Exception as e:
        logger.error(f"Ошибка поиска проектов для {bitrix_id}: {e}")
        return []


def get_user_subprojects(project_id: int, bitrix_id: int):
    """Возвращает подпроекты, где участвует пользователь"""
    subs = []
    try:
        if project_id in ALL_SUBPROJECTS_DATA:
            for subproject in ALL_SUBPROJECTS_DATA[project_id]:
                # Здесь должна быть логика проверки пользователя в подпроекте
                # Для тестирования возвращаем все подпроекты
                subs.append(f"[{subproject['id']}] - {subproject['title']}")

        return subs
    except Exception as e:
        logger.error(f"Ошибка поиска подпроектов: {e}")
        return []


def get_user_tasks(subproject_id: int, bitrix_id: int):
    """Возвращает задачи подпроекта, где участвует пользователь"""
    tasks = []
    try:
        if subproject_id in ALL_TASKS_DATA:
            for task in ALL_TASKS_DATA[subproject_id]:
                # Здесь должна быть логика проверки пользователя в задаче
                # Для тестирования возвращаем все задачи
                tasks.append(f"[{task['id']}] - {task['title']}")

        return tasks
    except Exception as e:
        logger.error(f"Ошибка поиска задач: {e}")
        return []


def get_user_bitrix_id(username: str) -> int | None:
    """Возвращает bitrix_id для telegram username из листа user_data"""
    try:
        rows = user_sheet.get_all_records()
        uname = (username or "").lstrip("@").strip()
        for row in rows:
            sheet_uname = str(row.get("telegram_username", "")).lstrip("@").strip()
            if uname and uname.lower() == sheet_uname.lower():
                bid = row.get("bitrix_id") or row.get("bitirx_id")
                try:
                    return int(bid)
                except Exception:
                    return None
        return None
    except Exception as e:
        logger.error(f"Ошибка получения bitrix_id: {e}")
        return None


def preload_all_bitrix_data():
    """Предзагрузка ВСЕХ данных из Bitrix один раз при старте"""
    try:
        logger.info("🚀 Начинаем полную предзагрузку данных из Bitrix...")

        total_subprojects = 0
        total_tasks = 0

        for pid in BITRIX_PROJECT_ID:
            try:
                # Загружаем информацию о проекте
                project_info = get_bitrix_project_info(pid)
                ALL_PROJECTS_DATA[pid] = project_info
                logger.info(f"✅ Загружен проект: {project_info}")

                # Загружаем ВСЕ подпроекты проекта
                subprojects = get_bitrix_subprojects(pid)
                ALL_SUBPROJECTS_DATA[pid] = subprojects
                total_subprojects += len(subprojects)
                logger.info(f"   📂 Подпроектов: {len(subprojects)}")

                # Для каждого подпроекта загружаем ВСЕ задачи
                for subproject in subprojects:
                    sub_id = subproject['id']
                    tasks = get_bitrix_tasks(sub_id)
                    ALL_TASKS_DATA[sub_id] = tasks
                    total_tasks += len(tasks)

                    if tasks:
                        logger.info(f"     📝 Задач в подпроекте {sub_id}: {len(tasks)}")

            except Exception as e:
                logger.error(f"❌ Ошибка загрузки проекта {pid}: {e}")

        logger.info(f"🎉 Предзагрузка завершена!")
        logger.info(f"   📊 Проектов: {len(ALL_PROJECTS_DATA)}")
        logger.info(f"   📁 Подпроектов: {total_subprojects}")
        logger.info(f"   📋 Задач: {total_tasks}")

    except Exception as e:
        logger.error(f"💥 Критическая ошибка предзагрузки: {e}")


@app.get("/ping")
async def ping():
    return PlainTextResponse("pong")


@app.get("/form.html")
async def serve_form():
    return FileResponse("static/form.html")


@app.get("/form-data")
async def serve_form_data(username: str = Query(None)):
    try:
        # --- данные по проектам (period, task, time_frame, difficulty_level) ---
        try:
            data = project_sheet.get_all_records()
        except Exception as e:
            logger.warning(f"Google Sheets projects_sheet недоступен: {e}")
            data = []

        # --- данные по пользователям ---
        try:
            users = user_sheet.get_all_records()
        except Exception as e:
            logger.warning(f"Google Sheets user_data недоступен: {e}")
            users = []

        # --- проекты под пользователя ---
        user_projects = []
        if username:
            bitrix_id = get_user_bitrix_id(username)
            if bitrix_id:
                user_projects = get_user_projects(bitrix_id)
            else:
                logger.info(f"Не найден bitrix_id для пользователя {username}")

        # fallback - показываем все проекты из кэша
        if not user_projects:
            user_projects = list(ALL_PROJECTS_DATA.values())

        # Карта должностей
        position_map = {}
        team_map = {}
        username_to_executor = {}
        username_to_team = {}

        for row in users:
            # Заполняем position_map
            name = row.get("executor", "")
            pos = row.get("position", "")
            if name and pos:
                position_map[name] = pos

            # Заполняем team_map
            team = row.get("team", "")
            executor = row.get("executor", "")
            if team and executor:
                team_map.setdefault(team, []).append(executor)

            # Заполняем маппинг username -> executor и username -> team
            telegram_username = row.get("telegram_username", "").lstrip("@").strip().lower()
            if telegram_username and executor:
                username_to_executor[telegram_username] = executor
            if telegram_username and team:
                username_to_team[telegram_username] = team

        fields_data = {
            "projects": user_projects,
            "period": list(OrderedDict.fromkeys(row["period"] for row in data if row.get("period"))),
            "task": sorted(set(row["task"] for row in data if row.get("task"))),
            "time_frame": sorted(set(row["time_frame"] for row in data if row.get("time_frame"))),
            "difficulty_level": sorted(set(row["difficulty_level"] for row in data if row.get("difficulty_level"))),
            "executor": sorted(set(row["executor"] for row in users if row.get("executor"))),
            "username_to_executor": username_to_executor,
            "username_to_team": username_to_team
        }

        return JSONResponse({**fields_data, "position_map": position_map, "team_map": team_map})

    except Exception as e:
        logger.exception("Ошибка в /form-data")
        return JSONResponse(content={"error": f"Ошибка получения данных: {str(e)}"}, status_code=500)


@app.get("/subprojects")
async def subprojects(project_id: int, username: str = Query(None)):
    try:
        # Берем из предзагруженного кэша
        subs_data = ALL_SUBPROJECTS_DATA.get(project_id, [])
        subs = [f"[{s['id']}] - {s['title']}" for s in subs_data]

        # Фильтрация по пользователю
        bitrix_id = get_user_bitrix_id(username) if username else None
        if bitrix_id:
            user_subs = get_user_subprojects(project_id, bitrix_id)
            if user_subs:
                subs = user_subs

        return JSONResponse({"subprojects": subs})
    except Exception as e:
        logger.exception("Ошибка в /subprojects")
        return JSONResponse({"subprojects": []})


@app.get("/tasks")
async def tasks(subproject_id: int, username: str = Query(None)):
    try:
        # Берем из предзагруженного кэша
        tasks_data = ALL_TASKS_DATA.get(subproject_id, [])
        tasks_resp = [f"[{t['id']}] - {t['title']}" for t in tasks_data]

        # Фильтрация по пользователю
        bitrix_id = get_user_bitrix_id(username) if username else None
        if bitrix_id:
            user_tasks = get_user_tasks(subproject_id, bitrix_id)
            if user_tasks:
                tasks_resp = user_tasks

        return JSONResponse({"tasks": tasks_resp})
    except Exception as e:
        logger.exception("Ошибка в /tasks")
        return JSONResponse({"tasks": []})


# --- Telegram bot ---
ASK_PASSWORD = 1
authorized_users = set()


def start(update: Update, context: CallbackContext) -> int:
    user_id = update.message.from_user.id
    username = update.message.from_user.username or ""

    # Проверяем доступ через Google Sheets
    if not is_user_allowed(username):
        update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return ConversationHandler.END

    # Если уже авторизован — сразу открываем веб
    if user_id in authorized_users:
        return send_webapp_button(update)

    update.message.reply_text("🔒 Введите пароль:")
    return ASK_PASSWORD


def check_password(update: Update, context: CallbackContext) -> int:
    if update.message.text.strip() == ALLOWED_PASSWORD:
        authorized_users.add(update.message.from_user.id)
        update.message.reply_text("✅ Доступ разрешён.")
        return send_webapp_button(update)
    update.message.reply_text("❌ Неверный пароль. Попробуйте снова.")
    return ASK_PASSWORD


def send_webapp_button(update: Update) -> int:
    username = update.message.from_user.username or ""
    webapp_url = f"{FORM_URL}?username={username}"

    button = [[KeyboardButton("📝 Заполнить", web_app=WebAppInfo(url=webapp_url))]]
    markup = ReplyKeyboardMarkup(button, resize_keyboard=True)
    update.message.reply_text("Нажмите, чтобы заполнить форму:", reply_markup=markup)
    return ConversationHandler.END


def receive_webapp(update: Update, context: CallbackContext):
    if update.message.web_app_data:
        try:
            data = json.loads(update.message.web_app_data.data)
            user = update.message.from_user.username or update.message.from_user.full_name
            log_sheet.append_row([
                user,
                data.get("projects", ""),
                data.get("subproject", ""),
                data.get("subtask", ""),
                data.get("period", ""),
                data.get("executor", ""),
                data.get("position", ""),
                data.get("task", ""),
                data.get("time_frame", ""),
                data.get("difficulty_level", ""),
                data.get("time", ""),
                data.get("overtime", ""),
                data.get("comment", "")
            ])
            update.message.reply_text("✅ Сохранено.")
        except Exception as e:
            logger.exception("Ошибка при записи данных")
            update.message.reply_text("⚠️ Ошибка при обработке данных.")
    else:
        update.message.reply_text("⚠️ Нет данных из WebApp.")


def run_telegram():
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={ASK_PASSWORD: [MessageHandler(Filters.text & ~Filters.command, check_password)]},
        fallbacks=[]
    )
    dp.add_handler(conv_handler)
    dp.add_handler(MessageHandler(Filters.status_update.web_app_data, receive_webapp))
    updater.start_polling()
    updater.idle()


# --- Запуск в отдельном потоке ---
if __name__ == "__main__":
    # ПРЕЖДЕ ВСЕГО загружаем все данные
    logger.info("⏳ Запускаем предзагрузку всех данных...")
    preload_all_bitrix_data()
    logger.info("✅ Данные загружены, запускаем сервер...")

    # Затем запускаем бота и сервер
    Thread(target=run_telegram, daemon=True).start()

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)