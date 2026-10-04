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

`data/raw/`, `data/processed/` и `data/runs/` игнорируются Git; `data/golden/` принимает только синтетические фикстуры. Manifest данных, архивы, обработанные тексты и примеры остаются на Mac по указанному пути и не отправляются в GitHub. Phase 2 добавляет только синтетический Benchmark V1 в Golden-каталог. Не подключать эти данные к production AGATA, PostgreSQL или обучению.

## Известные ограничения

- Лицензия исходного кода репозитория не обязательно распространяется на вложенный корпус. `unknown` или исследовательские условия требуют проверки OWNER перед любым дальнейшим использованием, особенно коммерческим.
- Классификация естественности/эмпатии/юмора эвристическая и предназначена для сортировки ручного просмотра, а не для оценки пригодности к обучению.
- У HF rows endpoint лимит страниц и доступность зависят от актуального состояния конкретного публичного dataset build. Недоступность фиксируется `PROCESS_BLOCKED`; данные не подменяются зеркалами.
- Для аудируемой версии каждая HF загрузка использует immutable commit SHA из HF API. Архивные источники не всегда публикуют версию; их SHA256 становится локальным идентификатором фактически полученного файла.

## Отчёты

`python3 .../lab.py report` обновляет `data/runs/report.json` и печатает статусы, объёмы, счётчики обработки и свободное место. `data/runs/manifest.json` — подробный локальный журнал происхождения. Ни один отчёт не создаёт GitHub Release и не запускает deployment.


## Phase 2 — безопасное исследование существующих данных

Дизайн и границы: experiments/phase2-spec.md; план: experiments/phase2-plan.md. В Phase 2 оригиналы неизменяемы. **Не запускайте команды V1 download/process/quality-review над существующим canonical data:** quality-review может перезаписать обработанные записи. Используйте новый read-only CLI:

```sh
python3 services/agata/conversation-lab/evaluators/cli.py verify
python3 services/agata/conversation-lab/evaluators/cli.py review --source daily-dialog --start 0 --count 5
python3 services/agata/conversation-lab/evaluators/cli.py benchmark
python3 services/agata/conversation-lab/evaluators/cli.py leakage --run-id phase2-my-unique-audit
python3 services/agata/conversation-lab/evaluators/cli.py blind --results /absolute/local/results.json --run-id phase2-my-unique-blind
python3 -m unittest discover -s services/agata/conversation-lab/tests
```

CLI по умолчанию читает canonical data на Mac, даже из изолированного worktree; --data-root допускает тестовую копию небольших фикстур. Не копировать реальные корпуса. Report output ограничен новой папкой data/runs/phase2-<id>, существующие файлы не заменяются. Review показывает до пяти полных диалогов за вызов; автоматическая маскировка контактов неполная и не заменяет локальную проверку персональных данных.

Benchmark: 128 независимых авторских сценариев, 16 категорий × 8; обычные — минимум четыре раунда, long_dialogue — двенадцать. Проверяются схема, роли, покрытие, точные/лексически близкие дубликаты и manifest/Persona hashes. Проверка утечки сканирует исходные обработанные записи потоково; её PASS не доказывает отсутствие смыслового заимствования. Future training/few-shot collision до появления такого набора — NOT_YET_APPLICABLE.

Протоколы: experiments/blind-evaluation.md и experiments/model-comparison.md; шкалы: configs/rubric-v1.json. Blind CLI только упаковывает импортированные результаты, не вызывает модели. Реальные ответы, статистика качества, OWNER-оценки и winner отсутствуют до отдельного разрешённого эксперимента.

180 ручных оценок и исследования хранятся в canonical data/runs/phase2-2026-10-04-audit/. Итоговая проверка утечки — phase2-2026-10-04-final-leakage/. Все исследовательские результаты и roadmap публикуются только в существующем документе Linear AGATA Conversation Lab. Эти локальные отчёты игнорируются Git; исходные тексты не коммитятся. Production, Core, G Sales, paid API и обучение не затронуты.

## Phase 3 — baseline audit и offline pilot preparation

Спецификация и план: `experiments/phase3-spec.md`, `experiments/phase3-plan.md`. Это подготовка, а не запуск сравнения: adapters только собирают документированные payloads и разбирают переданные fixtures; в коде нет provider transport, API-key lookup, Codex subprocess или enqueue в Mac worker. Без отдельного execution grant никакой network generation не выполняется.

Замороженный пилот: `configs/pilot-v1.json` выбирает существующий сценарий `-01` из каждой категории, сохраняет scenario/manifest/Persona/rubric/memory/context hashes и задаёт 72 ответа на модель (288 всего). Точные candidate model IDs и прайс-ссылки — в `configs/pilot-budget-v1.json`; budget guard fail-closed, пока baseline quota не проверена и полный лимит не зафиксирован. `$10` — лишь предложенный candidate-only stop ceiling, не разрешение тратить деньги.

Локальный офлайн-прогон (создаёт только новый каталог `data/runs/phase3-<id>`, не перезаписывает существующий):

```sh
python3 services/agata/conversation-lab/evaluators/pilot_cli.py dry-run --run-id phase3-local-20261004-rerun1
```

Вывод и reviewer packet имеют статус `FIXTURE_ONLY`; ответы — тестовые строки, а не результаты моделей. `ratings.py` требует lock до unblind, создаёт новый review cycle при исправлениях и считает описательные A–J показатели без объявления победителя. Платные API, Codex CLI benchmark jobs, worker, AGATA Core и Production запрещены в Phase 3.

## Phase 4A — Naturalness V1 offline preparation

Спецификация и пошаговый план: `experiments/naturalness-v1.md` и `experiments/naturalness-v1-plan.md`. Разработка политики использует только 16 замороженных Benchmark V1 сценариев `-02` и 15 синтетических авторских примеров. Отдельные 16 сценариев Phase 3 `-01` остаются независимым holdout: CLI ниже их не читает как материал для настройки и сообщает `holdout_scenarios_used: 0`.

Из корня GPoint запустите безопасную локальную проверку:

```sh
python3 services/agata/conversation-lab/evaluators/naturalness_cli.py dry-run
```

Команда проверяет SHA манифеста, формирует парные baseline / Naturalness V1 payloads по всем репликам `-02`, собирает анонимный fixture packet через существующий Phase 3 blind evaluator и печатает JSON-сводку с `status: FIXTURE_ONLY`, `live_inference_requests: 0`, числом candidate-turn fixtures и SHA packet/private mapping. Она не сохраняет файлы, не обращается к сети, не запускает модель, Codex CLI или worker и не читает Production. Реплики в blind packet — одинаковые placeholder-строки `модель не запускалась`, поэтому packet предназначен только для проверки структуры, порядка и анонимизации. Он не является A/B-результатом и не допускает вывода о качестве или победителе. Для этого потребуется отдельное решение OWNER и отдельный безопасный план генерации.

Подготовленная граница будущей интеграции: **SHARED policy** задаёт общий разговорный стиль и safety; **PER MODEL Persona** сохраняет голос и границы каждой модели/персонажа; **RELATIONSHIP context** передаёт существующую историю отношений; **MEMORY** содержит только уже доступные проверенные факты. Это описание архитектурного предложения: текущий offline workflow ничего не устанавливает в AGATA Core и не меняет API, память, модель, Mac worker или Production. Запуск реального A/B сравнения, изменение worker и интеграция в runtime требуют отдельного согласования.

После dry-run можно прогнать регрессии:

```sh
python3 -m unittest discover -s services/agata/conversation-lab/tests
```

## Prepared local shared-worker lane (not enabled)

`evaluators/worker_client.py` can build a synthetic job from the hash-checked
Naturalness V1 development scenarios (`-02`) and submit/read/cancel it through a
mode-0700 local spool consumed by the shared Mac worker. The spool has no HTTP
listener or production gateway route. It accepts only a bounded JSON contract,
requires unique job IDs, and writes mode-0600 local receipts. The worker executes
LAB through the same serial Codex executor as GPOINT/GSALES; it never writes LAB
responses to production PostgreSQL. Each candidate's history must be supplied
independently by its caller.

The worker ignores LAB unless `AGATA_LAB_WORKER_ENABLED=true`; it also requires
a separate mode-0600 OWNER approval file outside the repository, pinned to the
exact combined worker-plus-queue-helper SHA256, an allowlist of experiment/configuration/scenario IDs,
an expiry, and a one-to-eight-call budget. Approval usage is consumed before
inference. A claimed job left by a crash becomes `ambiguous` and is never
retried. These interfaces are prepared only: this task does not create an
approval file, enable the lane, start Codex, or access installed worker files.
