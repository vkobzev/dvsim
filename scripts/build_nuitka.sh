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
ENTRY_POINT="${SRC_DIR}/dvsim/cli/run.py"

# Директория для результатов сборки
BIN_DIR="${PROJECT_ROOT}/bin"

# Имя выходного бинарника
OUTPUT_NAME="dvsim"

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

    # # Следовать за импортами (по умолчанию включено, но фиксируем явно)
    # "--follow-imports"

    # # Включить весь пакет dvsim целиком
    # "--include-package=dvsim"

    # # Plotly использует динамический импорт (_plotly_utils/importers.py через
    # # importlib.import_module), который Nuitka не может отследить статически.
    # # Без этого падает с ModuleNotFoundError: No module named 'plotly.graph_objs._bar'
    # "--include-package=plotly"
    # "--include-package-data=plotly"

    # Matplotlib тоже использует динамические импорты внутренне
    "--include-package=matplotlib"
    "--include-package-data=matplotlib"

    # Включить данные: шаблоны Jinja2 (reports, dashboard)
    "--include-data-dir=${SRC_DIR}/dvsim/templates=dvsim/templates"

    # Включить данные: статика (CSS, JS) для HTML-отчётов
    "--include-data-dir=${SRC_DIR}/dvsim/templates/static=dvsim/templates/static"

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

# Проверяем, установлен ли Nuitka
if ! python3 -c "import nuitka" 2>/dev/null; then
    echo "✗ Nuitka не установлен. Установка..."
    pip install nuitka
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
