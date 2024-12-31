import os
import json
import uuid
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import aiofiles
import httpx

load_dotenv()

# Папка для хранения историй
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# Файл с данными пользователей
USERS_FILE = "users.json"
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)

# Словарь "токен → username" (в памяти)
TOKENS = {}

# OpenAI
GPT_TEAM_TOKEN = os.getenv("GPT_TEAM_TOKEN")
if not GPT_TEAM_TOKEN:
    raise ValueError("Не найден GPT_TEAM_TOKEN в переменных окружения")
GPT_API_URL = "https://api.openai.com/v1/chat/completions"

# Ограничения
MAX_USER_INPUT_LENGTH = 4096
MAX_GPT_TOKENS = 4096

# Инициализация приложения
app = FastAPI()

# Подключаем статику
app.mount("/static", StaticFiles(directory="static"), name="static")


# ----- Модели -----
class Message(BaseModel):
    role: str
    message: str

class LoginInput(BaseModel):
    username: str
    password: str

class NewChatInput(BaseModel):
    title: str


# ----- Утилиты -----
async def load_users() -> dict:
    """Асинхронно загружаем файл с пользователями."""
    async with aiofiles.open(USERS_FILE, "r", encoding="utf-8") as f:
        content = await f.read()
    return json.loads(content)

async def save_users(data: dict):
    """Асинхронно сохраняем файл с пользователями."""
    async with aiofiles.open(USERS_FILE, "w", encoding="utf-8") as f:
        # Запись JSON в строку, потом записываем в файл
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))

def get_chat_file(chat_id: str) -> str:
    """Возвращает путь к файлу, в котором хранится история чата."""
    return os.path.join(DATA_DIR, f"{chat_id}.json")

def get_current_user(request: Request) -> str:
    """
    Синхронная проверка токена в заголовке Authorization: Bearer <token>.
    Если токен корректен — возвращаем username. Иначе — выбрасываем 401.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Токен не найден или неверный формат")

    token = auth_header.split(" ", 1)[1]
    if token not in TOKENS:
        raise HTTPException(status_code=401, detail="Неверный токен")

    return TOKENS[token]


# ----- Роуты -----

@app.get("/", response_class=HTMLResponse)
async def root():
    """При заходе на корень отдаём login.html (если есть)."""
    login_page = os.path.join("static", "login.html")
    if os.path.exists(login_page):
        async with aiofiles.open(login_page, "r", encoding="utf-8") as f:
            return await f.read()
    return "<h1>Login page not found</h1>"


@app.post("/login")
async def login(data: LoginInput):
    """
    Проверка логина/пароля из users.json.
    Генерация токена и возврат {token, message}.
    """
    users = await load_users()
    user_data = users.get(data.username)
    if not user_data or user_data["password"] != data.password:
        raise HTTPException(status_code=401, detail="Неверное имя пользователя или пароль")

    token = str(uuid.uuid4())
    TOKENS[token] = data.username  # сохраняем в памяти
    return {"token": token, "message": "Успешный вход"}


@app.get("/chat_page", response_class=HTMLResponse)
async def chat_page():
    """Отдаём chat.html (интерфейс чата)."""
    chat_html = os.path.join("static", "chat.html")
    if os.path.exists(chat_html):
        async with aiofiles.open(chat_html, "r", encoding="utf-8") as f:
            return await f.read()
    return "<h1>Chat page not found</h1>"


@app.get("/my_chats")
async def get_user_chats(request: Request):
    """
    Возвращаем список чатов (id, title) текущего пользователя.
    """
    username = get_current_user(request)
    users = await load_users()
    if username not in users:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    chats = users[username].get("chats", [])
    return {"chats": chats}


@app.post("/chat/new")
async def create_new_chat(request: Request, data: NewChatInput):
    """
    Создаём новый чат для текущего пользователя.
    Генерируем chat_id, сохраняем в users.json в список chats.
    Создаём пустой data/<chat_id>.json с полем "history": [].
    """
    username = get_current_user(request)
    users = await load_users()

    if username not in users:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    chat_id = str(uuid.uuid4())
    new_chat = {
        "id": chat_id,
        "title": data.title.strip() if data.title.strip() else "New chat"
    }

    # Добавляем этот чат в список чатов пользователя
    if "chats" not in users[username]:
        users[username]["chats"] = []
    users[username]["chats"].append(new_chat)
    await save_users(users)

    # Создаём пустой JSON-файл для хранения истории
    chat_file = get_chat_file(chat_id)
    async with aiofiles.open(chat_file, "w", encoding="utf-8") as f:
        await f.write(json.dumps({"chat_id": chat_id, "history": []}, ensure_ascii=False, indent=2))

    return {"chat_id": chat_id, "title": new_chat["title"]}


@app.get("/chat_history")
async def get_chat_history(request: Request, chat_id: str, limit: int = 10, offset: int = 0):
    """
    Возвращаем часть истории чата (последние limit сообщений, сдвигаясь на offset).
    """
    username = get_current_user(request)

    users = await load_users()
    if username not in users:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    # Проверяем, что chat_id принадлежит этому пользователю
    user_chats = users[username].get("chats", [])
    if not any(c["id"] == chat_id for c in user_chats):
        raise HTTPException(status_code=403, detail="У вас нет доступа к этому чату")

    chat_file = get_chat_file(chat_id)
    if not os.path.exists(chat_file):
        # Если по какой-то причине отсутствует, создаём пустой
        async with aiofiles.open(chat_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps({"chat_id": chat_id, "history": []}))

    async with aiofiles.open(chat_file, "r", encoding="utf-8") as f:
        content = await f.read()
    chat_data = json.loads(content)

    history = chat_data.get("history", [])
    total_messages = len(history)

    # Срез: offset=0 => последние limit, offset=10 => ещё более ранние и т.п.
    start = max(total_messages - offset - limit, 0)
    end = total_messages - offset
    messages_slice = history[start:end]

    # Ищем title из списка чатов
    chat_title = next((c["title"] for c in user_chats if c["id"] == chat_id), "Untitled")

    return {
        "chat_id": chat_id,
        "title": chat_title,
        "total_messages": total_messages,
        "messages_returned": len(messages_slice),
        "messages": messages_slice
    }


@app.post("/chat_send")
async def chat_send(request: Request, chat_id: str, msg: Message):
    """
    Отправляем сообщение в указанный чат. GPT отвечает.
    Сохраняем оба сообщения в data/<chat_id>.json
    """
    username = get_current_user(request)

    if len(msg.message) > MAX_USER_INPUT_LENGTH:
        raise HTTPException(status_code=400, detail="Слишком длинное сообщение.")

    users = await load_users()
    if username not in users:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    user_chats = users[username].get("chats", [])
    if not any(c["id"] == chat_id for c in user_chats):
        raise HTTPException(status_code=403, detail="Нет доступа к этому чату")

    chat_file = get_chat_file(chat_id)
    if not os.path.exists(chat_file):
        async with aiofiles.open(chat_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps({"chat_id": chat_id, "history": []}))

    # Читаем историю
    async with aiofiles.open(chat_file, "r", encoding="utf-8") as f:
        content = await f.read()
    chat_data = json.loads(content)

    # Добавляем сообщение пользователя
    chat_data["history"].append({"role": "user", "message": msg.message})

    # Берём последние 15 сообщений для контекста GPT
    context = chat_data["history"][-15:]

    # Асинхронный запрос к GPT
    async with httpx.AsyncClient() as client:
        response = await client.post(
            GPT_API_URL,
            headers={
                "Authorization": f"Bearer {GPT_TEAM_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4",  # или "gpt-3.5-turbo"
                "messages": [
                    {"role": m["role"], "content": m["message"]} for m in context
                ],
                "max_tokens": MAX_GPT_TOKENS,
                "temperature": 0.7
            },
            timeout=60  # при необходимости увеличить таймаут
        )

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    assistant_message = response.json()["choices"][0]["message"]["content"]

    # Сохраняем ответ
    chat_data["history"].append({"role": "assistant", "message": assistant_message})

    # Перезаписываем историю
    async with aiofiles.open(chat_file, "w", encoding="utf-8") as f:
        await f.write(json.dumps(chat_data, ensure_ascii=False, indent=2))

    return {"reply": assistant_message}


@app.delete("/chat/{chat_id}")
async def delete_chat(request: Request, chat_id: str):
    """
    Удаляет указанный чат у текущего пользователя:
    1) Проверяем, что чат принадлежит этому пользователю
    2) Удаляем его из массива chats в users.json
    3) Удаляем файл data/<chat_id>.json
    """
    username = get_current_user(request)
    users = await load_users()

    if username not in users:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    user_chats = users[username].get("chats", [])

    # Ищем чат среди user_chats
    chat_index = None
    for i, c in enumerate(user_chats):
        if c["id"] == chat_id:
            chat_index = i
            break

    if chat_index is None:
        raise HTTPException(status_code=404, detail="Чат не найден или у вас нет доступа")

    # Удаляем чат из списка
    user_chats.pop(chat_index)
    users[username]["chats"] = user_chats
    await save_users(users)

    # Удаляем файл
    chat_file = get_chat_file(chat_id)
    if os.path.exists(chat_file):
        os.remove(chat_file)

    return {"detail": "Чат успешно удалён"}


# ------------------- НОВЫЙ РОУТ ДЛЯ ПЕРЕИМЕНОВАНИЯ -------------------
@app.put("/chat/{chat_id}")
async def rename_chat(request: Request, chat_id: str, data: NewChatInput):
    """
    Переименовать указанный чат (title).
    - Проверяем, что чат принадлежит этому пользователю
    - Обновляем поле title в users.json
    - (При желании можно обновить в файле истории)
    """
    username = get_current_user(request)
    users = await load_users()

    if username not in users:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    user_chats = users[username].get("chats", [])
    chat_obj = next((c for c in user_chats if c["id"] == chat_id), None)
    if not chat_obj:
        raise HTTPException(status_code=404, detail="Чат не найден или у вас нет доступа")

    new_title = data.title.strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="Название чата не может быть пустым")

    # Обновляем название в users.json
    chat_obj["title"] = new_title
    await save_users(users)

    # При желании обновляем title в файле чата
    chat_file = get_chat_file(chat_id)
    if os.path.exists(chat_file):
        async with aiofiles.open(chat_file, "r", encoding="utf-8") as f:
            content = await f.read()
        chat_data = json.loads(content)

        # Сохраняем новое название
        chat_data["title"] = new_title

        async with aiofiles.open(chat_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps(chat_data, ensure_ascii=False, indent=2))

    return {"detail": "Чат успешно переименован", "chat_id": chat_id, "new_title": new_title}
