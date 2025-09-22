
/** ========= Константы/глобальные ========= **/
const BASE_URL = 'https://terentimesheetv2-terentimesheetv2.up.railway.app';

let username = "";
let positionMap = {};
let teamMap = {};
let BITRIX_TASKS = {};                 // { pid: Task[] }
let PROJECT_ID_BY_LABEL = new Map();   // "[123] - Name" -> 123

// GS-хранилище (приходит в data.gs из /form-data)
let GS = {
  projects: [],                        // ["General", ...]
  subprojects_map: {},                 // { "General": ["Админ", ...] }
  tasks_map: {}                        // { "General": { "Админ": ["Ведение ...", ...] } }
};

/** ========= Утилиты ========= **/
function dbg(...args){ try { console.log(...args); } catch(e){} }

function parseIdFromLabel(label){
  if (!label) return null;
  const m = String(label).match(/\[(\d+)\]/);
  return m ? Number(m[1]) : null;
}
function isBitrixProject(label){
  return !!parseIdFromLabel(label);
}
function toOptionText(task){
  const id = task.id ?? task.ID;
  const title = task.title ?? task.TITLE ?? '';
  return `[${id}] - ${title}`;
}
function getParentId(task){ return Number(task.parentId ?? task.PARENT_ID ?? 0); }
function getTaskId(task){ return Number(task.id ?? task.ID ?? 0); }

function indexByParent(tasks){
  const byParent = new Map();
  for (const t of tasks){
    const pid = getParentId(t);
    if (!byParent.has(pid)) byParent.set(pid, []);
    byParent.get(pid).push(t);
  }
  return byParent;
}
function collectDescendants(tasks, rootId){
  const byParent = indexByParent(tasks);
  const out = [];
  const q = [Number(rootId)];
  const seen = new Set();
  while (q.length){
    const pid = q.shift();
    const children = byParent.get(pid) || [];
    for (const c of children){
      const cid = getTaskId(c);
      if (seen.has(cid)) continue;
      seen.add(cid);
      out.push(c);
      q.push(cid);
    }
  }
  return out;
}

const PLACEHOLDERS = {
  projects: "Выберите проект",
  subproject: "Выберите подпроект",
  subtask: "Выберите задачу",
  period: "Выберите период",
  task: "Выберите вид работы",
  time_frame: "Выберите временные рамки",
  difficulty_level: "Выберите уровень сложности",
  executor: "Выберите исполнителя",
  time: "Укажите время"
};

function fillSelect(id, values){
  const sel = document.getElementById(id);
  if (!sel) return;
  sel.innerHTML = "";
  const ph = PLACEHOLDERS[id];
  if (ph) sel.appendChild(new Option(ph, ""));
  (values || []).forEach(v => sel.appendChild(new Option(v, v)));
}

function fillTimeOptions(){
  const sel = document.getElementById("time");
  if (!sel) return;
  sel.innerHTML = "";
  sel.appendChild(new Option(PLACEHOLDERS.time, ""));
  for (let i = 0.5; i <= 12; i += 0.5){
    const opt = document.createElement("option");
    opt.value = i;
    opt.textContent = `${i} ч`;
    sel.appendChild(opt);
  }
}

function fillUserProjects(projects){
  const sel = document.getElementById("projects");
  sel.innerHTML = "";
  sel.appendChild(new Option(PLACEHOLDERS.projects, ""));
  if (projects?.length){
    projects.forEach(p => sel.appendChild(new Option(p, p)));
  } else {
    sel.appendChild(new Option("Нет доступных проектов", ""));
  }
}

function autoFillUserFields(username, data){
  if (!username) return;
  const clean = username.toLowerCase().replace('@','');
  const userExecutor = data.username_to_executor?.[clean];
  const userTeam = data.username_to_team?.[clean];
  if (userTeam) { document.getElementById("team").value = userTeam; }
  if (userExecutor) {
    document.getElementById("executor").value = userExecutor;
    fillPosition();
  }
}
function fillPosition(){
  const exec = document.getElementById("executor").value;
  document.getElementById("position").value = positionMap[exec] || "";
}

/** ========= Bitrix & GS bundle ========= **/
function applyBitrixBundleFromFormData(data){
  // Битрикс-пакет: либо пользовательские ветки, либо полный пакет
  BITRIX_TASKS = (data.bitrix_tasks_user && Object.keys(data.bitrix_tasks_user).length)
    ? data.bitrix_tasks_user
    : (data.bitrix_tasks || {});

  // Индексация «[ID] - NAME» → ID
  PROJECT_ID_BY_LABEL = new Map();
  (data.projects || []).forEach(lbl => {
    const id = parseIdFromLabel(lbl);
    if (id) PROJECT_ID_BY_LABEL.set(lbl, id);
  });

  // GS-блок
  GS = {
    projects: data.gs?.projects || [],
    subprojects_map: data.gs?.subprojects_map || {},
    tasks_map: data.gs?.tasks_map || {}
  };
}

/** ========= GS helpers ========= **/
function getGsProjectKey(label){
  if (!label) return null;
  if (isBitrixProject(label)) return null; // это не GS

  const cand = String(label).trim().toLowerCase();

  // Сначала точное совпадение
  if (GS.projects?.includes(label)) return label;

  // Затем без учёта регистра/пробелов
  for (const p of (GS.projects || [])){
    if (String(p).trim().toLowerCase() === cand) return p;
  }
  return null;
}

/** ========= Подстановка значений в селекты ========= **/
function populateSubprojectsForProject(projectLabel){
  const subSel  = document.getElementById("subproject");
  const taskSel = document.getElementById("subtask");

  subSel.innerHTML  = "";
  taskSel.innerHTML = "";

  subSel.appendChild(new Option(PLACEHOLDERS.subproject, ""));
  taskSel.appendChild(new Option("Сначала выберите подпроект", ""));

  if (!projectLabel) return;

  if (isBitrixProject(projectLabel)){
    // Битрикс: верхнеуровневые задачи (PARENT_ID = 0)
    const projectId = parseIdFromLabel(projectLabel);
    if (!projectId || !BITRIX_TASKS[projectId]) return;

    const allTasks = BITRIX_TASKS[projectId];
    const subprojects = allTasks.filter(t => getParentId(t) === 0);
    subprojects.forEach(s => {
      const label = toOptionText(s);
      subSel.appendChild(new Option(label, label));
    });
  } else {
    // Google Sheets: subprojects из таблицы
    const key = getGsProjectKey(projectLabel);
    const gsSubs = (GS.subprojects_map?.[key]) || [];
    // лог для быстрой проверки ключа
    dbg('GS subprojects for', projectLabel, ' -> key:', key, 'list:', gsSubs);
    gsSubs.forEach(sp => subSel.appendChild(new Option(sp, sp)));
  }
}

function populateTasksForSubproject(projectLabel, subprojectLabel){
  const taskSel = document.getElementById("subtask");
  taskSel.innerHTML = "";
  taskSel.appendChild(new Option(PLACEHOLDERS.subtask, ""));

  if (!projectLabel || !subprojectLabel) return;

  if (isBitrixProject(projectLabel)){
    // Битрикс: находим ID подпроекта и берём всех потомков
    const projectId = parseIdFromLabel(projectLabel);
    const subId     = parseIdFromLabel(subprojectLabel);
    if (!projectId || !subId || !BITRIX_TASKS[projectId]) return;

    const allTasks = BITRIX_TASKS[projectId];
    const tasks    = collectDescendants(allTasks, subId);
    tasks.forEach(t => {
      const label = toOptionText(t);
      taskSel.appendChild(new Option(label, label));
    });
  } else {
    // Google Sheets: tasks_map[project][subproject] (в колонке "subtasks")
    const key     = getGsProjectKey(projectLabel);
    const gsTasks = (GS.tasks_map?.[key]?.[subprojectLabel]) || [];
    dbg('GS tasks for', projectLabel, '/', subprojectLabel, ' -> key:', key, 'list:', gsTasks);
    gsTasks.forEach(t => taskSel.appendChild(new Option(t, t)));
  }
}

/** ========= Лоадер ========= **/
function showLoader(){
  document.body.classList.add('loading');
  const el = document.getElementById('pageLoader');
  if (el) el.classList.add('active');
}
function hideLoader(){
  document.body.classList.remove('loading');
  const el = document.getElementById('pageLoader');
  if (el) el.classList.remove('active');
}

/** ========= Загрузка/инициализация ========= **/
function debugTelegramWebApp(){
  const tg = window.Telegram?.WebApp;
  const debugEl = document.getElementById("debug");
  if (!tg) { dbg('Telegram WebApp недоступен'); return; }
  dbg('=== Telegram WebApp Debug ===', tg.version, tg.platform, tg.initDataUnsafe);
  if (debugEl){
    debugEl.textContent =
      `WebApp version: ${tg?.version || 'N/A'}\n` +
      `Platform: ${tg?.platform || 'N/A'}\n` +
      `initData: ${tg?.initData || 'empty'}\n` +
      `initDataUnsafe: ${JSON.stringify(tg?.initDataUnsafe || {}, null, 2)}\n`;
  }
}

async function loadFields(){
  showLoader();
  const tg = window.Telegram?.WebApp;
  const debugEl = document.getElementById("debug");

  try{
    // username
    let foundUsername = '';
    if (tg?.initDataUnsafe?.user?.username) foundUsername = tg.initDataUnsafe.user.username;
    if (!foundUsername && tg?.initData){
      try{
        const params = new URLSearchParams(tg.initData);
        const userParam = params.get('user');
        if (userParam){
          const userData = JSON.parse(decodeURIComponent(userParam));
          if (userData.username) foundUsername = userData.username;
        }
      }catch(e){}
    }
    if (!foundUsername){
      const urlParams = new URLSearchParams(window.location.search);
      foundUsername = urlParams.get('username') || '';
    }
    if (foundUsername){
      username = foundUsername;
      if (debugEl) debugEl.textContent += `\n✅ Username: ${username}`;
    } else {
      if (debugEl) debugEl.textContent += "\n❌ username недоступен\n⚠️ Будут показаны все проекты (без фильтрации)";
    }

    // fetch form-data
    const url = `${BASE_URL || ''}/form-data?username=${encodeURIComponent(username)}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
    const data = await res.json();

    // Сразу применяем пакеты (важно, чтобы GS был готов до работы UI)
    applyBitrixBundleFromFormData(data);

    // Быстрый лог того, что реально пришло
    console.log("GS received (from server):", {
      projects: GS.projects,
      subs_for_General: GS.subprojects_map?.["General"] || [],
      tasks_for_General_Admin: GS.tasks_map?.["General"]?.["Административные задачи"] || []
    });

    // карты
    positionMap = data.position_map || {};
    teamMap = data.team_map || {};

    // проекты (уже объединённый список: Битрикс + GS)
    fillUserProjects(data.projects || []);

    // справочники
    fillSelect("period", data.period || []);
    fillSelect("task", data.task || []);
    fillSelect("time_frame", data.time_frame || []);
    fillSelect("difficulty_level", data.difficulty_level || []);
    fillSelect("executor", data.executor || []);
    fillTimeOptions();

    // select2 — только у селектов с классом .select2
    document.querySelectorAll('select.select2').forEach(select => {
      const $select = window.jQuery(select);
      const $parent = $select.closest('.container');
      $select.select2({
        width: '100%',
        placeholder: 'Выберите...',
        allowClear: false,
        dropdownParent: $parent.length ? $parent : window.jQuery('body'),
        language: { noResults: () => "Ничего не найдено" }
      });
    });

    // автофилл
    autoFillUserFields(username, data);

    // бинды
    bindLocalHandlers();
  } catch(err){
    console.error('Ошибка загрузки полей:', err);
    if (debugEl) debugEl.textContent += `\n❌ Ошибка загрузки: ${err.message}`;
  } finally{
    hideLoader();
  }
}

/** ========= Обработчики ========= **/
function bindLocalHandlers(){
  // Проект → подгрузить подпроекты
  document.getElementById("projects").addEventListener("change", function(){
    populateSubprojectsForProject(this.value);
  });

  // Подпроект → подгрузить задачи
  document.getElementById("subproject").addEventListener("change", function(){
    const projectLabel = document.getElementById("projects").value;
    populateTasksForSubproject(projectLabel, this.value);
  });

  // При смене исполнителя — обновить позицию
  document.getElementById("executor").addEventListener("change", fillPosition);

  // Отправка
  document.getElementById("submitBtn").addEventListener("click", submitForm);
}

/** ========= Отправка ========= **/
function submitForm(){
  const data = {
    projects: document.getElementById("projects").value,
    subproject: document.getElementById("subproject").value,
    subtask: document.getElementById("subtask").value,
    period: document.getElementById("period").value,
    executor: document.getElementById("executor").value,
    position: document.getElementById("position").value,
    task: document.getElementById("task").value,
    time_frame: document.getElementById("time_frame").value,
    difficulty_level: document.getElementById("difficulty_level").value,
    time: document.getElementById("time").value,
    comment: document.getElementById("comment").value
  };
  dbg('Отправка данных:', data);
  Telegram.WebApp.sendData(JSON.stringify(data));
  Telegram.WebApp.close();
}

/** ========= Bootstrap ========= **/
document.addEventListener('DOMContentLoaded', () => {
  showLoader(); // сразу показываем пока всё инициализируется
  debugTelegramWebApp();

  if (window.Telegram?.WebApp){
    Telegram.WebApp.ready();
    Telegram.WebApp.expand();
    dbg('Telegram WebApp инициализирован');
    loadFields(); // внутри сам спрячет лоадер
  } else {
    console.error('Telegram WebApp не найден');
    alert('Пожалуйста, откройте эту страницу через Telegram бота');
    hideLoader();
  }
});
