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


# ======== Performance helpers (Bitrix) ========
import requests
from requests.adapters import HTTPAdapter
_BX_SESS = requests.Session()
_BX_SESS.mount("http://", HTTPAdapter(pool_connections=20, pool_maxsize=50))
_BX_SESS.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=50))
_BX_TIMEOUT = (5, 30)
class _TTLCache:
    def __init__(self):
        self._data = {}
        self.ttl = int(os.getenv("CACHE_TTL_SEC", "120"))
    def get(self, key):
        item = self._data.get(key)
        if not item:
            return None, False
        ts, val = item
        import time
        fresh = (time.time() - ts) < self.ttl
        return val, fresh
    def set(self, key, val):
        import time
        self._data[key] = (time.time(), val)
_BX_CACHE = _TTLCache()
def _bx_get(method: str, params: dict) -> dict:
    url = f"{BITRIX_WEBHOOK}{method}.json"
    r = _BX_SESS.get(url, params=params or {}, timeout=_BX_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data.get("error_description") or data.get("error"))
    return data
def _bx_post(method: str, payload: dict) -> dict:
    url = f"{BITRIX_WEBHOOK}{method}.json"
    r = _BX_SESS.post(url, json=payload or {}, timeout=_BX_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data.get("error_description") or data.get("error"))
    return data
def _bx_batch(cmd: dict, halt: int = 0) -> dict:
    return _bx_post("batch", {"halt": halt, "cmd": cmd})
def _list_user_tasks_in_project_fast(project_id: int, bitrix_id: int, top: int = 1000):
    select = ["ID","TITLE","GROUP_ID","PARENT_ID","RESPONSIBLE_ID","ACCOMPLICES"]
    try:
        data = _bx_get("tasks.task.list", {
            "filter[GROUP_ID]": project_id,
            "filter[MEMBER_ID]": bitrix_id,
            "select[]": select,
            "order[ID]": "desc",
            "nav_params[nTopCount]": top
        })
        tasks = data.get("result", {}).get("tasks", [])
        if tasks:
            return tasks
    except Exception:
        pass
    try:
        resp = _bx_get("tasks.task.list", {
            "filter[GROUP_ID]": project_id,
            "filter[RESPONSIBLE_ID]": bitrix_id,
            "select[]": select,
            "order[ID]": "desc",
            "nav_params[nTopCount]": top
        }).get("result", {}).get("tasks", [])
        recent = _bx_get("tasks.task.list", {
            "filter[GROUP_ID]": project_id,
            "select[]": select,
            "order[ID]": "desc",
            "nav_params[nTopCount]": top
        }).get("result", {}).get("tasks", [])
        by_id = {int(t["id"]): t for t in resp}
        for t in recent:
            acc = t.get("accomplices") or []
            if bitrix_id in acc:
                by_id[int(t["id"])] = t
        return list(by_id.values())
    except Exception:
        return []
def _resolve_roots_for_tasks(task_items: list):
    if not task_items:
        return {}, {}
    parent = {int(t["id"]): int(t.get("parentId") or t.get("PARENT_ID") or 0) for t in task_items}
    known = set(parent.keys())
    to_fetch = {pid for pid in parent.values() if pid and pid not in known}
    while to_fetch:
        batch = list(to_fetch)[:50]
        cmd = {f"k{i}": f"tasks.task.get?taskId={tid}" for i, tid in enumerate(batch, 1)}
        res = _bx_batch(cmd).get("result", {}).get("result", {})
        for v in res.values():
            if isinstance(v, dict) and v.get("result"):
                tt = v["result"]
                tid = int(tt["id"])
                pid = int(tt.get("parentId") or 0)
                parent[tid] = pid
        known |= set(batch)
        to_fetch = {pid for pid in parent.values() if pid and pid not in known}
    root_for = {}
    for tid in list(parent.keys()):
        seen = set()
        cur = tid
        while parent.get(cur, 0):
            if cur in seen:
                break
            seen.add(cur)
            cur = parent[cur]
        root_for[tid] = cur if cur in parent else cur
    root_ids = sorted(set(root_for.values()))
    titles = {}
    for i in range(0, len(root_ids), 50):
        part = root_ids[i:i+50]
        cmd = {f"t{i2}": f"tasks.task.get?taskId={tid}" for i2, tid in enumerate(part, 1)}
        res = _bx_batch(cmd).get("result", {}).get("result", {})
        for v in res.values():
            if isinstance(v, dict) and v.get("result"):
                tt = v["result"]
                titles[int(tt["id"])] = tt.get("title") or f"Task {tt['id']}"
    return root_for, titles
# ======== /Performance helpers ========
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Или укажи только https://terentimesheet.utc-service.kz
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    """Быстро возвращает проекты (из BITRIX_PROJECT_ID), где пользователь участвует (как ответственный/соисполнитель)."""
    projects = []
    try:
        ids = list(BITRIX_PROJECT_ID)
        hits = []
        for i in range(0, len(ids), 50):
            part = ids[i:i+50]
            cmd = {}
            for idx, gid in enumerate(part, 1):
                cmd[f"c{idx}"] = f"tasks.task.list?filter[GROUP_ID]={gid}&filter[MEMBER_ID]={bitrix_id}&select[]=ID&nav_params[nTopCount]=1"
            data = _bx_batch(cmd)
            result = data.get("result", {}).get("result", {})
            for key, val in result.items():
                tasks_list = val.get("result", {}).get("tasks", []) if isinstance(val, dict) else []
                if tasks_list:
                    hits.append(part[int(key[1:]) - 1])
        if hits:
            for i in range(0, len(hits), 45):
                part = hits[i:i+45]
                cmd = {f"g{idx}": f"sonet_group.get?id={gid}" for idx, gid in enumerate(part, 1)}
                data = _bx_batch(cmd)
                res = data.get("result", {}).get("result", {})
                for v in res.values():
                    if isinstance(v, dict) and v.get("ID"):
                        gid = int(v["ID"])
                        name = v.get("NAME") or v.get("PROJECT_NAME") or f"Group {gid}"
                        projects.append(f"[{gid}] - {name}")
        if not projects:
            for pid in BITRIX_PROJECT_ID:
                if is_user_in_project_tasks(pid, bitrix_id):
                    name = get_bitrix_project_info(pid)
                    if name:
                        projects.append(name)
    except Exception as e:
        logger.error(f"Ошибка быстрого поиска проектов: {e}")
        if not projects:
            for pid in BITRIX_PROJECT_ID:
                try:
                    pinfo = get_bitrix_project_info(pid)
                    if pinfo:
                        projects.append(pinfo)
                except Exception:
                    pass
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
    """Возвращает подпроекты (верхнеуровневые задачи), где пользователь участвует в дереве задач проекта)."""
    cache_key = f"usr_subs:{project_id}:{bitrix_id}"
    cached, fresh = _BX_CACHE.get(cache_key)
    if cached:
        return cached
    try:
        tasks = _list_user_tasks_in_project_fast(project_id, bitrix_id, top=2000)
        root_for, titles = _resolve_roots_for_tasks(tasks)
        roots = sorted(set(root_for.values()))
        subs = [f"[{rid}] - {titles.get(rid, f'Task {rid}')}" for rid in roots if rid]
    except Exception as e:
        logger.error(f"Ошибка быстрого поиска подпроектов: {e}")
        subs = []
    _BX_CACHE.set(cache_key, subs)
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


def get_user_tasks(subproject_id: int, bitrix_id: int):
    """Возвращает задачи подпроекта, где участвует пользователь (без глубокой рекурсии)."""
    try:
        root_info = _bx_get("tasks.task.get", {"taskId": subproject_id}).get("result", {})
        project_id = int(root_info.get("groupId") or 0) if root_info else 0
        if not project_id:
            return []
        tasks = _list_user_tasks_in_project_fast(project_id, bitrix_id, top=3000)
        root_for, _titles = _resolve_roots_for_tasks(tasks)
        filtered = [t for t in tasks if root_for.get(int(t["id"])) == int(subproject_id)]
        return [f"[{t['id']}] - {t.get('title','')}" for t in filtered]
    except Exception as e:
        logger.error(f"Ошибка быстрого поиска задач подпроекта: {e}")
        try:
            return get_all_user_subtasks(subproject_id, bitrix_id)
        except Exception:
            return []

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
        # fallback (если нет username/bitrix_id) — показывать все, как раньше
        if not user_projects:
            for pid in BITRIX_PROJECT_ID:
                try:
                    pinfo = get_bitrix_project_info(pid)
                    if pinfo:
                        user_projects.append(pinfo)
                except Exception as e:
                    logger.warning(f"Не удалось получить проект {pid}: {e}")

        # Карта должностей
        position_map = {}
        # Карта команд -> исполнители
        team_map = {}
        # Карта username -> executor
        username_to_executor = {}
        # Карта username -> team
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
async def tasks(subproject_id: int, username: str = Query(None)):
    try:
        tasks_resp = []
        bitrix_id = get_user_bitrix_id(username) if username else None
        if bitrix_id:
            tasks_resp = get_user_tasks(subproject_id, bitrix_id)
        else:
            # fallback: все задачи
            tasks_list = get_bitrix_tasks(subproject_id)
            tasks_resp = [f"[{t['id']}] - {t['title']}" for t in tasks_list]
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
    Thread(target=run_telegram, daemon=True).start()

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)