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
from cachetools import TTLCache
import time
import requests

#NOTORIGIN2
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

# Кэши на 5 минут
cache_projects = TTLCache(maxsize=100, ttl=300)
cache_tasks = TTLCache(maxsize=100, ttl=300)

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
    user_sheet = client.open(SPREADSHEET_NAME).worksheet(USER_SHEET)  # <-- добавил
except Exception as e:
    logger.error(f"Ошибка подключения к Google Sheets: {e}")
    raise

# --- FastAPI ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Или укажи только https://terentimesheet.utc-service.kz
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_all_project_tasks(project_id: int):
    """Возвращает ВСЕ задачи проекта (включая подзадачи) одним большим запросом"""
    if project_id in cache_tasks:
        return cache_tasks[project_id]

    url = f"{BITRIX_WEBHOOK}tasks.task.list.json"
    start = 0
    all_tasks = []

    while True:
        response = requests.get(url, params={
            "filter[GROUP_ID]": project_id,
            "select[]": ["ID", "TITLE", "RESPONSIBLE_ID", "ACCOMPLICES", "PARENT_ID"],
            "start": start
        }).json()

        if "result" not in response:
            break

        tasks = response["result"].get("tasks", [])
        if not tasks:
            break

        all_tasks.extend(tasks)
        if "next" in response:
            start = response["next"]
        else:
            break

    cache_tasks[project_id] = all_tasks
    return all_tasks


def is_user_allowed(username: str) -> bool:
    """Проверяет, есть ли username в Google Sheets (лист user_data)"""
    try:
        data = user_sheet.get_all_records()   # <-- теперь из user_data
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
            "filter[PARENT_ID]": 0,   # верхнеуровневые
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


def get_user_projects(bitrix_id: int):
    """Возвращает список проектов, где участвует пользователь"""
    projects = []
    for pid in BITRIX_PROJECT_ID:
        tasks = get_all_project_tasks(pid)
        for t in tasks:
            resp = int(t.get("responsibleId", 0))
            accomplices = [int(x) for x in t.get("accomplices", []) if x]
            if bitrix_id in [resp] + accomplices:
                projects.append(get_bitrix_project_info(pid))
                break
    return projects



def is_user_in_project_tasks(project_id: int, bitrix_id: int, parent_id: int = 0) -> bool:
    """Рекурсивно проверяет, есть ли пользователь в задачах проекта"""
    try:
        url = f"{BITRIX_WEBHOOK}tasks.task.list.json"
        params = {
            "filter[GROUP_ID]": project_id,
            "select[]": ["ID", "TITLE", "RESPONSIBLE_ID", "ACCOMPLICES", "PARENT_ID"]
        }

        # Если проверяем подзадачи конкретного родителя
        if parent_id > 0:
            params["filter[PARENT_ID]"] = parent_id
        else:
            # Проверяем только задачи верхнего уровня
            params["filter[PARENT_ID]"] = 0

        response = requests.get(url, params=params)
        data = response.json()

        if "result" not in data:
            return False

        tasks = data["result"]["tasks"]

        for task in tasks:
            # Проверяем текущую задачу
            responsible = int(task.get("responsibleId", 0))
            accomplices = [int(x) for x in task.get("accomplices", []) if x]

            if bitrix_id in [responsible] + accomplices:
                return True

            # Рекурсивно проверяем подзадачи этой задачи
            task_id = task.get("id")
            if task_id and is_user_in_project_tasks(project_id, bitrix_id, task_id):
                return True

    except Exception as e:
        logger.error(f"Ошибка проверки задач проекта {project_id}: {e}")

    return False


def get_user_subprojects(project_id: int, bitrix_id: int):
    """Подпроекты (верхнеуровневые задачи) для пользователя"""
    tasks = get_all_project_tasks(project_id)
    subs = []
    for t in tasks:
        if int(t.get("parentId", 0)) == 0:  # верхний уровень
            resp = int(t.get("responsibleId", 0))
            accomplices = [int(x) for x in t.get("accomplices", []) if x]
            if bitrix_id in [resp] + accomplices or any(
                bitrix_id in [int(st.get("responsibleId", 0))] + [int(x) for x in st.get("accomplices", []) if x]
                for st in tasks if st.get("parentId") == t.get("id")
            ):
                subs.append(f"[{t['id']}] - {t['title']}")
    return subs



def is_user_in_task_or_subtasks(project_id: int, task_id: int, bitrix_id: int) -> bool:
    """Проверяет, есть ли пользователь в задаче или ее подзадачах"""
    try:
        # Сначала проверяем саму задачу
        url = f"{BITRIX_WEBHOOK}tasks.task.get.json"
        response = requests.get(url, params={"taskId": task_id})
        data = response.json()

        if "result" in data and data["result"]:
            task = data["result"]
            responsible = int(task.get("responsibleId", 0))
            accomplices = [int(x) for x in task.get("accomplices", []) if x]

            if bitrix_id in [responsible] + accomplices:
                return True

        # Затем рекурсивно проверяем подзадачи
        return has_user_in_subtasks(project_id, task_id, bitrix_id)

    except Exception as e:
        logger.error(f"Ошибка проверки задачи {task_id}: {e}")
        return False


def has_user_in_subtasks(project_id: int, parent_task_id: int, bitrix_id: int) -> bool:
    """Рекурсивно проверяет подзадачи на наличие пользователя"""
    try:
        url = f"{BITRIX_WEBHOOK}tasks.task.list.json"
        response = requests.get(url, params={
            "filter[PARENT_ID]": parent_task_id,
            "select[]": ["ID", "TITLE", "RESPONSIBLE_ID", "ACCOMPLICES"]
        })
        data = response.json()

        if "result" not in data:
            return False

        for task in data["result"]["tasks"]:
            responsible = int(task.get("responsibleId", 0))
            accomplices = [int(x) for x in task.get("accomplices", []) if x]

            # Проверяем текущую подзадачу
            if bitrix_id in [responsible] + accomplices:
                return True

            # Рекурсивно проверяем вложенные подзадачи
            subtask_id = task.get("id")
            if subtask_id and has_user_in_subtasks(project_id, subtask_id, bitrix_id):
                return True

    except Exception as e:
        logger.error(f"Ошибка проверки подзадач задачи {parent_task_id}: {e}")

    return False


def get_user_tasks(project_id: int, subproject_id: int, bitrix_id: int):
    """Задачи внутри подпроекта для пользователя"""
    tasks = get_all_project_tasks(project_id)
    subtasks = []
    for t in tasks:
        parent = int(t.get("parentId", t.get("PARENT_ID", 0)) or 0)
        if parent == int(subproject_id):
            resp = int(t.get("responsibleId", t.get("RESPONSIBLE_ID", 0)) or 0)
            accomplices = [int(x) for x in (t.get("accomplices") or t.get("ACCOMPLICES") or []) if x]
            if bitrix_id in [resp] + accomplices:
                tid = t.get("id") or t.get("ID")
                title = t.get("title") or t.get("TITLE") or ""
                subtasks.append(f"[{tid}] - {title}")
    return subtasks



def get_all_user_subtasks(parent_task_id: int, bitrix_id: int):
    """Рекурсивно получает все подзадачи где участвует пользователь"""
    user_tasks = []
    try:
        url = f"{BITRIX_WEBHOOK}tasks.task.list.json"
        response = requests.get(url, params={
            "filter[PARENT_ID]": parent_task_id,
            "select[]": ["ID", "TITLE", "RESPONSIBLE_ID", "ACCOMPLICES"]
        })
        data = response.json()

        if "result" not in data:
            return user_tasks

        for task in data["result"]["tasks"]:
            task_id = task.get("id")
            task_title = task.get("title", "")

            responsible = int(task.get("responsibleId", 0))
            accomplices = [int(x) for x in task.get("accomplices", []) if x]

            # Если пользователь участвует в этой задаче
            if bitrix_id in [responsible] + accomplices:
                user_tasks.append(f"[{task_id}] - {task_title}")

            # Рекурсивно получаем подзадачи
            subtasks = get_all_user_subtasks(task_id, bitrix_id)
            user_tasks.extend(subtasks)

    except Exception as e:
        logger.error(f"Ошибка получения подзадач задачи {parent_task_id}: {e}")

    return user_tasks

def get_user_bitrix_id(username: str) -> int | None:
    """Возвращает bitrix_id для telegram username из листа user_data"""
    try:
        rows = user_sheet.get_all_records()
        uname = (username or "").lstrip("@").strip()
        for row in rows:
            sheet_uname = str(row.get("telegram_username", "")).lstrip("@").strip()
            if uname and uname.lower() == sheet_uname.lower():
                bid = row.get("bitrix_id") or row.get("bitirx_id")  # на случай старого названия колонки
                try:
                    return int(bid)
                except Exception:
                    return None
        return None
    except Exception as e:
        logger.error(f"Ошибка получения bitrix_id: {e}")
        return None


@app.get("/ping")
async def ping():
    return PlainTextResponse("pong")

@app.get("/form.html")
async def serve_form():
    return FileResponse("static/form.html")


@app.get("/form-data")
async def serve_form_data(username: str = Query(None)):
    try:
        bitrix_id = None

        # --- 1) Загружаем исходные данные из Google Sheets ---
        try:
            data = project_sheet.get_all_records()  # лист projects_sheet
        except Exception as e:
            logger.warning(f"Google Sheets projects_sheet недоступен: {e}")
            data = []

        try:
            users = user_sheet.get_all_records()    # лист user_data
        except Exception as e:
            logger.warning(f"Google Sheets user_data недоступен: {e}")
            users = []

        # --- 2) Карты должностей/команд/username → executor/team ---
        position_map: dict[str, str] = {}
        team_map: dict[str, list[str]] = {}
        username_to_executor: dict[str, str] = {}
        username_to_team: dict[str, str] = {}

        for row in users:
            # position_map
            name = (row.get("executor") or "").strip()
            pos = (row.get("position") or "").strip()
            if name and pos:
                position_map[name] = pos

            # team_map
            team = (row.get("team") or "").strip()
            executor = (row.get("executor") or "").strip()
            if team and executor:
                team_map.setdefault(team, []).append(executor)

            # username → executor/team
            telegram_username = (row.get("telegram_username") or "").lstrip("@").strip().lower()
            if telegram_username:
                if executor:
                    username_to_executor[telegram_username] = executor
                if team:
                    username_to_team[telegram_username] = team

        # --- 3) Справочники из projects_sheet для select'ов ---
        period_list = list(OrderedDict.fromkeys(
            (row.get("period") or "").strip()
            for row in data if row.get("period")
        ))
        task_list = sorted(set(
            (row.get("task") or "").strip()
            for row in data if row.get("task")
        ))
        time_frame_list = sorted(set(
            (row.get("time_frame") or "").strip()
            for row in data if row.get("time_frame")
        ))
        difficulty_list = sorted(set(
            (row.get("difficulty_level") or "").strip()
            for row in data if row.get("difficulty_level")
        ))
        executor_list = sorted(set(
            (row.get("executor") or "").strip()
            for row in users if row.get("executor")
        ))

        fields_data = {
            "period": period_list,
            "task": task_list,
            "time_frame": time_frame_list,
            "difficulty_level": difficulty_list,
            "executor": executor_list,
            "username_to_executor": username_to_executor,
            "username_to_team": username_to_team
        }

        # --- 4) Пользовательские проекты из Битрикс (если известен username) ---
        user_projects: list[str] = []
        if username:
            bitrix_id = get_user_bitrix_id(username)
            if bitrix_id:
                user_projects = get_user_projects(bitrix_id)
            else:
                logger.info(f"Не найден bitrix_id для пользователя {username}")

        # Fallback: если своих проектов нет — показываем все Битрикс-проекты из списка окружения
        if not user_projects:
            for pid in BITRIX_PROJECT_ID:
                try:
                    pinfo = get_bitrix_project_info(pid)
                    if pinfo:
                        user_projects.append(pinfo)
                except Exception as e:
                    logger.warning(f"Не удалось получить проект {pid}: {e}")

        # --- 5) Собираем иерархию из Google Sheets: projects / subprojects / subtasks ---
        gs_projects_set: set[str] = set()
        gs_subprojects_map: dict[str, set[str]] = {}
        gs_tasks_map: dict[tuple[str, str], set[str]] = {}

        def norm_row_keys(row: dict) -> dict:
            # нормализуем ключи: " Projects " -> "projects"
            return {(k or "").strip().lower(): row.get(k) for k in row.keys()}

        for _row in data:
            row = norm_row_keys(_row)

            p = str(row.get("projects", "")).strip()
            sp = str(row.get("subprojects", "")).strip()
            # берём subtasks (ваша текущая колонка); если вдруг поменяете название на "tasks" — тоже подхватится
            st = str(row.get("subtasks", row.get("tasks", ""))).strip()

            if p:
                gs_projects_set.add(p)
                gs_subprojects_map.setdefault(p, set())
                if sp:
                    gs_subprojects_map[p].add(sp)
                    gs_tasks_map.setdefault((p, sp), set())
                    if st:
                        gs_tasks_map[(p, sp)].add(st)

        # Преобразуем set → отсортированные структуры
        gs_projects = sorted(gs_projects_set)
        gs_subprojects_map_sorted: dict[str, list[str]] = {
            k: sorted(v) for k, v in gs_subprojects_map.items()
        }
        gs_tasks_nested: dict[str, dict[str, list[str]]] = {}
        for (p, sp), tasks in gs_tasks_map.items():
            gs_tasks_nested.setdefault(p, {})
            gs_tasks_nested[p][sp] = sorted(tasks)

        # --- 6) Объединяем список проектов: сперва Битрикс-строки "[ID] - NAME", затем GS-проекты ---
        merged_projects: list[str] = []
        _seen = set()
        for p in user_projects:
            if p not in _seen:
                merged_projects.append(p)
                _seen.add(p)
        for p in gs_projects:
            if p not in _seen:
                merged_projects.append(p)
                _seen.add(p)

        # --- 7) Полезный пакет задач для пользователя (ветки, где он участвует) ---
        def build_user_filtered_tasks(tasks: list[dict], _bitrix_id: int) -> list[dict]:
            """
            Оставляет только задачи, где пользователь участвует (responsible/accomplices),
            плюс всех предков этих задач до корня, чтобы селекты могли корректно навигировать.
            """
            def tid(t):
                return int(t.get("id") or t.get("ID") or 0)

            def pid(t):
                return int(t.get("parentId", t.get("PARENT_ID", 0)) or 0)

            def resp(t):
                return int(t.get("responsibleId", t.get("RESPONSIBLE_ID", 0)) or 0)

            def accs(t):
                arr = t.get("accomplices") or t.get("ACCOMPLICES") or []
                return [int(x) for x in arr if x]

            by_id = {tid(t): t for t in tasks}
            keep = set()

            involved = {tid(t) for t in tasks if _bitrix_id in [resp(t)] + accs(t)}
            keep |= involved

            for iid in list(involved):
                cur = by_id.get(iid)
                while cur:
                    p = pid(cur)
                    if p <= 0:
                        break
                    if p in keep:
                        break
                    keep.add(p)
                    cur = by_id.get(p)

            return [by_id[i] for i in keep if i in by_id]

        all_data: dict[int, list[dict]] = {}
        bitrix_tasks_user: dict[int, list[dict]] = {}

        if bitrix_id:
            for pid in BITRIX_PROJECT_ID:
                all_tasks = get_all_project_tasks(pid)
                all_data[pid] = all_tasks

                is_user_project = any(f"[{pid}]" in p for p in user_projects)
                if is_user_project:
                    bitrix_tasks_user[pid] = build_user_filtered_tasks(all_tasks, bitrix_id)
        else:
            # без bitrix_id просто вернём пустой user-пакет
            bitrix_tasks_user = {}

        # --- 8) Ответ ---
        return JSONResponse({
            **fields_data,
            "position_map": position_map,
            "team_map": team_map,

            # объединённый список проектов (Битрикс + Google Sheets)
            "projects": merged_projects,

            # полный пакет Битрикс-задач по project_id (как было)
            "bitrix_tasks": all_data,

            # только «мои» ветки задач по project_id (если есть bitrix_id)
            "bitrix_tasks_user": bitrix_tasks_user,

            # новый блок для фронта c иерархией из Google Sheets (общедоступно для всех)
            "gs": {
                "projects": gs_projects,
                "subprojects_map": gs_subprojects_map_sorted,  # {project: [subprojects]}
                "tasks_map": gs_tasks_nested                   # {project: {subproject: [subtasks]}}
            }
        })

    except Exception as e:
        logger.exception("Ошибка в /form-data")
        return JSONResponse(
            content={"error": f"Ошибка получения данных: {str(e)}"},
            status_code=500
        )




@app.get("/subprojects")
async def subprojects(project_id: int, username: str = Query(None)):
    try:
        subs = []
        bitrix_id = get_user_bitrix_id(username) if username else None
        if bitrix_id:
            subs = get_user_subprojects(project_id, bitrix_id)
        else:
            # fallback: все подпроекты
            subs_full = get_bitrix_subprojects(project_id)
            subs = [f"[{s['id']}] - {s['title']}" for s in subs_full]
        return JSONResponse({"subprojects": subs})
    except Exception as e:
        logger.exception("Ошибка в /subprojects")
        return JSONResponse({"subprojects": []})


@app.get("/tasks")
async def tasks(
    subproject_id: int,
    project_id: int | None = Query(None),
    username: str = Query(None)
):
    try:
        bitrix_id = get_user_bitrix_id(username) if username else None

        def find_project_for_subproject(sp_id: int) -> int | None:
            # ищем подпроект среди всех проектов; используем кэш get_all_project_tasks
            for pid in BITRIX_PROJECT_ID:
                tasks = get_all_project_tasks(pid)
                if any(str(t.get("id") or t.get("ID")) == str(sp_id) for t in tasks):
                    return pid
            return None

        effective_project_id = project_id or find_project_for_subproject(subproject_id)
        if not effective_project_id:
            return JSONResponse({"tasks": [], "all_tasks": []})

        # все прямые дочерние задачи выбранного подпроекта
        all_tasks_list = get_all_project_tasks(effective_project_id)
        children = [
            t for t in all_tasks_list
            if int(t.get("parentId", t.get("PARENT_ID", 0)) or 0) == int(subproject_id)
        ]
        all_tasks = [
            f"[{(t.get('id') or t.get('ID'))}] - {(t.get('title') or t.get('TITLE') or '')}"
            for t in children
        ]

        # если есть username — отберём только задачи, где пользователь участвует
        if bitrix_id:
            user_tasks = []
            for t in children:
                resp = int(t.get("responsibleId", t.get("RESPONSIBLE_ID", 0)) or 0)
                accomplices = [int(x) for x in (t.get("accomplices") or t.get("ACCOMPLICES") or []) if x]
                if bitrix_id in [resp] + accomplices:
                    tid = t.get("id") or t.get("ID")
                    title = t.get("title") or t.get("TITLE") or ""
                    user_tasks.append(f"[{tid}] - {title}")
        else:
            user_tasks = all_tasks[:]  # если юзер не указан — вернём все

        return JSONResponse({"tasks": user_tasks, "all_tasks": all_tasks})
    except Exception as e:
        logger.exception("Ошибка в /tasks")
        return JSONResponse({"tasks": [], "all_tasks": []})


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
                # data.get("overtime", ""),
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
    Thread(target=run_telegram, daemon=True).start()

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)