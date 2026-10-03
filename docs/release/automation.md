# Как выпускать GPoint

## Два режима разрешения выпуска

Актуальные условия: [Release authorization modes](../RELEASE_AUTHORIZATION.md).
По умолчанию владелец подтверждает подготовленный exact SHA. Для небольшой
конкретной задачи «Исправь [задачу] и выкатывай» разрешает один Task-scoped Auto
Release без повторного запроса SHA. Dev фиксирует scope, полный unreleased diff,
разрешение, exact SHA и проверки в Linear до apply и передаёт существующий
production approval input по этому разрешению. Рискованные изменения, миграции,
провал gates или расширение scope останавливают этот режим. Инструкции ручного
согласования ниже описывают обычный Exact-SHA режим. Все технические проверки
одинаковы в обоих режимах; это не автоматический deploy каждого push.

## Для владельца

1. Архитектор задаёт изменения и критерии приёмки. Dev реализует их и отправляет чистый commit в `origin/main`.
2. Откройте [GPoint — Prepare](https://github.com/akhripunov/gpoint/actions/workflows/gpoint-prepare.yml). GitHub сам запускает проверки после push. Ждите завершения всего workflow, включая публикацию в Linear.
3. В [Архитектор — Handoff](https://linear.app/bestuzhev/document/arhitektor-handoff-36f91afb3d2c) смотрите блок **Автоматизация: кандидат**. READY — кандидат проверен; это ещё не выкладка. Dev должен объяснить изменения, миграции, backup и риски перед вашим подтверждением.
4. Откройте [GPoint — Deploy](https://github.com/akhripunov/gpoint/actions/workflows/gpoint-deploy.yml), нажмите **Run workflow**, выберите **main**, вставьте полный SHA из успешного Prepare, выберите режим **production**, отметьте **I authorize this exact candidate…**, затем **Run workflow**. Это ваше разрешение на production для этого SHA.
5. Следите за шагами Validate → Deploy. Mac должен быть включён, иметь сеть, активную CLI authentication и работающий GPoint runner. Incy остаётся включённым. Пока runner не подключён, Deploy останется Queued — это не работающий автоматический деплой.
6. После технического COMPLETE откройте Telegram → GPoint → Models Hub. Отправьте новое сообщение и дождитесь ответа. Сообщите Dev «ответ получил» или «ответа нет». Dev фиксирует подтверждение сценария в журнале; автоматизация не создаёт тестовые production-диалоги от вашего имени.

История: [Журнал релизов](https://linear.app/bestuzhev/document/zhurnal-relizov-640efa60dacf). Каждый результат содержит SHA и ссылку на GitHub run. В Handoff история и архитектурные ограничения сохраняются, автоматизация меняет только свои именованные блоки.

Можно пользоваться через Dev: «Подготовь релиз и дай SHA со ссылкой на проверки», затем «Разрешаю выкатить SHA …». Dev обязан проверить успешный Prepare, использовать его артефакты и canonical entrypoint. Кнопка Deploy удобнее тем, что фиксирует оператора и запускает стандартные шаги без пересказа команд.

Режим **preflight** в той же форме проверяет доступ, live baseline, worker/config и артефакты без применения миграций и переключения runtime. Он выбран по умолчанию и не требует production approval. Результат PLANNED означает «можно планировать выкладку», а не «релиз уже выпущен».

## Значение состояний

| Состояние | Значение и следующее действие |
| --- | --- |
| READY | Этот SHA прошёл проверки, Docker/БД репетицию; владелец может разрешить выпуск |
| FAILED в Prepare | Production не изменён; Dev исправляет первый упавший шаг |
| Queued в Deploy | Исполнитель занят/выключен/не подключён; проверять Settings → Actions → Runners |
| PLANNED | Preflight завершён; production не переключался |
| COMPLETE в manifest | Runtime, health, parity, миграции и production ref технически проверены |
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
4. Зарегистрировать repo-scoped runner для `https://github.com/akhripunov/gpoint`, имя `gpoint-owner-mac`, label `gpoint-production`. Временный registration token из GitHub не сохранять в Git/Linear/логи. Установить пользовательский сервис официальным `svc.sh install` / `svc.sh start` согласно инструкции GitHub. Проверить **Idle** в GitHub. На Mac разрешить sleep policy по решению владельца: отключённый/спящий Mac не может выполнять релиз или отвечать через mac_worker.
5. PATH сервиса должен содержать рабочие `node`, `pnpm` и `python3`, а git — читать подтверждённые known_hosts. Проверить отдельные GPoint SSH/SCP runners, key/env, Mac worker status. Не менять Incy/DNS/hosts. Сравнить установленный `gpoint_mac_worker.py` с исходником кандидата; при изменении worker Dev обновляет его опубликованным исходником по штатному installer перед релизом. Автоматизация должна блокироваться при несовпадении.
6. Первый запуск Prepare обязан быть зелёным, включая Linear. Выпуск нового application candidate разрешается владельцем отдельно; установка автоматизации не является разрешением произвольного production apply.

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
