# Как выпускать GPoint

## Два режима разрешения выпуска

Актуальные условия: [Release authorization modes](../RELEASE_AUTHORIZATION.md).
По умолчанию владелец подтверждает подготовленный exact SHA. Конкретное
поручение владельца «исправь [задачу] и выкатывай» разрешает один
`TASK_SCOPED_AUTO` release без повторного подтверждения. До реализации создаётся
durable authorization record; controller проверяет baseline, полный scope,
одноразовость, eligibility и все release gates до передачи существующего
exact-SHA approval input. Release infrastructure и другие owner-only категории
в этот режим не входят. Runner может штатно ждать в GitHub `Queued`; первый
этап запущенного self-hosted job выполняет fail-closed readiness check.
Подробности: [Release authorization modes](../RELEASE_AUTHORIZATION.md).

## Для владельца

1. Архитектор задаёт изменения и критерии приёмки. Dev реализует их и отправляет чистый commit в `origin/main`.
2. Откройте [GPoint — Prepare](https://github.com/akhripunov/gpoint/actions/workflows/gpoint-prepare.yml). GitHub проверит exact SHA и автоматически запустит preflight после успешной публикации immutable bundle. Отдельный controller использует существующий GPoint self-hosted runner, атомарный claim предотвращает повторный preflight для одной пары SHA/run/attempt; он не запускает повторный CI или image build.
3. В [Архитектор — Handoff](https://linear.app/bestuzhev/document/arhitektor-handoff-36f91afb3d2c) смотрите блок **Автоматизация: кандидат** и автоматическую запись `READY_FOR_OWNER`. Она появляется только после успешного read-only preflight и опубликованного immutable evidence. Это ещё не production release.
4. Для Exact-SHA режима один раз запустите существующий [GPoint — Deploy](https://github.com/akhripunov/gpoint/actions/workflows/gpoint-deploy.yml) с полным SHA и явным production approval. Selector требует самый свежий успешный immutable `READY_FOR_OWNER` preflight того же SHA и Prepare run/attempt; без него production workflow блокируется.
5. После approval существующий canonical workflow продолжает Deploy, verification, evidence и Linear publication автоматически. Mac должен быть включён, иметь сеть, активную CLI authentication и работающий GPoint runner. Incy остаётся включённым. Если runner недоступен, run останется Queued и не считается завершённым.
6. После технического COMPLETE откройте Telegram → GPoint → Models Hub. Отправьте новое сообщение и дождитесь ответа. Сообщите Dev «ответ получил» или «ответа нет». Dev фиксирует подтверждение сценария в журнале; автоматизация не создаёт тестовые production-диалоги от вашего имени.

История: [Журнал релизов](https://linear.app/bestuzhev/document/zhurnal-relizov-640efa60dacf). Каждый результат содержит SHA и ссылку на GitHub run. В Handoff история и архитектурные ограничения сохраняются, автоматизация меняет только свои именованные блоки.

Можно пользоваться через Dev: «Подготовь релиз и дай SHA со ссылкой на проверки», затем «Разрешаю выкатить SHA …». Dev обязан проверить успешный Prepare, использовать его артефакты и canonical entrypoint. Кнопка Deploy удобнее тем, что фиксирует оператора и запускает стандартные шаги без пересказа команд.

Режим **preflight** проверяет доступ, live baseline, worker/config и артефакты без применения миграций и переключения runtime. Он запускается автоматически после Prepare. Результат `PLANNED` плюс immutable evidence переводится в `READY_FOR_OWNER`; production не изменяется.

Runner telemetry считает wall-clock seconds отдельно для GitHub-hosted и self-hosted jobs (это не billable-minute отчёт) и фиксирует cache HIT/MISS. Автоматический controller выполняется на существующем GPoint Mac runner, отдельный hosted controller job не добавлен. Prepare tests и image build не дублируются.

## Значение состояний

| Состояние | Значение и следующее действие |
| --- | --- |
| READY | Этот SHA прошёл проверки, Docker/БД репетицию; владелец может разрешить выпуск |
| FAILED в Prepare | Production не изменён; Dev исправляет первый упавший шаг |
| Queued в Deploy | Исполнитель занят/выключен/не подключён; проверять Settings → Actions → Runners |
| PLANNED | Preflight завершён; production не переключался |
| COMPLETE в manifest | VPS runtime, server health, parity, миграции и production ref технически проверены; доступность Mac Worker записывается отдельно |
| OWNER_SMOKE_PENDING | Ответ в Telegram ещё не подтверждён после этого релиза |
| FAILED_PRE_SWITCH | Переключение не началось; Dev сверяет baseline перед новой попыткой |
| RECOVERY_REQUIRED | Могли измениться БД/AGATA/runtime; сначала read-only reconciliation, потом отдельное восстановление |
| Красный Publish Linear / bookkeeping | Не считать приложение автоматически упавшим. Сверить canonical manifest, live runtime и результат выкладки; восстановить публикацию/запись отдельно |

Новый SHA требует нового Prepare; в Exact-SHA режиме — нового разрешения. В Task-scoped режиме применяются ограничения Release authorization modes. Устаревший кандидат, проваленный/незавершённый последний Prepare, отсутствующий/истёкший artifact или неверный checksum блокируют Deploy. Артефакты хранятся семь дней: после истечения повторно запустите Prepare для текущего main. Автоматический rollback БД не выполняется.

## Что выполняется автоматически

Prepare запускает frozen-lockfile install, сборку, typecheck, unit/release tests, тесты Linear, затем PostgreSQL/pgvector в отдельной Docker-сети. Существующие интеграционные fixtures создают tenants/roles под test administrator; проверка RLS использует отдельную роль без bypass, а внутренний HTTP dialogue дополнительно выполняется под NOSUPERUSER/NOBYPASSRLS. Отдельная БД проверяет RLS migration; legacy backfill также выполняется test administrator. Проверяются свежая установка и legacy migration/backfill, внутренний HTTP dialogue с детерминированным provider, реальная Prisma/Alembic migration из production Docker images, повторный Alembic upgrade, AGATA readiness и Mini App SHA. Тестовые базы имеют суффикс `_agata_test` и удаляются вместе с временными контейнерами; production PostgreSQL не используется.

Сборка выдаёт Linux/amd64 images, exact revision labels, image IDs, архив SHA256 и READY receipt. Deploy проверяет успешный GitHub run текущего SHA, receipt/run ID, checksum, загружает Docker images на VPS и сверяет IDs. Повторная сборка приложения в режиме CI bundle запрещена. Canonical tooling сохраняет live baseline, проверяет config/provider, делает GPoint-only backup при миграциях, переключает runtime, выполняет health/parity, сохраняет immutable production ref, manifest и cleanup. Safety detector остаётся отдельным артефактом с source hash и canonical build; он не включён в архив приложения.

Receipt `images.*.id` — точный SHA256 конфигурации, полученный в classic Docker
build store. Prepare импортирует единственный сохранённый архив в отдельный
Docker 29.1.3/containerd/overlayfs daemon с собственными socket/data/exec roots.
Те же production command builders выполняют import → capture native IDs → parity
→ reuse; созданные без запуска и без сети контейнеры проверяют runtime `.Image`.
Отрицательная проверка подставляет config digest вместо native proof и обязана
завершиться отказом для обоих образов. Обязательный gate `deployRehearsal=PASS`
и измеренное время репетиции сохраняются в receipt. Старый receipt без gate
не разрешает apply. Эта репетиция не проверяет SSH/SCP, production env или
пользовательский Telegram reply. В containerd store VPS `.Id`
обозначает OCI manifest, поэтому verifier доказывает связь native manifest →
receipt config: хеширует оба content blob, проверяет exact config digest,
linux/amd64, revision label и rootfs diff IDs. Неизвестный descriptor, OCI index,
отсутствующий content blob или любое расхождение блокируют import/reuse.
Это сравнение одной content identity в разных stores, а не замена exact ID
проверкой только source label. Native runtime IDs сохраняются для parity.

GitHub credentials, VPS key и production env не входят в архив и не сохраняются в Linear. Логи публикации не содержат текст пользовательских сообщений и raw production environment. Secret `LINEAR_API_KEY` предоставляется только шагу публикации; его отсутствие явно ломает публикацию, а не выдаёт ложный READY.

## Первичная настройка Dev/владельцем

1. Убедиться, что GitHub Actions включён для приватного репозитория. Проверить Settings → Secrets and variables → Actions: repository secret `LINEAR_API_KEY` должен иметь доступ к двум существующим документам. Сам key в GitHub/чат/Linear не публиковать. Старый workflow AGENTS использует тот же secret, но его наличие и работоспособность нужно подтвердить успешной публикацией.
2. На Mac в Settings → Actions → Runners → New self-hosted runner выбрать macOS и архитектуру `uname -m`. Скачать официальный runner, проверить SHA256 по команде GitHub. Установить вне репозитория, например `~/Library/Application Support/GPoint/release-runner`. При регистрации задать рабочую папку без пробелов: `--work "$HOME/.local/share/gpoint-release-work"`. Runner хранит временные shell scripts внутри рабочей папки; пробел в стандартном `Application Support/.../_work` ломает их запуск. На текущем Mac рабочая папка уже настроена в `~/.local/share/gpoint-release-work`.
3. Перед подключением владелец подтверждает доступ: runner выполняет команды доверенного workflow GPoint на Mac от его пользователя, имеющего VPS key и локальную CLI authentication. Это не runner для PR из неизвестных веток. Никогда не назначать ему PR workflows; доступ к push/main и изменению workflows ограничить доверенными участниками.
4. Зарегистрировать repo-scoped runner для `https://github.com/akhripunov/gpoint`, имя `gpoint-owner-mac`, label `gpoint-production`. Временный registration token из GitHub не сохранять в Git/Linear/логи. Установить пользовательский сервис официальным `svc.sh install` / `svc.sh start` согласно инструкции GitHub. Предварительный API опрос runner не нужен: GitHub `Queued` — штатное ожидание. Первый этап запущенной job проверяет runner identity/labels, macOS/ARM64, Docker Linux runtime, disk, cache/runtime paths, обязательные tools/network и отсутствие конфликтующей production mutation до загрузки bundle и дорогих шагов. На Mac разрешить sleep policy по решению владельца: отключённый/спящий Mac не может выполнять релиз или отвечать через mac_worker.
5. PATH сервиса должен содержать рабочие `node`, `pnpm` и `python3`, а git — читать подтверждённые known_hosts. Проверить отдельные GPoint SSH/SCP runners, key/env, Mac worker status. Не менять Incy/DNS/hosts. Сравнить установленный `gpoint_mac_worker.py` с исходником кандидата; при изменении worker Dev обновляет его опубликованным исходником по штатному installer перед релизом. Автоматизация должна блокироваться при несовпадении.
6. Первый запуск Prepare обязан быть зелёным, включая Linear. Выпуск нового application candidate разрешается владельцем отдельно; установка автоматизации не является разрешением произвольного production apply.

## Зависание self-hosted job при финализации

В run `38769710148` все шаги Mac controller и post-checkout завершились успешно; job оставалась в `Complete job`. Локальный runner diagnostic зафиксировал повторные таймауты загрузки ResultsLog step logs в GitHub Actions/Azure Blob во время финализации, затем завершение cancellation window и статус run `cancelled`. Это подтверждает сбой пути загрузки/финализации для того запуска, но не устанавливает инициатора отмены и не доказывает общий отказ сети или самого controller.

При таком состоянии сохраняйте штатную загрузку логов и artifacts. Не объявляйте job успешной по результату шага dispatch: итог `cancelled`/`failure` остаётся неуспешным. Сначала по точным `run_id` и `run_attempt` сверьте атомарную controller claim и созданный дочерний preflight; повторно используйте найденный child-run только после проверки его exact SHA и receipt. Не запускайте duplicate controller/preflight, пока reconciliation не доказала, что прежний dispatch не создал пригодного child-run. Не выключайте ResultsLog upload и не перезапускайте Mac runner во время активной работы. Сетевую доступность Actions Results/Blob исследуйте отдельно в разрешённом runner-контексте; изменение глобальной сети не является способом обхода этого состояния.

## Команды для Dev

Каноническая ручная диагностика сохраняется:

```sh
pnpm release:gpoint:status
pnpm release:gpoint --dry-run --candidate <40-char-sha>
```

Автоматический job использует `scripts/automated-release.mjs` с bundle вне checkout. Apply не запускать по имени ветки, latest или произвольному локальному receipt: workflow сначала подтверждает provenance через GitHub API. Общий lock на Mac: `~/.config/gpoint/release-production.lock`, вне checkout runner; старый `.gpoint-release.lock` также блокирует запуск до явной reconciliation. Не снимать stale lock молча и не повторять mutation после RECOVERY_REQUIRED.

Release bookkeeping хранится в отдельной ветке `gpoint-release-ledger`, а не в `main` приложения. COMPLETE, RECOVERY_REQUIRED и неизвестные исходы сохраняют state и всю историю manifests в этой ветке. Push выполняется без force; конфликт или ошибка публикации видны как failure. Production tags, result/manifests artifacts и Linear сохраняются независимо. Безопасные BLOCKED/FAILED_PRE_SWITCH остаются только в artifacts/Linear.

Перед dry-run/apply canonical CLI и workflow читают проверенный remote ledger в отдельный каталог вне checkout. Состояние recovery и drift gates остаются обязательными. При первом внедрении ledger отдельно инициализируется последним подтверждённым baseline из main. Отсутствие/удаление ledger после этого блокирует выпуск; автоматического возврата к старому state из main нет. Внутри workflow state и manifests доступны через GPOINT_RELEASE_EVIDENCE и сохраняются artifact job. Запись нового релиза больше не меняет SHA приложения и не обесценивает Prepare.

Один тестовый запуск обоих проектов с автоматическим ожиданием, выбором exact SHA, Prepare при отсутствии, сохранением полных логов и отчёта:

```sh
python3 scripts/release-preflight-benchmark.py --owner-start '<ISO timestamp with timezone>' --output '<new evidence directory>' --maxibot-migration 0098_durable_autopilot_execution_claim_v1 --prepare-missing
```

Координатор выполняет только read-only preflight, проверяет exact receipt и неизменность main; По умолчанию production apply не выполняется. Для mode production отдельно требуются --approved-gpoint и --approved-maxibot с явно разрешёнными exact SHA; несовпадение с текущим main блокирует запуск. Runtime изменяют только канонические workflows, не сам координатор. Время от команды владельца до завершения отчёта и время самого координатора записываются отдельно. Неудачный последний Prepare не обходится и не заменяется более старым успешным. Полные workflow logs, run/job boundaries, receipts и checksum списка файлов собираются без ручного копирования. Измерение не включает VPS upload/import/runtime switch.

Для проверки самого конвейера до merge разрешён ручной Prepare dispatch из
ветки: выполняются только hosted Linux jobs без production secrets, Mac/VPS
операций и публикации READY в Linear. Branch artifact не подходит Deploy;
после merge требуется успешный Prepare exact current main.


## Бесплатное хранение сборок и evidence

Для чистого benchmark Release Orchestrator V2 на новом main candidate отдельно
фиксируйте push/commit, Prepare, automatic controller, read-only Deploy
preflight и публикацию `READY_FOR_OWNER` в Linear. Считайте jobs и их runner
seconds по GitHub API; записывайте cache hit/miss и повторные build/test/download.
Валидный прогон проходит одной попыткой без ручного preflight/recovery и без
production apply.

GPoint публикует CI bundle и документы Deploy в immutable GitHub Releases,
чтобы расход Actions Artifact Storage не блокировал выпуск. Не требует billing
или платного бюджета. На репозитории включён immutable-releases. Prepare создаёт
новый lightweight tag на exact SHA `gpoint-prepare-<sha>-<run>-<attempt>`,
загружает `bundle.zip` в draft и только затем публикует. ZIP содержит прежние
`images.tar.gz` и `receipt.json`; receipt дополнительно закрепляет run attempt.
Deploy использует отдельный `gpoint-evidence-<sha>-<run>-<attempt>` с `result.zip`
и `manifests.zip`. Branch rehearsal публикует только receipt под отдельным tag
и никогда не квалифицирует production. Опубликованные tag/assets неизменяемы.

Selector и Mac cache проверяют успешный exact Prepare, current main, immutable
publication, exact tag SHA, asset ID, uploaded state и GitHub SHA256. Перед
использованием байтов ZIP checksum проверяется независимо; images/receipt/parity
и все canonical gates остаются обязательными. Draft, отсутствующий digest,
подмена run/attempt или mutable release блокируют применение. Публикация Linear
читает новые immutable result assets и сохраняет legacy Actions evidence для
истории. Ошибка publication не превращается в COMPLETE. Старые draft/tag после
неудачной публикации не переиспользуются автоматически: нужен новый run attempt.
Время и квоты hosted Actions для CI продолжают действовать; плата не включается.


## Server deployment и Mac Worker dependency — разные результаты

OWNER contract от 2026-10-05: технический исход server apply не зависит от
временной операционной доступности локального Mac. Это правило применяется
к новой версии canonical tooling; прежние attempts не переписываются.

| Поле | Значения | Семантика |
| --- | --- | --- |
| `status` / `serverDeployStatus` | legacy canonical status / `COMPLETE`, `FAILED`, `RECOVERY_REQUIRED` | Применение и доказательство VPS runtime; миграции, exact image identity, RLS, tenant, backup, server health и production ref обязательны |
| `workerOperationalStatus` | `HEALTHY`, `DEGRADED`, `UNAVAILABLE`, `NOT_VERIFIED` | Отдельная bounded operational observation локального worker |
| `overallServiceReadiness` | `READY`, `DEGRADED` | READY только при server COMPLETE и healthy dependencies |
| `workerDependencies.GPOINT/GSALES` | status, reason codes, safe timestamps | Отдельные lane observations; данные LAB не читаются и не публикуются |
| `inferenceAcceptance` | `NOT_VERIFIED` или явно записанная OWNER acceptance | Реальный end-to-end inference smoke; polling/health его не подтверждают |

До apply блокируются: несовпадение exact worker assets/protocol; неверные
gateway/lane URLs, tenant pins или известное несовпадение credentials;
отсутствующий server API/schema contract и любая несовместимая конфигурация.
Read-only compatibility gate не вызывает polling, generation, service restart
или signal. Tokens сравниваются через stdin и не входят в evidence.

После проверенных migrations/runtime/image parity/server health выполняется
отдельный bounded dependency probe. Неудачная auth/status/network операция,
`worker_target_unavailable`, stale heartbeat/poll или timeout дают DEGRADED /
UNAVAILABLE / NOT_VERIFIED для worker и service readiness, но server остаётся
COMPLETE. Probe не вызывает apply, rollback, token rotation или повторный
Deploy. Обязательная GSales DB/Core RLS-проверка сохраняется; operational
`production_inference_ready=false` больше не ломает server runtime proof.
Пользовательский `/health/ready` GSales по-прежнему отражает готовность inference;
release verifier отдельно проверяет server liveness, DB и Core security.

Первичное явно разрешённое GSales setup сохраняет отдельный защищённый переход
Mac lane configuration. Это локальная операция с собственными safety gates.
Повторные server Deploy не запускают эту операцию автоматически; неизменная
конфигурация независимо проверяется compatibility gate. Worker source/LAB/
business flags не меняются этим контрактом.

Повторная проверка для **уже записанного COMPLETE** server release:

```sh
pnpm release:gpoint --recheck-worker --candidate <exact-current-production-sha>
```

Команда использует shared lock и проверенный ledger, сохраняет
`DEPENDENCY_RECHECK` manifest и readiness state в ledger без изменения VPS
runtime, migrations, SHA, production ref, server release ID или acceptance.
При восстановлении Worker получается COMPLETE / HEALTHY / READY без Deploy.
Если ledger содержит RECOVERY_REQUIRED или другой production SHA, recheck
блокируется; read-only reconciliation и publication repair выполняются отдельно.
После записи ledger `STATE_SYNC_ONLY=true python3 scripts/publish-release-linear.py`
обновляет только owned current-state блоки Linear (требуется настроенный
`LINEAR_API_KEY`, не помещать его в командные аргументы).

Результаты server/dependency отображаются отдельно в immutable release result,
manifest, canonical state и Linear. Красная публикация ledger/Linear остаётся
видимым bookkeeping failure и не доказывает ошибку runtime. Новый contract
не финализирует автоматически старый run и не разрешает новый production apply.
