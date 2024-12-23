from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import json
import uuid
import requests
from dotenv import load_dotenv
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
MAX_USER_INPUT_LENGTH = 2048
MAX_GPT_TOKENS = 300

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
def load_users() -> dict:
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users(data: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_chat_file(chat_id: str) -> str:
    return os.path.join(DATA_DIR, f"{chat_id}.json")

def get_current_user(request: Request) -> str:
    """ Проверяем токен в заголовке Authorization: Bearer <token> """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Токен не найден или неверный формат")
    
    token = auth_header.split(" ", 1)[1]
    if token not in TOKENS:
        raise HTTPException(status_code=401, detail="Неверный токен")

    return TOKENS[token]

# ----- Роуты -----

@app.get("/", response_class=HTMLResponse)
def root():
    """ При заходе на корень отдаём login.html (если есть). """
    login_page = os.path.join("static", "login.html")
    if os.path.exists(login_page):
        with open(login_page, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Login page not found</h1>"

@app.post("/login")
def login(data: LoginInput):
    """ 
    Проверка логина/пароля из users.json.
    Генерация токена и возврат {token, message}.
    """
    users = load_users()
    user_data = users.get(data.username)
    if not user_data or user_data["password"] != data.password:
        raise HTTPException(status_code=401, detail="Неверное имя пользователя или пароль")

    token = str(uuid.uuid4())
    TOKENS[token] = data.username  # сохраняем в памяти
    return {"token": token, "message": "Успешный вход"}

@app.get("/chat_page", response_class=HTMLResponse)
def chat_page():
    """ Отдаём chat.html (интерфейс чата) """
    chat_html = os.path.join("static", "chat.html")
    if os.path.exists(chat_html):
        with open(chat_html, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Chat page not found</h1>"

@app.get("/my_chats")
def get_user_chats(request: Request):
    """
    Возвращаем список чатов (id, title) текущего пользователя.
    """
    username = get_current_user(request)
    users = load_users()
    if username not in users:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    chats = users[username].get("chats", [])
    return {"chats": chats}

@app.post("/chat/new")
def create_new_chat(request: Request, data: NewChatInput):
    """
    Создаём новый чат для текущего пользователя.
    Генерируем chat_id, сохраняем в users.json в список chats.
    Создаём пустой data/<chat_id>.json с полем "history": [].
    """
    username = get_current_user(request)
    users = load_users()

    if username not in users:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    chat_id = str(uuid.uuid4())
    new_chat = {
        "id": chat_id,
        "title": data.title if data.title.strip() else "New chat"
    }

    # Добавляем этот чат в список чатов пользователя
    if "chats" not in users[username]:
        users[username]["chats"] = []
    users[username]["chats"].append(new_chat)
    save_users(users)

    # Создаём пустой JSON-файл для хранения истории
    chat_file = get_chat_file(chat_id)
    with open(chat_file, "w", encoding="utf-8") as f:
        json.dump({"chat_id": chat_id, "history": []}, f, ensure_ascii=False, indent=2)

    return {"chat_id": chat_id, "title": new_chat["title"]}

@app.get("/chat_history")
def get_chat_history(request: Request, chat_id: str, limit: int = 10, offset: int = 0):
    """
    Возвращаем часть истории чата (последние limit сообщений, сдвигаясь на offset).
    """
    username = get_current_user(request)

    # Проверяем, что chat_id принадлежит этому пользователю
    users = load_users()
    if username not in users:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    # Проверка, что chat_id есть в списке чатов
    user_chats = users[username].get("chats", [])
    if not any(c["id"] == chat_id for c in user_chats):
        raise HTTPException(status_code=403, detail="У вас нет доступа к этому чату")

    chat_file = get_chat_file(chat_id)
    if not os.path.exists(chat_file):
        # Если по какой-то причине отсутствует, создаём пустой
        with open(chat_file, "w", encoding="utf-8") as f:
            json.dump({"chat_id": chat_id, "history": []}, f)

    with open(chat_file, "r", encoding="utf-8") as f:
        chat_data = json.load(f)

    history = chat_data.get("history", [])
    total_messages = len(history)

    # Срез: offset=0 => последние limit, offset=10 => ещё более ранние и т.п.
    start = max(total_messages - offset - limit, 0)
    end = total_messages - offset
    messages_slice = history[start:end]

    return {
        "chat_id": chat_id,
        "title": next((c["title"] for c in user_chats if c["id"] == chat_id), "Untitled"),
        "total_messages": total_messages,
        "messages_returned": len(messages_slice),
        "messages": messages_slice
    }

@app.post("/chat_send")
def chat_send(request: Request, chat_id: str, msg: Message):
    """
    Отправляем сообщение в указанный чат. GPT отвечает. 
    Сохраняем оба сообщения в data/<chat_id>.json
    """
    username = get_current_user(request)
    if len(msg.message) > MAX_USER_INPUT_LENGTH:
        raise HTTPException(status_code=400, detail="Слишком длинное сообщение.")

    # Проверка, что chat_id принадлежит этому пользователю
    users = load_users()
    if username not in users:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    user_chats = users[username].get("chats", [])
    if not any(c["id"] == chat_id for c in user_chats):
        raise HTTPException(status_code=403, detail="Нет доступа к этому чату")

    chat_file = get_chat_file(chat_id)
    if not os.path.exists(chat_file):
        with open(chat_file, "w", encoding="utf-8") as f:
            json.dump({"chat_id": chat_id, "history": []}, f)

    with open(chat_file, "r", encoding="utf-8") as f:
        chat_data = json.load(f)

    # Добавляем сообщение пользователя
    chat_data["history"].append({"role": "user", "message": msg.message})

    # Берём последние 15 для контекста GPT
    context = chat_data["history"][-15:]

    # Запрос к GPT
    response = requests.post(
        GPT_API_URL,
        headers={
            "Authorization": f"Bearer {GPT_TEAM_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "model": "gpt-3.5-turbo",  # или "gpt-4", если у вас есть доступ
            "messages": [
                {"role": m["role"], "content": m["message"]} for m in context
            ],
            "max_tokens": MAX_GPT_TOKENS,
            "temperature": 0.7
        }
    )

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    assistant_message = response.json()["choices"][0]["message"]["content"]

    # Сохраняем ответ
    chat_data["history"].append({"role": "assistant", "message": assistant_message})

    # Перезаписываем историю
    with open(chat_file, "w", encoding="utf-8") as f:
        json.dump(chat_data, f, ensure_ascii=False, indent=2)

    return {"reply": assistant_message}

@app.delete("/chat/{chat_id}")
def delete_chat(request: Request, chat_id: str):
    """
    Удаляет указанный чат у текущего пользователя:
    1) Проверяем, что чат принадлежит этому пользователю
    2) Удаляем его из массива chats в users.json
    3) Удаляем файл data/<chat_id>.json
    """
    username = get_current_user(request)  # Проверка токена
    users = load_users()

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
    save_users(users)

    # Удаляем файл
    chat_file = get_chat_file(chat_id)
    if os.path.exists(chat_file):
        os.remove(chat_file)

    return {"detail": "Чат успешно удалён"}

