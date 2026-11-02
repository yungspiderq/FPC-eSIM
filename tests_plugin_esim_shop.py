"""
Функциональные тесты плагина esim_shop.py на моках.

Проверяют: метаданные и настройки, авторизацию v2 (HMAC), отказ работать в legacy
(eSIM доступен только в API v2), привязки лотов (variant_id/product_id), полный цикл
выдачи (DRY-RUN, покупка через /orders, опрос, выдача QR/LPA/ICCID/ссылок),
контроль остатка (stock=0 -> -301), лимит max_per_order, каталог eSIM с курсорной
пагинацией в Telegram-меню, денежные защиты, идемпотентность и удаление плагина.

Все запросы к DesslyHub и Telegram подменены заглушками - тест не тратит деньги и не
пишет в ваши чаты. Настройки создаются во временной папке: ваши реальные
storage/plugins/esim_shop/ НЕ затрагиваются.

Запуск из корня FunPayCardinal:
    cp tests_plugin_esim_shop.py /path/to/FunPayCardinal/
    cd /path/to/FunPayCardinal && pip install -r requirements.txt && python3 tests_plugin_esim_shop.py
"""

MODULE_NAME = "esim_shop"
TEST_TXN = "tx-1"

ESIM_RESULT = {"iccid": "890123456789", "matching_id": "MATCH-1",
               "qr_code_text": "LPA:1$smdp.example.com$MATCH-1",
               "smdp_address": "smdp.example.com", "status": "available",
               "created_at": "2026-10-24", "can_renew": False, "last_bundle": "",
               "android_universal_link": "https://android.link",
               "universal_link": "https://ios.link"}

ATTRIBUTES = {"esim_package_type": "data", "geo_scope": "TR", "continent": "",
              "country": "Turkey", "esim_countries": ["TR"]}

VARIANT = {"id": "var-1", "name": "Turkey 5GB", "description": "30 days", "image": "",
           "attributes": ATTRIBUTES}

VARIANT2 = {"id": "var-2", "name": "Europe 1GB", "description": "7 days", "image": "",
            "attributes": {"esim_package_type": "data", "geo_scope": "EU", "continent": "Europe",
                           "country": "", "esim_countries": ["DE", "FR"]}}

DETAILS = {"id": "var-1", "name": "Turkey 5GB", "description": "", "image": "",
           "attributes": ATTRIBUTES,
           "products": [{"id": "prod-1", "name": "5GB / 30 days", "description": "",
                         "price": 4.5, "stock": 7, "max_per_order": 3, "attributes": {}}]}

DETAILS_EMPTY = {"id": "var-2", "name": "Europe 1GB", "description": "", "image": "",
                 "attributes": VARIANT2["attributes"],
                 "products": [{"id": "prod-2", "name": "1GB / 7 days", "description": "",
                               "price": 2.0, "stock": 0, "max_per_order": 1, "attributes": {}}]}


def _orders_route(method="GET", path="/orders", json=None, data=None, params=None):
    if method == "POST":
        return {"order_id": 555, "status": "pending"}
    return {"items": [], "total": 0, "limit": 20, "offset": 0}


def _esim_catalog(method="GET", path="/catalog/esim/products", json=None, data=None, params=None):
    if params and params.get("cursor") == "cur2":
        return {"variants": [VARIANT2], "next_cursor": "", "prev_cursor": "cur1"}
    return {"variants": [VARIANT], "next_cursor": "cur2", "prev_cursor": ""}


DEFAULT_ROUTES = {
    # v2 (HMAC) - основной режим eSIM
    "/balance": {"balance": "50.0000", "available_balance": "48.0000", "overdraft": "0.0000",
                 "reserve": "2.0000"},
    "/orders": _orders_route,
    "/orders/555": {"order_id": 555, "order_status": "success", "service_type": "esim",
                    "payment_method": "balance", "commission": "0.10", "final_amount": "4.60",
                    "created_at": "2026-10-24 10:00:00", "updated_at": "2026-10-24 10:05:00",
                    "completed_at": None, "error_code": "", "payment_url": "",
                    "service_result": ESIM_RESULT},
    "/catalog/esim/products/var-1": {"data": DETAILS},
    "/catalog/esim/products/var-2": {"data": DETAILS_EMPTY},
    "/catalog/esim/products": _esim_catalog,
    "/currency/exchange_rates/steam/5": {"exchange_rate": 90.0},
    "/currency/exchange_rates/steam": {"exchange_rates": {"1": 1.0, "5": 90.0}},
    # legacy - для общих тестов клиента и поллера
    "/merchants/balance": {"balance": "50.0000"},
    "/merchants/transactions": {"transactions": []},
    "/merchants/transaction/tx-1/status": {"status": "success"},
    "/exchange_rate/steam/5": {"exchange_rate": 90.0},
    "/exchange_rate/steam": {"exchange_rates": {"1": 1.0, "5": 90.0}},
}

PENDING_ROUTES = {"/merchants/transaction/tx-1/status": {"status": "pending"}}

import hashlib
import hmac as hmac_lib
import importlib.util
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid as uuid_lib
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

_spec = importlib.util.spec_from_file_location(f"plugins.{MODULE_NAME}", f"plugins/{MODULE_NAME}.py")
plugin = importlib.util.module_from_spec(_spec)
sys.modules[f"plugins.{MODULE_NAME}"] = plugin
_spec.loader.exec_module(plugin)

import telebot.types as tt  # noqa: E402
from FunPayAPI import types  # noqa: E402
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


# ══════════════════════════════════════ МОКИ ══════════════════════════════════════
class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class FakeSession:
    """Заменяет requests.Session: отвечает заготовками и пишет все запросы в self.calls."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []
        self.fail_times = 0

    def request(self, method, url, headers=None, json=None, data=None, params=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "json": json, "data": data, "params": params, "timeout": timeout})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("connection reset by peer")
        path = url.split("/api/v1")[-1].split("?")[0]
        for key in sorted(self.routes, key=len, reverse=True):
            if path == key or path.startswith(key):
                value = self.routes[key]
                if callable(value):
                    value = value(method=method, path=path, json=json, data=data, params=params)
                if isinstance(value, tuple):
                    return FakeResponse(value[0], value[1])
                return FakeResponse(value)
        return FakeResponse({"error_code": -1}, 200)

    def posts(self, path=None):
        result = [i for i in self.calls if i["method"] == "POST"]
        if path:
            result = [i for i in result if path in i["url"]]
        return result

    def post_body(self, path):
        """Тело последнего POST на path (json-парс из data или json)."""
        calls = self.posts(path)
        if not calls:
            return None
        call = calls[-1]
        if call.get("data"):
            return json.loads(call["data"].decode("utf-8"))
        return call.get("json")


class FakeBot:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.answers = []
        self.deleted = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=len(self.messages), text=text)

    def reply_to(self, message, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=1), id=len(self.messages), text=text)

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edits.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=message_id, text=text)

    def answer_callback_query(self, callback_id, text=None, **kwargs):
        self.answers.append(text)

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeTelegram:
    def __init__(self):
        self.bot = FakeBot()
        self.authorized_users = {1: {}}
        self.notifications = []
        self.msg_handlers = []
        self.cbq_handlers = []
        self.user_states = {}
        self.commands = {}

    def send_notification(self, text, keyboard=None, notification_type="10", photo=None, pin=False):
        self.notifications.append((text, notification_type))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def add_command_to_menu(self, command, help_text):
        self.commands[command] = help_text

    def get_state(self, chat_id, user_id):
        try:
            return self.user_states[chat_id][user_id]
        except KeyError:
            return None

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states.setdefault(chat_id, {})[user_id] = {"state": state, "mid": message_id,
                                                             "data": data or {}}

    def clear_state(self, chat_id, user_id, del_msg=False):
        try:
            mid = self.user_states[chat_id][user_id].get("mid")
            del self.user_states[chat_id][user_id]
            if del_msg:
                self.bot.delete_message(chat_id, mid)
            return mid
        except KeyError:
            return None

    def check_state(self, chat_id, user_id, state):
        try:
            return self.user_states[chat_id][user_id]["state"] == state
        except KeyError:
            return False


class FakeAccount:
    def __init__(self):
        self.id = 111
        self.username = "seller"
        self.orders = {}
        self.chats = {}

    def get_order(self, order_id, **kwargs):
        return self.orders.get(str(order_id))

    def get_chat_by_name(self, name, make_request=False):
        return self.chats.get(name)


class FakeCardinal:
    def __init__(self):
        self.telegram = FakeTelegram()
        self.account = FakeAccount()
        self.blacklist = ["badguy"]
        self.run_id = 1
        self.sent = []
        self.registered_commands = []
        self.old_mode_enabled = False

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands.append((uuid, commands))

    def send_message(self, chat_id, message_text, chat_name=None, interlocutor_id=None, attempts=3,
                     watermark=True):
        self.sent.append({"chat_id": chat_id, "text": message_text, "chat_name": chat_name,
                          "watermark": watermark})
        return [SimpleNamespace(id=1, text=message_text, chat_id=chat_id)]


class SyncThread:
    """Заменяет threading.Thread: выполняет функцию сразу, синхронно (детерминированные тесты)."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.kwargs = target, args or (), kwargs or {}
        self.started = False

    def start(self):
        self.started = True
        if self.target:
            self.target(*self.args, **self.kwargs)

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


class FakeTgMessage:
    def __init__(self, text, chat_id=1, user_id=1):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.from_user = SimpleNamespace(id=user_id, username="tester", language_code="ru")
        self.id = 1
        self.content_type = "text"


class FakeChatMessage:
    """Сообщение покупателя в чате FunPay (для NewMessageEvent)."""

    def __init__(self, text, chat_id=888, author_id=999, by_bot=False):
        self.text = text
        self.chat_id = chat_id
        self.author_id = author_id
        self.by_bot = by_bot
        self.id = 1
        self.is_image = False
        self.is_audio = False

    def __str__(self):
        return self.text


def FakeCallback(data, chat_id=1, user_id=1):
    """Настоящий telebot.types.CallbackQuery - плагин проверяет тип через isinstance."""
    return tt.CallbackQuery.de_json({
        "id": f"cbq-{abs(hash(data)) % 10 ** 9}",
        "from": {"id": user_id, "is_bot": False, "first_name": "Tester", "username": "tester"},
        "chat_instance": "1",
        "data": data,
        "message": {"message_id": 5, "date": 1700000000,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": 111, "is_bot": True, "first_name": "Bot"},
                    "text": "/menu"},
    })


def make_order_full(order_id="900001", fields=None, price=1500.0, buyer="buyer1",
                    currency=types.Currency.RUB, status=types.OrderStatuses.PAID):
    """Настоящий объект FunPayAPI.types.Order с заполненными полями покупателя."""
    lot_fields = {}
    for index, (name, value) in enumerate((fields or {}).items()):
        key = f"f{index + 1}"
        lot_fields[key] = types.LotField(key, value, name)
    return types.Order(order_id, status, None, None, None, lot_fields, 1, price, currency, None,
                       999, buyer, 111, "seller", 888, None, [], "ru")


def make_order_short(order_id="900001", description="Тестовый лот", price=1500.0, buyer="buyer1",
                     status=types.OrderStatuses.PAID):
    import datetime as _dt
    return types.OrderShortcut(order_id, description, price, types.Currency.RUB, buyer, 999, 888,
                               status, _dt.datetime.now(), "Steam, Игры", None, "<html>")


def make_event(order_id="900001", description="Тестовый лот", lot_name=None, lot_id=None,
               price=1500.0, buyer="buyer1"):
    event = NewOrderEvent("tag", make_order_short(order_id, description, price, buyer))
    event.config_section_name = lot_name
    event.config_section_obj = None
    event.lot_id = lot_id
    event.lot_shortcut = None
    event.delivered = False
    return event


# ══════════════════════════════ ПЕСОЧНИЦА ══════════════════════════════
TMP = tempfile.mkdtemp(prefix=f"{MODULE_NAME}_tests_")


def sandbox(settings=None, routes=None, orders=None, sync_threads=True):
    """
    Готовит чистое окружение: временные файлы настроек, кардинал с моками,
    синхронные потоки и (опционально) маршруты DesslyHub.
    """
    plugin.PLUGIN_DIR = TMP
    plugin.SETTINGS_PATH = os.path.join(TMP, "settings.json")
    plugin.ORDERS_PATH = os.path.join(TMP, "orders.json")
    plugin.CACHE_PATH = os.path.join(TMP, "catalog.json")
    for path in (plugin.SETTINGS_PATH, plugin.ORDERS_PATH, plugin.CACHE_PATH):
        if os.path.exists(path):
            os.remove(path)
    plugin._settings = {}
    plugin._orders = {}
    plugin._last_low_balance_alert = 0.0
    plugin._thread_started = False
    for extra_global in ("_esim_pages", "_esim_variants"):
        if hasattr(plugin, extra_global):
            setattr(plugin, extra_global, [])
    if sync_threads:
        plugin.threading = SimpleNamespace(Thread=SyncThread)
    current = plugin.load_settings()
    current.update({"enabled": True, "dry_run": False, "apikey": "test-key", "secret": "test-sec",
                    "api_mode": "legacy", "poll_interval": 3})
    if settings:
        current.update(settings)
    plugin.save_settings(current)
    cardinal = FakeCardinal()
    session = FakeSession(DEFAULT_ROUTES.copy() if routes is None else routes)

    def _make_client(s=None):
        s = s or plugin.load_settings()
        return plugin.DesslyClient(s.get("apikey"), s.get("secret"), s.get("base_url"),
                                   s.get("api_mode"), 20, 0, session=session)

    plugin.make_client = _make_client
    for order_id, order in (orders or {"900001": make_order_full()}).items():
        cardinal.account.orders[order_id] = order
    return cardinal, session


def notifications(cardinal, needle=None):
    texts = [t for t, _ in cardinal.telegram.notifications]
    if needle is None:
        return texts
    return [t for t in texts if needle in t]


def sent_texts(cardinal, needle=None):
    texts = [i["text"] for i in cardinal.sent]
    if needle is None:
        return texts
    return [t for t in texts if needle in t]


# ══════════════════════════════ ОБЩИЕ ТЕСТЫ ══════════════════════════════
def test_metadata():
    check("NAME на английском", bool(plugin.NAME) and plugin.NAME.isascii())
    check("VERSION формата X.Y.Z", bool(re.fullmatch(r"\d+\.\d+\.\d+", plugin.VERSION)))
    check("CREDITS = yungspider", plugin.CREDITS == "yungspider")
    try:
        uuid_lib.UUID(plugin.UUID)
        valid_uuid = True
    except ValueError:
        valid_uuid = False
    check("UUID валиден", valid_uuid)
    check("SETTINGS_PAGE = True", plugin.SETTINGS_PAGE is True)
    doc = inspect.getdoc(plugin) or ""
    check("докстринг: Автор yungspider", "Автор: yungspider" in doc)
    check("докстринг: блок Возможности", "Возможности:" in doc)
    check("докстринг: FunPay Cardinal v0.1.17+", "v0.1.17+" in doc)
    check("DESCRIPTION на русском", any("а" <= ch <= "я" for ch in plugin.DESCRIPTION.lower()))
    check("BIND_TO_PRE_INIT есть", getattr(plugin, "BIND_TO_PRE_INIT", None))
    check("BIND_TO_POST_START есть", getattr(plugin, "BIND_TO_POST_START", None))
    check("BIND_TO_DELETE есть", getattr(plugin, "BIND_TO_DELETE", None) is not None)


def test_settings():
    plugin.PLUGIN_DIR = TMP
    plugin.SETTINGS_PATH = os.path.join(TMP, "settings.json")
    plugin.ORDERS_PATH = os.path.join(TMP, "orders.json")
    plugin.CACHE_PATH = os.path.join(TMP, "catalog.json")
    if os.path.exists(plugin.SETTINGS_PATH):
        os.remove(plugin.SETTINGS_PATH)
    plugin._settings = {}
    settings = plugin.load_settings()
    check("по умолчанию выключен", settings.get("enabled") is False)
    check("по умолчанию DRY-RUN", settings.get("dry_run") is True)
    check("файл настроек создан", os.path.exists(plugin.SETTINGS_PATH))
    raw = json.load(open(plugin.SETTINGS_PATH, encoding="utf-8"))
    check("api_mode по умолчанию auto", raw.get("api_mode") == "auto")
    # поврежденный файл -> дефолты
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write("not a json {{{")
    plugin._settings = {}
    settings = plugin.load_settings(force=True)
    check("битые настройки -> дефолты", settings.get("dry_run") is True)
    # приведение типов
    raw = dict(settings)
    raw["min_balance_usd"] = "7.5"
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    plugin._settings = {}
    settings = plugin.load_settings(force=True)
    check("строка -> float для min_balance_usd", settings.get("min_balance_usd") == 7.5)


def test_client_auth_legacy():
    c, session = sandbox(settings={"api_mode": "legacy", "secret": ""})
    client = plugin.make_client()
    balance = client.balance()
    check("legacy: баланс получен", balance.get("balance") is not None)
    call = session.calls[-1]
    check("legacy: заголовок apikey", call["headers"].get("apikey") == "test-key")
    check("legacy: путь /merchants/balance", "/merchants/balance" in call["url"])
    check("legacy: нет HMAC-заголовков", "X-Signature" not in call["headers"])


def test_client_auth_v2():
    c, session = sandbox(settings={"api_mode": "v2"})
    client = plugin.make_client()
    balance = client.balance()
    check("v2: баланс получен", balance.get("balance") is not None)
    call = session.calls[-1]
    check("v2: путь /balance", call["url"].endswith("/api/v1/balance"))
    check("v2: заголовок X-Api-Key", call["headers"].get("X-Api-Key") == "test-key")
    timestamp = call["headers"].get("X-Timestamp", "")
    expected = hmac_lib.new(b"test-sec", ("test-key" + timestamp).encode("utf-8"),
                            hashlib.sha256).hexdigest()
    check("v2: подпись HMAC для GET (пустое тело)", call["headers"].get("X-Signature") == expected)
    check("v2: timestamp числовой", timestamp.isdigit())


def test_client_auth_v2_post_signature():
    c, session = sandbox(settings={"api_mode": "v2"})
    client = plugin.make_client()
    client.create_order("steam_refill", {"username": "gamer", "amount": 5.0})
    call = session.posts("/orders")[-1]
    body = call["data"].decode("utf-8")
    timestamp = call["headers"]["X-Timestamp"]
    expected = hmac_lib.new(b"test-sec", ("test-key" + timestamp + body).encode("utf-8"),
                            hashlib.sha256).hexdigest()
    check("v2: подпись HMAC покрывает тело POST", call["headers"]["X-Signature"] == expected)
    parsed = json.loads(body)
    check("v2: тело POST компактное (separators)", body == json.dumps(parsed, ensure_ascii=False,
                                                                     separators=(",", ":")))
    check("v2: Content-Type json", call["headers"].get("Content-Type") == "application/json")


def test_resolve_mode():
    routes = DEFAULT_ROUTES.copy()
    c, session = sandbox(settings={"api_mode": "auto"}, routes=routes)
    client = plugin.make_client()
    check("auto: найден v2 (есть /balance)", client.resolve_mode() == "v2")
    routes_no_v2 = {k: v for k, v in DEFAULT_ROUTES.items() if not k.startswith("/balance")}
    c, session = sandbox(settings={"api_mode": "auto"}, routes=routes_no_v2)
    client = plugin.make_client()
    check("auto: откат на legacy", client.resolve_mode() == "legacy")
    c, session = sandbox(settings={"api_mode": "auto"}, routes={})
    client = plugin.make_client()
    check("auto: ничего не отвечает -> none", client.resolve_mode() == "none")
    try:
        client.require_mode()
        raised = False
    except plugin.DesslyError as e:
        raised = e.code == -5
    check("require_mode на none -> DesslyError(-5)", raised)
    c, session = sandbox(settings={"api_mode": "v2", "secret": ""})
    client = plugin.make_client()
    check("v2 без секрета -> none", client.resolve_mode() == "none")
    c, session = sandbox(settings={"api_mode": "legacy"})
    client = plugin.make_client()
    check("legacy игнорирует секрет", client.resolve_mode() == "legacy")


def test_client_errors_and_retries():
    c, session = sandbox(settings={"api_mode": "legacy"},
                         routes={"/merchants/balance": {"balance": "9.0"}})
    client = plugin.make_client()
    try:
        client.exchange_rate(5)   # маршрута нет -> дефолтный {"error_code": -1}
        got = None
    except plugin.DesslyError as e:
        got = e
    check("ошибка API -> DesslyError(-1)", got is not None and got.code == -1)
    session.fail_times = 2
    client_no_retry = plugin.DesslyClient("test-key", "", plugin.BASE_URL, "legacy", 20, 0,
                                          session=session)
    try:
        client_no_retry.balance()
        raised = False
    except plugin.DesslyError:
        raised = True
    check("сетевой сбой без ретраев -> ошибка", raised)
    session.fail_times = 2
    client_retry = plugin.DesslyClient("test-key", "", plugin.BASE_URL, "legacy", 20, 2,
                                       session=session)
    balance = client_retry.balance()
    check("сетевой сбой с ретраями -> успех", balance.get("balance") == 9.0)


def test_storage():
    c, _ = sandbox()
    plugin.set_record("1", status="done", lot="x")
    check("set_record/get_record", (plugin.get_record("1") or {}).get("status") == "done")
    for i in range(600):
        plugin.set_record(f"bulk-{i}", status="done")
    check("журнал подрезается", len(plugin.load_orders()) <= 510)
    plugin.save_cache("unit", [1, 2, 3])
    data, ts = plugin.load_cache("unit")
    check("кэш каталога работает", data == [1, 2, 3] and ts > 0)


def test_menu_ui():
    c, _ = sandbox()
    plugin.pre_init(c)
    registered = [cmd for _uuid, cmds in c.registered_commands for cmd, _t, _v in cmds]
    check(f"команда /{plugin.CMD} зарегистрирована", plugin.CMD in registered)
    plugin.on_cmd_command(FakeTgMessage("/" + plugin.CMD))
    check("меню открылось по команде", any(plugin.PLUG in m for m in c.telegram.bot.messages))
    unauthorized = FakeCallback(f"{plugin.CB}:menu", user_id=777)
    before = len(c.telegram.bot.messages)
    plugin.on_settings_callback(c, unauthorized)
    check("неавторизованный пользователь отбит", c.telegram.bot.answers and
          c.telegram.bot.answers[-1] == "Недостаточно прав.")
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:toggle_enabled"))
    check("toggle_enabled переключил", plugin.load_settings().get("enabled") is False)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:toggle_enabled"))
    check("toggle_enabled вернул", plugin.load_settings().get("enabled") is True)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:apimode"))
    check("apimode: legacy -> v2", plugin.load_settings().get("api_mode") == "v2")
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:apimode"))
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:apimode"))
    check("apimode: цикл v2 -> auto -> legacy", plugin.load_settings().get("api_mode") == "legacy")
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:close"))
    check("close закрывает меню", any("закрыто" in e for e in c.telegram.bot.edits))


def test_input_flow():
    c, _ = sandbox()
    plugin.pre_init(c)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:input:apikey"))
    check("ask_input выставил состояние",
          c.telegram.check_state(1, 1, plugin.INPUT_STATE))
    plugin.handle_input(FakeTgMessage("new-secret-key"))
    check("apikey сохранен", plugin.load_settings().get("apikey") == "new-secret-key")
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:input:min_balance_usd"))
    plugin.handle_input(FakeTgMessage("12.5"))
    check("min_balance_usd сохранен float", plugin.load_settings().get("min_balance_usd") == 12.5)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:input:min_balance_usd"))
    plugin.handle_input(FakeTgMessage("-3"))
    check("отрицательный порог отклонен", plugin.load_settings().get("min_balance_usd") == 12.5)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:input:apikey"))
    plugin.handle_input(FakeTgMessage("-"))
    check("ключ удален по '-'", plugin.load_settings().get("apikey") == "")


def test_delete():
    c, _ = sandbox()
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write("{}")
    plugin.on_delete(c, SimpleNamespace(id="call-1"))
    check("on_delete удалил папку плагина", not os.path.exists(plugin.PLUGIN_DIR))


def test_recover_interrupted():
    c, _ = sandbox()
    plugin.set_record("777", status="sending", lot="Лот")
    started = []

    def fake_thread(target=None, args=(), kwargs=None, daemon=None, name=None):
        def start():
            started.append(name)
            if name != f"{plugin.PLUG}Poller" and target:
                target(*(args or ()), **(kwargs or {}))
        return SimpleNamespace(start=start)

    plugin.threading = SimpleNamespace(Thread=fake_thread)
    plugin._thread_started = False
    plugin.post_start(c)
    record = plugin.get_record("777")
    check("прерванная отправка -> error", record.get("status") == "error")
    check("уведомление о прерванном заказе", bool(notifications(c, "оборвался")))
    check("поток опроса запущен", f"{plugin.PLUG}Poller" in started)


class RunIdController:
    """Кардинал, у которого run_id «меняется» после N обращений - так останавливается поллер."""

    def __init__(self, cardinal, allow=8):
        self.telegram = cardinal.telegram
        self.account = cardinal.account
        self.blacklist = cardinal.blacklist
        self.sent = cardinal.sent
        self.old_mode_enabled = False
        self._allow = allow
        self._calls = 0

    @property
    def run_id(self):
        self._calls += 1
        return 0 if self._calls <= self._allow else 1

    @run_id.setter
    def run_id(self, value):
        pass


def test_poller_timeouts():
    """Поллер: зависшая транзакция -> timeout; ожидание данных -> timeout_data."""
    routes = dict(DEFAULT_ROUTES)
    routes.update(PENDING_ROUTES)
    c, _ = sandbox(settings={"poll_max_minutes": 0, "wait_data_minutes": 0, "api_mode": "legacy"},
                   routes=routes)
    real_time = plugin.time
    plugin.time = SimpleNamespace(time=time.time, sleep=lambda s: None)
    try:
        now = int(time.time())
        plugin.set_record("t1", status="waiting_result", txn=TEST_TXN, sent_at=now - 120,
                          created=now - 120, chat_id=888, buyer="buyer1")
        plugin.set_record("t2", status="waiting_data", asked_at=now - 120, created=now - 120,
                          chat_id=888, buyer="buyer1")
        c.account.orders["t1"] = make_order_full("t1")
        plugin.poller_loop(RunIdController(c, allow=8), 0)
        check("поллер: зависшая транзакция -> timeout", plugin.get_record("t1").get("status") == "timeout")
        check("поллер: ожидание данных -> timeout_data",
              plugin.get_record("t2").get("status") == "timeout_data")
        check("поллер: уведомление о зависшей транзакции", bool(notifications(c, "не завершилась")))
    finally:
        plugin.time = real_time


def test_low_balance_notify():
    routes = dict(DEFAULT_ROUTES)
    routes["/merchants/balance"] = {"balance": "1.0000"}
    c, _ = sandbox(settings={"min_balance_usd": 5.0, "api_mode": "legacy"}, routes=routes)
    plugin.check_low_balance(c)
    check("низкий баланс -> уведомление", bool(notifications(c, "баланс DesslyHub опустился")))


# ══════════════════════════════ ТЕСТЫ ПЛАГИНА ══════════════════════════════
def add_mapping(name="eSIM Турция 5GB", variant_id="var-1", product_id="prod-1"):
    settings = plugin.load_settings()
    mappings = dict(settings.get("mappings") or {})
    mappings[name] = {"variant_id": variant_id, "product_id": product_id}
    settings["mappings"] = mappings
    plugin.save_settings(settings)


def test_mappings():
    c, _ = sandbox()
    plugin.apply_mapping("eSIM Турция 5GB | var-1 | prod-1")
    mappings = plugin.load_settings()["mappings"]
    check("привязка variant/product", mappings.get("eSIM Турция 5GB") ==
          {"variant_id": "var-1", "product_id": "prod-1"})
    try:
        plugin.apply_mapping("Лот | var-1")
        raised = False
    except plugin.PluginConfigError:
        raised = True
    check("привязка без product_id -> ошибка", raised)
    check("render_mapping", "var-1" in plugin.render_mapping("x", mappings["eSIM Турция 5GB"]))


def test_legacy_mode_rejected():
    c, session = sandbox(settings={"api_mode": "legacy"})
    add_mapping()
    plugin.set_record("900001", status="new", mapping={"variant_id": "var-1",
                                                       "product_id": "prod-1"},
                      buyer="buyer1", chat_id=888, funpay_price=1500.0, currency_code=5)
    plugin.start_order(c, "900001")
    record = plugin.get_record("900001")
    check("legacy -> failed(-5)", record.get("status") == "failed" and
          record.get("error_code") == -5)
    check("legacy: ошибка требует секретный ключ", "v2" in str(record.get("error_text")))
    check("legacy: заказов не создавалось", not session.posts("/orders"))


def test_v2_full_flow():
    c, session = sandbox(settings={"api_mode": "v2"})
    add_mapping()
    plugin.on_new_order(c, make_event(lot_name="eSIM Турция 5GB", price=1500.0))
    record = plugin.get_record("900001")
    check("v2: waiting_result", record.get("status") == "waiting_result")
    body = session.post_body("/orders")
    check("v2: тело /orders",
          body == {"payment_method": "balance", "service_type": "esim",
                   "service_params": {"product_id": "prod-1", "variant_id": "var-1"},
                   "reference": "funpay-900001"})
    plugin.poll_record(c, "900001")
    record = plugin.get_record("900001")
    check("v2: success -> done", record.get("status") == "done")
    check("v2: покупателю ушел QR/LPA", bool(sent_texts(c, "LPA:1$smdp.example.com$MATCH-1")))
    check("v2: покупателю ушел ICCID", bool(sent_texts(c, "890123456789")))
    check("v2: покупателю ушла iOS-ссылка", bool(sent_texts(c, "https://ios.link")))
    check("v2: покупателю ушла Android-ссылка", bool(sent_texts(c, "https://android.link")))
    check("v2: spent из final_amount", record.get("spent_usd") == 4.60)


def test_stock_zero():
    c, session = sandbox(settings={"api_mode": "v2"})
    add_mapping(variant_id="var-2", product_id="prod-2")
    plugin.on_new_order(c, make_event(lot_name="eSIM Турция 5GB", price=1500.0))
    record = plugin.get_record("900001")
    check("stock=0 -> failed(-301)", record.get("status") == "failed" and
          record.get("error_code") == -301)
    check("stock=0: заказов не создавалось", not session.posts("/orders"))


def test_max_per_order():
    c, session = sandbox(settings={"api_mode": "v2"})
    add_mapping()
    plugin.set_record("900002", status="new", mapping={"variant_id": "var-1",
                                                       "product_id": "prod-1"}, amount=5,
                      buyer="buyer1", chat_id=888, funpay_price=1500.0, currency_code=5)
    plugin.start_order(c, "900002")
    record = plugin.get_record("900002")
    check("amount > max_per_order -> failed(-3)", record.get("status") == "failed" and
          record.get("error_code") == -3)


def test_dry_run():
    c, session = sandbox(settings={"api_mode": "v2", "dry_run": True})
    add_mapping()
    plugin.on_new_order(c, make_event(lot_name="eSIM Турция 5GB", price=1500.0))
    record = plugin.get_record("900001")
    check("dry-run: статус dry_run", record.get("status") == "dry_run")
    check("dry-run: заказов не создавалось", not session.posts("/orders"))
    check("dry-run: уведомление", bool(notifications(c, "DRY-RUN")))


def test_guards():
    c, session = sandbox(settings={"api_mode": "v2", "max_order_cost_usd": 1.0})
    add_mapping()
    plugin.on_new_order(c, make_event(lot_name="eSIM Турция 5GB", price=1500.0))
    check("лимит стоимости -> failed(-3)",
          plugin.get_record("900001").get("error_code") == -3)


def test_idempotency():
    c, session = sandbox(settings={"api_mode": "v2"})
    add_mapping()
    event = make_event(lot_name="eSIM Турция 5GB", price=1500.0)
    plugin.on_new_order(c, event)
    plugin.on_new_order(c, event)
    check("ровно один заказ на заказ FunPay", len(session.posts("/orders")) == 1)


def test_catalog_ui():
    c, _ = sandbox(settings={"api_mode": "v2"})
    plugin.pre_init(c)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:catalog"))
    check("каталог: первая страница",
          any("Turkey 5GB" in m for m in c.telegram.bot.messages + c.telegram.bot.edits))
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:catpage:1"))
    check("каталог: вторая страница по курсору",
          any("Europe 1GB" in m for m in c.telegram.bot.messages + c.telegram.bot.edits))
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:catalog"))
    plugin.on_extra_callback(c, FakeCallback(f"{plugin.CB}:variant:0"), "variant:0")
    combined = c.telegram.bot.messages + c.telegram.bot.edits
    check("вариант: продукты показаны", any("prod-1" in m and "4.50" in m for m in combined))
    check("вариант: подсказка привязки", any("Привязка:" in m for m in combined))


def main():
    tests = [test_metadata, test_settings, test_client_auth_legacy, test_client_auth_v2,
             test_client_auth_v2_post_signature, test_resolve_mode, test_client_errors_and_retries,
             test_storage, test_menu_ui, test_input_flow, test_recover_interrupted,
             test_low_balance_notify, test_poller_timeouts,
             test_mappings, test_legacy_mode_rejected, test_v2_full_flow, test_stock_zero,
             test_max_per_order, test_dry_run, test_guards, test_idempotency,
             test_catalog_ui, test_delete]
    for test in tests:
        print(f"\n──── {test.__name__} ────")
        try:
            test()
        except Exception:
            import traceback
            traceback.print_exc()
            FAIL.append(f"{test.__name__}: EXCEPTION")
    print(f"\n{'=' * 60}")
    print(f"Результат: {len(PASS)} пройдено, {len(FAIL)} провалено")
    for name in FAIL:
        print("  ✗", name)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
