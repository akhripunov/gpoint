# AGATA Conversation Lab — Dataset Collection V1

## Conversation Lab V2 — Promptfoo-backed evaluation (offline implementation)

Promptfoo is the execution/evaluation backbone; the AGATA-owned coordinator remains the authority for scenario allowlists, arm parity, independent histories, privacy, blinding, sanity findings, OWNER locks, and deterministic decisions. V2 split membership and SHA256 commitments live in `configs/splits-v2.json`. The manifest intentionally contains no conversation text; `SEALED_HOLDOUT` plaintext and paths are forbidden, and compromised Benchmark V1 `-01` remains excluded.

Run the safe local preflight from the GPoint repository root:

```sh
python3 services/agata/conversation-lab/evaluators/naturalness_cli.py dry-run
```

This validates only the V2 split manifest. It does not open scenario payloads, contact a provider, execute Promptfoo, read legacy frozen-pilot files, write run artifacts, or access production. An empty DEV manifest reports `SPLITS_NOT_POPULATED`; it must not be presented as an A/B result. Generator calls, conversation arms, and judge calls are reported as separate zero counters.

The only supported live authentication path is the existing Codex/ChatGPT subscription session through the isolated Promptfoo runner. Effective API-key environment/config paths fail closed. The provider is pinned to `gpt-6-luna / medium`, with `maxRetries=0`, no fallback, no inherited process environment, and no API-key forwarding. Offline unit tests do not prove SDK/provider compatibility. Before any live invocation, require a separately reviewed bounded compatibility decision; no live compatibility call, `promptfoo optimize` campaign, paid API, optimizer execution, production prompt/runtime, database, VPS, Worker, GPOINT, or GSALES operation is authorized by this implementation.

For a separately owner-approved **direct Codex Local Naturalness experiment** when the Worker path is blocked, run the no-inference app-server preflight in the same normal macOS execution context intended for the experiment:

```sh
python3 services/agata/conversation-lab/evaluators/codex_app_server_preflight.py \
  --codex /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex \
  --working-dir /absolute/path/to/empty-isolated-dir \
  --timeout-seconds 10 --repeat 2
```

Both sequential checks must report `PASS`. The preflight verifies the existing ChatGPT subscription login and performs only the Codex app-server `initialize` handshake; `model_calls` remains zero. It rejects API-key environment/configuration paths, does not forward API-key variables, and never starts `codex exec`. A direct experiment still requires a separate bounded OWNER approval and does not demonstrate Promptfoo/Worker compatibility.

OWNER ratings for V2 use the exact eight-dimension V2 rubric and are write-once locked against both the exact public packet hash and rubric hash. The lock and subsequent unblind are persisted as separate mode-`0600` append-only events in a receipt-specific directory; unblind requires that receipt's persisted lock event. Private receipts are atomically created with mode `0600` and contain the durable call-ledger snapshot, journal hash, run hashes, seeded randomization, sanity findings, judge/aggregate evidence, and private mapping. The call ledger fsyncs each reservation before a provider invocation, uses a file lock across processes, and recovers prior attempts after interruption; ambiguous attempts stay charged and cannot be retried. OWNER packets omit evaluator-oracle notes and fail closed if any displayed scenario/transcript field leaks arm labels or technical metadata.

Candidate arm compilation requires the treatment text and SHA256 to match the committed `configs/naturalness-policy-v1.md` byte-for-byte. A receipt cannot accept caller-provided counter numbers or an in-memory ledger: it requires a verified durable ledger object and validates arm-to-scenario/configuration mapping, unique sequential turns, judge invocation receipts, and the separate 8/32/15 ceilings. These contracts are tested offline only; no provider compatibility or live evaluation has been demonstrated.

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
python3 services/agata/conversation-lab/tests/run_offline_safe_suite.py
```

The safe suite installs a Python audit guard before test discovery; opening the legacy `agata-benchmark-v1.jsonl` plaintext fails closed. The three historical tests that read that corpus are skipped during all current discovery runs. Do not invoke those tests manually; they remain excluded from this workstream.

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

## Phase 4A — Naturalness V1 offline preparation (historical)

This V1 procedure is retained as history. Current evaluation uses the guarded V2 path; compromised V1 holdout fixtures are excluded.

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

## Shared-worker LAB lane (permanent candidate contract)

`evaluators/worker_client.py` builds a synthetic job only from the hash-checked
Naturalness V1 development scenarios (`-02`) and submits/reads/cancels it through
a mode-0700 local spool consumed by the shared Mac worker. The installed worker
bundle includes a hash-pinned fixture manifest; it verifies each turn's canonical
request and user text. Later turns can use only completed local receipts from the
same scenario and A/B arm as history. Arbitrary chat text cannot enter through
the adapter or queue. The worker executes LAB through the same serial Codex
executor as GPOINT/GSALES; it never writes LAB responses to production PostgreSQL.

In the canonical candidate, LAB is permanently registered in the existing
single Worker Pool beside GPOINT and GSALES. Lane presence is independent from
authorization: without a valid mode-0600 OWNER approval file outside the
repository, LAB remains `AVAILABLE` with its inference gate closed. Approval is
pinned to the exact combined worker/queue/fixture-manifest SHA256, an allowlist
of experiment/configuration/scenario IDs, expiry, and a one-to-eight-call
budget. The Lab adapter checks the Worker-owned approval before reserving a
durable attempt; the Worker rechecks and consumes it immediately before
inference. No experiment toggles LAB presence or edits the LaunchAgent.

The shared scheduler checks configured GPOINT/GSALES queues first to preserve
production priority when their work is observable. A GPOINT or GSALES network
failure/backoff is recorded for that lane and does not block the later local LAB
claim; LAB remains pollable when both VPS paths are unavailable. With `N=1`, an
active inference keeps the shared provider slot until it finishes, so another
lane may wait but cannot overlap it. Ordinary Lab experiments submit only to the
canonical LAB spool and do not require a production queue `0/0` snapshot.
Fresh authoritative GPOINT/GSALES queue snapshots at 0/0, idle checks, locks,
plist preservation, and rollback remain mandatory for a full-pool bootstrap or
any operation that stops/replaces the shared supervisor or LaunchAgent. After
that bootstrap, a LAB-only slot update changes only the LAB runtime pointer;
it requires the local transition/release/slot locks but does not require
GPOINT/GSALES queue 0/0 and does not restart the LaunchAgent. See
`../worker/THREE_SLOT_BOOTSTRAP.md` for the exact transition
boundary.

The canonical candidate contract is not an installation record: this change
does not modify the installed Worker, LaunchAgent, production configuration,
create an approval file, or start Codex.

### Promptfoo → Worker V2 DEV and judge pipeline (offline implementation only)

The bounded V2 DEV path uses only the hash-committed `DEV` scenarios in
`configs/splits-v2.json` and the Worker-bundled
`services/agata/worker/lab_v2_dev_manifest.json`. For one approved scenario,
`run_v2_dev_scenario` runs four turns for Baseline and four for Naturalness V1
as eight separate local Promptfoo CLI evaluations. Each evaluation uses the
Python Worker provider, the canonical LAB spool, Slot 03, and a receipt-backed
history belonging only to its own arm. The Worker revalidates the fixed model,
Persona/context/memory/user-turn request hash and DEV membership before claim.
The compromised V1 corpus and `SEALED_HOLDOUT` are outside this path.

Approvals are sequential and separate. Phase A is an exact, expiring,
bundle-bound approval for one run/scenario and exactly eight generator calls.
Only after all eight Promptfoo results, Worker terminal receipts, and durable
ledger entries verify can Phase A close. Phase B requires a distinct approval
for the same run/scenario and exactly three independent judge calls. The Worker
rechecks the closed Phase A receipts before accepting judge work. The phase
state code does not create or activate approvals. Live inference remains
disabled until separately authorized approvals are created and the LAB runtime
is safely installed.

The offline integration test invokes the pinned Promptfoo 0.123.1 CLI against
the real Python provider and local Worker queue using only fake model output;
it exercises eight generator and three judge invocations followed by the
deterministic aggregator. This proves the offline orchestration contract only,
not live subscription authentication, Worker installation, or model
compatibility. The old one-turn `promptfoo-worker-compat` fixture below remains
a separate historical compatibility contract.

### Historical one-turn Promptfoo compatibility fixture (offline contract)

The technical compatibility fixture `promptfoo-worker-compat / promptfoo-compat-001`
is separately hash-pinned in `services/agata/worker/lab_promptfoo_compat_manifest.json`.
Promptfoo's Python provider is `evaluators/promptfoo_worker_provider.py`; it can
submit only to the existing LAB spool, and the Worker remains the only Codex
process manager and approval authority. The fixture is synthetic and independent
of DEV, VALIDATION, the compromised V1 corpus, and SEALED HOLDOUT.

The provider reserves its durable generator attempt before queue submission,
binds Promptfoo/run/attempt/Worker IDs, hashes the terminal Worker receipt, and
writes a private mode-0600 evidence record. A terminal Worker rejection proven
to occur before inference is `provider_error`; missing terminal evidence after
submission is `ambiguous`, and the job is never retried. Exact `gpt-6-luna / medium`
is enforced only on this compatibility LAB job. API-key environment/config,
Promptfoo retries, or fallback configuration fail closed. The native
`openai:codex-sdk` route is disabled in the runner.

This code change does not install or restart a Worker, create an approval file,
submit a LAB job, or run Promptfoo/model calls. The compatibility smoke remains
behind a separate OWNER decision.
