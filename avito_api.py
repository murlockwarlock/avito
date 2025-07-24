import requests
import logging
import openai
import google.generativeai as genai
import json
import time
import os

logger = logging.getLogger(__name__)
TOKEN_CACHE_FILE = 'avito_tokens.json'


def _load_token_cache():
    if not os.path.exists(TOKEN_CACHE_FILE):
        return {}
    try:
        with open(TOKEN_CACHE_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def _save_token_cache(cache):
    with open(TOKEN_CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)


def clear_token(client_id: str):
    logger.warning(f"Принудительная очистка кэша токена для ID {client_id}.")
    cache = _load_token_cache()
    if client_id in cache:
        del cache[client_id]
        _save_token_cache(cache)


def get_token(client_id, client_secret):
    cache = _load_token_cache()

    if client_id in cache and cache[client_id].get('expires_at', 0) > time.time():
        logger.info(f"Используется кэшированный токен для ID {client_id}")
        return cache[client_id]['access_token']

    logger.info(f"Запрашивается новый токен для ID {client_id}")
    url = "https://api.avito.ru/token/"
    data = {'grant_type': 'client_credentials', 'client_id': client_id, 'client_secret': client_secret}

    try:
        response = requests.post(url, data=data, timeout=10)
        response.raise_for_status()
        token_data = response.json()

        access_token = token_data.get('access_token')
        expires_in = token_data.get('expires_in', 3600)

        cache[client_id] = {
            'access_token': access_token,
            'expires_at': time.time() + expires_in - 60
        }
        _save_token_cache(cache)

        return access_token

    except requests.RequestException as e:
        logger.error(f"Ошибка токена Avito для ID {client_id}: {e}")
        return None


def get_chats(token, profile_id, limit=100, offset=0, unread_only=False):
    url = f"https://api.avito.ru/messenger/v2/accounts/{profile_id}/chats"
    headers = {'Authorization': f'Bearer {token}'}
    params = {'limit': limit, 'offset': offset}
    if unread_only:
        params['unread_only'] = 'true'

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        return response.json().get('chats', [])
    except requests.RequestException as e:
        logger.error(f"Ошибка при получении чатов для {profile_id}: {e}")
        # Возвращаем None в случае ошибки, чтобы внешний код мог ее обработать
        return None


def get_messages(token, profile_id, chat_id):
    url = f"https://api.avito.ru/messenger/v3/accounts/{profile_id}/chats/{chat_id}/messages"
    headers = {'Authorization': f'Bearer {token}'}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json().get('messages', [])
    except requests.RequestException as e:
        logger.error(f"Ошибка при получении сообщений для чата {chat_id}: {e}")
        return None


def send_message(token, profile_id, chat_id, message_text):
    if len(message_text) > 1990:
        logger.warning(f"Попытка отправить слишком длинное сообщение в чат {chat_id}. Усекаю.")
        message_text = message_text[:1990] + "..."

    url = f"https://api.avito.ru/messenger/v1/accounts/{profile_id}/chats/{chat_id}/messages"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {
        "message": {
            "text": message_text
        },
        "type": "text"
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Ошибка при отправке сообщения в чат {chat_id}: {e}")
        raise e


def get_chat_history(token, profile_id, chat_id, limit=10):
    messages = get_messages(token, profile_id, chat_id)
    if messages is None:
        return "Не удалось загрузить историю сообщений."
    history = ""
    for msg in sorted(messages, key=lambda x: x['created'])[-limit:]:
        prefix = "Клиент:" if msg['direction'] == 'in' else "Вы:"
        history += f"{prefix} {msg['content'].get('text', '')}\n"
    return history


async def generate_ai_reply(history, api_key, provider, prompt_text):
    prompt = f"{prompt_text}\n\nНиже представлена история переписки с клиентом на Avito. Последнее сообщение от клиента. Сгенерируй короткий, вежливый и релевантный ответ от лица продавца.\n\nИстория:\n{history}"
    try:
        if provider == 'openai':
            client = openai.AsyncOpenAI(api_key=api_key)
            completion = await client.chat.completions.create(messages=[{"role": "user", "content": prompt}],
                                                              model="gpt-4o")
            return completion.choices[0].message.content
        elif provider == 'gemini':
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            response = await model.generate_content_async(prompt)
            return response.text
        elif provider == 'deepseek':
            client = openai.AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            completion = await client.chat.completions.create(messages=[{"role": "user", "content": prompt}],
                                                              model="deepseek-chat")
            return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка генерации ответа через {provider}: {e}")
        return None


def subscribe_webhook(token, profile_id, webhook_url):
    url = f"https://api.avito.ru/messenger/v3/webhook"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {"url": webhook_url, "user_id": profile_id}
    response = requests.post(url, headers=headers, json=payload, timeout=15)
    response.raise_for_status()
    logger.info(f"Аккаунт {profile_id} успешно подписан на вебхук: {webhook_url}")
    return response.json()