# Как выпускать GPoint

## FAST/FULL: подготовка кандидата до push

Этот порядок сокращает локальную подготовку, не меняя Native Release, его CI,
production gates или полномочия OWNER. Для GPoint push в `main` запускает
`Tests → Build/GHCR → Kamal Deploy → Runtime Verify`; поэтому push считается
production-triggering действием. Само FAST-классифицирование, готовый коммит,
успешные тесты или факт подготовки не дают разрешения на публикацию.

### 1. Сначала синхронизировать и изолировать работу

В начале задачи зафиксировать UTC `SYNC_START`, затем проверить `origin/main`,
локальные `HEAD` и branch, полный `git status --short`, staged/unstaged diff и
существующие worktrees. Если рабочая копия грязная или отстала, не исправлять её
вручную и ничего не reset/stash/overwrite: сохранить её как есть и продолжить в
чистой задачно-изолированной копии от подтверждённого SHA `origin/main`.
Сверить HEAD новой копии с этим SHA, убедиться в чистом status и зафиксировать
`SYNC_DONE` и `SYNC_DURATION`. При сетевой ошибке не считать прежний remote ref
актуальным; публикация блокируется, пока remote SHA не подтверждён. Не повторять
fetch без изменения состояния или другой объективной причины.

Перед началом и в конце использовать обязательные
`pnpm repo:clean:start` / `pnpm repo:clean:end`. Эти проверки не заменяют
проверку dirty исходной рабочей копии и не разрешают захватить чужие изменения.

### 2. Подтвердить разрешение и полный состав кандидата

Перед выбором FAST или FULL установить:

1. Текущий production source SHA из успешного GitHub Deployment, прошедшего
   Runtime Verify, и read-only подтверждения фактически работающего runtime.
   Историческая запись Linear, последний `main` или один статус GitHub
   Deployment сами по себе не подтверждают установленный production SHA.
2. Точные SHA production baseline, локального candidate и актуального
   `origin/main`; проверить ancestry и происхождение всех входящих коммитов.
3. Полный commit list и `production SHA → candidate SHA` diff, включая ранее
   накопленные изменения. Сопоставить каждый путь/коммит с разрешением OWNER,
   handoff и исключениями. В отчёте перечислить полный включённый scope и
   подтвердить отсутствие чужих, неразрешённых и исключённых изменений.
4. Какое разрешение допускает публикацию именно этого полного кандидата и
   production-triggering push. При неясном, более узком или отсутствующем
   разрешении — не публиковать; сохранить разрешённую локальную работу как
   `LOCAL_PENDING_RELEASE`. Переход на FULL не расширяет разрешение.

Если production SHA, ancestry, cumulative diff или provenance нельзя доказать,
остановить публикацию в обоих режимах. Если накопленный diff содержит scope,
выходящий за поручение, сначала урегулировать его; не маскировать его FAST
правкой README.

### 3. Классифицировать весь кандидат

| Режим | Когда применять | Локальная проверка |
|---|---|---|
| **FAST** | Только если весь `production SHA → candidate SHA` diff ограничен простыми обычными текстовыми/README изменениями, которые не меняют код, пользовательское поведение, эксплуатационные процедуры, релизные/безопасностные правила или production-конфигурацию; все коммиты входят в OWNER scope. | Минимальные применимые проверки, `git diff --check`, Markdown/link checks если они обязательны для этих файлов, clean/scope gates. Не повторять необязательные полные локальные тесты, которые штатно выполнит GitHub. |
| **FULL** | Любой код или тесты; `AGENTS.md` и другие инструкции/правила; CI, workflows, Kamal, Docker, инфраструктура; миграции, schema, permissions, secrets; Security, Worker, AGATA, Eros; смешанный, неизвестный или накопленный защищённый scope. Одна строка в защищённом файле также требует FULL. | Все применимые локальные проверки, security/review/independent validation и текущие согласования. Повторно не запускать лишь те проверки, для которых действующее правило разрешает использовать точное, проверяемое свидетельство того же содержимого. |

Любой другой файл документации, включая release runbook, не считается автоматически
FAST: оценивается его фактическое влияние на production и процедуры. Если есть
сомнение, применять FULL. Режим определяется целым кандидатом, а не последним
коммитом или размером текущей правки.

Контрольные сценарии для локальной классификации: чистый кандидат, где единственное
изменение от подтверждённого production SHA — обычная строка в `README.md`, проходит
критерий FAST; кандидат с изменением `AGENTS.md` или кода проходит только FULL;
README-коммит поверх накопленного изменения кода также проходит только FULL;
неизвестный production SHA или отсутствие разрешения блокирует публикацию в обоих
режимах.

### 4. Подготовить и проверить exact candidate

В выбранной чистой ветке изменить только разрешённые файлы. Просмотреть staged и
unstaged diff и полный список путей; выполнить проверки режима и все обязательные
clean/scope gates. Создать локальный path-scoped commit только из task-owned
изменений, сохранить полный commit SHA, base и результаты проверок. Перед
публикацией заново подтвердить актуальный `origin/main`, SHA, production baseline,
полный diff и достаточное разрешение; если состав или состояние изменились,
повторить классификацию и применимые проверки.

Push допускается только если OWNER явно разрешил publication/production-triggering
push для полного проверенного кандидата и действующие правила GPoint разрешают
этот точный путь, включая технически обеспеченное ограничение для `main`. Перед
публикацией read-only проверить соответствующие GitHub branch protection/rulesets
и зафиксировать реально применяемые правила. Процессная проверка разрешения не
является доказательством server-side блокировки. Если read-only проверка недоступна
или не доказывает требуемое техническое ограничение, остановить push и указать
`NOT_VERIFIED`/BLOCKED; не считать это ограничение существующим. Не создавать
rulesets, hooks, permissions или workflow для этой процедуры. После отдельно
разрешённого push подтвердить remote SHA и соответствующий автоматически
запущенный Native Release; вручную не дублировать CI dispatch.

### 5. Замерить время

Все доступные события записывать в UTC; недоступные метки обозначать
`NOT_VERIFIED`, не оценивать задним числом. Включать ожидание человека/инструмента
в общий wall-clock и отдельно указывать его длительность, если она существенна.

| Метка | Что фиксировать |
|---|---|
| `TASK_RECEIVED` | Получение поручения; если timestamp чата недоступен — `NOT_VERIFIED`. |
| `SYNC_START`, `SYNC_DONE`, `SYNC_DURATION` | Проверка/fetch и создание или сверка изолированной копии, отдельно от остальной работы. |
| `PREFLIGHT_DONE` | Завершена проверка production baseline, candidate SHA, ancestry, полного diff, scope и разрешения. |
| `EDIT_DONE`, `CHECKS_DONE` | Завершены изменения и применимые локальные проверки. |
| `COMMIT_DONE` | Локальный commit SHA подтверждён. |
| `PUSH_CONFIRMED`, `ACTIONS_STARTED` | Remote SHA подтверждён и фактический workflow run обнаружен; без разрешённого push — `NOT_RUN`. |
| `RUNTIME_VERIFY_DONE` | Фактический Runtime Verify точного run/SHA завершился; без релиза — `NOT_RUN`. |

Итог показывать как: время от `TASK_RECEIVED` до push, время после push до
Runtime Verify и полный wall-clock до Runtime Verify; разбивку sync, preflight,
edit/check/commit, существенного ожидания и каждого GitHub job брать из
подтверждённых timestamps. Не экстраполировать benchmark MaxiBot или исторические
значения на новый запуск.

Ориентир для сопоставимого простого README-only FAST-кандидата — 2–3 минуты от
`TASK_RECEIVED` до подтверждённого push. Это целевое значение, а не доказанная
скорость GPoint; подтверждать его только серией сопоставимых замеров. Изменения
защищённого scope оцениваются отдельно и не сравниваются с этим ориентиром.

### 6. Граница текущей задачи

Эта процедура не меняет workflow и не разрешает push, GitHub Actions, Deploy или
изменения production/БД сама по себе. Если поручение разрешает только локальную
подготовку, остановиться после проверенного локального commit/handoff со статусом
`LOCAL_PENDING_RELEASE`; поля push/Actions/Runtime Verify отметить `NOT_RUN`.


## ME-52: Native Kamal release (Native First V3)

Production переводится на Kamal отдельным workflow
`.github/workflows/gpoint-first-kamal-cutover.yml`; он требует свежий inventory,
encrypted backup/isolated restore, migration parity, паузу старых consumers и
полную runtime Verify. После cutover обычный workflow
`.github/workflows/gpoint-native-release.yml` выполняет
`Tests → immutable Build/GHCR → protected Deploy job (Prisma при необходимости
→ stock Kamal 2 → Runtime Verify)`. Exact SHA, immutable digest/provenance,
platform/revision, production Environment, runtime identity, backup и migration
gates сохраняются.

Обычный путь содержит четыре jobs: независимые Workspace и Python tests,
затем Build, затем Kamal Deploy + Runtime Verify. Проверка события и путей
выполняется шагом Workspace, без отдельного runner/job. PR запускает только
тесты и не получает production Environment/secrets или GHCR write.

Разрешённый push в main проходит Tests и Build, после чего тот же workflow
выполняет protected Deploy и Runtime Verify. Prisma migration SQL допускается
только с точным именем и SHA-256 checksum в
`packages/db/prisma/production-approved-migrations.json`; неизвестные,
несовпадающие и AGATA migrations блокируются. Prisma schema без migration SQL,
удаление/перемещение migration и неизвестные schema paths блокируются. Deploy
проверяет полный applied prefix/checksums, свежий backup/isolated-restore
manifest, применяет точный pending allowlist через `prisma migrate deploy` один
раз, проверяет итоговые heads/checksums и только потом вызывает Kamal. Ошибка
или неопределённый Apply останавливает Kamal; автоматический downgrade или
слепой повтор не выполняется. Production allowlist сейчас пуст.

GitHub Tests выполняют Prisma migrations на disposable PostgreSQL, сравнивают
полную историю миграций с `schema.prisma` через отдельный Prisma shadow database,
повторно запускают `migrate deploy` и проверяют `migrate status`. `migrate diff`
для migration directory использует shadow database; `migrate deploy` сам по
себе drift не проверяет ([Prisma CLI v6](https://www.prisma.io/docs/orm/v6/reference/prisma-cli-reference)).
Destructive SQL и совместимость предыдущего/кандидатного приложения остаются
обязательным предметом review exact migration перед включением её checksum в
allowlist; универсальный SQL risk classifier не вводился.

Перед Kamal scope определяется полным diff от общего проверенного live
baseline. Изменённые Core, Mini App и AGATA определяются независимо, поэтому
изменение нескольких точно распознанных компонентов обновляет только эти
компоненты. Неизвестный/небезопасный путь, удаление/переименование, пустой diff,
кандидат равный всем production baselines или компонентный baseline, который не
является предком кандидата, останавливает Deploy до DB preflight/Apply и требует
отдельного подтверждения component scope. У компонентов могут быть разные
production source SHA после предыдущих scoped releases: сравнивается baseline
каждого компонента отдельно; роли одного образа при этом должны совпадать.
Неоднозначный scope не превращается автоматически в FULL и не перезапускает
незаявленные сервисы.
Runtime Verify проверяет все шесть ролей, digest, source SHA, version, Docker
image ID неизменённых ролей, health и database/cache identities. PostgreSQL,
Redis, proxy и неизменённые компоненты не перезапускаются.

До дополнительной Docker-инспекции Deploy вызывает для каждой Kamal-конфигурации
`kamal app version`, `kamal app details` и `kamal app containers`. Workflow
проверяет актуальную версию и наличие ожидаемых role/version записей в штатном
выводе Kamal, затем сверяет её с inventory. Docker read-only проверки дополняют
этот inventory точными OCI digest/image ID, количеством работающих контейнеров,
mounts и сетевой/database идентичностью, которых Kamal CLI не выдаёт в требуемом
машиночитаемом виде.

Build пока публикует все три runtime-образа. Точный live baseline читается только
в защищённом Deploy job после Tests и Build; до этого job production credentials
недоступны. `github.event.before` не является надёжной заменой live baseline:
предыдущая публикация могла завершиться ошибкой, а в main могли накопиться
невыкаченные изменения. Поэтому ранний path-only выбор образа не определяет
Deploy scope: неоднозначный live diff блокируется до DB действий, а не
расширяется до FULL. Тест
`test_all_runtime_images_build_before_production_baseline_is_available`
закрепляет это ограничение и порядок: полный безопасный build, затем live
inventory, затем component plan. Для настоящего selective Build требуется
надёжно доказать production source и применить тот же cumulative diff до Build,
не выдавая production secrets тестовым job; пока такого проверенного безопасного
источника нет, удалять эти три build шага нельзя.

Rollback, backup maintenance и isolated migration rehearsal остаются отдельными
ручными операциями; обычный push их не запускает. Первый cutover и его recovery
сохраняют свои допуски. Исторические workflow runs не подтверждают текущее
production состояние. Production SUCCESS устанавливается только свежим успешным
Native Runtime Verify и read-only runtime evidence; успешные Tests/Build его не
заменяют.

У отдельного First Cutover workflow есть `allow_source_update`, по умолчанию
`false`. Его включают только для отдельно OWNER-approved exact candidate, если
при первоначальном переходе Kamal application source должен продвинуться дальше
live Compose `RELEASE_SHA`. Тогда проверяется корректный непустой текущий
Mini App static tree, а новый immutable image независимо проверяется по exact
digest/provenance. Этот First Cutover-only flag не меняет обычный Native Release
и не отменяет migration parity, backup/offsite/isolated restore, recovery,
consumer и runtime Verify gates.

Публичный TLS/HTTP вход после перехода обслуживает Kamal Proxy на портах 80 и
443: `/api` и `/media` идут в Core, `/` и Mini App paths — в Mini App. Caddy
останавливается в cutover и сохраняется только вместе с checksum как recovery
material. GSales не собирается и не стартует в обычных релизах; Eros,
Sensitive Detector и ACL/pilot остаются вне обычного release scope.

Production Deploy выполняется после успешных CI и Build, authorization и
read-only live-state checks. Для app-only кандидата Prisma Apply пропускается;
для миграционного кандидата допустим только точный checksum allowlist и полный
совместимый pending набор. Runtime Verify является единственным runtime gate
для GitHub Deployment SUCCESS.

В обычном Native Release job использует GitHub Actions `environment: production`:
GitHub сам создаёт Deployment и управляет `in_progress`/финальным статусом по
результату job. Read-only resolver находит ровно одну запись по exact candidate
SHA/ref, production environment, task, run URL и попытке; неоднозначность
блокирует job. Успешный Verify отдаёт в `environment.url` URL с SHA-256 runtime
manifest, поэтому автоматический SUCCESS связан с тем же проверенным manifest.
Отдельные ручные POST статусов в обычном пути не нужны.

Rollback пока сохраняет отдельный ручной Deployment для выбранного предыдущего
target SHA. Workflow исполняется с текущего `main`, поэтому автоматически
созданный Environment Deployment имел бы SHA workflow, а не SHA восстановленной
версии и сделал бы историю ложной. До подтверждения эквивалентной штатной модели
rollback продолжает создавать запись для exact target SHA и вручную отмечать её
только после полного Runtime Verify. Это зафиксированный архитектурный gap, не
разрешение обходить OWNER approval или safety gates.

Для отдельно разрешённого перехода AGATA `agata0011 → agata0012` результат
реального Apply фиксируется в приватном root-owned mode-0600 файле
`/opt/gpoint/release-logs/<candidate>/agata-applied-migration.json`: source SHA,
точные heads, immutable AGATA image и SHA-256 выполненной миграции. Этот proof
создаёт существующий `agata-production-migration.py migrate` с `--image` по
immutable digest, `--expected-migration-sha256` и `--record-receipt`, только после
успешного guarded Apply и read-back. Уже существующий receipt блокирует повторный Apply для этого кандидата. Если ещё работающий
старый source не содержит нового revision, Native проверяет этот proof и checksum.
Изменение ранее существовавшего revision, включая rename с изменением кода,
по-прежнему блокируется. После переключения приложения на новый source такой
bootstrap proof больше не нужен; это evidence миграции, а не новый release controller.

Обычный production preflight требует ровно по одной работающей версии каждой из
шести Kamal-ролей, согласованной идентичности ролей внутри каждой общей image
группы, активного Proxy, точных
PostgreSQL/Redis container IDs и SHA-256 fingerprints их persistent mounts.
После Deploy идентичность БД и Redis сверяется повторно. Core Worker, Bot и
AGATA memory-worker обновляются отдельными stock Kamal вызовами. Перед этим
`kamal app stop --roles ...` останавливает прежний consumer: grammY Bot использует
long-polling, а stock Kamal обычно запускает новую версию до остановки старой.
После каждого перехода проверяется ровно один процесс на candidate SHA. Очередь
Worker/AGATA и накопленные Telegram updates переживают короткое окно остановки. Частичный отказ
записывается как `RECOVERY_REQUIRED`; автоматический rollback и повторный Apply
запрещены до read-only reconciliation.

Проверки количества контейнеров и source revision для промежуточного перехода
выполняются read-only helper `scripts/verify-kamal-role.sh`, переданным через
проверенное SSH-соединение на `GPOINT_PRODUCTION_HOST`. Docker CLI GitHub runner
не используется как источник состояния VPS.

Защищённый rollback доступен только как ручной exact-main dispatch с явным
OWNER approval, exact target SHA и ID предыдущего успешного Native Production
Deployment. Проверяются последний статус SUCCESS, связанный успешный Native
Release run/build, SHA-256-bound production manifest для всех шести ролей,
GHCR immutable digests и provenance, retained target containers, совместимость
текущих Prisma/Alembic heads и сохранность DB/Redis IDs и mounts. Kamal rollback
выполняется по одной роли: consumers раньше API. Закреплённый Kamal 2.12.0
вызывается как `kamal rollback <version> --roles <role>`: версия — позиционный
аргумент stock CLI. Полный runtime Verify должен
завершиться до GitHub Deployment SUCCESS. Любая неоднозначность блокирует
операцию; production DB migrations автоматически не откатываются. Пока нет
предыдущего Native Release с подтверждённым successful manifest, rollback
цель отсутствует и workflow должен отказать до изменения runtime. Состояние и свежесть
scheduled backup не являются условием обычного релиза и должны контролироваться
отдельным мониторингом ([ME-56](https://linear.app/bestuzhev/issue/ME-56/gpoint-independent-scheduled-backup-failure-and-freshness-alert)).
Установка/обновление timer и один первичный backup выполняются отдельно через
`backup_maintenance` с точным main SHA и явным OWNER approval. Обязательные
backup/restore проверки перед миграциями и опасными
инфраструктурными операциями, offsite gate первого cutover и прочие действующие
backup/offsite требования не меняются. Удалять recovery/legacy путь можно
только после отдельной авторизации и успешной production Verify.

### Инвентаризация workflows и прежних release helpers

Статусы основаны на workflow calls, package scripts, imports, tests и
recovery/transition paths. Отсутствие автоматического вызова не доказывает
отсутствие ручного или внешнего использования. Таблица фиксирует безопасное
условие удаления, а не разрешение на удаление.

| Путь | Статус | Замена / назначение | Test evidence | Условие безопасного удаления |
|---|---|---|---|---|
| `.github/workflows/gpoint-native-release.yml` | ACTIVE | Канонический Tests → Build/GHCR → protected Kamal Deploy → Runtime Verify; штатный GitHub Deployment для обычного Deploy. | `tests/test_kamal_native_automatic_release.py`, `tests/test_kamal_release_config.py`, `tests/test_native_environment_deployment.py`, `tests/test_deploy_evidence_workflow.py`. | Не удалять; заменять только отдельным OWNER-approved workflow с равными или более строгими gates и доказанной миграцией активных callers. |
| `.github/workflows/gpoint-deploy.yml` | REQUIRED FOR EROS TRANSITION / RECOVERY | Eros Pilot, ACL, authorization reconciliation и task-scoped operations. Не является обычным Native release-путём. | `scripts/test_prepare_coalescing.py`, `scripts/release-authorization.test.mjs`, `tests/test_eros_postgres_acl_workflow.py`. | После закрытия Eros transition и перевода каждого caller на проверенную замену; сохранить recovery evidence и per-task authorization. |
| `.github/workflows/gpoint-prepare.yml`, `.github/workflows/gpoint-recover-interrupted.yml` | REQUIRED FOR RECOVERY | Legacy exact-SHA Prepare/coalescing и metadata-only recovery без повторного Apply. | `scripts/test_prepare_coalescing.py`, `tests/test_interrupted_recovery_workflow.py`, `tests/test_native_release_intent.py`. | После подтверждения production/runtime SHA и отсутствия открытых legacy attempts; эквивалентный recovery путь должен пройти exact-SHA regression и operational acceptance. |
| `.github/workflows/gpoint-first-kamal-cutover.yml` | REQUIRED FOR RECOVERY | Первый Compose-to-Kamal cutover и recovery. | `tests/test_kamal_cutover_owner.py`, `tests/test_recover_kamal_cutover.py`, `tests/test_kamal_native_automatic_release.py`. | Только после закрытого cutover, проверенной native runtime evidence и отсутствия активных recovery callers. |
| `.github/workflows/gpoint-linear-reconcile.yml` | ACTIVE | Read-only reconciliation и публикация фактов после Prepare/Deploy/recovery. | `scripts/reconcile-deployment.test.mjs`, `tests/test_interrupted_recovery_workflow.py`. | Не удалять, пока Linear release queue использует этот reconciliation; замена должна сохранять проверку точных run/SHA/runtime facts. |
| `scripts/release-gpoint-vps-adapter.mjs`, `scripts/release-gpoint.mjs`, `scripts/automated-release.mjs`, `scripts/prepare-release.py` | LEGACY / REQUIRED FOR RECOVERY / UNKNOWN BY ENTRYPOINT | Старый Compose release/apply и orchestration; отдельные функции остаются в старых workflows, cutover, static exchange или recovery. Обычный выпуск заменён `gpoint-native-release.yml` + stock Kamal. | `scripts/release-gpoint.test.mjs`, `scripts/release-orchestrator.test.mjs`, `scripts/test_prepare_coalescing.py`, `tests/test_prepare_release_migration_inventory.py`. | Не удалять по названию. Сначала доказать отсутствие workflow/package/manual callers, перечислить каждую оставшуюся функцию и перевести её caller с regression и recovery acceptance. |
| `infra/production/docker-compose*.yml`, `infra/shared/docker-compose.yml`, `infra/docker/docker-compose.yml` | LEGACY / TRANSITION / ACTIVE FOR SCOPED SERVICES | Compose-конфигурация нужна для оставшихся cutover, Eros/GSales, shared services или recovery; штатный Native app lifecycle выполняет Kamal. | `tests/test_eros_permanent_compose.py`, `tests/test_eros_vps_pilot_compose.py`, `tests/test_kamal_release_config.py`. | Удалять пофайлово после отсутствия production/manual callers, закрытого соответствующего transition и доказательства сохранения shared DB/cache, volumes, network, health и rollback. |
| `scripts/verify-gpoint-backup-restore.sh` | REQUIRED FOR RECOVERY / CUTOVER | Защищённое одноразовое восстановление и cutover evidence; routine Bunny backup использует `gpoint-scheduled-backup.py` и `verify-gpoint-scheduled-backup-restore.sh`. | `tests/test_kamal_release_config.py`, `tests/test_gpoint_media_tree.py`, `tests/test_recover_kamal_cutover.py`. | Только после успешной production restore rehearsal через принятую замену и подтверждения, что ни один recovery workflow/caller не использует этот путь. |
| `scripts/gpoint-scheduled-backup.py`, `scripts/verify-gpoint-scheduled-backup-restore.sh`, `scripts/gpoint-backup-policy.py` | ACTIVE | Отдельный systemd backup runner: штатный PostgreSQL archive, Bunny upload/read-back и isolated restore из одного снимка. Не входит в обычный app Deploy. | `tests/test_gpoint_scheduled_backup.py`; в том числе проверка upload → exact-object read-back → restore. | Не удалять до OWNER-approved replacement и подтверждённой непрерывности расписания, freshness/failed-run monitoring, Bunny restore proof и recovery acceptance. |
| Ручные `gh api POST /deployments` и deployment statuses в rollback части `gpoint-native-release.yml` | ACTIVE, DOCUMENTED GAP | Нужны для rollback target SHA: workflow запускается с текущего `main`, а цель — ранее выпущенный другой SHA. Обычный Deploy использует native Environment Deployment и ручные POST не вызывает. | `tests/test_kamal_native_automatic_release.py`, `tests/test_native_environment_deployment.py`. | Только если штатный Environment workflow сможет точно представлять target SHA и сохранить approval, run linkage и SUCCESS-after-Verify; иначе gap остаётся и не создаётся ложный SUCCESS. |
| Docker lifecycle в `scripts/release-gpoint-vps-adapter.mjs` и cutover/recovery helpers | LEGACY / REQUIRED FOR TRANSITION OR RECOVERY | Для обычных Native app контейнеров заменено stock `kamal deploy --skip-push`, `kamal app stop` и `kamal rollback`. Остальные команды могут быть одноразовыми technical DB/backup или переходными операциями. | `tests/test_kamal_native_automatic_release.py`, `tests/test_kamal_cutover_owner.py`, `tests/test_gpoint_scheduled_backup.py`. | Удалять только после caller-by-caller доказательства, что операция управляет application lifecycle и stock Kamal эквивалентен; DB/backup technical actions не считать дублирующим app lifecycle. |

На момент проверки не найден путь, который можно безопасно удалить безусловно.
Финальная классификация runtime/production callers остаётся `PROD_NOT_VERIFIED`;
таблица не утверждает, что старые файлы не используются вручную. Исторические
procedures ниже текущей модели помечены отдельно и не являются рабочими
instructions.

## Текущая модель выпуска и состояния (ME-42)

Этот раздел является единственным действующим описанием хранения артефактов и
production state после OWNER addendum ME-42. Все разделы ниже него — исторические
сведения о прежних процедурах; их команды и инструкции не запускать.

- **Единое хранилище build artifacts — GHCR.** GitHub Releases и Actions Artifact
  Storage не используются в действующем Prepare/Deploy transport. Prepare
  публикует малый OCI receipt artifact в GHCR. Доверенная цепочка provenance
  состоит из exact GitHub run/attempt, успешного Prepare job и publisher step,
  единственной записи digest в publisher logs, затем повторного чтения manifest
  и layer по digest с проверкой hash/size. Это run-bound evidence, а не
  криптографическая GitHub Artifact Attestation. Receipt связывает точный source SHA,
  тесты и provenance,
  digest-и runtime images, а также migration/static metadata. Deploy принимает
  только OCI artifact по проверенному digest и проверяет всю цепочку provenance;
  moving tags, подмена digest, неизвестный source/run/attempt или неполная
  публикация блокируют Apply.
- **Runtime images и Mini App:** Deploy получает проверенные GHCR images только
  по immutable digest и сверяет receipt, labels, platform, image/config IDs и
  эффективную конфигурацию с независимо прочитанным состоянием VPS. Для
  отдельно одобренного V3/V4 UI-only scope допускается самостоятельный Mini App
  OCI artifact; если UI изменение действительно независимо от runtime, Prepare
  не собирает все три Docker images. GHCR outage/tampering блокирует до Apply.
  Нельзя собирать новые application images на VPS или подменять недоступный
  artifact произвольным старым образом. Сохранять только проверенные
  production-compatible rollback images и действующие rollback safety gates.
- **Состав receipt и запретные данные:** receipt содержит только необходимые
  provenance/identity и безопасные metadata. Не загружать в GHCR backup БД,
  secrets, `.env`, production payloads или private user data. `fallback-images.zip`
  и `images.tar.gz` не являются действующим транспортом сборок.
- **Production attempt:** один точный Apply связан с записью GitHub Deployment
  для среды `production`. Статусы этого же Deployment обновляются по GitHub
  Deployments REST API и содержат точный candidate SHA, Prepare provenance,
  workflow run/attempt, authorization identity, stage/result и ссылку на run.
  API timeout/неизвестный ответ требует read-back той же записи; он никогда не
  разрешает повторный Apply или создание второго Deployment для обхода ошибки.
  Для повторного запуска того же workflow допустима только read-only проверка
  исходного attempt и публикация исправленного статуса в ту же Deployment;
  Apply не повторяется.
- **Фактическое production состояние:** read-only инспекция VPS и БД является
  авторитетной для запущенного SHA, Mini App/Worker и остальных компонентов,
  image IDs, health, migration heads и сохранности данных. GitHub Deployment
  сообщает о ходе и provenance операции, но сам по себе не доказывает, что VPS
  достиг этого состояния. Расхождение Deployment и VPS блокирует продолжение
  до read-only reconciliation; неизвестный или частичный исход не финализируется
  отдельным workflow.
- **Первый Deploy:** перед изменением оператор обязан считать фактический
  baseline с уже работающего VPS/БД и проверить его целостность, включая
  обязательный защищённый backup при миграциях. Отсутствие GitHub Deployment не
  означает пустой production и не разрешает усыновить/перезаписать данные.
- **Recovery/failure:** ошибки и неопределённые исходы остаются в status того же
  Deployment с точным run URL и описанием. Разрешены read-only проверки и
  публикация статуса на том же Deployment. Новая Apply попытка возможна лишь
  после доказанного VPS baseline и обычной авторизации/гейтов. При API
  неоднозначности, двойном запуске, damaged/missing receipt, неверном digest,
  незавершённой публикации или несовпадении readback действие fail-closed;
  нельзя обходить ошибку fallback архивом или загружать образ с VPS.
- **Экономия Prepare:** удалены экспорт `images.tar.gz`, архивная проверка и
  публикация/чтение GitHub Release assets. Исторический run оценивал эти этапы
  примерно в 90 секунд суммарно; это не замер новой схемы. Вместо них остаются
  публикация/чтение OCI receipt и точные digest/hash проверки. Полный экономический
  эффект не измерялся, поскольку Actions не запускались.
- **Запрещённые operational state stores:** не читать и не записывать
  `gpoint-release-ledger`, production tags, `docs/release/state/production.json`
  или отдельный production Release catalog для выбора baseline, проверки
  COMPLETE/recovery, подготовки к Deploy либо восстановления статуса. Не
  создавать Recovery Finalization/Diagnostics pipeline как параллельный
  оркестратор.

Точные механизмы, разрешения и тестируемые контракты реализованы в текущих
workflow/scripts и проверяются отдельно. Эта документация не даёт права на
публикацию, GitHub Actions запуск, GHCR write, production API write или Deploy.

## Обязательная диагностика ошибок GitHub/GHCR

До объявления GitHub/GHCR недоступным сначала прочитать актуальный стандарт
[«GitHub доступ» PROSTOR](https://linear.app/bestuzhev/document/github-dostup-7970682d1b80).
Проверять операцию в её реальном execution context и доступные для неё штатные
способы авторизации; классифицировать `NETWORK`, `TOOLING`, `AUTH`, `PERMISSION`,
`OWNER_AUTHORIZATION` раздельно. DNS failure внутри Codex sandbox означает сетевой
отказ только в этом контексте; он не доказывает недоступность host Mac или hosted
runner. Host Mac read-only check тоже не доказывает доступ sandbox или workflow
`GITHUB_TOKEN`. HTTP 401/405 на GHCR `/v2/` подтверждает достижимость registry,
не право читать/писать package. Не выводить секреты, не обходить sandbox или host
approval и не расширять OWNER scope; сообщать точную причину и минимальное действие.

## Исторические процедуры (заменены ME-42)

Ниже — архивный текст прежнего дизайна, включая GitHub Releases, Actions
artifacts, ledger и устаревшие команды; это не инструкция к запуску и не
операционный fallback. При конфликте действует только текущий раздел выше.

## Два режима разрешения выпуска

Актуальные условия: [Release authorization modes](../RELEASE_AUTHORIZATION.md).
OWNER task, прямо разрешающее полный выпуск подготовленных изменений до
production, авторизует штатные попытки в границах одного scope, production
baseline и перечисленных рисков. Финальный SHA определяется после интеграции.
До изменений фиксируется schema-v3 durable authorization record. Разработка
без поручения о выпуске не разрешает Deploy. После exact-SHA Prepare controller
повторно проверяет baseline и полный scope и вызывает тот же Deploy workflow в
`task-scoped-auto` режиме. Неудачная попытка не требует нового OWNER approval:
можно повторно использовать действительный immutable Prepare для того же
exact SHA либо выполнить новую Prepare для replacement SHA в том же scope.
Read-only preflight проходит внутри Deploy перед Apply; изменившийся baseline,
recovery state, отозванная/использованная авторизация или неизвестное состояние
блокируют Apply. Критичные категории допускаются только при явном risk contract
в исходном OWNER release task и durable record; без контракта выпуск блокируется.
Runner может штатно ждать в GitHub `Queued`; readiness check остаётся
fail-closed.
Подробности: [Release authorization modes](../RELEASE_AUTHORIZATION.md).

## Для владельца

1. Архитектор задаёт изменения и критерии приёмки. Dev реализует их и отправляет чистый commit в `origin/main`.
2. После успешного exact-SHA Prepare и immutable bundle controller перепроверяет текущий SHA, provenance и отсутствие активного Deploy. Для задачи без действующей full-cycle authorization он не запускает production. OWNER применяет обычное exact-SHA approval через тот же Deploy workflow; отдельный диагностический preflight можно запустить вручную.
3. Для заранее зарегистрированной schema-v3 авторизации полного production-цикла controller повторно проверяет scope/baseline/risk и сохраняет первоначальный immutable claim. Затем он вызывает существующий production Deploy в `task-scoped-auto`; дополнительное READY и второе подтверждение не требуются. После известной неудачи допустим повторный dispatch того же действительного immutable Prepare либо нового Prepare для replacement SHA в том же scope.
4. Deploy валидирует Prepare artifact и authorization, затем внутри того же run проводит полноценный read-only preflight до Apply. Под release lock критичные live conditions проверяются снова перед первой mutation. Любой scope/baseline drift, неизвестный риск, провал preflight, recovery state или неопределённый исход останавливают автоматизацию. Успешный выпуск отмечает authorization consumed; неизвестный результат не повторяется вслепую.
5. Existing canonical workflow продолжает Deploy, verification, evidence и Linear publication. Mac должен быть включён, иметь сеть, активную CLI authentication и работающий GPoint runner. Incy остаётся включённым. Если runner недоступен, run останется Queued и не считается завершённым.
6. После технического COMPLETE откройте Telegram → GPoint → Models Hub. Отправьте новое сообщение и дождитесь ответа. Сообщите Dev «ответ получил» или «ответа нет». Dev фиксирует подтверждение сценария в журнале; автоматизация не создаёт тестовые production-диалоги от вашего имени.

История: [Журнал релизов](https://linear.app/bestuzhev/document/zhurnal-relizov-640efa60dacf). Каждый результат содержит SHA и ссылку на GitHub run. В Handoff история и архитектурные ограничения сохраняются, автоматизация меняет только свои именованные блоки.

Если OWNER поручил только подготовить релиз, отдельное разрешение на production остаётся обязательным. Если OWNER выдал полное ТЗ на выпуск, система сама проводит разрешённый цикл после прохождения технических gates; ручной ввод SHA и подтверждение после READY не требуются. В обоих режимах Deploy использует canonical entrypoint и Prepare artifacts.

Режим **preflight** остаётся ручной read-only диагностикой: проверяет доступ, live baseline, worker/config и артефакты без применения миграций и переключения runtime. При успешной отдельной диагностике `PLANNED` плюс immutable evidence публикуются как `READY_FOR_OWNER`. В production или `task-scoped-auto` статус `PASS` означает, что тот же read-only preflight прошёл внутри Deploy до Apply; `READY_FOR_OWNER` для уже выполняемого production цикла не публикуется.

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

Новый SHA требует нового Prepare; в Exact-SHA режиме — нового разрешения. В Task-scoped режиме применяются ограничения Release authorization modes. Устаревший кандидат, проваленный/незавершённый Prepare, отсутствующий/истёкший artifact или неверный checksum блокируют Deploy. Для повтора того же exact SHA можно использовать успешный Prepare, пока immutable artifact действителен; после истечения срока снова выполните Prepare для текущего main. Автоматический rollback БД не выполняется.

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

## Историческая настройка runner и release transport (не запускать)

1. Убедиться, что GitHub Actions включён для приватного репозитория. Проверить Settings → Secrets and variables → Actions: repository secret `LINEAR_API_KEY` должен иметь доступ к двум существующим документам. Сам key в GitHub/чат/Linear не публиковать. Старый workflow AGENTS использует тот же secret, но его наличие и работоспособность нужно подтвердить успешной публикацией.
2. На Mac в Settings → Actions → Runners → New self-hosted runner выбрать macOS и архитектуру `uname -m`. Скачать официальный runner, проверить SHA256 по команде GitHub. Установить вне репозитория, например `~/Library/Application Support/GPoint/release-runner`. При регистрации задать рабочую папку без пробелов: `--work "$HOME/.local/share/gpoint-release-work"`. Runner хранит временные shell scripts внутри рабочей папки; пробел в стандартном `Application Support/.../_work` ломает их запуск. На текущем Mac рабочая папка уже настроена в `~/.local/share/gpoint-release-work`.
3. Перед подключением владелец подтверждает доступ: runner выполняет команды доверенного workflow GPoint на Mac от его пользователя, имеющего VPS key и локальную CLI authentication. Это не runner для PR из неизвестных веток. Никогда не назначать ему PR workflows; доступ к push/main и изменению workflows ограничить доверенными участниками.
4. Зарегистрировать repo-scoped runner для `https://github.com/akhripunov/gpoint`, имя `gpoint-owner-mac`, label `gpoint-production`. Временный registration token из GitHub не сохранять в Git/Linear/логи. Установить пользовательский сервис официальным `svc.sh install` / `svc.sh start` согласно инструкции GitHub. Предварительный API опрос runner не нужен: GitHub `Queued` — штатное ожидание. Первый этап запущенной job проверяет runner identity/labels, macOS/ARM64, Docker Linux runtime, disk, cache/runtime paths, обязательные tools/network и отсутствие конфликтующей production mutation до загрузки bundle и дорогих шагов. На Mac разрешить sleep policy по решению владельца: отключённый/спящий Mac не может выполнять релиз или отвечать через mac_worker.
5. PATH сервиса должен содержать рабочие `node`, `pnpm` и `python3`, а git — читать подтверждённые known_hosts. Проверить отдельные GPoint SSH/SCP runners, key/env, Mac worker status. Не менять Incy/DNS/hosts. Сравнить установленный `gpoint_mac_worker.py` с исходником кандидата; при изменении worker Dev обновляет его опубликованным исходником по штатному installer перед релизом. Автоматизация должна блокироваться при несовпадении.
6. Первый запуск Prepare обязан быть зелёным, включая Linear. Выпуск нового application candidate разрешается владельцем отдельно; установка автоматизации не является разрешением произвольного production apply.

## Исторические команды для Dev (не запускать без актуальной процедуры)

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

Координатор выполняет только read-only benchmark/preflight и проверяет exact receipt и неизменность main; production apply выполняет только канонический Deploy workflow с отдельной применимой authorization. Runtime изменяют только канонические workflows, не сам координатор. Время от команды владельца до завершения отчёта и длительность workflow измеряются отдельно. Неудачный последний Prepare не обходится и не заменяется более старым успешным. Полные workflow logs, run/job boundaries, receipts и checksum списка файлов собираются без ручного копирования.

Для проверки самого конвейера до merge разрешён ручной Prepare dispatch из
ветки: выполняются только hosted Linux jobs без production secrets, Mac/VPS
операций и публикации READY в Linear. Branch artifact не подходит Deploy;
после merge требуется успешный Prepare exact current main.


## Историческая схема хранения сборок (заменена GHCR; не запускать)

Для сопоставимого benchmark отдельно фиксируйте commit/push, Prepare,
automatic controller, Deploy preflight/apply/verify и публикацию результата в
Linear. Считайте jobs и runner seconds по GitHub API; записывайте cache hit/miss
и повторные build/test/download. Для TASK_SCOPED_AUTO preflight находится
внутри Deploy и должен пройти до Apply; ручной диагностический preflight
отдельно маркируется `READY_FOR_OWNER` и не является обязательным шагом.

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
