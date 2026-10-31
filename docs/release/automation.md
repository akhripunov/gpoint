# Как выпускать GPoint

## ME-52: локальный Kamal candidate (2026-10-27)

Эта реализация остаётся локальной и не описывает уже переключённое production:
до отдельно разрешённого и успешно проверенного cutover действующая Compose/V5
операция и recovery остаются актуальными. Обычные будущие релизы используют
`.github/workflows/gpoint-native-release.yml` (Workspace CI + Python CI →
immutable Core/AGATA/Mini App images → exact-SHA protected Kamal Deploy/Verify).
Разовый переход обслуживает `.github/workflows/gpoint-first-kamal-cutover.yml`
и требует свежий inventory, encrypted backup/isolated restore, migration
parity, паузу старых consumers и полную runtime Verify.

У workflow есть обязательный `allow_source_update`, по умолчанию `false`.
Его включают только для отдельно OWNER-approved exact candidate, если при
первоначальном переходе Kamal application source должен продвинуться дальше
live Compose `RELEASE_SHA`. Тогда проверяется корректный непустой текущий
Mini App static tree, а новый immutable image независимо проверяется по exact
digest/provenance. Это не отменяет migration parity, backup/offsite/isolated
restore, recovery, consumer и runtime Verify gates.

Публичный TLS/HTTP вход после перехода обслуживает Kamal Proxy на портах 80 и
443: `/api` и `/media` идут в Core, `/` и Mini App paths — в Mini App. Caddy
останавливается в cutover и сохраняется только вместе с checksum как recovery
material. GSales не собирается и не стартует в обычных релизах; Eros,
Sensitive Detector и ACL/pilot остаются вне обычного release scope.

Это локальное описание не разрешает публикацию, Actions, GHCR build, production
Apply, изменение БД/инфраструктуры или retirement Compose/V5. Удалять legacy
путь можно только после отдельной авторизации и успешной production Verify.

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
