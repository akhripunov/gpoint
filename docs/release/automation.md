# Как выпускать GPoint

> **Native First V3 — 2026-11-21.** Единственный production-controller —
> `.github/workflows/gpoint-release.yml`. Layer7 — единственный
> production VPS; отдельного staging VPS, БД, Environment или workflow нет.
>
> | Workflow | Назначение |
> |---|---|
> | `gpoint-release.yml` | Required Tests → выбор компонентов → Buildx/GHCR → защищённый production Deploy → Runtime Verify |
> | `gpoint-operations.yml` | Отдельные операции migration rehearsal; это не release-controller и не Deployment |
>
> Каждый обычный push в `main` запускает required tests и Buildx на exact SHA.
> Каждый продуктовый релиз и допущенный same-repository PR собирают все три
> независимых образа: Core, MiniApp и Eros. Один декларативный path map задаёт
> область тестов и выборочного Deploy. Deploy сравнивает live Kamal/Docker image
> identity с кандидатом и применяет только изменённые роли. Immutable digests,
> защищённый Deploy, миграционные gates, rollback, Runtime Verify и smoke
> сохраняются. Последний успешный Native Deployment служит только маркером уже
> одобренных инфраструктурных изменений; он не заменяет live runtime baseline.
>
> Native production SSH принимает только документированный адрес Layer7
> `95.133.236.59`; устаревший или ошибочный `GPOINT_KAMAL_HOST` блокируется
> до записи SSH credentials и до любой удалённой команды. `GPOINT_KAMAL_HOST`
> читается из защищённого GitHub Environment `production`, а не из repository
> secret. Exact-address и matching `GPOINT_KAMAL_KNOWN_HOSTS` checks выполняются
> сразу после проверки exact SHA/OWNER — до Python setup, migration dependency
> installation и Kamal setup. Секреты остаются только в Deploy job: переносить
> их в Tests/Build ради более раннего preflight нельзя. Изменение адреса требует
> согласованного обновления guard и matching host-key конфигурации; DNS lookup
> сам по себе не подтверждает production identity.
>
> Same-repository PR от `OWNER`, `MEMBER` или `COLLABORATOR` собирает и
> проверяет все три SHA-bound образа с GHCR readback, без production secrets и
> без доступа к Deploy. Fork PR не получает GHCR publication. Documentation-only
> изменения также проходят все три Build, но live diff выбирает `NONE` и не
> перезапускает runtime. Ошибки Build или неизвестный live baseline блокируют
> production Apply.

### Резервное копирование (не является релизным job)

Ежедневные копии выполняет systemd-таймер на VPS (`gpoint-backup.timer` →
`gpoint-scheduled-backup.py`): шифрование age, Bunny S3 `gpoint-backups`,
read-back и isolated restore. GitHub не управляет расписанием и не зависит от
релизов. Установка/обновление релиза backup-скриптов — отдельная защищённая
процедура, а не job:

```sh
APPROVE_PRODUCTION=true \
CANDIDATE_SHA=<40-char origin/main SHA> \
GPOINT_PRODUCTION_HOST=<host> \
bash scripts/gpoint-backup-install.sh
```

Процедура fail-closed: требует чистый checkout ровно на этом SHA, свежий
`origin/main`, явное `APPROVE_PRODUCTION=true`, сверяет sha256 payload, ставит
versioned release, включает таймер, запускает первый backup и проверяет
доказательства (policy `freshness`, isolated restore из первого снимка).

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
`node scripts/repo-clean-gate.mjs start` / `node scripts/repo-clean-gate.mjs end`. Эти проверки не заменяют
проверку dirty исходной рабочей копии и не разрешают захватить чужие изменения.

### Быстрый измеряемый gate перед push

```sh
python3 scripts/prepush.py --production-sha <40-char production SHA>
```

Печатает `PREPUSH_MODE`, `SYNC_SECONDS`, `CLASSIFY_SECONDS`, `LOCAL_CHECKS_SECONDS`
и `PREPUSH_STATUS`. Проверяет: актуальность `origin/main`, ancestry, чистоту
worktree, полный `production → candidate` diff, `git diff --check`, синтаксис
изменённых Python-файлов и YAML изменённых workflow. Тяжёлые проверки
(pytest/pnpm/Playwright/Docker/migration rehearsal) остаются в hosted CI и
локально не дублируются.

Если свежий `origin/main` уже подтверждён и синхронизирован в этом же
preflight, используй `--no-fetch --main-sha <verified-main-sha>` вместо
повторного fetch. Не запускай классификатор отдельно: `prepush.py` уже
выполняет его. Не используй pnpm-обёртки для этих Git/Python/Node-проверок;
они не требуют установки workspace dependencies. Отдельно сохрани проверку
OWNER scope/authorization и обязательные специальные проверки риска.

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
| **FULL** | Любой код или тесты; `AGENTS.md` и другие инструкции/правила; CI, workflows, Kamal, Docker, инфраструктура; миграции, schema, permissions, secrets; Security, Worker, AGATA, Eros; смешанный, неизвестный или накопленный защищённый scope. Одна строка в защищённом файле также требует FULL. | Только применимые целевые проверки, обязательные security/review/independent validation и согласования. FULL — класс риска, а не полный локальный CI. Результаты DEV повторно использовать при доказанном совпадении tree, тестов, зависимостей, инструментов и условий. Не запускать production Docker build, полный pytest/pnpm/Playwright или migration rehearsal рутинно до push; обязательные специальные проверки риска сохранить. |

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

Классификатор уже вызывается из `scripts/prepush.py`. Следующая команда
предназначена только для отдельной диагностики, не для второго gate:

```sh
python3 scripts/classify-release-prepush.py \
  --repo . \
  --production-sha <verified-runtime-source-sha> \
  --main-sha <fresh-origin-main-sha> \
  --candidate-sha <exact-candidate-sha>
```

Команда проверяет полные commit SHA, что production и свежий `main` являются
предками кандидата, и классифицирует весь `production..candidate` path diff.
Она возвращает `FAST` только для изменения существующего корневого `README.md`;
добавления, удаления, остальные файлы и смешанный накопленный diff дают `FULL`.
Непроверяемая SHA, ancestry или path inventory дают `BLOCK` с exit code 2.
Классификатор не проверяет фактический production/runtime evidence, безвредность
текста или OWNER authorization, не выполняет push и не заменяет эти проверки.
Allowlist оставлен узким намеренно: расширение по другим путям требует
поддерживаемых content-aware правил, которые доказывают отсутствие влияния на
runtime, эксплуатацию, релизные процедуры и безопасность. Один лишь суффикс
`.md` такого доказательства не даёт; неоднозначные документы остаются FULL.
Тесты для локальной имитации:

```sh
python3 -m pytest tests/test_release_pre_push_mode.py -q
```

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

Цель для готового обычного кандидата без сетевых ошибок и специального
risk scope — 30–90 секунд от PREPUSH_START до FIRST_PUSH (readback удалённой
ветки). Это цель, а не измеренный результат. Отдельно фиксируй ожидание PR CI,
merge/main push и production workflow. Не исключай из измерений реальные
повторные проверки или ошибки; не ослабляй обязательные gates ради цели.

### 6. Граница текущей задачи

Эта процедура не меняет workflow и не разрешает push, GitHub Actions, Deploy или
изменения production/БД сама по себе. Если поручение разрешает только локальную
подготовку, остановиться после проверенного локального commit/handoff со статусом
`LOCAL_PENDING_RELEASE`; поля push/Actions/Runtime Verify отметить `NOT_RUN`.


## Current GPoint Release Pipeline (ME-62 candidate)

`.github/workflows/gpoint-release.yml` is the single production controller.
Its ordinary path is `Tests → Build/GHCR → protected Deploy → Runtime Verify`.
A permitted push to `main` starts the path automatically. PRs
run the same required tests and do not receive production secrets or Deploy
access. Eligible same-repository PR Build jobs receive `packages: write` only to
publish candidate images for immutable GHCR read-back. Fork PRs never enter
that Build job.

The required test matrix is exactly `workspace` and `python`. Workspace also
contains actionlint, pinned Kamal configuration/proxy checks, and the conditional
stock Eros SQLx/conversation test. Python owns the release-boundary tests and
conditional isolated database suites. Every matrix run evaluates the same
fail-closed release intent and push baseline so combined job outputs do not
depend on matrix completion order. Every eligible PR and main candidate builds
Core, MiniApp and Eros. Documentation-only candidates still pass the required
tests and builds; live path classification selects no production Deploy.

There are three independently scoped logical components and three unique images:

| Logical component | Kamal roles | Image |
|---|---|---|
| Core | API, Worker, Bot | `gpoint-runtime` |
| Mini App | Web | `gpoint-miniapp` |
| Eros Engine | Pilot Engine | `gpoint-eros` |

Buildx unconditionally publishes all three images from the exact candidate SHA
with immutable digests, SBOM and provenance. Live Kamal/Docker inventory alone
selects which component roles Deploy updates; no build-scope planner or GitHub
Deployment history participates in Build. Deploy verifies the selected image
read-back and uses those digests without rebuilding. A production dispatch cannot reuse
an image from another run. The Eros image build fetches the pinned upstream
commit from `etherfunlab/eros-engine`; BuildKit layer cache reduces repeat work,
but GitHub/upstream availability remains a required CI dependency. Its failure
fails the PR or production Build closed even for Core/MiniApp-only changes.
The minimum way to remove that third-party availability dependency while still
building Eros every run is to use a controlled first-party mirror of the exact
pinned source; until such a mirror exists, do not bypass or downgrade the Eros
build check.

### GSales release scope

GSales не входит в активную архитектуру релизов GPoint и отсутствует на Layer7.
Native workflow не запускает `tests/test_gsales_*` и не выполняет обязательные
проверки GSales runtime. Проверки общей PostgreSQL-интеграции, безопасности
GPoint и изоляции tenant остаются активными. Исторические тесты, исходный код,
документация и данные GSales сохраняются.

`dorny/paths-filter` supplies declarative path classification for five
components. The build-scope adapter compares each path set with its recorded
installed component source SHA; an unrecognized path, unavailable/invalid
baseline, or non-ancestor blocks. Deploy re-reads installed Kamal/Docker identity
and component baselines before mutation. The workflow-level
`gpoint-native-production-transaction` concurrency group serializes every
non-PR release from baseline discovery through Verify; PR runs use a separate
group. The Deploy job keeps its environment-specific lock as a second guard.

Migration planning and isolated PostgreSQL checks run only for schema or
infrastructure scope. Apply accepts only the exact approved pending migration
set with checksum and compatibility proof, then requires the applicable backup
and restore evidence. No pending migration means those gates are skipped. A
failed Apply blocks Kamal; database downgrade is never automatic. Eros Engine
changes require the exact candidate's stock Eros SQLx and synthetic conversation
E2E. Core/MiniApp-only changes skip that long Eros suite. For main pushes, the
workflow compares the candidate with the last successful Native source marker,
so skipped/failed prior releases keep Eros checks pending; an unavailable or
ambiguous marker requires the Eros suite. Live Kamal/Docker inventory still
selects the actual Eros Deploy.
Infrastructure changes require the protected manual route and its OWNER gate;
an ordinary automatic Deploy is not admitted.

Deploy applies only selected targets through stock Kamal 2, verifies health,
image digest, source SHA, topology and short HTTP smoke, and marks the native
GitHub Environment Deployment successful only after Runtime Verify. A skipped
Deploy makes no runtime claim. Failed or partial Apply blocks blind retry; the
operator must read the actual baseline and use the existing recovery/rollback
gates. Rollback selects a previously verified compatible component version and
never downgrades the database. Telegram Bot remains a single Kamal role, so
selective updates preserve its one-poller invariant.

### ME-62 release-code audit and reduction status

The Native First audit covers the whole executable path rather than only
counting the main workflow. The ordinary no-migration/no-Eros route currently
uses the `tests → build → deploy` GitHub job dependencies, one protected
production concurrency transaction, one selected Buildx image per unique
artifact, one stock Kamal Apply, post-Apply Docker/runtime identity checks,
and the required API/Mini App/Bot smoke. The tests matrix has two suites;
conditional PostgreSQL/Eros/Mini App work is skipped unless its path filter or
dispatch requires it. The deploy route still reads live DB and installed
topology facts so that a previously pending migration, a partial prior Apply,
or a stale baseline cannot be hidden by the current commit's paths.

The table below maps every executable helper directly referenced by the Native
workflow. “Retain” describes the guarantee the helper currently carries; it is
not a claim that this implementation is the final design. Later tasks must
remove a helper once the named native replacement is proven by the listed
contract tests and exact-SHA hosted CI.

<!-- ME62_HELPER_AUDIT_START -->
| Referenced helper | Function and Native First replacement to prove | Why any custom part remains / evidence |
|---|---|---|
| `scripts/run-pinned-kamal.sh` | One pinned Kamal CLI Docker entrypoint shared by inventory, ordinary Apply and rollback, preserving checkout scope and phase-specific environment propagation. | **KEEP, consolidated** — avoids three independent Docker invokers while enforcing the exact verified Kamal digest, linux/amd64 and no implicit pull. `tests/test_pinned_kamal_invoker.py`. |
| `scripts/gpoint-bot-bootstrap.sh` | One-time owner-scoped recovery of the absent Telegram Bot inside the existing protected Deploy, using a verified installed Core image and stock Kamal `--roles bot`. | Not an alternative release controller: fail-closed main-push authorization, live SHA/digest parity, Telegram webhook/identity and single polling checks, then Bot health; `tests/test_gpoint_bot_bootstrap.py`. No Core API/Worker/MiniApp/Eros restart. |
| `scripts/check-kamal-configs.sh` | Validate the pinned production Kamal config; prefer `kamal config` for parsing and validation. | Keep only GPoint's cross-file role/proxy contract if Kamal does not validate it; `tests/test_kamal_release_config.py`, `tests/test_kamal_proxy_routes.py`. |
| `scripts/docker_dependency_manifests.py` | Stage lockfile-derived dependency manifests for the actual BuildKit context. | Docker/Buildx consumes a context but does not generate the repository-specific manifests; `tests/test_docker_dependency_manifests.py`. |
| `scripts/eros-native-database.py` | Eros SQLx migration prepare/apply/rollback and isolated backup restore; use stock `pg_dump`, `pg_restore`, PostgreSQL and Eros SQLx migration commands. | Preserve exact history, checksum, identity, restore and partial-apply recovery unless the stock Eros migration command proves all of them; `tests/test_eros_native_database.py` and Docker/PostgreSQL integration. Runs only for Eros changes. |
| `scripts/eros-production-secrets.py` | Prepare the Eros service's protected environment; evaluate Kamal's native secrets mechanism and GitHub Environment secrets. | Existing helper enforces tenant, database ACL, env-file ownership/mode and read-only prerequisites for the externally sourced Eros image; `tests/test_eros_production_secrets.py`. Runs only for Eros deployment/bootstrap. |
| `scripts/native_release_intent.py` | Classify event/path intent and exact component scope from the installed Kamal/Docker role inventory; share declarative component path filters with CI. | Retain unknown-path fail-closed, Git ancestry and per-component joins not expressible by the action; `tests/test_native_release_intent.py`, `tests/test_release_scope_consistency.py`. |
| `scripts/prisma_migration_release_plan.py` | Compare applied/pending migrations against a candidate; use `prisma migrate status`/`deploy` for ordinary Prisma semantics. | Keep exact owner-approved migration set, immutable applied checksums and cross-stack compatibility where Prisma's own commands do not enforce them; `tests/test_prisma_migration_release_plan.py`, isolated PostgreSQL tests. |
| `scripts/resolve_native_environment_deployment.py` | Link the current protected Environment job and read the last successful Native release marker plus Runtime Verify's installed Eros source SHA. | The read-only Deployment/Actions lookup is scoped to cumulative infra and Eros test checks; the live component Deploy baseline still comes from Kamal/Docker. `tests/test_native_environment_deployment.py`. |
| `scripts/test-kamal-proxy-integration.sh` | Test Kamal's proxy routing and preserved unrelated services using isolated Docker. | Keep the local integration test until the same proxy/route contract is asserted by stock Kamal configuration validation; never run it against Layer7. |
| `scripts/verify-eros-runtime-labels.py` | Verify installed Eros image labels against source/upstream identity; prefer `docker image inspect --format` and direct expected-label comparisons. | The Python adapter is removable if shell/Docker checks cover every required label and fail closed; `tests/test_eros_runtime_labels.py`. |
| `scripts/verify-kamal-migration-parity.py` | Verify Prisma/AGATA/SQLx graph, applied history, approvals and compatibility; use each database stack's native status/history commands. | Keep only cross-stack approval, source ancestry and applied-history invariants not supplied by those commands; `tests/test_kamal_migration_parity.py`, isolated migration suites. This is a primary simplification target. |
| `scripts/verify-kamal-production-baseline.py` | Enumerate current live service/role topology; prefer stock Kamal app inventory and Docker inspect on its hosts. | Keep only mixed-version, shared database/cache and exact image identity checks absent from Kamal output; `tests/test_kamal_production_baseline.py`. |
| `scripts/verify-kamal-role.sh` | Check selected Kamal services and retained-role behavior; prefer `kamal app containers/images` and Docker labels. | Keep a thin assertion only if Kamal's role selection cannot establish the one-Bot/one-role invariant; `tests/test_kamal_role.py`. |
| `scripts/verify-layer7-ollama.py` | Read-only check of the existing Ollama accessory/model state. | This verifies a production dependency outside the image release path; preserve service state and replace only with a documented existing health endpoint/stock Kamal accessory check. `tests/test_layer7_ollama_verifier.py`. |
| `scripts/verify_native_eros_image.py` | Verify the stock Eros OCI image and its provenance. | Prefer GHCR pull by immutable digest, GitHub artifact-attestation verification, and Docker label/platform inspection; retain only Eros upstream/patch assertions not covered by those outputs. `tests/test_native_image_readback.py`. |
| `scripts/verify_native_image_attestation.py` | Parse BuildKit SBOM/provenance JSON and validate source/build args. | Replace with signed GitHub artifact attestations and `gh attestation verify` only after an exact-subject/repository/SHA/workflow test proves equivalence; never drop source or digest identity. `tests/test_native_image_attestation.py`. |
| `scripts/verify_native_image_readback.py` | Validate repository, immutable digest, platform and source labels from Docker inspect. | Prefer a small `docker pull IMAGE@DIGEST` plus `docker image inspect --format` assertion; retain only any compound identity rule not readable from Docker outputs. `tests/test_native_image_readback.py`. |
| `scripts/verify_native_rollback_evidence.py` | Join the selected historical Deployment, successful Verify run and component manifest. | Prefer GitHub Deployment/Actions records plus stock Kamal's retained old containers; retain proof that target SHA/digest was actually successful and DB-compatible. `tests/test_native_rollback_evidence.py`, rollback fixtures. This is a primary simplification target. |
| `scripts/write-kamal-production-manifest.py` | Persist per-component installed source/image/role identity for scope and rollback. | Replace with actual Kamal/Docker inventory and native GitHub Deployment metadata if those reconstruct partial/mixed state without a separate release-state file; current manifest remains only until read-back equivalence tests pass. `tests/test_kamal_production_manifest.py`, `tests/test_kamal_ordinary_manifest.py`. This is a primary simplification target. |
<!-- ME62_HELPER_AUDIT_END -->

| Function | Current code | Native mechanism | Decision |
|---|---|---|---|
| CI component path filters | Pinned `dorny/paths-filter` and `.github/filters/native-release.json` | Git and the pinned `dorny/paths-filter` action | **KEEP one declarative map** — CI and the minimal component-path adapter consume the same filters; no duplicate inline path table remains. |
| Event admission and exact-scope fail-closed rules | `scripts/native_release_intent.py` | GitHub event/dispatch context and workflow conditions | **KEEP** — policy checks reject unauthorized events, unsafe paths and migration scope; retired migration-rehearsal dispatch handling was removed because the separate protected operation is owned by `gpoint-operations.yml`. |
| Deleted paths in cumulative component diffs | Retired exact-path deletion allowlist | Git diff status plus the existing component/non-runtime path map | **REPLACE** — deleted runtime paths select their owning component; deleted release scripts and known non-runtime paths do not select an image; unmapped deletions still block. |
| Installed component source baselines | `scripts/verify-kamal-production-baseline.py` and `scripts/native_release_intent.py` | Stock Kamal plus Docker inspect of running image IDs, source labels and GHCR RepoDigests | **KEEP, live-first** — Apply selects against verified installed role inventory. The last successful Native Deployment SHA is read only as the infra-approval marker, not as a component baseline. |
| Build and OCI provenance | Build steps plus `scripts/verify_native_image_readback.py`, `verify_native_image_attestation.py`, `verify_native_eros_image.py` | Docker Buildx, GHCR digest, SBOM and GitHub artifact provenance | **KEEP** — retain only digest/source/platform/provenance checks not supplied by the native outputs. |
| Buildx build orchestration | Three component-specific `docker/build-push-action` steps | Buildx Bake with per-target metadata | **KEEP current steps** — the targets use distinct contexts, Dockerfile targets, labels and cache scopes. Each SHA-bound image is built once; live Kamal inventory still selects which immutable digest to deploy. |
| Native cross-run prepared-image reuse | Dispatch inputs, prepared-run API/log verification and digest fallback outputs | Buildx in the same Native release run | **DELETE** — production Build creates and reads back each required image in its own exact-SHA run. |
| Migration admission and parity | `scripts/prisma_migration_release_plan.py`, `scripts/verify-kamal-migration-parity.py` | Prisma, SQLx, isolated PostgreSQL and Alembic `ScriptDirectory` APIs | **KEEP custom release policy** — Prisma `migrate deploy` applies every pending migration and does not detect schema drift; it cannot enforce the exact approved pending set, migration source checksums, missing applied history, or cross-stack compatibility. Alembic graph discovery already uses its native `ScriptDirectory` API. Keep only GPoint's exact-set, immutable-history, compatibility and recovery rules around those native tools. See the [Prisma production command contract](https://docs.prisma.io/docs/cli/migrate/deploy). |
| Live role topology and component manifest | `scripts/verify-kamal-production-baseline.py`, `scripts/write-kamal-production-manifest.py` | Kamal 2 app inventory and Docker inspect | **KEEP, reduce** — the adapters verify identities and per-component provenance absent from Kamal's required machine-readable output. |
| Kamal diagnostic text capture | `kamal app details` and `kamal app containers` for each of three configs | `kamal app version` plus one authoritative Docker inspect inventory | **DELETE duplicate views** — the workflow cross-checks Kamal's version against live role versions; the Docker adapter verifies the complete running role set, image IDs, source labels, and immutable GHCR RepoDigests. The removed human-readable tables added remote queries but no independent acceptance evidence. |
| Eros database backup, rehearsal and recovery | `scripts/eros-native-database.py` | PostgreSQL `pg_dump`/`pg_restore`, stock Eros SQLx migrations | **KEEP, reduce** — preserve isolated restore, ledger and recovery checks; remove duplicate orchestration only after equivalent integration proof. |
| Standalone release readiness report | Retired `scripts/release-readiness.mjs` (71 LOC at baseline) | Native GitHub Deployment plus Runtime Verify | **DELETED** — no active workflow/package caller; historical audit remains in the archive. |
| Dormant unified release v1 contract | `scripts/unified_release_v1/` (286 Python LOC plus metadata) | Native V5 workflow and current component adapters | **DELETED** — repository-wide search found no active import, workflow, package or test caller; its README and project metadata point only to retired `gpoint-prepare.yml` / `gpoint-deploy.yml`. |
| Legacy Eros Pilot Compose deploy adapter | `scripts/eros-pilot-deployment.mjs` and its standalone unit test (150 LOC implementation) | Native Kamal production controller and its exact-SHA Runtime Verify | **DELETED** — repository-wide source/workflow/package audit found no runtime caller; production Eros services deploy through Native Kamal. Production Compose configuration was left intact. |
| GPoint scheduled backup, install and isolated restore | `gpoint-scheduled-backup.py`, `gpoint-backup-policy.py`, `verify-gpoint-scheduled-backup-restore.sh`, and install scripts (2,169 LOC in this candidate) | systemd timer, PostgreSQL tools, Bunny S3 and isolated restore | **KEEP, out of app-release scope** — these independently protect production data; no replacement or equivalent continuity proof exists, so they are not removed to meet the LOC target. |
| Database migration and parity | `agata-production-migration.py`, `eros-native-database.py`, Prisma checksum/planning tools, and `verify-kamal-migration-parity.py` (2,071 LOC) | Prisma, SQLx, PostgreSQL and Alembic | **KEEP, reduce only with proof** — the stacks have distinct databases and compatibility/approval rules. The current audit did not establish safe removal of exact-set, checksum, backup, restore or recovery checks. |
| Image, Kamal and proxy verification | `verify_native_*`, Kamal topology/role checks, `check-kamal-configs.sh`, and `kamal_proxy_routes.py` (574 LOC) | Buildx, GHCR, stock Kamal and Docker inspect | **KEEP, reduce** — custom checks bind immutable digest, source identity, SBOM/provenance, installed role topology and proxy routing where native tools do not provide the combined release contract. |
| Local pre-push gate and Layer7 Ollama verifier | `classify-release-prepush.py` and `verify-layer7-ollama.py` (235 LOC) | Git and read-only service checks | **KEEP, outside Native Apply** — the former enforces repository release policy; the latter verifies an existing Layer7 service and is outside this task's permission to alter. |

Historical LOC snapshot measured against `3d680fa153fd6aebf786455d6f36d3b1a67cc78f`
(main at task start), using the Native workflow plus the unique `scripts/` files
referenced by it. LOC figures are informational snapshots, not current counts,
CI thresholds, or acceptance criteria:

| Measure | Main baseline | Historical candidate snapshot | Change |
|---|---:|---:|---:|
| Main controller + five control adapters (six paths) | 4,562 | 3,871 | −15.1% |
| Workflow LOC | 2,437 | 2,363 | −3.0% |
| Referenced helper LOC | 5,439 | 4,572 | −16.0% |
| Combined workflow + referenced scripts LOC | 7,876 | 6,935 | −12.0% |
| Extended release/deploy/migration/backup inventory LOC | 12,486 | 9,098 | −27.1% |
| Referenced helper files | 21 | 19 | −2 |
| Required test matrix runs | 4 | 2 | −50% |
| Ordinary workflow named jobs | 3 | 3 | unchanged |
| Ordinary release wall time | not measured | not measured | pending production run |

Counts use source-line totals (`git show <revision>:<path> | wc -l`) from each
revision's inventory; helper names and workflow references are resolved at
that revision, not from the candidate only. The six-path controller measure is
the exact executable set used by the earlier ME-62 count: the Native workflow,
`native_change_surface.py`,
`native_release_intent.py`,
`verify_native_rollback_evidence.py` and `write-kamal-production-manifest.py`.
The extended inventory includes workflow and action files under
`.github/workflows/` and `.github/actions/` whose names contain `release`,
`deploy` or `layer7`, plus tracked `.py`, `.mjs`, `.js` and `.sh` files under
`scripts/` whose path contains `release`, or whose filename contains `deploy`,
`rollback`, `migration`, `backup`, `kamal`, `native` or `layer7`. Test files (`*.test.*`, `test-*`,
`test_*`, `*_test.py` and `_test.sh`) are excluded.
This filename-based inventory is an upper bound, not a precise production-Apply
LOC count. The referenced-helper count includes
workflow-invoked CI validation scripts, so that combined figure is an upper
bound for production Apply code. The Native cross-run digest reuse adapter was
removed; production dispatch now builds the required images in the same run.
The matrix consolidation cuts redundant runners and PostgreSQL services in
half, and `native_change_surface.py` was replaced with declarative filters.
The seven complexity measures from the OWNER specification are kept separate.
Shared Native adapters are counted once; non-release backup/install utilities
outside the workflow's call graph are not credited as deletions. The fixed
baseline and selectors are unchanged.

| Measure | Main baseline | Historical candidate snapshot |
|---|---:|---:|
| Native production workflow LOC | 2,437 | 2,363 |
| Release-specific referenced-helper LOC | 4,257 | 3,720 |
| Shared referenced-helper LOC (unique source files) | 1,182 | 852 |
| Production release controllers | 1 | 1 |
| Additional Layer7 staging deployment workflows | 1 | 0 |
| Maximum distinct production images in one full run | 3 | 3 |
| Custom prepared-run/production-manifest state formats | 2 | 1 |
| Core-only ordinary Deploy steps (no migration/Eros/rollback) | 17 | 17 |

The three shared files are `native_release_intent.py`,
`resolve_native_environment_deployment.py`, and
`verify_native_image_attestation.py`; each has multiple Native helper callers.
The Core-only path builds one `gpoint-runtime` image once. The four-image value
is the maximum for a full component run, not four builds on every release. The
17 Deploy steps include exact-SHA/main recheck, live Kamal/Docker baseline and
topology, candidate digest/DB compatibility checks, the migration-status
command (which must remain a no-op with no pending migrations), protected
Environment linkage, stock Kamal Apply, Runtime Verify/smoke, and timing. Eros,
rollback, and failure-recovery steps are conditional.

These tables are historical informational snapshots, not acceptance gates. The
architecture tests enforce functional invariants and do not fail when LOC
changes.

The historical candidate inventory counted source lines in files that remained on
disk and preserved the baseline selectors and denominator. The Native workflow
invokes `resolve_native_environment_deployment.py` during Runtime Verify, so
that helper and its tests remain active and are included in that historical helper
count. That candidate replaced the exact retired-path deletion list with
status-aware scope classification. Runtime deletions now select their owning
component; a deleted release script has no image impact, while cumulative
infrastructure changes still require the protected dispatch route. Unmapped
deletions continue to fail closed. Exact retired Layer7 staging paths are
ignored only when deleted from the cumulative candidate diff; new and
reintroduced staging paths fail closed. Same-repository PR Build jobs request `packages: write` and can publish
candidate images to GHCR; fork PRs do not run that job. This permission does
not grant production Deploy access. The deleted
staging workflow/action and obsolete prepared-run adapters account for much of
the extended-inventory reduction. LOC remains an informational measure, not an
acceptance gate. At candidate `0b721128f910748fe3643ffe1b83de7d9f27559b`, the
six-path measure is down 15.1%, workflow plus referenced helpers is down 12.0%,
and the extended inventory is down 27.1%. The ordinary-only Kamal manifest also
removed an obsolete cutover branch and its tests. Active migration, backup,
recovery, digest and runtime identity checks remain. No staging evidence is
applicable. Exact-SHA hosted Tests, isolated PostgreSQL, Eros/Mini App E2E,
Buildx and GHCR digest read-back passed in run
[44757686891](https://github.com/akhripunov/gpoint/actions/runs/44757686891).
The PR event correctly skipped production Deploy/Verify. Production Deploy,
Runtime Verify and smoke remain pending separate OWNER authorization for the
exact integrated SHA.

### ME-62 acceptance scenario matrix

Scenario rows use the exact tests named in each row; current PR candidate is
`0b721128f910748fe3643ffe1b83de7d9f27559b`, base
`3d680fa153fd6aebf786455d6f36d3b1a67cc78f`. Exact-head hosted run
[44757686891](https://github.com/akhripunov/gpoint/actions/runs/44757686891)
passed Python, Workspace, isolated PostgreSQL, Eros and Mini App E2E, all four
Buildx images and GHCR digest read-back. The PR event correctly skipped
production Deploy/Verify. `PASS` below means the stated
local/hosted test evidence passed; it does not imply production execution.

| # | Input | Expected → actual | Result and evidence |
|---:|---|---|---|
| 01 | Docs-only diff | CI only; no image or Deploy → planner returns no image scope | PASS — `test_build_plan_skips_images_for_documentation_only_diff`; `test_documentation_only_candidate_verifies_runtime_without_restarting_components` |
| 02 | Core-only diff | Core only → Core selected | PASS — `test_build_plan_uses_per_component_sources_and_builds_core_for_prisma_migrations` |
| 03 | Mini App-only diff | Mini App only → Mini App selected | PASS — `test_component_scope_regressions_keep_eros_independent` and path planner tests |
| 04 | AGATA-only diff | AGATA only → AGATA selected | PASS — `test_shared_dependency_and_agata_migration_paths_select_their_real_images` |
| 05 | Eros Engine-only diff | Engine plus required Eros E2E → selected; exact Eros E2E passed | PASS — `test_cumulative_engine_scope_runs_e2e_and_build_requires_exact_pass`; run 44711957261 |
| 06 | Eros Relay-only diff | Relay only; no Core deploy/Engine E2E → separate relay scope | PASS — `test_eros_relay_source_updates_only_relay_with_shared_runtime_image` |
| 07 | Core and installed Relay share image | One runtime image, independent deploy scope → shared image selected once | PASS — `test_shared_runtime_dockerfile_builds_core_once_for_core_and_installed_relay` |
| 08 | Shared dependency diff | Every dependent image → dependency planner selects affected images | PASS — `test_shared_dependency_and_agata_migration_paths_select_their_real_images` |
| 09 | Unknown file | Fail closed → blocked | PASS — `test_unknown_paths_block_instead_of_becoming_a_deploy_or_silent_prepare` |
| 10 | Unknown installed baseline | Fail closed → baseline resolution blocks | PASS — `test_push_apply_gate_uses_verified_accumulated_production_baseline` and resolver tests |
| 11 | Different component baselines | Compare each component independently → staggered baseline test passes | PASS — `test_staggered_component_baselines_do_not_redeploy_already_updated_components` |
| 12 | Earlier component release skipped | Preserve unshipped changes → cumulative planner retains scope | PASS — `test_component_scope_regressions_keep_eros_independent` and build-scope tests |
| 13 | Earlier component release failed | Preserve unshipped changes → exact source baselines retain scope | PASS — failed/skipped source cases in native intent suite |
| 14 | Partial component release | Determine actual state and fail closed → per-role runtime identity checks block inconsistency | PASS — `test_core_consumers_are_updated_one_role_at_a_time_and_partial_failure_fails_closed`; production recovery itself NOT VERIFIED |
| 15 | Infra commit A followed by API commit B | Ordinary push cannot bypass protected infrastructure scope → cumulative infra test blocks | PASS — `test_api_commit_cannot_bypass_approval_for_accumulated_infrastructure` |
| 16 | Migration-free release | No unnecessary migration/backup gates → pending-migration plan is empty | PASS — migration plan tests and exact candidate CI; production apply NOT VERIFIED |
| 17 | Safe migration | Apply only exact approved plan → isolated PostgreSQL migration/integration passes | PASS — exact run 44711957261; no production migration performed |
| 18 | Dangerous/unapproved migration | Protected approval required → classifier rejects unsafe migration scope | PASS — `test_agata_migration_and_unknown_schema_paths_remain_blocked_from_prisma_apply` |
| 19 | Migration failure | Stop Apply; never auto-downgrade DB → failure path blocks, no rollback migration | PASS — migration and deploy boundary tests; production DB unchanged |
| 20 | Eros Engine change | Run relevant SQLx/E2E → exact hosted Eros E2E passed | PASS — run 44711957261 |
| 21 | Ordinary infrastructure-only PR | Skip irrelevant Eros E2E and images → path-selection tests show no app scope | PASS — `test_known_release_infrastructure_candidate_has_no_application_component_scope`; full-build label on this acceptance PR intentionally overrides selective Build |
| 22 | Concurrent production runs | Serialize without cancelling baseline/Apply → shared non-cancelling lock asserted | PASS — `test_production_mutations_share_one_non_cancelling_lock` |
| 23 | Image build failure | Deploy forbidden → Deploy requires successful Build | PASS — `test_main_push_runs_exact_candidate_tests_build_kamal_and_verify` and routing matrix |
| 24 | GHCR publication/read-back failure | Deploy forbidden → PR exact Build and digest read-back passed | PASS — actual GHCR Build/read-back in run 44711957261; failure gate covered by boundary tests |
| 25 | Wrong digest or provenance | Deploy forbidden → immutable identity/provenance validators reject mismatch | PASS — image validation suite and exact Build read-back in run 44711957261 |
| 26 | Wrong production topology | Deploy forbidden → topology preflight rejects non-Kamal/Compose state | PASS — `test_ordinary_release_rejects_compose_topology_before_deploy`; live production topology NOT VERIFIED |
| 27 | Incompatible rollback target | Reject target → exact previous deployment evidence is required | PASS — rollback evidence suite; no production rollback performed |
| 28 | Compatible production rollback | Kamal rollback plus smoke → not executed | NOT VERIFIED — requires separate OWNER authorization for exact SHA |
| 29 | Single Telegram Bot polling role | One active role → config/verification enforces one running Bot | PASS — `test_core_consumers_are_updated_one_role_at_a_time_and_partial_failure_fails_closed`; live process count NOT VERIFIED |
| 30 | Production secrets on PR | No production secrets in PR job → Tests job has read-only token permissions; Build uses GHCR write only | PASS — workflow permission tests and run 44711957261; no production secret exposure |
| 31 | Fork PR | No GHCR publish or production Deploy → Build condition excludes fork | PASS — `test_pr_candidate_runs_build_without_production_deploy`; no fork run was created |
| 32 | Same-repository PR | Test Build allowed; Deploy forbidden → Build/GHCR read-back passed, Deploy skipped | PASS — run 44711957261 |
| 33 | Protected infrastructure dispatch | OWNER/ref/exact SHA and Environment protection required → classifier and workflow gates assert these conditions | PASS — Native intent and boundary tests; live GitHub protection settings NOT VERIFIED |
| 34 | Current Layer7 | Production target only, never staging → workflow has only `production` Environment | PASS — `test_layer7_is_production_and_retired_staging_paths_cannot_deploy`; live VPS identity NOT re-read in this task |
| 35 | Retired staging paths | No active release calls → workflow/configs absent; exact retired paths are accepted only as deletions and additions/reintroductions are blocked. Other `staging` mentions are dated history, test fixtures, or local atomic/temp-directory operations; none names a GPoint environment or server. | PASS — `test_layer7_is_not_configured_as_a_staging_release_target`, `test_new_staging_path_is_not_silently_ignored`, and run 44719646468 |

Production-only checks explicitly remain unverified. No staging infrastructure
or replacement workflow exists or is required. Do not interpret passing local
or hosted tests as production acceptance.

## Local test selection under FULL

FULL is a risk level, not a mandate to run every local test. `classify-release-prepush.py` remains the canonical FAST/FULL procedure. Hosted CI uses the pinned `dorny/paths-filter` action and `.github/filters/native-release.json` as the single path map. PR Eros E2E selection uses its `eros_e2e` filter; main uses the same filter against changes accumulated since the last successful Native deployment, with E2E as the fail-safe when the marker cannot be verified. Build always creates three images. Deploy compares exact candidate changes with each component's installed Kamal/Docker source SHA and fails closed on unknown paths or ambiguous live inventory. PostgreSQL, Redis, Ollama and Kamal Proxy remain infrastructure, not images. AGATA and Eros Relay are outside the active runtime map.

`dorny/paths-filter` accepts one exact commit SHA as `base`. PR `base` is the exact target SHA; push filters use the event's `before` SHA. GitHub's pull-request read permission lets the pinned action obtain PR file changes. A single action invocation cannot compare each production component against its independently installed SHA. The minimal Python adapter therefore consumes the same JSON filter definitions for cumulative per-component diffs after read-only Kamal/Docker inventory; it contains no second component path table. This preserves skipped changes and rejects unmapped/deleted inputs. See the [`dorny/paths-filter` input and output contract](https://github.com/dorny/paths-filter#usage).

| `classify-release-prepush.py` | Keep outside CI | Implements local FAST/FULL preparation rules only; it is not a release router and does not weaken hosted gates. |

No second release controller or duplicate Deployment-status writer is in the
ordinary Native workflow. The supplemental Docker checks cover exact container
identity, OCI digest and persistent mounts that Kamal inventory does not expose;
they follow `kamal app version/details/containers` and do not replace Kamal.
Recovery, rollback, backup maintenance and first-cutover paths remain because
they have no verified equivalent in the ordinary app release path.

AGATA migration graph проверяется установленной версией Alembic из
`services/agata/pyproject.toml`: verifier использует `ScriptDirectory`,
`walk_revisions()` и `get_heads()`, а не извлекает revision поля собственным
разбором Python AST. Deploy устанавливает этот же пакет до проверки live и
candidate heads/checksums. Это позволяет Alembic разрешать стандартные
выражения revision и parent так же, как приложение; граф с несколькими heads
или некорректными ссылками блокируется.

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
Known non-runtime paths receive scope `NONE`: root `README.md`, Markdown under
`docs/`, `tests/`, `.github/workflows/*.yml` and `*.yaml`, `AGENTS.md`, `.codex/roles/*.md`,
the exact release/backup helpers listed in `KNOWN_NON_RUNTIME_FILES` in
`scripts/native_release_intent.py`, the production Prisma allowlist manifest,
exact Prisma migration SQL paths matching `PRISMA_MIGRATION` in
`scripts/native_release_intent.py`, and `systemd/gpoint-backup.service`. This lets a release-infrastructure-only
candidate use the protected Deploy and Runtime Verify without restarting
application roles or installing backup-service changes. A changed Prisma
migration remains subject to the separate migration planner and Apply gates.
Any other non-component path, unsafe path, deletion, or rename still blocks
before DB preflight/Apply and requires explicit component-scope approval.
Runtime Verify проверяет все шесть ролей, digest, source SHA, version, Docker
image ID неизменённых ролей, health и database/cache identities. PostgreSQL,
Redis, proxy и неизменённые компоненты не перезапускаются.

До дополнительной Docker-инспекции Deploy вызывает `kamal app version` для
Core, AGATA и Mini App и сверяет версии с соответствующими запущенными ролями.
Отдельные текстовые `kamal app details` и `kamal app containers` удалены как
повторные диагностические запросы: окончательный состав ролей, единые версии и
source SHA, OCI digest/image ID, количество работающих контейнеров, mounts и
сетевую/database идентичность проверяет read-only Docker inventory.

До сборки отдельный Build-шаг читает только GitHub Production Deployment history
и связанный успешный Native Actions run. Он проверяет точный workflow, SHA,
attempt, Runtime Verify status и manifest digest, содержащий независимые source
SHA Core, AGATA и Mini App. Кандидатный diff сравнивается отдельно с source SHA
каждого компонента; известные пути выбирают только затронутые образы. Prisma
migration-only изменение также собирает CORE image для Apply, не меняя runtime
Deploy scope. Неизвестный путь, неуспешный последний Deployment, неполный
metadata или неоднозначная история останавливают Build. Если есть только
успешный legacy Deployment без component metadata, выполняется один полный
bootstrap build.

Этот ранний план не получает production secrets и не заменяет чтение VPS. После
сборки защищённый Deploy сверяет точные per-component source SHA и Deploy scope
со свежим шестиролевым Kamal inventory. Любое изменение baseline между Build и
Deploy блокирует шаг до database preflight/Apply. Для неизменённых компонентов
используется их текущий runtime digest; candidate digest применяется только к
собранным и выбранным компонентам. Runtime Verify записывает manifest digest и
три source SHA в GitHub Deployment URL, чтобы следующая сборка получила
проверяемый baseline. Изолированная migration rehearsal собирает CORE и AGATA;
PR тесты не читают production Deployment history и не публикуют образы.

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
`sha`, production environment, task, run URL и попытке; GitHub `ref` может быть
именем triggering branch и не заменяет проверку deployed SHA. Неоднозначность
блокирует job. Успешный Verify отдаёт в `environment.url` URL с SHA-256 runtime
manifest, поэтому автоматический SUCCESS связан с тем же проверенным manifest.
Отдельные ручные POST статусов в обычном пути не нужны.

Rollback использует тот же штатный GitHub `environment: production`, поэтому
Environment создаёт Deployment, связанный с SHA текущего защищённого workflow.
После полного Runtime Verify `environment.url` фиксирует SHA-256 manifest,
выбранный runtime target SHA и source SHA каждого компонента. Deployment SHA
остаётся SHA workflow, а проверенный manifest описывает фактически восстановленную
версию. Это различие явно сохраняется при следующем rollback resolver.

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
текущих Prisma/Alembic heads и сохранность DB/Redis IDs и mounts. Роли каждой
компонентной группы возвращаются к версии, source SHA и digest из manifest;
разные группы могут иметь разные версии и source SHA. Kamal rollback выполняется
по одной роли: consumers раньше API. Закреплённый Kamal 2.12.0 вызывается как
`kamal rollback <version> --roles <role>`: версия — позиционный аргумент stock CLI. Полный runtime Verify должен
завершиться до GitHub Deployment SUCCESS. Любая неоднозначность блокирует
операцию; production DB migrations автоматически не откатываются. Пока нет
предыдущего Native Release с подтверждённым successful manifest, rollback
цель отсутствует и workflow должен отказать до изменения runtime. Состояние и свежесть
scheduled backup не являются условием обычного релиза и должны контролироваться
отдельным мониторингом ([ME-56](https://linear.app/bestuzhev/issue/ME-56/gpoint-independent-scheduled-backup-failure-and-freshness-alert)).
Установка/обновление timer и один первичный backup выполняются отдельной
защищённой процедурой `scripts/gpoint-backup-install.sh` с точным main SHA и
явным OWNER approval (`APPROVE_PRODUCTION=true`). Обязательные
backup/restore проверки перед миграциями и опасными
инфраструктурными операциями, offsite gate первого cutover и прочие действующие
backup/offsite требования не меняются. Удалять recovery/legacy путь можно
только после отдельной авторизации и успешной production Verify.

### Инвентаризация workflows и прежних release helpers

Статусы основаны на workflow calls, package scripts, imports, tests и
recovery/transition paths. Отсутствие автоматического вызова не доказывает
отсутствие ручного или внешнего использования. Таблица фиксирует безопасное
условие удаления; для явно одобренного удаления ниже зафиксирован результат текущего P0.

| Путь | Статус | Замена / назначение | Test evidence | Условие безопасного удаления |
|---|---|---|---|---|
| `.github/workflows/gpoint-release.yml` | ACTIVE | Канонический Tests → Build/GHCR → protected Kamal Deploy → Runtime Verify; штатный GitHub Deployment для обычного Deploy. | `tests/test_kamal_native_automatic_release.py`, `tests/test_kamal_release_config.py`, `tests/test_native_rollback_evidence.py`, `tests/test_native_build_boundary.py`. | Не удалять; заменять только отдельным OWNER-approved workflow с равными или более строгими gates и доказанной миграцией активных callers. |
| `.github/workflows/miniapp-recovery.yml` | **REMOVED 2026-11-22** | Удалён устаревший параллельный Mini App deploy controller; recovery/deploy остаётся в каноническом protected `gpoint-release.yml`. | `tests/test_simple_release_architecture.py`, `tests/test_native_environment_deployment.py`. | — |
| `.github/workflows/gpoint-deploy.yml` | **REMOVED 2026-11-12** | Legacy Deploy заменён `gpoint-release.yml`. Незавершённых legacy-попыток не осталось (все runs завершены; 5 записей `inactive` от 18–19.10 — история). | — | — |
| `.github/workflows/gpoint-prepare.yml`, `.github/workflows/gpoint-recover-interrupted.yml` | **REMOVED 2026-11-12** | Recovery больше не требуется: Native First выполняет forward-resume повторным dispatch точного SHA (pending-миграции считаются по live-БД, Kamal сходится), а `rollback` job восстанавливает предыдущий совместимый релиз без downgrade БД. | `tests/test_native_release_intent.py`, `tests/test_native_rollback_evidence.py`, `tests/test_prisma_migration_release_plan.py`. | Новый механизм не добавлялся: существующих достаточно. |
| `.github/workflows/gpoint-first-kamal-cutover.yml` | **REMOVED 2026-11-12** | Первый Kamal-cutover завершён (10 успешных прогонов); production с 04.11 работает на Kamal-сервисах, замена — `gpoint-release.yml`. | — | — |
| `.github/workflows/gpoint-linear-reconcile.yml` | **REMOVED 2026-11-12** | Публикация релизных фактов в Linear дублировала канонический журнал. По «Правилам релизов» (стр. 101) технический журнал — GitHub Deployments; Linear хранит задачи и решения. | — | Журнал релизов больше не обновляется автоматически; статусы задач Linear ведутся вручную. |
| `scripts/resolve_native_environment_deployment.py`, `tests/test_native_environment_deployment.py` | ACTIVE | Resolve the current protected Environment Deployment and verify the last successful Native run identity; read the Eros source SHA recorded by Runtime Verify. | `tests/test_native_environment_deployment.py`. | Preserve exact workflow/source identity; failed or non-Native runs never advance cumulative markers. |
| `scripts/release-gpoint-vps-adapter.mjs`, `scripts/release-gpoint.mjs`, `scripts/automated-release.mjs`, `scripts/prepare-release.py` | LEGACY / REQUIRED FOR RECOVERY / UNKNOWN BY ENTRYPOINT | Старый Compose release/apply и orchestration; отдельные функции остаются в старых workflows, cutover, static exchange или recovery. Обычный выпуск заменён `gpoint-release.yml` + stock Kamal. | `scripts/release-gpoint.test.mjs`, `scripts/release-orchestrator.test.mjs`, `scripts/test_prepare_coalescing.py`, `tests/test_prepare_release_migration_inventory.py`. | Не удалять по названию. Сначала доказать отсутствие workflow/package/manual callers, перечислить каждую оставшуюся функцию и перевести её caller с regression и recovery acceptance. |
| `infra/production/docker-compose*.yml`, `infra/shared/docker-compose.yml`, `infra/docker/docker-compose.yml` | LEGACY / TRANSITION / ACTIVE FOR SCOPED SERVICES | Compose-конфигурация нужна для оставшихся cutover, Eros/GSales, shared services или recovery; штатный Native app lifecycle выполняет Kamal. | `tests/test_eros_permanent_compose.py`, `tests/test_eros_vps_pilot_compose.py`, `tests/test_kamal_release_config.py`. | Удалять пофайлово после отсутствия production/manual callers, закрытого соответствующего transition и доказательства сохранения shared DB/cache, volumes, network, health и rollback. |
| `scripts/verify-gpoint-backup-restore.sh` | REQUIRED FOR RECOVERY / CUTOVER | Защищённое одноразовое восстановление и cutover evidence; routine Bunny backup использует `gpoint-scheduled-backup.py` и `verify-gpoint-scheduled-backup-restore.sh`. | `tests/test_kamal_release_config.py`, `tests/test_gpoint_media_tree.py`, `tests/test_recover_kamal_cutover.py`. | Только после успешной production restore rehearsal через принятую замену и подтверждения, что ни один recovery workflow/caller не использует этот путь. |
| `scripts/gpoint-scheduled-backup.py`, `scripts/verify-gpoint-scheduled-backup-restore.sh`, `scripts/gpoint-backup-policy.py` | ACTIVE | Отдельный systemd backup runner: штатный PostgreSQL archive, Bunny upload/read-back и isolated restore из одного снимка. Не входит в обычный app Deploy. | `tests/test_gpoint_scheduled_backup.py`; в том числе проверка upload → exact-object read-back → restore. | Не удалять до OWNER-approved replacement и подтверждённой непрерывности расписания, freshness/failed-run monitoring, Bunny restore proof и recovery acceptance. |
| Rollback в `.github/workflows/gpoint-release.yml` | ACTIVE | Использует штатный production Environment Deployment текущего workflow SHA; после Runtime Verify записывает manifest SHA, выбранный runtime target SHA и source SHA всех компонентов в Environment URL. Компонентные версии и digests восстанавливаются раздельно из проверенного manifest. | `tests/test_kamal_native_automatic_release.py`, `tests/test_native_rollback_evidence.py`. | Сохранять exact-main/OWNER gates, native Environment lifecycle, manifest-bound runtime metadata, component identity и SUCCESS только после полного Verify. |
| Docker lifecycle в `scripts/release-gpoint-vps-adapter.mjs` и cutover/recovery helpers | LEGACY / REQUIRED FOR TRANSITION OR RECOVERY | Для обычных Native app контейнеров заменено stock `kamal deploy --skip-push`, `kamal app stop` и `kamal rollback`. Остальные команды могут быть одноразовыми technical DB/backup или переходными операциями. | `tests/test_kamal_native_automatic_release.py`, `tests/test_kamal_cutover_owner.py`, `tests/test_gpoint_scheduled_backup.py`. | Удалять только после caller-by-caller доказательства, что операция управляет application lifecycle и stock Kamal эквивалентен; DB/backup technical actions не считать дублирующим app lifecycle. |

Удалён `scripts/unified_release_v1/`: активные callers отсутствуют, а оставшиеся
ссылки указывали на workflows, удалённые 12.11.2026. Это локальное решение по
подтверждённой инвентаризации, не меняющее текущие release gates.
Финальная классификация runtime/production callers остаётся `PROD_NOT_VERIFIED`;
таблица не утверждает, что старые файлы не используются вручную. Исторические
procedures ниже текущей модели помечены отдельно и не являются рабочими
instructions.

## Архивная модель артефактов и состояния (ME-42)

Этот раздел сохраняет правила прежнего Prepare/OCI-receipt потока. Для обычных
релизов действует Native First V3 + Kamal 2 в разделе `ME-52` выше; этот архивный
поток не является fallback и не заменяет его. Все команды ниже этого заголовка
описаны только для исторических или отдельно сохранённых recovery callers.

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

## Исторические процедуры (заменены Native First V3; ME-42 также архивный)

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


## Исторический Mac Worker и GSales lane contract

Исторический контракт GSales Worker ниже не является release gate. Он сохранён
для истории; текущая Native First архитектура не требует GSales runtime,
GSALES lane probes или GSales DB/Core RLS checks.

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
