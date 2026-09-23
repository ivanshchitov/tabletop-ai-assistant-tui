"""Загрузка конфигурации и переменных окружения."""

import os
import sys
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from dotenv import load_dotenv

# core/config.py -> подняться на уровень выше, к корню репозитория, где лежат
# .env, assets/ и history.json.
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_PATH)

# Базовый адрес переопределяется через окружение: это позволяет e2e-тестам поднять
# локальный stub-сервер вместо обращения к реальному OpenCode Zen.
DEFAULT_API_URL = "https://opencode.ai/zen/v1/chat/completions"
API_URL = os.getenv("OPENCODE_API_URL", DEFAULT_API_URL)
# Список моделей, которые пользователь может выбрать через /models; первая — модель
# по умолчанию. Панель и клиент читают отсюда, имена моделей не зашиваются нигде больше.
AVAILABLE_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "kimi-k2.5",
    "glm-5.1",
    "mimo-v2.5-free",
    "kimi-k2.6",
    "kimi-k3",
]
DEFAULT_MODEL = AVAILABLE_MODELS[0]
# Цена входных/выходных токенов (доллары за 1 млн токенов) по каталогу OpenCode Zen. Для моделей
# с разной ценой в пиковые/непиковые часы (DeepSeek V4 Flash/Pro) берётся более низкое (off-peak)
# значение — время суток на клиенте не отслеживается, точность не бухгалтерская, а сравнительная
# (см. openspec/changes/add-model-usage-metadata/design.md).
MODEL_PRICING = {
    "deepseek-v4-flash": (0.22, 0.66),
    "deepseek-v4-pro": (0.66, 1.98),
    "kimi-k2.5": (0.60, 3.00),
    "glm-5.1": (1.40, 4.40),
    "mimo-v2.5-free": (0.0, 0.0),
    "kimi-k2.6": (0.95, 4.00),
    "kimi-k3": (3.00, 15.00),
}
TEMPERATURE = 0.7
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0

MIN_MAX_WORDS = 10
MAX_MAX_WORDS = 1000
DEFAULT_MAX_WORDS = 200

# Пользователь ограничивает ответ в словах (инструкция в user-промпте — см.
# prompts.build_user_prompt), а не в токенах API. Но запрос к API всё равно принимает
# технический `max_tokens` — используем его как запас, чтобы генерация не обрывалась
# посреди слова/предложения раньше, чем модель сама уложится в лимит по инструкции.
# Не основной рычаг управления длиной, поэтому запас намеренно щедрый.
WORDS_TO_TOKENS_RATIO = 4
TOKENS_OVERHEAD = 50


def max_tokens_for_words(max_words: int) -> int:
    return max_words * WORDS_TO_TOKENS_RATIO + TOKENS_OVERHEAD

# Условие завершения ответа: если ответ — список/подборка (например,
# рекомендации настольных игр), модель должна ограничиться этим числом
# вариантов и сразу завершить ответ (см. prompts.build_user_prompt).
# Настраивается пользователем через экран /settings — см. answer_settings.AnswerSettings.
DEFAULT_LIST_LIMIT = 3
MIN_LIST_LIMIT = 1
MAX_LIST_LIMIT = 10

ASSETS_DIR = BASE_DIR / "assets"
# Потолок объёма резюме: дайджест — компактный (своя max_tokens-граница от этого объёма).
SUMMARY_MAX_WORDS = 150

# Стратегии управления контекстом сессии: что из прошлых обменов попадает в запрос. Первая —
# по умолчанию (сжатое резюме, поведение дня 9). Список и enum `answer_settings.ContextStrategy`
# держатся в согласии тестом: значения enum обязаны совпадать со списком.
CONTEXT_STRATEGIES = ["summary", "sliding_window", "sticky_facts", "branching"]
DEFAULT_CONTEXT_STRATEGY = CONTEXT_STRATEGIES[0]

# Блок фактов (стратегия фактов): потолок выхода извлекателя и предел числа ключей —
# вытесняются самые старые по времени появления, чтобы блок не рос бесконечно.
FACTS_MAX_WORDS = 120
MAX_FACTS_KEYS = 20

# Выбор инструмента MCP: сам ответ — один короткий JSON-объект, но модели пула тратят на выбор
# reasoning-токены внутри того же бюджета. Замеры на живом API (каталог из 22 инструментов):
# 818–1601 токен вывода на запрос выбора, поэтому потолок в 80 слов (370 токенов) иногда
# возвращал пустой content, и выбор читался как неразобранный ответ. 400 слов (1650 токенов)
# перекрывают наблюдённый разброс — та же причина, по которой MAX_MAX_WORDS подняли до 1000.
TOOL_CHOICE_MAX_WORDS = 400
# Автовыбор инструмента включён по умолчанию, но стоит одного вспомогательного запроса на
# вопрос. Переменная выключает его на весь запуск: так прогон не платит за выбор инструмента
# там, где проверяется не он, а пользователь может отказаться от автовызова насовсем, не
# повторяя `/tool auto off` каждую сессию.
AUTO_TOOLS = os.getenv("TABLETOP_AUTO_TOOLS", "1").strip().lower() not in {"0", "false", "no"}

# Память агента. Долговременная память (сведения о пользователе между сессиями) живёт в отдельном
# файле: /clear её не касается, поэтому хранить её в конверте истории нельзя. Путь выводится из
# расположения кода, как и путь истории, — иначе прогон (в том числе тестовый) писал бы в реальный
# файл репозитория. Предел длины записи ограничивает значение распознанного оборота: запись должна
# оставаться короткой, потому что она уходит в каждый запрос.
DEFAULT_MEMORY_FILE = BASE_DIR / "memory.json"
MEMORY_FILE = Path(os.getenv("TABLETOP_MEMORY_FILE", str(DEFAULT_MEMORY_FILE)))
MEMORY_VALUE_MAX_CHARS = 120

# Персонализация агента: профиль пользователя (предпочтения подачи и интересы) живёт своим файлом,
# потому что он не имеет отношения ни к диалогу, ни к выведенным правилами записям памяти: профиль
# настраивает сам пользователь, и он уходит в каждый запрос. Путь выводится из расположения кода —
# иначе прогон (в том числе тестовый) писал бы в реальный файл репозитория. Предел длины раздела
# ограничивает ответ диалога настройки: профиль уходит в каждый запрос, поэтому его объём ограничен.
DEFAULT_PROFILE_FILE = BASE_DIR / "profile.json"
PROFILE_FILE = Path(os.getenv("TABLETOP_PROFILE_FILE", str(DEFAULT_PROFILE_FILE)))
PROFILE_VALUE_MAX_CHARS = 400

# Состояние задачи агента (очередь задач и её конечный автомат) живёт своим файлом: задача — это
# не диалог и не сведения о пользователе, поэтому /clear её не касается. Путь выводится из
# расположения кода — иначе любой прогон, тестовый в том числе, писал бы в реальный файл
# репозитория. Предел длины цели ограничивает текст, который уходит в состояние и в сообщение
# состояния задачи в каждом диалоговом запросе.
# Инварианты агента проверяются кодом после ответа: нарушивший ответ возвращается модели с перечнем
# нарушений ровно столько раз, затем отклоняется. Второй повтор — та же вероятность нарушения
# за ещё одну цену.
INVARIANT_RETRIES = 1

DEFAULT_TASK_FILE = BASE_DIR / "task.json"
TASK_FILE = Path(os.getenv("TABLETOP_TASK_FILE", str(DEFAULT_TASK_FILE)))
TASK_GOAL_MAX_CHARS = 200

# Результат решённой задачи пишется отдельным markdown-файлом в свой каталог: состояние задачи —
# это её положение, а результат — то, что осталось у пользователя. Каталог выводится из
# расположения кода, как и остальные файлы состояния, и переопределяется для тестов.
DEFAULT_TASK_RESULTS_DIR = BASE_DIR / "tasks"
TASK_RESULTS_DIR = Path(os.getenv("TABLETOP_TASKS_DIR", str(DEFAULT_TASK_RESULTS_DIR)))
# Имя файла: номер задачи в очереди и слага из цели (латиницей, чтобы имя не зависело от
# нормализации юникода в файловой системе).
TASK_RESULT_SLUG_MAX_CHARS = 40

# Границы конвейера задачи. План подзадач, круги планирования и попытки проверки ограничены:
# иначе нестабильный ответ модели мог бы растянуть прогон, а проверки ходили бы по кругу.
# Объём одного запроса конвейера прижат — это вспомогательные запросы, как у суммаризатора и
# извлекателя фактов, а не ответ пользователю.
MIN_PLAN_ITEMS = 3
MAX_PLAN_ITEMS = 10
TASK_PLAN_ITEM_MAX_CHARS = 120
TASK_EDITS_MAX_CHARS = 500
MAX_PLAN_ROUNDS = 5
MAX_VALIDATION_ATTEMPTS = 3

# Длина журнала переходов задачи: он лежит в состоянии и уезжает в task.json, поэтому ограничен —
# отчёту нужен путь задачи и последние отклонённые попытки, а не вся её история до начала времён.
MAX_TRANSITION_LOG = 20

# Потолки объёмов запросов конвейера. Раздел артефакта — часть рабочего документа, а не статья:
# предел называется в запросе выполнения, иначе reasoning-модели пишут на тысячи токенов и
# конвейер растягивается на минуты. План и вердикт проверки — JSON, им нужен запас: обрезанный
# ответ не разбирается, и конвейер повторяет запрос впустую.
TASK_PLAN_MAX_WORDS = 200
TASK_SECTION_MAX_WORDS = 120
TASK_VALIDATE_MAX_WORDS = 400

# Порог сжатия контекста: сжимаем, когда неотжатых сообщений набирается столько.
# Значение-дефолт настройки сессии; переопределяется окружением для e2e (сжатие видно за секунды).
MIN_COMPRESS_AFTER = 5
MAX_COMPRESS_AFTER = 50
DEFAULT_COMPRESS_AFTER = int(os.getenv("TABLETOP_COMPRESS_AFTER", "10"))

# Потолок токенов собираемого запроса: глубину контекста сессии ограничивает не число записей
# (файл истории теперь неограничен), а оценка запроса против этого потолка — превышение
# вызывает внеочередное сжатие. Дефолт настройки сессии; переопределяется окружением для e2e.
MIN_MAX_SESSION_TOKENS = 5000
MAX_MAX_SESSION_TOKENS = 50000
DEFAULT_MAX_SESSION_TOKENS = int(os.getenv("TABLETOP_MAX_SESSION_TOKENS", "20000"))

# Путь к файлу истории переопределяется через окружение. Он выводится из __file__, а не из
# текущего каталога, поэтому без такого переключателя любой прогон приложения (в том числе
# тестовый) писал бы в единственный реальный history.json в корне репозитория.
DEFAULT_HISTORY_FILE = BASE_DIR / "history.json"
HISTORY_FILE = Path(os.getenv("TABLETOP_HISTORY_FILE", str(DEFAULT_HISTORY_FILE)))

# Планировщик заданий: отложенные и периодические вызовы инструментов собственного MCP-сервера.
# Задания, журнал их прогонов и накопленные ими данные живут своим файлом — их пишет серверный
# процесс, а приложение только читает. Путь выводится из расположения кода, как у прочих файлов
# состояния: без переключателя любой прогон (тестовый в том числе) писал бы в реальный
# schedule.json репозитория.
DEFAULT_SCHEDULE_FILE = BASE_DIR / "schedule.json"
SCHEDULE_FILE = Path(os.getenv("TABLETOP_SCHEDULE_FILE", str(DEFAULT_SCHEDULE_FILE)))

# Границы расписания. Период меньше минуты превратил бы фоновый исполнитель в непрерывный опрос
# внешнего API, период больше суток бессмыслен для сессии. Журнал прогонов ограничен по той же
# причине, что журнал переходов задачи: отчёту нужны последние прогоны, а не вся история.
# Накопленные заданием данные под ограничение не попадают — они и есть его результат.
MIN_EVERY_MINUTES = 1
MAX_EVERY_MINUTES = 1440
MIN_START_DELAY_MINUTES = 0
MAX_START_DELAY_MINUTES = 1440
MAX_RUN_LOG = 20

# Шаг фонового исполнителя расписания: он спит между попытками выполнить просроченные задания.
SCHEDULER_TICK_SECONDS = 60

# Таймаут переопределяется через окружение: e2e-сценарию с ретраями нужно, чтобы клиент
# сдавался за доли секунды, а не ждал полминуты на каждый намеренно зависший ответ.
REQUEST_TIMEOUT = int(os.getenv("TABLETOP_REQUEST_TIMEOUT", "90"))
MAX_RETRIES = 3

# Описания MCP-серверов. Сервер задаётся данными, а не кодом: имя, транспорт, команда запуска,
# её аргументы, имена переменных окружения с секретами сервера и описание. Замена сервера — одна
# запись реестра; ни имена инструментов, ни адрес источника данных нигде в коде не зашиты.
# Тот же приём, что у AVAILABLE_MODELS и MODEL_PRICING.
class MCPServerSpec(NamedTuple):
    name: str
    transport: str
    command: str
    args: Tuple[str, ...]
    env_keys: Tuple[str, ...]
    description: str


MCP_SERVERS = {
    "boardgamegeek": MCPServerSpec(
        name="boardgamegeek",
        transport="stdio",
        command="bgg-mcp",
        args=("-mode", "stdio"),
        env_keys=("BGG_API_KEY", "BGG_COOKIE", "BGG_USERNAME"),
        description="BoardGameGeek: поиск, детали, правила, рекомендации, коллекции, цены",
    ),
    "rulebooks": MCPServerSpec(
        name="rulebooks",
        transport="stdio",
        command="npx",
        args=("-y", "boardgame-rules-mcp"),
        env_keys=(),
        description="Рулбуки 1jour-1jeu: поиск правил игры, текст рулбука, структурированная сводка",
    ),
    "dnd-rules": MCPServerSpec(
        name="dnd-rules",
        transport="stdio",
        # Собственный сервер проекта — отдельный процесс, как и чужие записи реестра.
        # Запускается тем же интерпретатором, что и приложение: пакет `mcp` стоит только в нём,
        # а системный python3 на 3.9 его вовсе не поставит.
        command=sys.executable,
        args=(str(BASE_DIR / "mcp_server" / "dnd_server.py"),),
        # Планировщик сервера пишет свой файл, поэтому его путь тоже уходит в процесс:
        # без этого прогон (тестовый в том числе) писал бы в реальный schedule.json.
        env_keys=("TABLETOP_DND_API_URL", "TABLETOP_SCHEDULE_FILE"),
        description=(
            "Справочник D&D 5e: разделы, поиск, запись, сбор с накоплением; "
            "планировщик отложенных и периодических вызовов"
        ),
    ),
    "rule-disputes": MCPServerSpec(
        name="rule-disputes",
        transport="stdio",
        command="npx",
        args=("-y", "@mohitagw15856/rulebook", "mcp"),
        env_keys=(),
        description="Спорные правила офлайн: официальное против домашнего, факты об играх, план вечера",
    ),
}
DEFAULT_MCP_SERVER = "boardgamegeek"
# Запись реестра, чьи инструменты ставит и выполняет планировщик: собственный сервер проекта.
# Фоновый исполнитель расписания обращается прямо к ней, а не к разрешённому реестру, — иначе
# переопределение TABLETOP_MCP_COMMAND (подмена серверов приложения) увело бы и его.
SCHEDULER_SERVER = "dnd-rules"

# Имя описания, подставляемое при переопределении команды через окружение: сервер подменён
# целиком, поэтому и имя записи реестра к нему уже не относится.
MCP_OVERRIDE_NAME = "переопределён окружением"


def mcp_servers() -> Tuple[MCPServerSpec, ...]:
    """Серверы, к которым подключается сессия, в порядке реестра.

    Переменные TABLETOP_MCP_COMMAND/TABLETOP_MCP_ARGS заменяют **весь** реестр единственной
    записью: приложение обходит реестр при запуске, поэтому без такой замены e2e-прогон поднимал
    бы все настоящие серверы из сети — как без TABLETOP_HISTORY_FILE он писал бы в реальный
    history.json.
    """
    command = os.getenv("TABLETOP_MCP_COMMAND")
    if not command:
        return tuple(MCP_SERVERS[name] for name in MCP_SERVERS)
    args = tuple(os.getenv("TABLETOP_MCP_ARGS", "").split())
    override = MCP_SERVERS[DEFAULT_MCP_SERVER]._replace(
        name=MCP_OVERRIDE_NAME,
        command=command,
        args=args,
        env_keys=(),
        description="сервер задан переменными окружения",
    )
    return (override,)


def mcp_server_spec() -> MCPServerSpec:
    """Первый сервер разрешённого реестра — одиночный случай `mcp_servers()`."""
    return mcp_servers()[0]


MAX_INPUT_LENGTH = 2000

ESTIMATED_CHARS_PER_TOKEN = 3  # приближение для клиентской оценки токенов (кириллица, см. core/usage.py)

def get_api_key() -> Optional[str]:
    return os.getenv("OPENCODE_API_KEY")


def set_api_key_runtime(api_key: str) -> None:
    os.environ["OPENCODE_API_KEY"] = api_key
