# AGATA Conversation Lab — Dataset Collection V1

Локальная лаборатория для сбора и первичной проверки корпусов диалогов. Корневая папка данных фиксирована:

`/Users/akhripuniov/Downloads/Codex/GPoint/services/agata/conversation-lab/data/`

На первом этапе загрузчик рассматривает только шесть `starter`-источников из [`configs/datasets.json`](configs/datasets.json): Russian Dialogues 2, Blended Skill Talk RU, DialogSum RU, EmpatheticDialogues, DailyDialog и Cornell Movie-Dialogs. Оценки объёма — ориентиры до загрузки: файлы и версии источников могут измениться. Фактические bytes, revision, URL каждого файла, SHA256, лицензия/условия и результаты этапов записываются локально в `data/runs/manifest.json`.

RLDD и Toloka помечены `extended` и не входят ни в `starter`, ни в `all`. CLI отвергает их до сетевого запроса; запуск extended потребует отдельного явного решения OWNER и изменения конфигурации/политики отдельной задачей. В этом V1 нет команды, обходящей этот запрет.

## Команды

Из корня GPoint:

```sh
python3 services/agata/conversation-lab/pipelines/lab.py list-sources
python3 services/agata/conversation-lab/pipelines/lab.py download starter
python3 services/agata/conversation-lab/pipelines/lab.py verify starter
python3 services/agata/conversation-lab/pipelines/lab.py process starter
python3 services/agata/conversation-lab/pipelines/lab.py quality-review starter
python3 services/agata/conversation-lab/pipelines/lab.py status
python3 services/agata/conversation-lab/pipelines/lab.py report
```

Можно указать один starter ID вместо `starter`. Скачивание последовательно возобновляет `.part` при HTTP Range, сохраняет исходные файлы, сверяет размер и SHA256, фиксирует версию Hugging Face, ограничивает повторы и отказывает при остатке места ниже 15 GiB. Архивы распаковываются только в выделенную папку после проверки путей; из них не исполняется код. Для HF Parquet JSON-нормализация использует публичный datasets-server rows endpoint, сохраняя исходный Parquet без изменений. Продолжение обработки опирается на локальный checkpoint/SQLite-набор уже встреченных записей.

`data/processed/<dataset>/dialogs.jsonl` содержит нормализованные разговоры с исходными split/метаданными. `quality-review starter` выбирает по 30 равномерно расположенных уникальных записей на корпус (либо все, если корпус меньше 30) и записывает ID, строку записи в `dialogs.jsonl`, длину, число реплик и структурные категории в соседний `quality-review.jsonl`. Полный разговор можно открыть по этой строке. Обобщённые счётчики лежат в `data/runs/quality-findings.json`. Это навигация для ручной оценки, а не автоматическое заключение о пригодности: проверяются пустые/короткие и длинные записи, повторы, теги, роли и возможные email/телефоны. Подозрение на персональные данные помечается; оригинал не меняется.

## Структура и хранение

`data/raw/`, `data/processed/` и `data/runs/` игнорируются Git; `data/golden/` принимает только синтетические фикстуры. Manifest данных, архивы, обработанные тексты и примеры остаются на Mac по указанному пути и не отправляются в GitHub. Golden-каталог пока пустой. Не подключать эти данные к production AGATA, PostgreSQL или обучению.

## Известные ограничения

- Лицензия исходного кода репозитория не обязательно распространяется на вложенный корпус. `unknown` или исследовательские условия требуют проверки OWNER перед любым дальнейшим использованием, особенно коммерческим.
- Классификация естественности/эмпатии/юмора эвристическая и предназначена для сортировки ручного просмотра, а не для оценки пригодности к обучению.
- У HF rows endpoint лимит страниц и доступность зависят от актуального состояния конкретного публичного dataset build. Недоступность фиксируется `PROCESS_BLOCKED`; данные не подменяются зеркалами.
- Для аудируемой версии каждая HF загрузка использует immutable commit SHA из HF API. Архивные источники не всегда публикуют версию; их SHA256 становится локальным идентификатором фактически полученного файла.

## Отчёты

`python3 .../lab.py report` обновляет `data/runs/report.json` и печатает статусы, объёмы, счётчики обработки и свободное место. `data/runs/manifest.json` — подробный локальный журнал происхождения. Ни один отчёт не создаёт GitHub Release и не запускает deployment.
