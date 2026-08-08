#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# build_nuitka.sh — Сборка dvsim в standalone-бинарник через Nuitka
# =============================================================================

# --- Константы --------------------------------------------------------------

# Корень проекта (директория, где лежит pyproject.toml).
# Скрипт лежит в scripts/, поэтому берём родительскую директорию.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Источник пакета
SRC_DIR="${PROJECT_ROOT}/src"

# Точка входа (entry point)
ENTRY_POINT="${SRC_DIR}/dvsim/cli/admin.py"

# Директория для результатов сборки
BIN_DIR="${PROJECT_ROOT}/bin"

# Имя выходного бинарника
OUTPUT_NAME="dvsim-admin"

# --- Базовые опции Nuitka ---------------------------------------------------

NUITKA_FLAGS=(
    # Режим onefile — всё в одном исполняемом файле
    "--onefile"

    # Куда складывать результат
    "--output-dir=${BIN_DIR}"

    # Имя выходного файла
    "--output-filename=${OUTPUT_NAME}"

    # Отключить флаг deployment (избегает предупреждений при запуске)
    "--no-deployment-flag=self-execution"

    # Следовать за импортами (по умолчанию включено, но фиксируем явно)
    "--follow-imports"

    # Включить весь пакет dvsim целиком (все .py модули)
    "--include-package=dvsim"

    # Включить ВСЕ файлы данных внутри пакета dvsim (шаблоны, css, js, hjson и т.д.)
    # Это покрывает dvsim/templates/reports, dvsim/templates/dashboard,
    # dvsim/templates/static/css, dvsim/templates/static/js и любые другие данные.
    "--include-package-data=dvsim"

    # templates/static содержит CSS/JS файлы, которые читаются через
    # Path(__file__).parent / "static" — включаем все данные пакета dvsim.
    "--include-package=dvsim.templates.static"

    # --- Сторонние библиотеки с динамическими импортами ---------------------
    # Эти библиотеки используют importlib.import_module() или похожие приёмы,
    # которые Nuitka не может отследить статически. Включаем целиком.

    # Plotly: _plotly_utils/importers.py → ModuleNotFoundError: plotly.graph_objs._bar
    "--include-package=plotly"
    "--include-package-data=plotly"

    # Matplotlib: динамически грузит backend'ы и модули
    "--include-package=matplotlib"
    "--include-package-data=matplotlib"

    # Jinja2 / MarkupSafe: загрузчики шаблонов с ленивым импортом
    "--include-package=jinja2"
    "--include-package-data=jinja2"

    # Pydantic: внутренняя кодогенерация и ленивые импорты
    "--include-package=pydantic"
    "--include-package=pydantic_core"

    # Hjson: файлы данных
    "--include-package=hjson"
    "--include-package-data=hjson"

    # Tabulate, Plotly subdeps и т.д. — включаем данные пакетов
    "--include-package-data=tabulate"
    "--include-package-data=anybadge"

    # Подавить предупреждения об unused-модулях
    "--remove-output"

    # Показывать прогресс компиляции
    "--show-progress"

    # Включить инфо-вывод
    "--show-scons"
)

# --- Проверка зависимостей ---------------------------------------------------

check_dependency() {
    if ! command -v "$1" &>/dev/null; then
        echo "✗ '$1' не найден. Установите и повторите." >&2
        exit 1
    fi
}

check_dependency "python3"

# Проверяем, установлен ли Nuitka с поддержкой onefile (нужен zstandard для сжатия)
if ! python3 -c "import nuitka, zstandard" 2>/dev/null; then
    echo "✗ Nuitka или zstandard не установлены. Установка..."
    pip install "nuitka[onefile]"
fi

# --- Сборка ------------------------------------------------------------------

echo "=========================================="
echo "  Сборка ${OUTPUT_NAME} через Nuitka"
echo "=========================================="
echo "  Проект:   ${PROJECT_ROOT}"
echo "  Точка входа: ${ENTRY_POINT}"
echo "  Результат:   ${BIN_DIR}/${OUTPUT_NAME}"
echo "=========================================="
echo ""

# Создаём директорию для результата
mkdir -p "$BIN_DIR"

# Запуск Nuitka
python3 -m nuitka "${NUITKA_FLAGS[@]}" "$ENTRY_POINT"

# --- Пост-обработка ---------------------------------------------------------

# Права на исполнение
chmod 775 "${BIN_DIR}/${OUTPUT_NAME}"

# Очистка временных директорий Nuitka
echo ""
echo "Очистка временных файлов..."
rm -rf "${BIN_DIR}/${OUTPUT_NAME}.onefile-build"
rm -rf "${BIN_DIR}/${OUTPUT_NAME}.build"
rm -rf "${BIN_DIR}/${OUTPUT_NAME}.dist"

echo ""
echo "✓ Сборка завершена: ${BIN_DIR}/${OUTPUT_NAME}"
