# Archived complete GPoint Release Journal snapshot

Captured from canonical Linear journal on 2026-10-14 before size-limit compaction. This is a historical snapshot, not the live release ledger. The automatically managed CURRENT_STATE block below is archival only.

---

# Журнал релизов GPoint

<!-- GPOINT_CURRENT_STATE_BEGIN -->

Синхронизировано: `2026-10-14T04:47:33+00:00` UTC.

GitHub main: `a3fef9583784431804454997f053f6e67a93aed8` (commit time `2026-10-14T04:38:28Z`).

Production: `ee00d64665d4f63574f0ca2920f399ae01488b51`; canonical `gpoint-release-ledger/docs/release/state/production.json` + manifest; completed `2026-10-14T03:56:24.441Z`; `LEDGER VERIFIED` (отдельный свежий VPS readback не выполнялся).

Сверка SHA: **MAIN_AHEAD_OF_PRODUCTION**.

Последний завершившийся успешный Prepare: `ee00d64665d4f63574f0ca2920f399ae01488b51` / run `#38428563077`; current-main run `#38512436088`: **NOT_PREPARED_FOR_CURRENT_MAIN**.

Последний production Deploy: `ee00d64665d4f63574f0ca2920f399ae01488b51` / run `#38508347328` / canonical **COMPLETE**.

GitHub Deploy workflow: `completed` / conclusion `failure`; deploy job **SUCCESS** подтверждён отдельно.

Prisma head: `20260922090000_telegram_channel_avatar`; AGATA Alembic head: `agata0011` (manifest).

Server deploy: **COMPLETE**; Mac worker: **HEALTHY**; service readiness: **READY**; dependency checked `2026-10-14T03:56:24.441Z`.

Пользовательская приёмка: **OWNER_SMOKE_PENDING**; technical COMPLETE не является подтверждением Telegram-ответа.

При degraded worker повторить dependency recheck без Deploy. Реальный Telegram/GSales inference smoke подтверждается отдельно; production ради dependency check повторно не запускать.

<!-- GPOINT_CURRENT_STATE_END -->

### 2026-10-14 — GPoint V4 production COMPLETE

SHA `ee00d64665d4f63574f0ca2920f399ae01488b51`; [Deploy #38508347328](<https://github.com/akhripunov/gpoint/actions/runs/38508347328>): validate/deploy PASS, evidence PASS. Independent VPS health/image parity PASS; ledger COMPLETE, recovery=false. Old rollback images present, DB compatibility assessment required. `TOTAL_RELEASE=266s`. CI Linear publish failed; details in Release Queue/<issue id="3939d0dd-f94b-48ea-8ada-37ed1dce4f9c" href="https://linear.app/bestuzhev/issue/ME-26/gpoint-release-orchestrator-v3-impact-based-ci">ME-26</issue>. OWNER_SMOKE_PENDING.

### 2026-10-12T19:54:58.818Z — AGATA message chronology fix: production **COMPLETE / PASS**

* **Суть исправления:** assistant message `occurred_at` сохраняется строго после persisted user message `occurred_at`, с фактическим временем генерации либо user+1µs, если оно позднее. Стабилизирует хронологию диалога; исправлено в `services/agata/app/responses/pipeline.py`, добавлены `test_message_chronology.py` и хронологические assertions в `test_internal_api_e2e.py`.
* **Source → exact SHA:** source `codex/message-order-bug` `a5eafc410071314adc230460597b97080bfb6265`, [akhripunov/gpoint#70](<https://linear.app/bestuzhev/review/fixagata-preserve-user-assistant-message-order-in-history-0730d2218cdf>) `MERGED`, GPoint main и production `dd6ca175ecb78362aa352b82f15767ba724b2744` (предыдущий production `6afdf62692a58f8e8333c9da99ac7abbe603adc5`). Пользователь прямо поручил исправить и выполнить релиз. GitHub CLI keyring работал на Mac; старое сообщение Codex об invalid token было scoped к другому окружению, GitHub token глобально не ротировался.
* **V3 canonical gates (one attempt each, all SUCCESS):** [FULL Prepare #38298513885](<https://github.com/akhripunov/gpoint/actions/runs/38298513885>), [automatic controller #38299582253](<https://github.com/akhripunov/gpoint/actions/runs/38299582253>), [read-only READY_FOR_OWNER preflight #38299616155](<https://github.com/akhripunov/gpoint/actions/runs/38299616155>), [production Deploy #38300045099](<https://github.com/akhripunov/gpoint/actions/runs/38300045099>). `FULL` выбран автоматически, поскольку изменился AGATA backend; regression, PostgreSQL, E2E, immutable artifact, migration rehearsals, provenance, backups, health и auth gates не обходились. Ни одного дублирующего Prepare или Deploy.
* **Canonical immutable result (отдельно скачан и проверен):** `candidateSha=dd6ca175ecb78362aa352b82f15767ba724b2744`, `previousProductionSha=6afdf62692a58f8e8333c9da99ac7abbe603adc5`, `productionShaAfter=dd6ca175ecb78362aa352b82f15767ba724b2744`, `status=COMPLETE`, `releaseState=COMPLETE`, `preflight=PASS`, `authorizationMode=EXACT_SHA`, `workerOperationalStatus=HEALTHY`, `overallServiceReadiness=READY`. Production completed **2026-10-12T19:54:58.818Z**; `TOTAL_RELEASE=344.3s`. Prisma head `20260922090000_telegram_channel_avatar`, AGATA `agata0011`, GSales `001_gsales` не изменились.
* **Отдельный read-only VPS + ledger readback:** все запущенные контейнеры `compose-api-1`, `compose-bot-1`, `compose-worker-1`, `compose-agata-*`, `compose-gsales-*` используют exact SHA `dd6ca175ecb78362aa352b82f15767ba724b2744`; API и health-marked AGATA/GSales контейнеры `healthy`. `/opt/gpoint/env/production.env` `RELEASE_SHA` и `miniapp/index.html` build SHA совпадают. Canonical ledger `currentProductionSha=activeRuntimeSha=agataRuntimeSha=dd6ca175ecb78362aa352b82f15767ba724b2744`, `recoveryRequired=false`, `COMPLETE`, `HEALTHY/READY`.
* **Ограничения:** technical release `COMPLETE`, но личная пользовательская приемка Telegram/GSales inference `OWNER_SMOKE_PENDING / NOT_VERIFIED`. Не запускать новый Deploy ради пользовательского smoke. MAXIBOT не изменялся. Actual paid Actions cost not verified.

### 2026-10-12T17:16:46.451Z — GPoint V3 performance optimization: PRODUCTION **COMPLETE / PASS**

* **Exact-SHA / GitHub main / production / active runtime:** `6afdf62692a58f8e8333c9da99ac7abbe603adc5`. Previous production `97415945d9746e263db6f1faf01ab0e3cda77bbd`; immutable release ID `gpoint-release-20261012T171129127Z`. Canonical ledger: `COMPLETE`, `recoveryRequired=false`, `serverDeployStatus=COMPLETE`. Independent read-only GPoint VPS `docker ps` confirmed API, Bot, Worker and AGATA/GSales API/workers use all three matching SHA-tagged images and are running; health-marked containers healthy.
* **Source changes (no V3 policy bypass):** `10730e7` disables only disposable hosted Buildx teardown; `6afdf62` isolates stable runtime dependencies from UI-only cache invalidation in `Gpoint.Dockerfile` with regression guard. V3 impact planner chose `FULL` because infrastructure changed; all mandatory tests, migrations, immutable provenance, no-repeat selection, automatic read-only preflight, health and OWNER-gated Deploy preserved.
* **Canonical runs (one each, attempt 1, all SUCCESS):** [Prepare #38280837612](<https://github.com/akhripunov/gpoint/actions/runs/38280837612>), [automatic controller #38281855391](<https://github.com/akhripunov/gpoint/actions/runs/38281855391>), [read-only READY_FOR_OWNER preflight #38281890548](<https://github.com/akhripunov/gpoint/actions/runs/38281890548>), [production Deploy #38282446549](<https://github.com/akhripunov/gpoint/actions/runs/38282446549>) with `validate/deploy/publish` all SUCCESS. Exact production approval was issued in this conversation by OWNER's instruction «Делай всё до конца, не спрашивай ... пока не сделаешь всё до конца» referring to previously specified exact SHA `6afdf62692a58f8e8333c9da99ac7abbe603adc5`; the standard `EXACT_SHA` workflow used `approve_production=true`. No `TASK_SCOPED_AUTO` grant or bypass.
* **Measured timing boundaries (new FULL release, UTC):** Prepare workflow 16:56:10–17:05:05 = **8:55** (previous V3 FULL 9:54); preflight workflow 17:05:25–17:07:57 = **2:32** (previous 3:02); production workflow 17:10:24–17:18:11 = **7:47** (previous 7:52); Prepare-create→production-complete **22:01**, which includes all waits/overlaps (previous 34:14; not a comparable code-only speedup). `TOTAL_RELEASE=316.3s` vs 337.9s previously; SCP artifact upload **38.6s** vs 91.0s (network variation cause NOT_MEASURED), Docker load **63.4s** vs 58.6s, 3 Docker builds **140.34s** vs 164.60s, GSales PostgreSQL regression **248.738s** vs 280.17s (partially parallel). These are observed timings with different execution conditions, not guaranteed causal gains. Real hosted `UI_ONLY` performance still **NOT_MEASURED**. Actual paid GitHub minutes/cost **NOT_MEASURED**.
* **Canonical immutable production evidence** (read back with release asset SHA/digest verification): `candidateSha=6afdf62692a58f8e8333c9da99ac7abbe603adc5`, `productionShaAfter=6afdf62692a58f8e8333c9da99ac7abbe603adc5`, `previousProductionSha=97415945d9746e263db6f1faf01ab0e3cda77bbd`, `status=COMPLETE`, `releaseState=COMPLETE`, `preflight=PASS`, `authorizationMode=EXACT_SHA`, `workerOperationalStatus=HEALTHY`, `overallServiceReadiness=READY`. Prisma head `20260922090000_telegram_channel_avatar`, AGATA `agata0011`, GSales `001_gsales` unchanged; no database migration head drift. GitHub production ledger confirmed `recoveryRequired=false`, `main=activeRuntime=6afdf62692a58f8e8333c9da99ac7abbe603adc5`.
* **Outstanding:** actual Telegram/GSales/user inference acceptance `NOT_VERIFIED / OWNER_SMOKE_PENDING` — technical COMPLETE is not proof of real user dialogue. Do not redeploy for user acceptance. MAXIBOT unchanged.

### 2026-10-12T09:06:59.857Z — Release Orchestrator V3: PRODUCTION **COMPLETE / PASS**

* **GitHub main = production = exact SHA:** `97415945d9746e263db6f1faf01ab0e3cda77bbd`. Предыдущий production: `ffa768d9028a9591f1331838a29fe1429056e504`. Release ID: `gpoint-release-20261012T090120871Z`.
* **Canonical GitHub production Deploy:** [#38227070337](<https://github.com/akhripunov/gpoint/actions/runs/38227070337>), attempt 1, **workflow SUCCESS**; все jobs: `validate` SUCCESS, `deploy` SUCCESS (09:00:28–09:07:16Z), `publish` SUCCESS (09:07:19–09:08:04Z). Dispatch дублирован не был.
* **Подготовка исходного кандидата:** [Prepare #38224325297](<https://github.com/akhripunov/gpoint/actions/runs/38224325297>) SUCCESS; [automatic controller #38225355419](<https://github.com/akhripunov/gpoint/actions/runs/38225355419>) SUCCESS; [read-only preflight #38225386119](<https://github.com/akhripunov/gpoint/actions/runs/38225386119>) SUCCESS / READY_FOR_OWNER. GitHub подтверждает immutable receipt, exact-SHA build и artifact checks.
* **Canonical production ledger (свежая проверка после Deploy):** `currentProductionSha` и `activeRuntimeSha` = `97415945d9746e263db6f1faf01ab0e3cda77bbd`, `agataRuntimeSha` тот же; `releaseStatus`/`lastReleaseStatus`/`serverDeployStatus` = `COMPLETE`; `recoveryRequired=false`; `workerOperationalStatus=HEALTHY`; `overallServiceReadiness=READY`. Последний dependency check `2026-10-12T09:06:59.857Z`. Prisma head `20260922090000_telegram_channel_avatar`, AGATA Alembic head `agata0011`, GSales head `001_gsales`.
* **Разрешения:** владелец разрешил main integration, затем production Deploy, временно остановил его ради канонического учёта, после чего дважды возобновил («Продолжай деплой», «продлолжи»). Все эти датированные решения выше сохранены; данная строка фиксирует конечный технический результат.
* **Отдельный пользовательский smoke:** `OWNER_SMOKE_PENDING`. Успешный ledger/health **не означает**, что владелец проверил сообщения, Telegram и GSales inference. Не делать повторный Deploy только ради пользовательской приёмки.
* **Следующий шаг:** проверить рабочие сценарии отдельно, а на будущем реальном UI-only релизе измерить время/минуты V3 против V2 без ослабления security gates. MAXIBOT не затронут. Исторические записи IN_PROGRESS выше не переписывать; текущую истину предоставляет auto-managed GPOINT_CURRENT_STATE.

### 2026-10-12T09:08:22Z — GPoint Release Orchestrator V3: production **COMPLETE**

* **Результат:** [GitHub GPoint production run #38227070337](<https://github.com/akhripunov/gpoint/actions/runs/38227070337>), attempt 1, `validate` **SUCCESS**, `deploy` **SUCCESS**, `publish` **SUCCESS**, общий workflow **completed / success**.
* **Exact SHA в production и current main:** `97415945d9746e263db6f1faf01ab0e3cda77bbd`; `GPOINT_CURRENT_STATE` (автоматически синхронизируемый блок выше) и canonical `gpoint-release-ledger/docs/release/state/production.json` подтверждают `IN_SYNC`, `lastReleaseStatus=COMPLETE`, `serverDeployStatus=COMPLETE`, `recoveryRequired=false`, `releaseOrigin=durable-production-release`. Доказанное техническое завершение: `2026-10-12T09:06:59.857Z` (12:06:59 МСК).
* **Post-deploy evidence:** `workerOperationalStatus=HEALTHY`, GPOINT Worker dependency `HEALTHY` (пустой список reasons), `overallServiceReadiness=READY`, `dependencyCheckedAt=2026-10-12T09:06:59.857Z`. Prisma `20260922090000_telegram_channel_avatar`, AGATA Alembic `agata0011` — актуальные heads из release manifest.
* **Ограничение приёмки:** `inferenceAcceptance=NOT_VERIFIED`; реальный пользовательский Telegram/GSales/AI-диалог или OWNER smoke не подтверждён одним техническим deploy. Сохранять `OWNER_SMOKE_PENDING`, не представлять его как PASS. Если нужен inference smoke, выполнять отдельно без повторного production Deploy.
* **Процесс:** после OWNER `PAUSED` → нового `RESUMED` штатно выполнен ровно один production workflow, без вторичной сборки, принудительного push, ручного изменения VPS или отдельного MAXIBOT Deploy. Следующий шаг — при необходимости адресная пользовательская проверка и измерение ускорения на будущем действительно UI-only кандидате.

### 2026-10-12T09:00:13Z — GPoint V3 production: DISPATCHED / IN_PROGRESS

* Owner возобновил выпуск («Продолжай деплой») после фиксации release intent и проверки SHA. Canonical [GPoint Deploy #38227070337](<https://github.com/akhripunov/gpoint/actions/runs/38227070337>), attempt 1, `workflow_dispatch` под GitHub-аккаунтом владельца; exact candidate `97415945d9746e263db6f1faf01ab0e3cda77bbd`.
* Непосредственно перед сверкой: `main` совпадал, production ledger `ffa768d9028a9591f1331838a29fe1429056e504` COMPLETE / recovery=false, Prepare #38224325297 и preflight #38225386119 SUCCESS. GitHub run уже существовал на момент обнаружения; **повторный dispatch не выполнялся**.
* В run #38227070337 job `validate` завершился SUCCESS 09:00:25Z, `deploy` выполняет `Verify receipt, canonical preflight, apply and persist evidence`. Статус на момент этой записи: **IN_PROGRESS**, исход технической выкладки и post-deploy smoke ещё не подтверждён. Не ставить COMPLETE до GitHub result и canonical ledger.

### 2026-10-12 — V3 production Deploy, canonical workflow RUNNING

* OWNER возобновил ранее приостановленный выпуск командой «Продолжай деплой». Exact main/candidate SHA `97415945d9746e263db6f1faf01ab0e3cda77bbd`; старый production baseline `ffa768d9028a9591f1331838a29fe1429056e504` COMPLETE.
* Последняя read-only сверка перед отправкой: Prepare [#38224325297](<https://github.com/akhripunov/gpoint/actions/runs/38224325297>), automatic controller [#38225355419](<https://github.com/akhripunov/gpoint/actions/runs/38225355419>), preflight [#38225386119](<https://github.com/akhripunov/gpoint/actions/runs/38225386119>) — SUCCESS, run attempt 1; нет конкурирующих workflows, drift и recovery.
* Один штатный `GPoint — Deploy` запущен с `candidate=97415945...`, `mode=production`, `approve_production=true`, `prepare_run_id=38224325297`, `prepare_run_attempt=1`, `orchestrator_run_id=38225355419`. **GitHub production workflow:** [#38227070337](<https://github.com/akhripunov/gpoint/actions/runs/38227070337>), dispatched 2026-10-12 09:00:13 UTC. Job `validate` PASS; job `deploy` на Mac runner **IN_PROGRESS** на момент записи. **Текущий статус:** `DEPLOY_IN_PROGRESS`**, не** `COMPLETE`**.**
* Дальше дописать к этому событию фактический verdict, canonical ledger SHA, post-deploy readiness и риски. Никакого ручного обхода gates, повторной сборки или отдельного MAXIBOT Deploy.

### 2026-10-12 — V3: OWNER_RESUMED / production authorization (после паузы)

* После зафиксированной `DEPLOY_PAUSED_BY_OWNER` владелец новым сообщением **«Продолжай деплой»** прямо возобновил **один** production Deploy GPoint V3 для exact candidate `97415945d9746e263db6f1faf01ab0e3cda77bbd`.
* Предпусковая read-only сверка: current GitHub main тот же SHA; production ledger `ffa768d9028a9591f1331838a29fe1429056e504` / `COMPLETE`, `recoveryRequired=false`; активных workflow нет. Immutable Prepare [#38224325297](<https://github.com/akhripunov/gpoint/actions/runs/38224325297>) SUCCESS attempt 1; controller [#38225355419](<https://github.com/akhripunov/gpoint/actions/runs/38225355419>) SUCCESS; read-only preflight [#38225386119](<https://github.com/akhripunov/gpoint/actions/runs/38225386119>) SUCCESS, `READY_FOR_OWNER` / `PLANNED`.
* **Статус на момент записи:** `OWNER_DEPLOY_RESUMED / DISPATCH_PENDING`. Production V3 ещё **не** помечен COMPLETE. Следующее событие дополняется фактическим Deploy run ID, проверками и исходом. Не менять автоматически управляемый блок `GPOINT_CURRENT_STATE`.

### 2026-10-12 — Release Orchestrator V3: READY_FOR_OWNER / OWNER PAUSE (не Deploy)

* **Проект:** GPoint. **Candidate/main exact SHA:** `97415945d9746e263db6f1faf01ab0e3cda77bbd`. **Подтверждённый production baseline:** `ffa768d9028a9591f1331838a29fe1429056e504` / технический COMPLETE по последнему canonical ledger; `MAIN_AHEAD_OF_PRODUCTION`.
* **Полный GitHub Prepare:** [#38224325297](<https://github.com/akhripunov/gpoint/actions/runs/38224325297>) SUCCESS; источник релизной инфраструктуры изменён, поэтому V3 корректно выбрал FULL. Immutable exact-SHA Docker builds, AGATA/GSales PostgreSQL, migration gates, image parity и private containerd roundtrip PASS.
* **Automatic controller:** [#38225355419](<https://github.com/akhripunov/gpoint/actions/runs/38225355419>) SUCCESS. **Read-only preflight:** [#38225386119](<https://github.com/akhripunov/gpoint/actions/runs/38225386119>) SUCCESS / `READY_FOR_OWNER`; production в preflight не менялся.
* **OWNER-действия:** разрешена интеграция в обновлённый main, затем явно разрешён production Deploy V3; **следующей командой OWNER попросил «пока подожди» и перенести релизное администрирование в Правила, Журнал и Release Queue**. Текущий статус: `DEPLOY_PAUSED_BY_OWNER`, не `DEPLOY_IN_PROGRESS`, не `COMPLETE`. Нового production Deploy V3 по этому разрешению не запускали.
* **Дальше:** только после нового явного «продолжай» повторно проверить живые SHA/preflight/ledger/конкурирующие workflows, затем при неизменной пригодности выполнять штатный exact-SHA production Deploy и дополнять **эту же запись** ссылкой на run, техническими checks и итоговым verdict. Журнал автоматически поддерживает `GPOINT_CURRENT_STATE`; этот ручной release event не меняет его и не заменяет canonical ledger.
* **Правило учёта:** для подобных записей **не заводить отдельную ME/issue**. Development issues могут сохраняться как архивные источники handoff; текущие релизные факты вести в [Правилах GPoint](<https://linear.app/bestuzhev/document/pravila-reliza-gpoint-183dba239394>), [Журнале GPoint](<https://linear.app/bestuzhev/document/zhurnal-relizov-640efa60dacf>) и [общей Queue](<https://linear.app/bestuzhev/document/release-queue-gpoint-maxibot-2eb2a23040bc>).

### Оперативное уточнение — последний preflight, 2026-10-08

**Не считать current-main кандидата** `215776691e2613ce3bea64a3b1eb52129f69d519` **находящимся в** `READY_FOR_OWNER`**.** Для этого SHA Prepare `#37666648230` завершился SUCCESS и automatic controller `#37668284064` завершился SUCCESS, но автоматически запущенный read-only Deploy preflight [#37668319026](<https://github.com/akhripunov/gpoint/actions/runs/37668319026>) завершился **BLOCKED / workflow failure**. Шаг Mac runner `Restore or download the verified immutable artifact` не был успешно завершён; защищённый этап release preflight/apply после него не исполнялся. Конкретная первопричина не установлена доступными evidence/logs и не должна додумываться. Реальный production не менялся вследствие этой попытки. До нового независимого успешного exact-SHA preflight запрещено считать этот кандидат готовым к production apply. Исторический V2 coalescing proof остаётся PASS / FROZEN и от этого не отменяется.

Это датированное уточнение к последним событиям. Автоматически управляемый `GPOINT_CURRENT_STATE` и append-only события ниже не переписывать вручную; следующие reconciliation должны показывать актуальный последний preflight verdict, а не выводить готовность только из успешного Prepare.

## ACTIVE HANDOFF — Release Orchestrator V2 FINAL — 2026-10-07

**Текущий итог: COMPLETE / PASS / FROZEN.** Новый чат должен считать Release Orchestrator V2 завершённой канонической release system, а не незавершённой инфраструктурной задачей.

### Текущее состояние GPoint

* GitHub `main`: `6859932bb9c97ae67e0026d34f9e2754bd4e7dee`;
* production: `cf606812279e0cf9477d1816efb73f91bb2045f3`;
* state: `MAIN_AHEAD_OF_PRODUCTION`;
* current-main Prepare `#37631254518` — PASS / READY;
* automatic controller `#37632897110` — PASS;
* canonical read-only preflight `#37632939421` — PASS / `READY_FOR_OWNER`;
* production apply для `6859932...` **не выполнялся и не требуется только ради proof coalescing**;
* предыдущий production baseline `cf606812...` остаётся canonical COMPLETE / healthy по последнему release evidence.

### Финальный coalescing proof

Coalescing проверен реальными быстрыми push-событиями.

В реальной серии из двух быстрых SHA промежуточный GPoint SHA:

* дождался bounded quiet window;
* определил, что перестал быть current `main`;
* пропустил тяжёлые tests, PostgreSQL/GSales rehearsal, Docker build и Prepare artifact publication;
* не запустил automatic preflight.

Финальный SHA `6859932...` получил полный Prepare и ровно один automatic preflight.

Локальные regression tests дополнительно моделируют серию из четырёх SHA.

Измерения:

* push → automatic preflight + publication: **14 мин 50 с**;
* hosted: **4 jobs / 686 s**;
* self-hosted: **2 jobs / 123 s**;
* immutable bundle download: **56.0 s**;
* `cache_prevented_download=NO`;
* оценочная экономия одного coalesced тяжёлого Prepare: **\~560 hosted runner seconds** (`630 − 70`).

Immutable Prepare/evidence Releases подтверждены: `immutable=true`, `draft=false`, SHA и asset digest совпадают с кандидатом. GPoint evidence: `preflight=PASS`, `READY_FOR_OWNER`, `productionChanged=false`; live runtime остался на production baseline.

### Cross-project итог

MAXIBOT также PASS: current `main` `44c19ab59571ccb663ebf5c6d4de49fa5a866239`, Prepare `#37631234823`, controller `#37632023233`, preflight `#37632079388` PASS / READY_FOR_OWNER; production остаётся на `9df8175e...`. Реальный coalescing proof дал оценочную экономию \~188 hosted runner seconds.

В обоих проектах:

* `DUPLICATE_HOSTED_WORK_REMOVED=YES`;
* safety gates сохранены;
* production unchanged;
* DB changes = 0;
* provider/Direct writes = 0;
* Incy/VPN/DNS не менялись;
* Linear publication PASS.

### Канонический flow после freeze

`push → bounded quiet/coalescing → stale/current-main check → full Prepare only latest SHA → immutable artifact → automatic controller → read-only preflight → READY_FOR_OWNER → authorization → production apply → postdeploy/evidence/Linear/ledger → COMPLETE`.

Superseded SHA обязан завершаться до expensive Prepare и не должен запускать automatic preflight.

### Что дальше

Release Orchestrator V2 **заморожен**. Не продолжать инфраструктурный refactor без новой конкретной измеримой проблемы.

Текущий `main` `6859932...` можно не выкатывать отдельно: coalescing уже доказан без production mutation. При будущем production rollout этого exact SHA потребуется обычное действующее exact-SHA authorization; иначе изменения естественно войдут в следующий реальный продуктовый релиз через текущую каноническую схему.

Последняя ручная сводка от **2026-10-02** сохранена ниже как исторический снимок; текущие факты публикуются только в блоке GPOINT_CURRENT_STATE.

Связанный документ: <document id="1d84a3c6-f297-40c2-b9e7-ab81bc749c48" href="https://linear.app/bestuzhev/document/arhitektor-handoff-36f91afb3d2c">Архитектор — Handoff</document>.
Здесь хранится история отдельных попыток выпуска, ошибок, восстановления и проверки результата. Актуальная краткая сводка и правила работы между архитектором и Dev находятся в Handoff.

## Как вести журнал

Dev добавляет запись перед production apply и дополняет её после каждого завершённого этапа, сбоя, recovery или пользовательской проверки. Повторная попытка имеет отдельный release ID. Если изменился candidate SHA, это новый кандидат с новой проверкой и подтверждением оператора.

Исходные факты не удалять и не переписывать задним числом: к неудачной попытке добавлять результат восстановления и ссылку на новый manifest. Секреты, токены, содержимое приватных ключей и production env сюда не помещать.

Для каждого релиза фиксировать два результата отдельно:

* **Выкладка:** готовится / готов к выпуску / выкатывается / технически завершена / заблокирована / требуется recovery.
* **Работа приложения:** не проверена / проверяется / подтверждена для перечисленных сценариев / обнаружена ошибка.

Canonical manifest со статусом COMPLETE подтверждает завершение технического release flow. Статус «подтверждён» в этом журнале требует проверки заявленных пользовательских сценариев. Успешный heartbeat или HTTP 200 на health не заменяет такую проверку.

## Исторический снимок — 2026-10-02 (не текущее состояние)

* GitHub `main` и production: `657ade5a8b836e682c6b9d9b62299a53fa792510` — exact SHA совпадает.
* Последний production workflow: [#37016184144](<https://github.com/akhripunov/gpoint/actions/runs/37016184144>), SUCCESS; canonical manifest COMPLETE, завершён 2026-10-02 в 16:59:01.814 МСК. Прямая сверка VPS: **2026-10-02, 17:11:15 МСК (14:11:15 UTC)**.
* API / Worker / Bot и оба компонента AGATA на этом SHA: running, restarts=0, OOM=false; API и оба AGATA health=healthy. Mini App build meta проверен на тот же SHA в 17:01:24 МСК.
* Prisma: `20260922090000_telegram_channel_avatar`; AGATA Alembic: `agata0008`; provider: `mac_worker`. Heads до/после совпали, новых миграций не было.
* Production ref: `gpoint-production-20261002T135526660Z`; previous production / runtime rollback point: `8c30d676809f8c0f6d3254891bf096b0b8740bd4`. Совместимость rollback с БД оценивать отдельно.
* [Актуальный canonical state](<https://github.com/akhripunov/gpoint/blob/gpoint-release-ledger/docs/release/state/production.json>) и полная история manifests находятся в **gpoint-release-ledger**, а не в application main. recoveryRequired=false.
* Технический выпуск завершён. Реальный ответ Telegram после этого выпуска владельцем ещё не подтверждён: **OWNER_SMOKE_PENDING**. Два ответа 1 октября — историческая приёмка того релиза, не новое подтверждение.
* Основной checkout чистый. Старый технический worktree удалён после сохранения четырёх журналов и архивного state в ledger; Git bundle и локальный архив проверены.

Последний GPoint workflow: **16:54:42–16:59:35 МСК, 4 мин 53 с**. Измеренный интервал работы Dev после разрешения, включая итоговую сверку обоих VPS и запись журналов: **16:54:12–17:02:39, 8 мин 27 с**. Это выпуск с готовым Prepare и Mac cache HIT; не SLA для нового SHA. Последний Prepare GPoint занял 6 мин 31 с; сборки 77.8 с и 36.9 с — два образа **одного GPoint-релиза**, второй обслуживает AGATA. Добавлены замеры, сама сборка пока не ускорена. MaxiBot после последних измерений скорости не перевыкатывался ради нового benchmark; ускорение его полного production flow относительно исходного baseline не доказано.

Исторические READY, BLOCKED, ожидание первого apply и старые SHA ниже относятся к дате соответствующей попытки. Они не отменяют последний COMPLETE. Пользовательская приёмка учитывается отдельно.

## Историческое подтверждённое состояние — 2026-10-01

* Production SHA: `62d96acc67bd739599b7edf205e5fabeed6456ce`.
* Production ref: `gpoint-production-20261001T072211071Z`.
* Canonical apply: **COMPLETE**, завершён 2026-10-01 в **10:28:09 МСК / 07:28:09 UTC**, длительность **358 секунд**.
* API / Worker / Bot / Mini App / AGATA API / AGATA worker сверены с production SHA.
* Prisma head: `20260922090000_telegram_channel_avatar`; Alembic head: `agata0008`.
* Inference provider: `mac_worker`.
* Пользовательская проверка: **подтверждены два ответа подряд в Models Hub** на «Поговорим?» и «не знаю, расскажи о себе». Источник — сообщение владельца «а не вот щас ответила» и скриншот `Снимок экрана — 2026-10-01 в 10.37.24.png`, переданные Dev в текущем чате.
* Область подтверждения: отправка сообщения, генерация и отображение ответа в открытом чате. Долговременная память, восстановление после закрытия UI и длительная стабильность этой проверкой не подтверждены.
* [Итоговый manifest](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/manifests/gpoint-release-20261001T072211071Z.json>).

## 2026-10-01 — исходный production перед сегодняшними попытками

Исходная версия: `93d0c38c7452516f4cea41e1e78f1fe0966f48a9`, ref `gpoint-production-20260928T094858578Z`. Это исторический baseline, а не текущая production-версия.

Полная текущая история хранится в [release-ledger](<https://github.com/akhripunov/gpoint/tree/gpoint-release-ledger/docs/release/manifests>); каталог manifests в application main содержит исторический снимок до внедрения отдельного ledger. Этот документ начинается с подтверждённых попыток 2026-10-01; полная ретроспектива предыдущих попыток пока не перенесена в Linear.

## 2026-10-01 — попытка 1: 7d4d394

* Release ID: `gpoint-release-20261001T063600024Z`.
* Кандидат: `7d4d394920d0f6cb3f83735b2d76d9ac1fbffe57`; предыдущий production: `93d0c38c7452516f4cea41e1e78f1fe0966f48a9`.
* Изменения: release provider gates и bounded GitHub retries.
* Исходный результат: **RECOVERY_REQUIRED**, сбой **POST_RELEASE_CLEANUP** после успешного переключения runtime. SSH exit 255: соединение закрыто удалённым хостом.
* Восстановление: production сверена; exact cleanup выполнен через защищённый файл с проверкой SHA256. Production ref `gpoint-production-20261001T064242044Z`; recovery финализирован **COMPLETE**.
* Пользовательский результат: сообщение «ты там?» сохранилось, но inference job не создалась; /respond возвращал 503. Health/heartbeat оказались недостаточными.
* [Исходный manifest](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/manifests/gpoint-release-20261001T063600024Z.json>), [recovery manifest](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/manifests/gpoint-recovery-20261001T064242044Z.json>), [дополнительный audit](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/manifests/gpoint-post-release-audit-20261001T064242044Z.json>).

## 2026-10-01 — попытка 2: ce38486

* Release ID: `gpoint-release-20261001T070511382Z`.
* Кандидат: `ce38486ef416869e217c5fa41773c02f258d38e0`; предыдущий production: `7d4d394920d0f6cb3f83735b2d76d9ac1fbffe57`.
* Причина отсутствия новой inference job: effective model `codex-default` не совпадала с включённым маршрутом `cx/gpt-5.6-terra`. Синхронизация неизменённого персонажа принимала любой включённый маршрут профиля, поэтому не создавался текущий.
* Исправление: синхронизация проверяет точный tuple provider/model/prompt/schema/policy. Явно отключённый точный маршрут остаётся отключённым.
* Проверки: 63 теста AGATA PASS, 24 PostgreSQL tests SKIPPED; 80 release tests PASS.
* Исходный результат: **RECOVERY_REQUIRED**, снова POST_RELEASE_CLEANUP / SSH exit 255.
* Exact cleanup успешно повторен файлом с хешем `69667aaea5df434d0c995b9ed3c18d6070f0c5433b8cfb43790eb67a67ee9d2e`; production ref `gpoint-production-20261001T071149396Z`; recovery **COMPLETE**.
* Повторная обработка уже сохранённого сообщения с тем же idempotency key: **200 pending**, inference job создана без дубликата сообщения.
* Обнаружена следующая ошибка: пустой `AGATA_CODEX_MODEL=` корректно выбирал default модель CLI, но callback отправлял `model=""`, который AGATA отклоняла **422**. Ответ не сохранялся, задача позднее истекла (`inference_job_expired`); вручную terminal job не сбрасывалась.
* [Исходный manifest](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/manifests/gpoint-release-20261001T070511382Z.json>), [recovery manifest](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/manifests/gpoint-recovery-20261001T071149396Z.json>).

## 2026-10-01 — попытка 3: 62d96ac, подтверждён диалог

* Release ID: `gpoint-release-20261001T072211071Z`.
* Кандидат: `62d96acc67bd739599b7edf205e5fabeed6456ce`; предыдущий production / точка runtime rollback: `ce38486ef416869e217c5fa41773c02f258d38e0`. Совместимость rollback с БД всегда оценивать отдельно.
* Исправления: пустая/пробельная callback model нормализуется в `codex-default`; большие команды cleanup (>32768 bytes) передаются файлом с mode 600 и SHA256 guard.
* Mac worker установлен из опубликованного исходника; installed/source SHA256: `606490fcb4f5e3bd119bff5759ee994d25543a8fa7e121db41014fd5617c5f60`. Повторная регистрация исправила ошибку bootstrap LaunchAgent после остановки прежнего процесса.
* Проверки: **64 AGATA tests PASS**, **24 PostgreSQL tests SKIPPED** (локальная тестовая БД отсутствовала), **83 release tests PASS**; canonical exact-SHA dry-run PASS.
* Миграционные heads до/после совпали: Prisma `20260922090000_telegram_channel_avatar`, AGATA `agata0008`.
* Выкладка: **COMPLETE** за **358 секунд**. Автоматический POST_RELEASE_CLEANUP прошёл за **25.6 секунды**, ручной recovery не потребовался.
* Пользовательская проверка сначала оставалась pending: manifests/audit, записанные до 10:37 МСК, корректно отражают отсутствие нового запроса на момент записи.
* Дополнение **10:37 МСК**: владелец прислал скриншот с двумя последовательными ответами Models Hub. Сценарий диалога **подтверждён владельцем**. Исторические manifests не переписывались.
* [Dry-run](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/manifests/gpoint-dry-run-20261001T072049175Z.json>), [release manifest](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/manifests/gpoint-release-20261001T072211071Z.json>), [audit до пользовательского подтверждения](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/manifests/gpoint-messages-incident-audit-20261001T072809078Z.json>), [разбор инцидента на момент выкладки](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/incidents/2026-10-01-messages-no-response.md>).

## Шаблон следующего релиза

### Дата / release ID / краткое название

* Что меняется; связанные задачи:
* Candidate SHA и GitHub proof:
* Production SHA, Prisma/Alembic heads до изменения:
* Rollback SHA и оценка DB compatibility:
* Миграции / backup:
* Проверки PASS / FAIL / SKIPPED с причинами:
* Разрешение владельца на этот кандидат:
* Этап / время последнего обновления:
* Технический результат / production SHA и heads после:
* Пользовательские сценарии и результат каждого:
* Ошибка / причина / исправление / recovery:
* Следующий шаг, ответственный и необходимое действие владельца:
* Ссылки на dry-run, release/recovery manifests и доказательства:

## Открытые ограничения процесса

Обновление 2026-10-01: candidate Docker/PostgreSQL rehearsal и автоматическая публикация результатов в Linear внедрены в GPoint — Prepare / GPoint — Deploy. Полный Prepare и read-only preflight на Mac прошли; первое production apply через новый workflow ещё не выполнялось. [Правила и инструкция](<https://github.com/akhripunov/gpoint/blob/main/docs/release/automation.md>). Проверка heartbeat не покрывает создание и завершение реального inference: ответ владельца в Telegram фиксирует Dev отдельно. Mac worker и release runner зависят от работающего Mac и сети. Ошибки memory_extract/extractor_unavailable и повторяющаяся relationship_note_refresh остаются отдельными открытыми вопросами, не закрытыми подтверждением диалога.

### GPOINT_RELEASE_EVENT_36838084518_candidate_FAILED

Время: 2026-10-01T08:43:49.840598+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `009e591144478ce64facf3ee5781711a2e14dbc2`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36838084518>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_36838466863_candidate_FAILED

Время: 2026-10-01T08:51:03.726661+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `824e2fb1e6acafa8075a6305b6e7985638ddf309`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36838466863>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_36839775181_candidate_READY

Время: 2026-10-01T09:03:35.587400+00:00.

Этап: candidate; статус: **READY**.

SHA: `259f4424af497a7808ef331f1394c9b12a6beaf4`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36839775181>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_36840381378_production_PLANNED

Время: 2026-10-01T10:20:46.946365+00:00.

Этап: production; статус: **PLANNED**.

SHA: `259f4424af497a7808ef331f1394c9b12a6beaf4`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36840381378>).

Canonical status: **PLANNED**. Production ref: `не создан`.

До релиза: SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0008`.

Это preflight: production runtime и БД не изменены. Следующее действие: отдельный запуск в режиме production с подтверждением владельца.

### Настройка автоматизации — диагностика первого Mac preflight, 2026-10-01

Первый attempt [Deploy run 36840381378](<https://github.com/akhripunov/gpoint/actions/runs/36840381378>) остановился до checkout и любых production операций: shell runner не экранировал путь временного script внутри `Library/Application Support/.../_work`. Исправлена локальная настройка только GPoint runner: `workFolder = ~/.local/share/gpoint-release-work`, сервис перезапущен. Повторный attempt этого же preflight завершился SUCCESS, canonical PLANNED; production сохранил `62d96acc67bd739599b7edf205e5fabeed6456ce`, Prisma `20260922090000_telegram_channel_avatar`, Alembic `agata0008`. Публикация в оба документа Linear и result artifact прошли. Инструкция настройки runner опубликована отдельным docs commit `061eef3c7ace047302dbdd6b6329a5dc02daf496`; новый SHA проходит отдельный Prepare. Production approval этому SHA не выдавалось.

### GPOINT_RELEASE_EVENT_36848626426_candidate_READY

Время: 2026-10-01T10:26:14.020968+00:00.

Этап: candidate; статус: **READY**.

SHA: `061eef3c7ace047302dbdd6b6329a5dc02daf496`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36848626426>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

## Исправление классификации события CI/CD — 2026-10-01

Историческая запись сохранена без изменений; ниже добавлена корректировка.

* Запуск [GPoint #36840381378](<https://github.com/akhripunov/gpoint/actions/runs/36840381378>) был выполнен в режиме preflight. Запись `production: PLANNED` ошибочно обозначила его как production-событие. Верная классификация — только `preflight: PLANNED`; production mode не запрашивался и production не изменялся.

Исходное audit-событие оставлено на месте.

### GPOINT_RELEASE_EVENT_36889780324_attempt_1_candidate_CANCELLED

Время: 2026-10-01T16:11:56.969206+00:00.

Этап: candidate; статус: **CANCELLED**.

SHA: `eaa94d93e8f3417e5dbe8c0bdde92eabfc8792c3`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36889780324/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_36890063384_attempt_1_candidate_READY

Время: 2026-10-01T16:17:14.943159+00:00.

Этап: candidate; статус: **READY**.

SHA: `42bd512dbbbc19fbbc872b325ef1c0bda1aea2f5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36890063384/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_36890954924_attempt_1_preflight_FAILED

Время: 2026-10-01T16:22:47.477690+00:00.

Этап: preflight; статус: **FAILED**.

SHA: `42bd512dbbbc19fbbc872b325ef1c0bda1aea2f5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36890954924/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36890954924_attempt_2_preflight_FAILED

Время: 2026-10-01T16:27:14.371511+00:00.

Этап: preflight; статус: **FAILED**.

SHA: `42bd512dbbbc19fbbc872b325ef1c0bda1aea2f5`.

Попытка GitHub Actions: `2`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36890954924/attempts/2>).

Это повторный запуск. Предыдущая запись сохранена; данный статус относится к указанной попытке.

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36890954924_attempt_3_preflight_PLANNED

Время: 2026-10-01T16:46:38.137040+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `42bd512dbbbc19fbbc872b325ef1c0bda1aea2f5`.

Попытка GitHub Actions: `3`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36890954924/attempts/3>).

Это повторный запуск. Предыдущая запись сохранена; данный статус относится к указанной попытке.

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36895373708_attempt_1_preflight_FAILED

Время: 2026-10-01T16:54:33.799008+00:00.

Этап: preflight; статус: **FAILED**.

SHA: `42bd512dbbbc19fbbc872b325ef1c0bda1aea2f5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36895373708/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36895988299_attempt_1_preflight_FAILED

Время: 2026-10-01T16:59:55.313384+00:00.

Этап: preflight; статус: **FAILED**.

SHA: `42bd512dbbbc19fbbc872b325ef1c0bda1aea2f5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36895988299/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36895988299_attempt_1_production_BLOCKED

Время: 2026-10-01T16:59:58.154514+00:00.

Этап: production; статус: **BLOCKED**.

SHA: `42bd512dbbbc19fbbc872b325ef1c0bda1aea2f5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36895988299/attempts/1>).

Canonical status: **BLOCKED**. Production ref: `не создан`.

До релиза: SHA `None`; Prisma `None`; Alembic `None`.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

## Диагностика VPS Docker preflight — 2026-10-01, 20:11 МСК / 17:11 UTC

Read-only проверка через canonical GPoint SSH runner после [production run 36895988299](<https://github.com/akhripunov/gpoint/actions/runs/36895988299>):

* GitHub `origin/main` подтверждён GitHub API: `42bd512dbbbc19fbbc872b325ef1c0bda1aea2f5` (кандидат запуска). Production не изменялся: фактический runtime SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`; API, Worker, Bot и Mini App используют этот release; AGATA API/worker используют соответствующий AGATA image. Сервисы healthy, restart count 0. Prisma head `20260922090000_telegram_channel_avatar`, AGATA head `agata0008`.
* Ошибка в production run #36895988299 произошла до apply на команде Docker Server Version. В GitHub log сохранены класс ошибки и exit=1, но stderr/stdout исходной неуспешной команды не сохранены; поэтому конкретный триггер разового сбоя установить нельзя. В journald Docker/containerd за 16:40–17:15 UTC записей нет.
* На момент read-only диагностики Docker 29.1.3 и containerd были active; `docker info`, context `default`, Unix socket `/var/run/docker.sock` (root:docker, mode 660) и полный `docker version` успешны. Docker version проверен шестью последовательными вызовами canonical Server.Version; все 6 завершились exit=0. Systemd показывал Result=success, NRestarts=0; daemon не перезапускался.
* Выявлен отдельный текущий блокер canonical prereq: свободно `5,241,336 KiB`, требование — не менее `5,242,880 KiB` (5 GiB), дефицит `1,544 KiB`. Округлённый `df -h` показывал 5.0G, но точное значение ниже gate. Это не доказывает причину Docker exit=1, однако повторять canonical preflight сейчас нельзя: disk gate должен остановить его. После освобождения места нужно read-only подтвердить порог до повтора.
* Сравнение: успешный [preflight run 36890954924, attempt 3](<https://github.com/akhripunov/gpoint/actions/runs/36890954924/attempts/3>) прошёл ту же первую удалённую Docker version проверку (exit=0, 16:46 UTC); failed production run #36895988299 получил exit=1 (около 16:59 UTC). Между этими наблюдениями имелась нестабильность/разовый отказ, но логов недостаточно, чтобы назвать его точную причину.
* Итог: постоянная неисправность Docker, неверный context/socket, restart daemon и нехватка inode не подтверждены. Ремонт Docker-инфраструктуры пока не обоснован; сначала нужно восстановить требуемый запас диска. Повтор preflight допустим после подтверждения свободного места ≥5 GiB и повторной read-only проверки Docker. Apply не выполнялся.

## VPS deep disk cleanup — 2026-10-01, 20:35 МСК / 17:35 UTC

Выполнена разрешённая очистка через canonical GPoint SSH runner. Production apply не запускался, SHA не менялся, Docker/containerd не перезапускались.

### Измеренный результат

| Объект | До | После | Освобождено / результат |
| -- | -- | -- | -- |
| Корневой раздел `/dev/sda1` | 30,865,176 KiB всего; 24,313,152 KiB занято; 5,241,168 KiB свободно | 12,006,652 KiB занято; 17,547,668 KiB свободно | Net по `df`: 12,306,500 KiB (≈11.74 GiB) освобождено |
| Старые GPoint/AGATA runtime generations | 8 SHA-пар образов | Оставлены только production `62d96acc…` и rollback `ce38486e…` | Удалены 6 старых SHA-пар (12 tags); физическое освобождение учтено только в общем `df`, без сложения виртуальных размеров shared layers |
| Docker BuildKit cache | 6.624 GB | 0 B | Удалён cache завершённых сборок; buildx history не показывал активных сборок |
| `/opt/gpoint/releases` staging | 167 MB | 20 MB | Удалены 18 устаревших SHA-каталогов; production и rollback каталоги оставлены |
| Временные release archives | 8 SHA archives | Сохранены только current и rollback archives | Удалены 6 устаревших архивов, 39,114,960 bytes |
| Docker containers | 9, один оставался в состоянии Created | 8 running | Удалён только временный `gpoint-static-45b046ba` без mounts |
| Docker images | Включая неиспользуемый `postgres:16-alpine` | Его больше нет | Удалён после проверки: не было контейнеров и ссылок в репозитории; live pgvector, rollback и `postgres:16` для backup сохранены |
| Apt package/index cache | Около 451 MB | Индексы \~32 KiB; apt archive cache очищен | Cache повторно создаётся при следующем `apt-get update` |

Shared Docker layers не суммировались как независимый размер: главным итогом является изменение занятых/свободных блоков `df`. Docker volumes не удалялись: осталось 6 volumes, 341.9 MB. BuildKit cache — 0; Docker images — 8, 4.902 GB; containers — 8, менее 70 KB writable data.

### Текущее состояние и preflight

* Свободно **17,547,668 KiB (≈16.74 GiB)**; диск занят на 41%. Canonical prereq 5 GiB пройден с запасом; ориентир 10 GiB свободного также пройден. `SAFE_TO_RUN_GPOINT_PREFLIGHT=YES`.
* Production SHA и Mini App: `62d96acc67bd739599b7edf205e5fabeed6456ce`; API/Worker/Bot image ID parity подтверждён. AGATA API и worker используют тот же SHA. Проверенный rollback image комплект `ce38486ef416869e217c5fa41773c02f258d38e0` сохранён.
* Docker Server Version `29.1.3` прошёл три последовательных read-only проверки; docker/containerd active. API live/ready OK; AGATA API ready HTTP 200; memory-worker healthcheck PASS; detector, PostgreSQL и Redis healthy. Prisma head `20260922090000_telegram_channel_avatar`; Alembic head `agata0008`.
* Запуск [36895988299](<https://github.com/akhripunov/gpoint/actions/runs/36895988299>) остаётся неуспешным preflight и не применялся. После освобождения места можно запускать отдельный canonical preflight; это не является production apply.

Занято **≈11.45 GiB**, поэтому ориентир «не более 10 GiB занято» не достигнут. Остаток включает system/runtime base, 4.6 GiB в `/var/lib/containerd` (около 3.5 GiB snapshot tree и 1.3 GiB content), текущие/rollback/shared Docker images, 341.9 MB защищённых volumes, 655 MB snapd и 847 MB системных логов (из них 601 MB journal). Удалять остаточные rollback/infra layers, volumes, системные пакеты или диагностические журналы ради дополнительного сокращения не стали; для этого понадобилось бы ослабить ограничения защиты.

### Retention на будущие релизы

В release tooling уже есть SHA-scoped image retention: `planReleaseRetention` защищает candidate/current и live rollback SHA, а старые images и cache допускает к удалению только для терминальных, reconciled release records. Фактической успешной выкладкой после внедрения это поведение ещё не подтверждено: последние run’ы остановились на preflight.

Есть два ограничения для отдельного изменения automation (код в этом production-кандидате не менялся): текущий `POST_RELEASE_CLEANUP` выполняется после runtime/health reconciliation, но до финальной записи manifest/ref; а `/opt/gpoint/releases` staging и устаревшие Mini App rollback-каталоги существующий cleanup не ротирует. Чтобы строго выполнить желаемую политику, надо отдельно перенести очистку за durable COMPLETE/ref finalization, оставить current+one verified rollback и добавить retention для staging. Build cache также стоит ограничивать target free-space после релиза, а не полагаться только на SHA-scoped cache eviction.

Примечание: после удаления apt indexes будущая системная установка пакетов потребует `apt-get update`. MAXIBOT, PostgreSQL data, shared services, все volumes, production env/config/secrets и активные runtime images не изменялись.

### GPOINT_RELEASE_EVENT_36901675538_attempt_1_preflight_BLOCKED

Время: 2026-10-01T17:46:50.685853+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `42bd512dbbbc19fbbc872b325ef1c0bda1aea2f5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36901675538/attempts/1>).

Детализация проверки (2026-10-01, 20:46 МСК / 17:46 UTC): GitHub `origin/main` и локальный clean gate совпали с exact SHA; GPoint — Prepare run [36890063384](<https://github.com/akhripunov/gpoint/actions/runs/36890063384>) завершился успешно, artifact ID `11176188040`. Validate job и повторная сверка SHA/Prepare на Mac прошли. Mandatory gate восстановления immutable artifact завершился FAIL: `prepared_artifact_cache.py` вывел `ARTIFACT_CACHE=BLOCKED ERROR_TYPE=URLError` и exit 1. Job `Verify receipt, canonical preflight, apply and persist evidence` был пропущен. Это сетевой сбой загрузки/восстановления артефакта; конкретная вложенная причина URLError в workflow output не раскрыта. Ссылка на canonical Deploy run: [36901675538](<https://github.com/akhripunov/gpoint/actions/runs/36901675538>). Не повторять автоматически.

Результат: `SAFE_TO_RELEASE=NO`, `RELEASE_READY=NO`, `PRODUCTION_CHANGED=FALSE`. Сбой произошёл до canonical VPS preflight; Docker/disk/services/DB heads и live production SHA в этом запуске заново не считывались. Apply и миграции не запускались; кандидат runtime не переключался. Последний подтверждённый baseline до этой попытки: production SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`, Prisma `20260922090000_telegram_channel_avatar`, Alembic `agata0008`, свободно 16.74 GiB после cleanup.

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36911404317_attempt_1_candidate_READY

Время: 2026-10-01T19:07:51.748202+00:00.

Этап: candidate; статус: **READY**.

SHA: `2593eddd0f2b8aea685cb115f18fc0d13dc5346d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36911404317/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_36914856728_attempt_1_preflight_PLANNED

Время: 2026-10-01T19:36:20.366927+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `2593eddd0f2b8aea685cb115f18fc0d13dc5346d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36914856728/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36917353729_attempt_1_preflight_PLANNED

Время: 2026-10-01T19:55:18.088434+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `2593eddd0f2b8aea685cb115f18fc0d13dc5346d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36917353729/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36917353729_attempt_1_production_FAILED_PRE_SWITCH

Время: 2026-10-01T19:55:21.745723+00:00.

Этап: production; статус: **FAILED_PRE_SWITCH**.

SHA: `2593eddd0f2b8aea685cb115f18fc0d13dc5346d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36917353729/attempts/1>).

Canonical status: **FAILED_PRE_SWITCH**. Production ref: `не создан`.

До релиза: SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `None`.

Этап сбоя: `CI_ARTIFACT_LOAD`. Dev обязан сверить runtime/БД до повторной попытки.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### GPOINT_RELEASE_EVENT_36961573602_attempt_1_candidate_READY

Время: 2026-10-02T03:51:57.024604+00:00.

Этап: candidate; статус: **READY**.

SHA: `7b243329e2b264c7c85aebadb168b90e2fc68560`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36961573602/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_36963607523_attempt_1_preflight_PLANNED

Время: 2026-10-02T04:14:31.427180+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `7b243329e2b264c7c85aebadb168b90e2fc68560`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36963607523/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

## 2026-10-02 — проверка причин задержки выпуска

Свежий canonical preflight [36963607523](<https://github.com/akhripunov/gpoint/actions/runs/36963607523>) завершился success, все jobs включая Publish Linear успешны. Exact candidate: `7b243329e2b264c7c85aebadb168b90e2fc68560`; Prepare [36961573602](<https://github.com/akhripunov/gpoint/actions/runs/36961573602>) success и обязательный `PREPARE_IMAGE_ROUNDTRIP=PASS`. Verified artifact cache HIT: `11208420835`. Canonical результат `PLANNED`, runtime/БД не переключались.

Подтверждённый live baseline: `62d96acc67bd739599b7edf205e5fabeed6456ce`; Prisma `20260922090000_telegram_channel_avatar`; AGATA `agata0008`; свободно `15396680 KiB`; rollback image доступен. Mac worker соответствует кандидату. Новых миграций относительно baseline нет.

Причины предыдущих задержек различны: сетевые сбои immutable artifact transfer; прежний production отказ `CI_ARTIFACT_LOAD` после успешного checksum/load; и отдельный отказ validate в [36945480725](<https://github.com/akhripunov/gpoint/actions/runs/36945480725>), где старый кандидат `2593eddd…` перестал быть current main после bookkeeping commit. Наличие round-trip проверки устраняет пробел в Prepare validation; успешная загрузка на production Docker для нового кандидата пока не доказана, поскольку apply не запускался. Не утверждать недоказанную причину движения тегов или смены Docker ID.

30-минутный bounded retry — механизм восстановления сети, а не ускорение. В этой попытке cache исключил повторную загрузку большого архива, и workflow завершился примерно за 45 секунд. Следующее действие: exact-SHA owner approval, затем canonical production Deploy. После COMPLETE пользовательский Telegram smoke фиксируется отдельно.

### GPOINT_RELEASE_EVENT_36963854025_attempt_1_preflight_PLANNED

Время: 2026-10-02T04:20:01.467911+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `7b243329e2b264c7c85aebadb168b90e2fc68560`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36963854025/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36963854025_attempt_1_production_FAILED_PRE_SWITCH

Время: 2026-10-02T04:20:03.786838+00:00.

Этап: production; статус: **FAILED_PRE_SWITCH**.

SHA: `7b243329e2b264c7c85aebadb168b90e2fc68560`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36963854025/attempts/1>).

Canonical status: **FAILED_PRE_SWITCH**. Production ref: `не создан`.

До релиза: SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `None`.

Этап сбоя: `CI_ARTIFACT_UPLOAD`. Dev обязан сверить runtime/БД до повторной попытки.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

## 2026-10-02 — подтверждённая причина GPoint CI_ARTIFACT_UPLOAD и исправление

Разрешённый владельцем [production run 36963854025](<https://github.com/akhripunov/gpoint/actions/runs/36963854025>) завершился `FAILED_PRE_SWITCH`, этап `CI_ARTIFACT_UPLOAD`: обе попытки `images.tar.gz` прерваны `spawnSync …gpoint-vps-scp-runner ETIMEDOUT` ровно через 60 секунд каждая. Новые images не загружены и runtime не переключался. Read-only SSH после сбоя подтвердил live SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`, API healthy, AGATA head `agata0008`. Факт предыдущего DB baseline Prisma `20260922090000_telegram_channel_avatar` сохранён в canonical manifests.

Установленная причина: release adapter ограничивал execFileSync 60000 ms; SCP wrapper независимо ограничивал весь transfer максимумом 60 секунд. Эта граница непригодна для immutable archive \~348 MiB. Это отдельная неисправность от предыдущего CI_ARTIFACT_LOAD и сетевой загрузки GitHub artifact.

Исправление опубликовано в exact SHA `2a7a104dbd74e556ea944a6b7be2713560a1727e`: deadline 30 секунд setup + 1 секунда/MiB, минимум 60, максимум 600 секунд; wrapper допускает 1–600 секунд; внешний timeout даёт 5 секунд на остановку child. Сохраняются две попытки максимум, key-only SSH, host-key policy, checksums и image identity gates. 99 release tests PASS; syntax/diff checks PASS; repo clean start/end PASS. Только SCP helper установлен из опубликованного candidate на Mac, предыдущая версия сохранена; credentials и LaunchAgents не менялись.

[Новый Prepare 36964386439](<https://github.com/akhripunov/gpoint/actions/runs/36964386439>) выполняется. Production нового SHA ещё не разрешён и не запускался. До нового apply требуются успешные Prepare/preflight и owner approval этого exact SHA.

### GPOINT_RELEASE_EVENT_36964386439_attempt_1_candidate_FAILED

Время: 2026-10-02T04:25:40.981015+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `2a7a104dbd74e556ea944a6b7be2713560a1727e`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36964386439/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

## 2026-10-02 — отдельный Prepare PostgreSQL readiness race

Prepare [36964386439](<https://github.com/akhripunov/gpoint/actions/runs/36964386439>) для `2a7a104…` остановился после начального pg_isready на `CREATE ROLE`: psql Unix socket отсутствовал. Причина в probe: он допускает временный socket-only сервер initdb, который PostgreSQL image entrypoint останавливает перед окончательным запуском. Проверка заменена на authenticated TCP SQL `SELECT 1` в bounded readiness loop; остальные migrations/fixtures/gates сохранены.

Исправленный exact candidate `5a1e25741b79d20669f2e12df414cbf35f444b54` опубликован в origin/main, содержит upload deadline fix и TCP readiness fix. Syntax/diff/clean-end PASS. Полный canonical [Prepare 36964621192](<https://github.com/akhripunov/gpoint/actions/runs/36964621192>) выполняется. Production этого SHA не запускался.

### GPOINT_RELEASE_EVENT_36964621192_attempt_1_candidate_READY

Время: 2026-10-02T04:32:06.030340+00:00.

Этап: candidate; статус: **READY**.

SHA: `5a1e25741b79d20669f2e12df414cbf35f444b54`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36964621192/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_36965012481_attempt_1_preflight_PLANNED

Время: 2026-10-02T04:38:05.212860+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `5a1e25741b79d20669f2e12df414cbf35f444b54`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36965012481/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36965675985_attempt_1_preflight_PLANNED

Время: 2026-10-02T04:44:33.829528+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `5a1e25741b79d20669f2e12df414cbf35f444b54`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36965675985/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36965675985_attempt_1_production_FAILED_PRE_SWITCH

Время: 2026-10-02T04:44:35.790338+00:00.

Этап: production; статус: **FAILED_PRE_SWITCH**.

SHA: `5a1e25741b79d20669f2e12df414cbf35f444b54`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36965675985/attempts/1>).

Canonical status: **FAILED_PRE_SWITCH**. Production ref: `не создан`.

До релиза: SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `None`.

Этап сбоя: `CI_ARTIFACT_LOAD`. Dev обязан сверить runtime/БД до повторной попытки.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

## 2026-10-02 — первопричина повторного CI_ARTIFACT_LOAD доказана

Разрешённый владельцем [production run 36965675985](<https://github.com/akhripunov/gpoint/actions/runs/36965675985>), exact SHA `5a1e25741b79d20669f2e12df414cbf35f444b54`: upload PASS за 42.4s (364885736 bytes, deadline 378s), checksum PASS, docker load обоих images PASS, затем `FAILED_PRE_SWITCH / CI_ARTIFACT_LOAD`. Runtime не переключался; read-only confirmed live SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`.

Read-only доказательство: VPS Docker 29.1.3 использует containerd snapshotter. `.Id` app = `sha256:55baf98b39282517075c370c67b0a253223b16e7c3746b086d785245efd8016e` — OCI manifest digest, его `config.digest` = `sha256:a4e4f7f0b5681de596d1ea8c3970aa8f31b8efde2e9c06a74acfe0643f9b01ce` — точно receipt app ID. AGATA: native manifest `sha256:9da5d6d48a548ee7e965813dc968926355b9c230cb95616153858f51c25aae5f`, config `sha256:2ce4a75f428804c65fc19d61d32766a36f3115d5d7e22772940dd2e430f2f0d8` — точно receipt AGATA ID. Поэтому это неверное сравнение manifest digest с config digest, а не повреждённый архив или неподтверждённое движение тегов. Classic CI save/load round-trip не воспроизводил containerd identity semantics.

Исправление опубликовано в `61d6872249919e7a1fa8b2a6a219201ba483dbf8`: общий import/reuse verifier сохраняет exact config hash и доказывает hashed native manifest → exact config blob → platform/revision/rootfs. Unknown descriptor, index, missing/corrupt blob и identity mismatch fail closed. Native IDs остаются для runtime parity. 100 release tests PASS, включая classic/containerd и corruption/rootfs rejection. Новая проверка read-only выполнена на actual VPS для обоих загруженных images: PASS. Repository clean-end PASS. Никаких runtime mutations в диагностике.

[Новый Prepare 36966272034](<https://github.com/akhripunov/gpoint/actions/runs/36966272034>) выполняется. Production нового SHA ещё не запускался; после Prepare/preflight потребуется новое exact-SHA owner approval.

### GPOINT_RELEASE_EVENT_36966272034_attempt_1_candidate_READY

Время: 2026-10-02T04:56:21.457889+00:00.

Этап: candidate; статус: **READY**.

SHA: `61d6872249919e7a1fa8b2a6a219201ba483dbf8`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36966272034/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_36966755070_attempt_1_preflight_PLANNED

Время: 2026-10-02T04:59:52.216453+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `61d6872249919e7a1fa8b2a6a219201ba483dbf8`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36966755070/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36967152577_attempt_1_preflight_PLANNED

Время: 2026-10-02T05:05:26.954245+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `61d6872249919e7a1fa8b2a6a219201ba483dbf8`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36967152577/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36967152577_attempt_1_production_FAILED_PRE_SWITCH

Время: 2026-10-02T05:05:29.358846+00:00.

Этап: production; статус: **FAILED_PRE_SWITCH**.

SHA: `61d6872249919e7a1fa8b2a6a219201ba483dbf8`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36967152577/attempts/1>).

Canonical status: **FAILED_PRE_SWITCH**. Production ref: `не создан`.

До релиза: SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `None`.

Этап сбоя: `RUNTIME_BUILD`. Dev обязан сверить runtime/БД до повторной попытки.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

## 2026-10-02 — downstream identity comparisons и проверка всей цепочки

Разрешённый [production run 36967152577](<https://github.com/akhripunov/gpoint/actions/runs/36967152577>) для `61d6872249919e7a1fa8b2a6a219201ba483dbf8` остановился `FAILED_PRE_SWITCH / RUNTIME_BUILD`. CI_ARTIFACT_UPLOAD PASS 72.4s (ранний 60s cutoff исправлен); CI_ARTIFACT_LOAD PASS 42.2s (manifest→config verification исправлена). Следующий gate содержал прежний прямой `.Id == receipt config ID` тест. Ещё такой тест найден в CI_ARTIFACT_PARITY. Предыдущая реализация не охватила эти downstream места.

Read-only live reconciliation: production SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`, API healthy, AGATA `agata0008`. Migration/runtime switch не выполнялись. Все remaining `bundle.images.*.id` downstream comparisons найдены и заменены общим exact config identity verifier. Parity отдельно доказывает соответствие записанных VPS native IDs текущим native IDs.

Исправление опубликовано как `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`. 100 release tests PASS, включая downstream containerd parity и отказ при config ID вместо native proof. Actual VPS: verify identity → capture native image proofs → parity повторная проверка для обоих `61d6872…` images PASS, `GPOINT_CI_PARITY_OK`. Дополнительно невыполняемый временный контейнер без сети подтвердил `.Image` == native image `.Id`; контейнер удалён. Runtime/БД не менялись. Repository clean-end PASS.

[Prepare 36967671335](<https://github.com/akhripunov/gpoint/actions/runs/36967671335>) выполняется. Production нового SHA не запускался; после Prepare/preflight требуется owner approval нового exact SHA.

### GPOINT_RELEASE_EVENT_36967671335_attempt_1_candidate_READY

Время: 2026-10-02T05:14:46.944376+00:00.

Этап: candidate; статус: **READY**.

SHA: `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36967671335/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_36968120980_attempt_1_preflight_PLANNED

Время: 2026-10-02T05:19:04.022125+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36968120980/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36968491432_attempt_1_preflight_PLANNED

Время: 2026-10-02T05:24:40.033307+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36968491432/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36968491432_attempt_1_production_COMPLETE

Время: 2026-10-02T05:24:42.575252+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36968491432/attempts/1>).

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261002T052102159Z`.

До релиза: SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0008`.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

## 2026-10-02 — production COMPLETE, итог после исправления release tooling

Owner разрешил exact SHA `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`. [GPoint Deploy 36968491432](<https://github.com/akhripunov/gpoint/actions/runs/36968491432>) завершился SUCCESS, включая публикацию в Linear. Canonical manifest: `docs/release/manifests/gpoint-release-20261002T052102159Z.json`, status COMPLETE, production ref `gpoint-production-20261002T052102159Z`. До релиза: `62d96acc67bd739599b7edf205e5fabeed6456ce`; после: `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`. Live SHA и labels API/AGATA независимо проверены на VPS; оба healthy. API/Worker/Bot/Mini App и AGATA artifact parity PASS; runtime health и reconciliation PASS. Prisma head до/после `20260922090000_telegram_channel_avatar`, AGATA head до/после `agata0008`; новых миграций нет.

Apply занял 200.1 с: upload 41.1 с, load/verification 41.4 с, runtime switch 13 с, container health 6.4 с. Причины прежних срывов устранены в GitHub: фиксированный SCP timeout 60 с, гонка готовности disposable PostgreSQL, ошибочное приравнивание Docker config digest к OCI manifest digest после import (включая downstream parity). Проверка checksum/config/rootfs/source SHA сохранена; application images потреблены из Prepare 36967671335 без rebuild на apply. 100 release tests, Prepare и preflight PASS.

MaxiBot также технически COMPLETE: [Deploy 36963856848](<https://github.com/akhripunov/maxibot/actions/runs/36963856848>), SHA `206a1a144be4e0c5de52238c273f89e908d4a84d`, migration head `0098_durable_autopilot_execution_claim_v1`, parity/health/finalization PASS. Его полный результат уже записан в собственных журнале и Handoff.

Реальный Telegram reply владельцем ещё не подтверждён: GPoint manifest `applicationVerification=OWNER_SMOKE_PENDING`, `productionUserSmoke=NOT_RUN`. Technical COMPLETE не является подтверждением E2E ответа. Synthetic Messages jobs не создавались.

### GPOINT_RELEASE_EVENT_36973066138_attempt_1_candidate_READY

Время: 2026-10-02T06:26:08.680118+00:00.

Этап: candidate; статус: **READY**.

SHA: `e92580af5807029315a968af475f9fe2fe72a1c8`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36973066138/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

## 2026-10-02 — защита от повторных циклов выпуска, без GHCR

По разрешению владельца изменения GPoint опубликованы в main: `e92580af5807029315a968af475f9fe2fe72a1c8`, [akhripunov/gpoint#1](<https://linear.app/bestuzhev/review/rehearse-deployment-identity-and-preserve-prepared-retry-candidates-0798f2538037>). [Prepare 36973066138](<https://github.com/akhripunov/gpoint/actions/runs/36973066138>) SUCCESS, включая Publish Linear. Branch rehearsal 36972528703 также PASS: реальные app/AGATA archives импортированы в отдельный пустой Docker 29.1.3/containerd/overlayfs с private containerd и отдельной network namespace; общий production import/capture/parity/reuse прошёл, прежнее config-as-native сравнение намеренно отклонено для обоих образов. Deploy требует обязательный receipt gate deployRehearsal=PASS. 104 release tests, 27 Linear tests, 12 cache tests PASS; repository clean start/end PASS.

Безопасные BLOCKED/FAILED_PRE_SWITCH попытки сохраняются в artifacts и Linear без bookkeeping push в main: exact подготовленный кандидат остаётся пригодным для повтора после baseline/preflight сверки. Две последовательные неудачи проверены на настоящем временном Git remote; RECOVERY_REQUIRED по-прежнему сохраняется в main и требует recovery. Новое приложение не собирается во время apply.

Почему вчерашние изменения не убрали часы ожидания: cache ускорял только повтор SAME SHA, bounded network retry повышал устойчивость, но не скорость. Bookkeeping менял main после неудачи, а classic-store roundtrip не воспроизводил production containerd identity. Устранены именно эти два механизма повторных полных циклов. Репетиция на branch заняла 29.3 с; Prepare job 331 с, новый main Prepare job 338 с. Это добавленная проверка до production, не измеренное ускорение последующего production release. Цель 5–15 минут пока не подтверждена несколькими новыми релизами.

Production этим изменением не выкатывался: последний технически COMPLETE SHA `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`; owner Telegram reply smoke остаётся отдельным подтверждением. Новый main SHA подготовлен, production approval для него не запрашивался. MaxiBot проходит отдельную аналогичную проверку; его статус нельзя выводить из GPoint PASS.

### 2026-10-02 — оба проекта завершены

MaxiBot release hardening также MERGED в main `5c7025d7342fe982e291f69b0aa5e6f6ca2f39f1`: [Prepare36975532711](<https://github.com/akhripunov/maxibot/actions/runs/36975532711>) SUCCESS, включая Publish Linear; 1068 tests passed, 1 skipped. Private production-store archive rehearsal PASS25.5с; Prepare job156с. GPoint и MaxiBot journal/Handoff обновлены, оба локальных main чистые. Новые production apply не выполнялись. Внедрение завершено; эффект на частоту срывов и end-to-end время необходимо измерять на следующих разрешённых реальных релизах.

### GPOINT_RELEASE_EVENT_36976212762_attempt_1_preflight_PLANNED

Время: 2026-10-02T07:03:42.298714+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `e92580af5807029315a968af475f9fe2fe72a1c8`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36976212762/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36976721658_attempt_1_preflight_PLANNED

Время: 2026-10-02T07:05:59.877498+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `e92580af5807029315a968af475f9fe2fe72a1c8`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36976721658/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

## 2026-10-02 — benchmark без production apply

По запросу владельца выполнены 4 настоящих GitHub Deploy workflow в режиме preflight, approve_production=false, без миграций/runtime switch. Все validate/Mac/publish SUCCESS. SHA и origin/main после двух попыток каждого проекта остались прежними.

| Проект | Первый workflow, с Linear | Повтор того же SHA, с Linear | Artifact restore first → cache | Mac→VPS SCP |
| -- | -- | -- | -- | -- |
| GPoint | 275с (4:35), [36976212762](<https://github.com/akhripunov/gpoint/actions/runs/36976212762>) | 61с (1:01), [36976721658](<https://github.com/akhripunov/gpoint/actions/runs/36976721658>) | MISS208.145с → HIT2.554с | 364876030bytes,91.977с,3.78MiB/s |
| MaxiBot | 217с (3:37), [36976215959](<https://github.com/akhripunov/maxibot/actions/runs/36976215959>) | 75с (1:15), [36976664675](<https://github.com/akhripunov/maxibot/actions/runs/36976664675>) | MISS153.584с → HIT2.031с | 252769632bytes,28.569с,8.44MiB/s |

Cold workflows стартовали одновременно и делили Mac/network; warm workflows и SCP не являются строгим контролируемым A/B. Это отдельные реальные наблюдения, не гарантированное SLA. VPS uploads выполнены последовательно во вновь созданные /tmp/<project>-release-speed-UUID каталоги, оба sha256 совпали с verified Prepare archives; checksum remote roundtrip3.631с/2.162с. Все временные remote files/catalogs удалены. Образы не загружались в рабочий Docker. Production images/source SHA остались прежними; API и GPoint контейнеры не переключались. Отдельные CI import rehearsal ранее измерены29.3с GPoint и25.5с MaxiBot; это не VPS Docker load/switch measurement. Runtime switch, production migration, post-switch health/rollback этим benchmark не измерены.

Вывод: сохранение exact SHA делает cache полезным, убирая около2.5–3.4мин повторного artifact restore в этом наблюдении. Первый download нового SHA всё ещё существенен; улучшать есть что. Следующий измеряемый transport эксперимент — direct GitHub artifact→временный VPS каталог, без GHCR, с exact run provenance/checksum и временными ограниченными download credentials. Пока не реализован и не объявлен ускорением. Уменьшение размера образа/избыточных assets также требует отдельного профиля состава, не удаления проверок.

Дополнительный текущий дефект: MaxiBot worker на production206a1a144be4e0c5de52238c273f89e908d4a84d продолжал самостоятельно restart-loop: RestartCount130→152, source/image те же, OOM=false, PATH=python -m app.poller, healthcheck=none. Его перезапуски не вызваны apply (apply не было); конкретная первопричина пока не установлена. API RestartCount0. Это открытая проблема стабильности и blind spot текущего preflight: его PASS не доказывает устойчивость worker. Перед следующим production выпуском требуется read-only диагностика worker/reconciliation; будущий health gate должен проверять устойчивость polling worker, а не только API. Runtime/env/БД для устранения этой проблемы в benchmark не изменялись.

## 2026-10-02 — direct GitHub Actions Artifacts → VPS: реальная проверка PASS

По вопросу владельца выполнен transport-only эксперимент без GHCR и production apply. Exact успешные main Prepare/artifact проверены через GitHub API: GPoint run36973066138/artifact11212442294/SHAe92580af5807029315a968af475f9fe2fe72a1c8; MaxiBot run36975532711/artifact11213052735/SHA5c7025d7342fe982e291f69b0aa5e6f6ca2f39f1. Mac получил временный302 download URL; GitHub-токен остался только на Mac. URL передан по SSH stdin, не записывался в файлы/логи и не включался в process argv. VPS скачал ZIP в уникальный /tmp/<project>-direct-artifact-UUID каталог; проверены GitHub ZIP digest, точный SHA receipt и checksum внутреннего образа. Docker load/Compose/env/БД не затрагивались. Remote временные каталоги удалены finally.

| Проект | Bytes ZIP | Download на VPS | Verify | Полный proof с GitHub metadata/SSH |
| -- | -- | -- | -- | -- |
| GPoint | 364877501 | 112.286с | 1.847с | 117.607с |
| MaxiBot | 252771652 | 27.348с | 1.020с | 31.353с |

В предыдущих разовых cold наблюдениях route GitHub→Mac restore+Mac→VPS upload составлял208.145+91.977=300.122с GPoint и153.584+28.569=182.153с MaxiBot. Cold downloads тогда шли одновременно; direct proofs сейчас последовательно, сеть могла измениться. Это доказательство работоспособности и основание для следующего улучшения, не строгий A/B и не гарантированное SLA. При уже имеющемся Mac cache прежний route restore+upload≈94.531с GPoint/30.600с MaxiBot; direct fetch не следует считать всегда быстрее. Будущая интеграция должна сначала проверять exact verified artifact на VPS и сохранять fail-closed checksum/provenance/fallback.

GHCR не требуется: GitHub Actions Artifacts хранит Docker save archive, Docker поддерживает load из tar/gzip без registry. Официальные источники: [https://docs.github.com/en/rest/actions/artifacts#download-an-artifact](<https://docs.github.com/en/rest/actions/artifacts#download-an-artifact>) (302 временная download ссылка), [https://docs.docker.com/reference/cli/docker/image/load/](<https://docs.docker.com/reference/cli/docker/image/load/>) (tar/compressed archive load). Лимиты хранения/retention Actions Artifacts остаются; отсутствие GHCR не означает безлимитное хранилище. Штатные workflows этим экспериментом не изменены; источник main и production SHA не менялись.

### 2026-10-02 — Sequential release improvement: measured Stage 2 / fault-tested Stage 3, no apply

Route remains GitHub → Mac → VPS. No GHCR, direct GitHub→VPS delivery, new servers, migrations, runtime apply or production container changes.

Baseline from GitHub API job timestamps: GPoint Prepare 36973066138 total 354s (prepare job 338s, build/test/rehearsal step 274s); cold preflight 36976212762 total 274s, restore/download step 209s; warm same-SHA preflight 36976721658 total 60s, deploy job 32s. MaxiBot baseline Prepare 36975532711 total170s; cold preflight36976215959 total216s, restore154s; warm36976664675 total74s. These totals use created_at → last job completed_at and can differ by a few seconds from prior CLI-reported durations.

Compression experiment on identical unpacked tar: GPoint gzip1 364876019B/6.979s versus gzip6 320009657B/21.196s; MaxiBot gzip1 251909344B/4.757s versus gzip6 225551774B/13.501s. Unpacked tar SHA256 parity PASS. Paired real Mac→VPS temporary-file transfer: GPoint original364876030B 50.235s versus smaller320009657B 131.913s; MaxiBot smaller225551774B 45.617s versus original252769632B 44.541s. Every remote checksum PASS and temp files removed. Bandwidth varied enough to dominate size reduction. Faster total delivery NOT demonstrated. Compression change rejected and restored to gzip1 in both repositories; no claimed speed gain from this experiment.

Confirmed deterministic delivery defect: after a mid-transfer interruption, current artifact downloader retries the same short-lived signed blob URL. Failure fixture BEFORE change ended in expired-URL403; minimal candidate now obtains a fresh GitHub redirect on each bounded attempt, truncates partial files, preserves shared retry budget, does not forward GitHub token to blob storage, and does not retry GitHub401. GPoint cache tests14 PASS; MaxiBot cache tests12 PASS. This repairs recovery after tested outages; ordinary-path acceleration is not yet established or claimed. No evidence this was the specific historical production incident.

GPoint release failure suite104 PASS (first sandbox run103 PASS/1 blocked by .git worktree permission, rerun with authorized local worktree writes104 PASS). Covers corrupted artifacts before upload/mutation, checksum/identity mismatch, transient/permanent network errors, bounded retries, shared budget, locks and repeated safe attempts preserving main. Hosted candidate Prepare and read-only cold/warm preflight follow.

### GPOINT_RELEASE_EVENT_36983812714_attempt_1_candidate_READY

Время: 2026-10-02T08:29:48.202572+00:00.

Этап: candidate; статус: **READY**.

SHA: `8c30d676809f8c0f6d3254891bf096b0b8740bd4`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36983812714/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_36984588078_attempt_1_preflight_PLANNED

Время: 2026-10-02T08:35:54.590319+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `8c30d676809f8c0f6d3254891bf096b0b8740bd4`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36984588078/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36985076120_attempt_1_preflight_PLANNED

Время: 2026-10-02T08:38:13.570633+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `8c30d676809f8c0f6d3254891bf096b0b8740bd4`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36985076120/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### 2026-10-02 — Final prepared-only closeout

Exact GitHub/local clean main candidate `8c30d676809f8c0f6d3254891bf096b0b8740bd4` (PR2 merged). Prepare36983812714 SUCCESS including Linear: total346s, prepare331s, build/test/rehearsal273s; baseline total354s (no material acceleration attributed). Release safety suite104PASS and artifact-cache14PASS.

Read-only cold preflight36984588078 SUCCESS: total223s, artifact restore163s, canonical checks13s. Read-only same-SHA warm repeat36985076120 SUCCESS: total61s, restore3s, canonical checks12s. Baseline cold274s/restore209s, warm60s. Warm speed is essentially unchanged; cold difference cannot be attributed to code because measured network bandwidth varied sharply. Corrected signed-URL renewal improves tested outage recovery, not demonstrated steady-state speed. Compression optimization rejected after slower paired transfer; gzip1 retained. Source current main unchanged by both preflights.

MaxiBot final candidate `2f6fd1dfc1565f6786b1fc3d423c15223a2bf736`: Prepare PASS1088tests; cold/warm production preflights correctly BLOCKED by primary_loop_unverified in legacy Worker. Its continuing kernel-proven OOM incident is documented in MaxiBot Journal; no MaxiBot production mutation performed by this work.

No production apply, migrations, working-container changes, GHCR, new delivery route or additional servers. Route GitHub→Mac→VPS retained. Full production release duration BEFORE/AFTER is NOT measured because apply was explicitly forbidden. Candidate is prepared; do not report deployed or owner-confirmed Telegram E2E. GPoint main repo clean gate PASS; final GitHub/local SHA parity verified. Evidence saved in journal and shared Handoff without replacing architect-owned content.

### GPOINT_RELEASE_EVENT_36987732477_attempt_1_preflight_BLOCKED

Время: 2026-10-02T09:10:33.848197+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `8c30d676809f8c0f6d3254891bf096b0b8740bd4`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36987732477/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36988364163_attempt_1_preflight_PLANNED

Время: 2026-10-02T09:15:52.152474+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `8c30d676809f8c0f6d3254891bf096b0b8740bd4`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36988364163/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36988364163_attempt_1_production_COMPLETE

Время: 2026-10-02T09:15:57.280566+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `8c30d676809f8c0f6d3254891bf096b0b8740bd4`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36988364163/attempts/1>).

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261002T091215302Z`.

До релиза: SHA `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0008`.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### 2026-10-02 — Production rollout completed

Owner authorised production rollout in this chat. GPoint Deploy [https://github.com/akhripunov/gpoint/actions/runs/36988364163](<https://github.com/akhripunov/gpoint/actions/runs/36988364163>) SUCCESS, including Linear publication. Production SHA changed 820c943b0b5d7db94a05ab9bd5fa26e2238a6131 → 8c30d676809f8c0f6d3254891bf096b0b8740bd4; API/Worker/Bot/Mini App and Agata parity verified. Prisma head stayed 20260922090000_telegram_channel_avatar, Agata head stayed agata0008; DATABASE_SCHEMA_UP_TO_DATE. Technical COMPLETE, owner Telegram user smoke remains OWNER_SMOKE_PENDING; no synthetic Messages jobs created.

Measured canonical apply: 196.5s (3m16.5s); Mac artifact cache restore 2.188s; Mac→VPS upload 36.6s; Docker import/load 43.2s; runtime switch 12.5s; container health wait 6.5s; cleanup 29.6s. No application image rebuild during apply. The preceding run 36987732477 was cancelled before apply because the Mac runner did not take the queued job after session reconnection. New dispatch executed successfully. Preserve this runner availability delay separately from actual apply speed.

Complete durable manifest and result retained in run artifacts; production ref gpoint-production-20261002T091215302Z. Local repo clean gate PASS.

### 2026-10-02 12:31 Moscow — Owner-timed non-production release benchmark

Owner requested a test rollout for both projects with no real deployment, measured from 12:31 including all preparation/coordination. First measured clock read12:32:01. GitHub main is now9e314c0e38e771749c219ff150ef07dedfcbf3a3, not deployed application SHA8c30d676809f8c0f6d3254891bf096b0b8740bd4. Commit is only3 release ledger files, message Record GPoint release ... \[skip ci\]. Deploy strictly requires exact current main and a matching successful Prepare; none exists for ledger SHA. Therefore dispatched fresh Prepare36990429223 and will run read-only Deploy preflight afterward. No safety bypass or production apply. This exposes a pipeline bottleneck: release bookkeeping changes the source SHA and invalidates existing preparation/artifact cache despite unchanged application code. Prepare uses hosted disposable databases and private Docker rehearsal; production runtime remains unchanged. Include this rebuild in owner's wall-clock benchmark, rather than reporting only dry-run seconds.

### GPOINT_RELEASE_EVENT_36990429223_attempt_1_candidate_READY

Время: 2026-10-02T09:38:41.898064+00:00.

Этап: candidate; статус: **READY**.

SHA: `9e314c0e38e771749c219ff150ef07dedfcbf3a3`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36990429223/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_36991007403_attempt_1_preflight_PLANNED

Время: 2026-10-02T09:42:49.023753+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `9e314c0e38e771749c219ff150ef07dedfcbf3a3`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36991007403/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### 2026-10-02 — Owner-timed non-production benchmark complete

Start12:31:00 Moscow (owner), first measured execution12:32:01. Both test workflows and automated Linear publication finished12:42:56: **11m56s end-to-end until checks/publication**. Final evidence/report preservation is additional and will be included in owner's final-response time.

MaxiBot preflight36990365319:12:32:27→12:34:05,98s workflow,3m05s from owner start. Cache HIT2.623s, canonical preflight40.997s including35s primary heartbeat observation, restarts0→0, bootstrap=false, production_changed=false.

GPoint required new Prepare36990429223 for ledger-only main9e314c0e38e771749c219ff150ef07dedfcbf3a3:12:33:06→12:38:49,343s; tests/disposable DB/build258s. Then preflight36991007403:12:39:08→12:42:56,228s; new artifact cacheMISS163.967s, canonical preflight workflow step12s. Only three release ledger files changed after application SHA8c30d67; exact-main rule invalidated its earlier Prepare. Fresh Prepare plus artifact restore\~507s=8m27s,71% of owner's716s interval. This overhead is specific to the post-release ledger-only retest; new application code still requires normal Prepare. Initial preparation,19s workflow handoff gap and Actions overhead remain counted. Project workflows overlapped.

No real deployment: GPoint PLANNED/NOT_DEPLOYED, MaxiBot production_changed=false. Independent runtime before/after matched container IDs, image IDs, StartedAt and restart counts for both projects. No new migrations/runtime replacements/advertising actions. Test does not measure Mac→VPS upload/Docker load/runtime switch, which were not performed. Main clean gatePASS.

Full logs/provenance: [https://github.com/akhripunov/gpoint/actions/runs/36990429223](<https://github.com/akhripunov/gpoint/actions/runs/36990429223>) ; [https://github.com/akhripunov/gpoint/actions/runs/36991007403](<https://github.com/akhripunov/gpoint/actions/runs/36991007403>) ; [https://github.com/akhripunov/maxibot/actions/runs/36990365319](<https://github.com/akhripunov/maxibot/actions/runs/36990365319>) . Persistent Mac evidence /Users/akhripuniov/Library/Application Support/GPoint/release-benchmarks/2026-10-02-1231 contains REPORT.md, full redacted workflow logs, run/job metadata, result manifests, runtime proof and SHA256SUMS. Proposed next fix, not implemented here: separate release ledger from application main so bookkeeping does not force fresh candidate/Prepare/download. Overall3-minute production release remains unproved.

### 2026-10-02 — Two release-speed fixes implemented; automated checks in progress

Owner command timestamp12:51:03 Moscow. GPoint PR [akhripunov/gpoint#3](<https://linear.app/bestuzhev/review/keep-release-ledger-out-of-application-main-and-automate-release-c8237a9c4214>) merged as06bd2818a385c7365a64f11af819e6c1a2ab6155. Main checkout clean gatePASS. Release state/history now persist to refs/heads/gpoint-release-ledger through non-force pushes, not application main. Existing confirmed release history seeded (ledger161b92311c83725262b2f0eebbe2a34f708267fe), source main verified unchanged during adoption; no production apply. Missing ledger, fetch race, recovery/drift and write conflict remain fail-closed. Canonical CLI and wrapper read external state/manifests; result artifacts and Linear remain mandatory. Integration tests prove COMPLETE/RECOVERY_REQUIRED history persistence without moving source main. Local107 release testsPASS; coordinator4 regressionsPASS.

New scripts/release-preflight-benchmark.py coordinates both projects concurrently: exactmain/latestPrepare, wait or explicit Prepare-if-missing, canonical preflight, full workflow logs/job timestamps/receipts/checksums/report. Default preflight; production requires owner-approved exactSHA parameters for each repo and still uses canonical workflows. It never silently substitutes an older successful Prepare for a failed latest one.

Automated test began about13:07:28 after\~16m25s implementation/verification from owner's command. This implementation time is included in full request elapsed, not presented as fast deployment. GPoint Prepare36993668030 in progress; MaxiBot preflight36993718366 completedPASS in\~115.6s including automated evidence collection. GPoint candidate06bd281 requires one new Prepare because actual release tooling source changed. MaxiBot source remainsa1e3cd1669e8d246ec18727af2513872e2340660. No migration, real deployment or ADS mutation authorised in this task.

### GPOINT_RELEASE_EVENT_36993668030_attempt_1_candidate_READY

Время: 2026-10-02T10:12:46.322376+00:00.

Этап: candidate; статус: **READY**.

SHA: `06bd2818a385c7365a64f11af819e6c1a2ab6155`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36993668030/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_36994231136_attempt_1_preflight_PLANNED

Время: 2026-10-02T10:16:23.578531+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `06bd2818a385c7365a64f11af819e6c1a2ab6155`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36994231136/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_36994680486_attempt_1_preflight_PLANNED

Время: 2026-10-02T10:18:48.330032+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `06bd2818a385c7365a64f11af819e6c1a2ab6155`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/36994680486/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### 2026-10-02 — Release ledger and automatic coordination: final measured result

Owner command12:51:03 Moscow. Implemented/pushed/merged GPoint PR3 [akhripunov/gpoint#3](<https://linear.app/bestuzhev/review/keep-release-ledger-out-of-application-main-and-automate-release-c8237a9c4214>); candidate06bd2818a385c7365a64f11af819e6c1a2ab6155. MaxiBot candidate unchangeda1e3cd1669e8d246ec18727af2513872e2340660. Existing verified release state/history seeded into gpoint-release-ledger without moving application main. Canonical CLI/workflow reads ledger outside checkout; COMPLETE/recovery history is preserved without source commits, non-force conflict and missing ledger fail closed. Local107 release tests +4 coordinator regressionsPASS, hosted PrepareSUCCESS. Main clean gatePASS.

One coordinator now runs both repositories concurrently, waits for exact latest Prepare, dispatches canonical workflows, saves complete logs/receipts/run/job metadata, verifies main unchanged/no production apply and generates report/checksums. Production mode separately requires explicit owner-approved exactSHA inputs. No operator production approval was supplied or apply performed here.

Implementation/verification until coordinator start13:07:28.458 =16m25.458s from owner command. First automated run13:07:28.458→13:16:43.684 =555.226s (9m15.226s), including wait for new-code Prepare, first GPoint download, both preflights, Linear workflow publication and full local evidence collection. Owner elapsed at first completion25m40.684s. Prepare36993668030 took352s (previous343s; builds not accelerated); GPoint preflight36994231136 took210s, artifact restore146s, canonical step13s. MaxiBot36993718366 took102s workflow /115.615s with evidence; no demonstrated improvement to MaxiBot's underlying98s baseline workflow.

Same exactSHA repeat13:17:45.124→13:19:38.350 =113.226s (1m53.226s) for BOTH projects with full generated report. No new Prepare, no main movement. GPoint36994680486:68s workflow /81.716s with evidence, cache2s, canonical step16s. MaxiBot36994680847:99s workflow /113.222s with evidence, cache3s, canonical step43s. Owner elapsed through both runs28m35.350s; final journal/readback preservation is additional and counted in final response.

Confirmed reporting improvement: last first-run workflow ended13:16:30; complete report/log evidence ready13:16:43.684 =13.684s, versus195s for previous manual preservation. Warm last workflow ended13:19:27; report ready13:19:38.350 =11.350s. Ledger integration tests and real initial adoption prove source main unchanged when publishing production facts; future production apply under the new tooling has NOT been performed. Full production release speed is still not measured by this read-only benchmark. First-download differences remain network-dependent; no cache redesign.

Independent VPS readback matched all old container/image IDs, StartedAt and restart counts in both projects. No migration/runtime switch/advertising actions. MaxiBot Worker stillrestart0 across the first subsequent hourly window; this does not prove absence of all leaks.

Full immutable workflow logs: [https://github.com/akhripunov/gpoint/actions/runs/36993668030](<https://github.com/akhripunov/gpoint/actions/runs/36993668030>) ; [https://github.com/akhripunov/gpoint/actions/runs/36994231136](<https://github.com/akhripunov/gpoint/actions/runs/36994231136>) ; [https://github.com/akhripunov/maxibot/actions/runs/36993718366](<https://github.com/akhripunov/maxibot/actions/runs/36993718366>) ; [https://github.com/akhripunov/gpoint/actions/runs/36994680486](<https://github.com/akhripunov/gpoint/actions/runs/36994680486>) ; [https://github.com/akhripunov/maxibot/actions/runs/36994680847](<https://github.com/akhripunov/maxibot/actions/runs/36994680847>) . Persistent reports/logs/metadata/receipts/checksums under /Users/akhripuniov/Library/Application Support/GPoint/release-benchmarks/2026-10-02-after-fix-cold and .../2026-10-02-after-fix-warm.

### GPOINT_RELEASE_EVENT_37009720243_attempt_1_candidate_READY

Время: 2026-10-02T13:02:31.775120+00:00.

Этап: candidate; статус: **READY**.

SHA: `657ade5a8b836e682c6b9d9b62299a53fa792510`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37009720243/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### 2026-10-02 — Закрытие старого технического worktree

Сохранены четыре ранее незакоммиченных исторических manifest от 20–21 сентября и архивная копия старого production state из `gpoint-linear-automation/GPoint`. Записи опубликованы в `gpoint-release-ledger`, commit `a38323f2527450101d2bf452f04c5777e6a2108b`: [https://github.com/akhripunov/gpoint/tree/a38323f2527450101d2bf452f04c5777e6a2108b/docs/release/manifests](<https://github.com/akhripunov/gpoint/tree/a38323f2527450101d2bf452f04c5777e6a2108b/docs/release/manifests>) . Побайтовая проверка всех пяти GitHub-копий прошла; действующий `production.json` не изменён.

Для восстановления сохранены и проверены Git bundle исходного HEAD `e14be413a97feccf7f0ee64104e649e91b696951`, архив пяти незакоммиченных файлов и SHA256: `/Users/akhripuniov/Library/Application Support/GPoint/release-archives/2026-10-02-legacy-worktree/archive-proof.json`. Старый checkout удалён после проверки архивов; main остался чистым на `657ade5a8b836e682c6b9d9b62299a53fa792510`.

Production-контейнеры при архивировании не изменялись. Следующий выпуск: GPoint вместе с AGATA, exact candidate `657ade5a8b836e682c6b9d9b62299a53fa792510`, Prepare #37009720243 SUCCESS. Новый read-only preflight: [https://github.com/akhripunov/gpoint/actions/runs/37014846357](<https://github.com/akhripunov/gpoint/actions/runs/37014846357>) . Production dispatch ещё не выполнен: автоматический контроль разрешений потребовал отдельное явное подтверждение полного SHA; вопрос владельцу отправлен.

### GPOINT_RELEASE_EVENT_37014846357_attempt_1_preflight_PLANNED

Время: 2026-10-02T13:46:36.623578+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `657ade5a8b836e682c6b9d9b62299a53fa792510`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37014846357/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_37016184144_attempt_1_preflight_PLANNED

Время: 2026-10-02T13:59:24.388519+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `657ade5a8b836e682c6b9d9b62299a53fa792510`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37016184144/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_37016184144_attempt_1_production_COMPLETE

Время: 2026-10-02T13:59:27.995481+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `657ade5a8b836e682c6b9d9b62299a53fa792510`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37016184144/attempts/1>).

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261002T135526660Z`.

До релиза: SHA `8c30d676809f8c0f6d3254891bf096b0b8740bd4`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0008`.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### 2026-10-02 — Финальная прямая сверка после production Deploy #37016184144

Владелец явно разрешил exact candidate `657ade5a8b836e682c6b9d9b62299a53fa792510`. Production workflow [https://github.com/akhripunov/gpoint/actions/runs/37016184144](<https://github.com/akhripunov/gpoint/actions/runs/37016184144>) — SUCCESS; canonical manifest — COMPLETE. Проверка напрямую на VPS в 14:01:24 UTC: GitHub main, RELEASE_SHA, API/Worker/Bot, Mini App build meta и оба контейнера AGATA совпадают с этим SHA. Все пять контейнеров running, перезапуски 0, OOM=false; API и оба AGATA health=healthy. API/Worker/Bot используют один image ID, AGATA API/Worker — один свой image ID. Prisma head `20260922090000_telegram_channel_avatar` и AGATA `agata0008` до/после совпадают; новых миграций не было. Remote release-ledger — COMPLETE, recoveryRequired=false.

MaxiBot одновременно проверен напрямую: GitHub main и API/Worker revision `a1e3cd1669e8d246ec18727af2513872e2340660`, API ready, перезапуски 0, OOM=false, ADS_AUTOPILOT_ENABLED=false и ADS_WRITE_ACTIONS_ENABLED=false. MaxiBot при этой операции не менялся.

Оба основных checkout чистые. Старый worktree GPoint удалён; все пять архивных исторических записей сохранены в release-ledger и после нового production выпуска. Полные workflow logs, manifest, dispatch evidence, live audit и SHA256SUMS: `/Users/akhripuniov/Library/Application Support/GPoint/release-closeouts/2026-10-02-production-657ade5/`. Отдельная проверка реального ответа Telegram не выполнялась; технический COMPLETE не обозначает такой ответ.

## 2026-10-03 — два режима разрешения будущих релизов

По поручению OWNER «делай такой режим для обоих проектов» обновлены AGENTS.md, release automation guide и docs/RELEASE_AUTHORIZATION.md. GPoint: commit `413753c83ccc1b22792cff2ec2233655377ecc0b` опубликован и main проверен напрямую в GitHub; checkout clean. В Linear синхронизированы Release Rules, AGENTS.md и Handoff. EXACT_SHA остаётся default; TASK_SCOPED_AUTO позволяет один ограниченный low-risk выпуск по «исправь \[задачу\] и выкатывай» без повторного human SHA approval. Scope и весь unreleased diff проверяются; exact SHA/Prepare/evidence/разрешение записываются до dispatch. Рискованные изменения, миграции, failed gates и scope drift останавливают этот режим. Все технические guards сохранены, маршрут GitHub→Mac→VPS прежний. В этой задаче production apply, миграции и runtime changes НЕ выполнялись. Это обновление политики, не доказательство текущего здоровья production и не измеренное ускорение сборки/доставки.

### GPOINT_RELEASE_EVENT_37097533607_attempt_1_candidate_READY

Время: 2026-10-03T04:50:33.859169+00:00.

Этап: candidate; статус: **READY**.

SHA: `413753c83ccc1b22792cff2ec2233655377ecc0b`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37097533607/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37098907143_attempt_1_candidate_FAILED

Время: 2026-10-03T05:11:35.758587+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `c38026108bd49989afe260d47c622504c4bcc66d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37098907143/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37099252938_attempt_1_candidate_READY

Время: 2026-10-03T05:20:50.858141+00:00.

Этап: candidate; статус: **READY**.

SHA: `57fdc1b7ddcd618e47c81bc7d90f0c10d7e3ceca`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37099252938/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37099572758_attempt_1_preflight_PLANNED

Время: 2026-10-03T05:25:34.956207+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `57fdc1b7ddcd618e47c81bc7d90f0c10d7e3ceca`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37099572758/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_37100023537_attempt_1_preflight_PLANNED

Время: 2026-10-03T05:35:02.861993+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `57fdc1b7ddcd618e47c81bc7d90f0c10d7e3ceca`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37100023537/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_37100023537_attempt_1_production_COMPLETE

Время: 2026-10-03T05:35:05.953268+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `57fdc1b7ddcd618e47c81bc7d90f0c10d7e3ceca`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37100023537/attempts/1>).

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261003T053052787Z`.

До релиза: SHA `657ade5a8b836e682c6b9d9b62299a53fa792510`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0008`.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### GPOINT_RELEASE_EVENT_37101256299_attempt_1_candidate_READY

Время: 2026-10-03T05:58:11.265194+00:00.

Этап: candidate; статус: **READY**.

SHA: `6d349cd6c019686c5491c2ed623be21da7288937`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37101256299/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37101595144_attempt_1_candidate_READY

Время: 2026-10-03T06:06:07.319127+00:00.

Этап: candidate; статус: **READY**.

SHA: `28801d99e5ed27a38873d36b95f6750544ccd453`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37101595144/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37102020153_attempt_1_preflight_PLANNED

Время: 2026-10-03T06:15:12.920277+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `28801d99e5ed27a38873d36b95f6750544ccd453`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37102020153/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_37102020153_attempt_1_production_COMPLETE

Время: 2026-10-03T06:15:16.426433+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `28801d99e5ed27a38873d36b95f6750544ccd453`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37102020153/attempts/1>).

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261003T061111662Z`.

До релиза: SHA `57fdc1b7ddcd618e47c81bc7d90f0c10d7e3ceca`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0009`.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### GPOINT_RELEASE_EVENT_37104062241_attempt_1_candidate_READY

Время: 2026-10-03T06:51:08.168885+00:00.

Этап: candidate; статус: **READY**.

SHA: `be38beef12c8b7083b0d6bbcf30b1a55991e1bc7`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37104062241/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37104453601_attempt_1_preflight_PLANNED

Время: 2026-10-03T06:59:44.839229+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `be38beef12c8b7083b0d6bbcf30b1a55991e1bc7`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37104453601/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_37104453601_attempt_1_production_COMPLETE

Время: 2026-10-03T06:59:48.496203+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `be38beef12c8b7083b0d6bbcf30b1a55991e1bc7`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37104453601/attempts/1>).

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261003T065614502Z`.

До релиза: SHA `28801d99e5ed27a38873d36b95f6750544ccd453`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0009`.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### GPOINT_RELEASE_EVENT_37106738690_attempt_1_candidate_READY

Время: 2026-10-03T07:39:48.810055+00:00.

Этап: candidate; статус: **READY**.

SHA: `beb6616a1b319f69d65459e2d6bf890501c39ce0`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37106738690/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37107615389_attempt_1_candidate_READY

Время: 2026-10-03T07:54:50.882302+00:00.

Этап: candidate; статус: **READY**.

SHA: `46d052d7ee57058c114f98bf910388ec0bb9767d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37107615389/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37109172745_attempt_1_candidate_CANCELLED

Время: 2026-10-03T08:20:49.519286+00:00.

Этап: candidate; статус: **CANCELLED**.

SHA: `dae72d69f6b61ca8f12da2fa3ce227b111257325`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37109172745/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37108229759_attempt_1_preflight_PLANNED

Время восстановления записи: 2026-10-03T08:20:30Z.

Этап: preflight; статус: **PLANNED**.

SHA: `46d052d7ee57058c114f98bf910388ec0bb9767d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37108229759/attempts/1>).

Canonical production preflight прошёл (4.9 с); сам preflight не изменяет runtime/БД. Запись восстановлена после ошибки исходного publisher; production-событие фиксируется отдельно.

### GPOINT_RELEASE_EVENT_37108229759_attempt_1_production_COMPLETE

Время восстановления записи: 2026-10-03T08:20:30Z.

Этап: production; статус: **COMPLETE**.

SHA: `46d052d7ee57058c114f98bf910388ec0bb9767d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37108229759/attempts/1>).

Canonical status: **COMPLETE**. Release ID: `gpoint-release-20261003T080525092Z`. Production ref: `gpoint-production-20261003T080525092Z`. Runtime COMPLETE: `2026-10-03T08:11:21.879Z`.

До релиза: SHA `be38beef12c8b7083b0d6bbcf30b1a55991e1bc7`; Prisma `20260922090000_telegram_channel_avatar`; AGATA Alembic `agata0009`. Heads после релиза неизменны; API/Worker/Bot/Mini App/AGATA exact-SHA parity и health подтверждены canonical manifest.

Источники сверены на один SHA/release ID: canonical ledger + manifest, immutable result artifact с verified ZIP checksum `sha256:9c4e79152ba7369cc771643e5130e5ab8c2ac2de09ce36a035a0b8dec0b2087e`, completed deploy job **SUCCESS**. Общий workflow conclusion фактически **failure** из-за publisher; не подменяется на success. Ошибка циклической проверки overall conclusion исправлена отдельными reporting-коммитами, без повторного production apply.

Пользовательская проверка: **OWNER_SMOKE_PENDING**. История исходного сбоя сохранена; эта запись — восстановление отчётности уже состоявшегося релиза.

### GPOINT_RELEASE_EVENT_37109340824_attempt_1_candidate_READY

Время: 2026-10-03T08:26:51.698876+00:00.

Этап: candidate; статус: **READY**.

SHA: `d2a11394cd79b8e31f4f6820e9a514d53be4b578`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37109340824/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37114269577_attempt_1_candidate_CANCELLED

Время: 2026-10-03T09:50:39.906351+00:00.

Этап: candidate; статус: **CANCELLED**.

SHA: `07b9a4be297210028380ba6dcabcf3218941f7cc`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37114269577/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37114376694_attempt_1_candidate_READY

Время: 2026-10-03T09:57:03.156834+00:00.

Этап: candidate; статус: **READY**.

SHA: `554c6cac0ad23052522de5aa4d5da82d93d67124`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37114376694/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37115506002_attempt_1_candidate_READY

Время: 2026-10-03T10:15:50.610022+00:00.

Этап: candidate; статус: **READY**.

SHA: `dfbfd19efd7d1e8ede95746712afb5b9b98e039c`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37115506002/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37119251864_attempt_1_candidate_FAILED

Время: 2026-10-03T11:23:30.357507+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `d412b47f42ad366468deeb538e7ef66507b64250`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37119251864/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37120655836_attempt_1_candidate_READY

Время: 2026-10-03T11:50:53.087306+00:00.

Этап: candidate; статус: **READY**.

SHA: `54e7fb08e09497cf0ba2e7d2fee7797f979750eb`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37120655836/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37121080340_attempt_1_preflight_PLANNED

Время: 2026-10-03T11:57:56.266111+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `54e7fb08e09497cf0ba2e7d2fee7797f979750eb`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37121080340/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_37135521362_attempt_1_candidate_FAILED

Время: 2026-10-03T16:10:47.061531+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `f0ffe26311efa570b1fb57cea780b70d6c70626b`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37135521362/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37136352732_attempt_1_candidate_FAILED

Время: 2026-10-03T16:23:40.690834+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `f0ffe26311efa570b1fb57cea780b70d6c70626b`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37136352732/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

---

# Архив из «Архитектор — Handoff» — перенос 2026-10-03

Это исторические снимки и дополнительные отчёты, перенесённые из Handoff для сокращения точки входа нового Архитектора. Они НЕ являются текущим состоянием production; для актуального состояния использовать верхний auto-block Handoff, GitHub и live readback. Разделы, уже дословно присутствовавшие выше в этом журнале, повторно не переносились.

## Исторические снимки (1–2 октября 2026)

## Историческая краткая сводка — 2026-10-02

* GitHub `main` и production: `657ade5a8b836e682c6b9d9b62299a53fa792510` — exact SHA совпадает.
* Последний production workflow: [#37016184144](<https://github.com/akhripunov/gpoint/actions/runs/37016184144>), SUCCESS; canonical manifest COMPLETE, завершён 2026-10-02 в 16:59:01.814 МСК. Прямая сверка VPS: **2026-10-02, 17:11:15 МСК (14:11:15 UTC)**.
* API / Worker / Bot и оба компонента AGATA на этом SHA: running, restarts=0, OOM=false; API и оба AGATA health=healthy. Mini App build meta проверен на тот же SHA в 17:01:24 МСК.
* Prisma: `20260922090000_telegram_channel_avatar`; AGATA Alembic: `agata0008`; provider: `mac_worker`. Heads до/после совпали, новых миграций не было.
* Production ref: `gpoint-production-20261002T135526660Z`; previous production / runtime rollback point: `8c30d676809f8c0f6d3254891bf096b0b8740bd4`. Совместимость rollback с БД оценивать отдельно.
* [Актуальный canonical state](<https://github.com/akhripunov/gpoint/blob/gpoint-release-ledger/docs/release/state/production.json>) и полная история manifests находятся в **gpoint-release-ledger**, а не в application main. recoveryRequired=false.
* Технический выпуск завершён. Реальный ответ Telegram после этого выпуска владельцем ещё не подтверждён: **OWNER_SMOKE_PENDING**. Два ответа 1 октября — историческая приёмка того релиза, не новое подтверждение.
* Основной checkout чистый. Старый технический worktree удалён после сохранения четырёх журналов и архивного state в ledger; Git bundle и локальный архив проверены.

Маршрут артефактов: **GitHub Actions → проверенный кеш на Mac → VPS**. GHCR и прямая доставка GitHub→VPS не используются. Prepare строит приложение один раз; Deploy использует тот же verified archive и не пересобирает application images. Bookkeeping в отдельной ветке не меняет application SHA и не запускает новый Prepare из-за журнала.

Последний GPoint workflow: **16:54:42–16:59:35 МСК, 4 мин 53 с**. Измеренный интервал работы Dev после разрешения, включая итоговую сверку обоих VPS и запись журналов: **16:54:12–17:02:39, 8 мин 27 с**. Это выпуск с готовым Prepare и Mac cache HIT; не SLA для нового SHA. Последний Prepare GPoint занял 6 мин 31 с; сборки 77.8 с и 36.9 с — два образа **одного GPoint-релиза**, второй обслуживает AGATA. Добавлены замеры, сама сборка пока не ускорена. MaxiBot после последних измерений скорости не перевыкатывался ради нового benchmark; ускорение его полного production flow относительно исходного baseline не доказано.

Открытые продуктовые вопросы сохраняются: надёжность отображения Mac worker status, отдельная проверка извлечения/обновления памяти и пользовательский ответ после текущего релиза. Текущая техническая сводка не закрывает эти задачи.

## Историческая краткая сводка — 2026-10-01

Ниже сохранён снимок на 1 октября; его SHA и ожидание первого apply не являются текущим состоянием.

* Production: `62d96acc67bd739599b7edf205e5fabeed6456ce`; техническая выкладка COMPLETE (10:28:09 МСК).
* Диалог Models Hub: **два последовательных ответа подтверждены владельцем скриншотом в 10:37 МСК**.
* Prisma: `20260922090000_telegram_channel_avatar`; AGATA: `agata0008`; provider: `mac_worker`.
* Автоматизация подготовлена и проверена на SHA `259f4424af497a7808ef331f1394c9b12a6beaf4`. После этого опубликовано только уточнение инструкции runner; актуальный SHA и его готовность проверять по блоку «Автоматизация: кандидат» и текущему origin/main. Разрешения production для нового кандидата ещё нет.
* Внедрены GitHub workflows **GPoint — Prepare** и **GPoint — Deploy**: проверки exact SHA, Docker/PostgreSQL rehearsal, immutable artifacts, ручное разрешение production и автоматическая публикация в Linear. Prepare для `259f4424af497a7808ef331f1394c9b12a6beaf4` прошёл полностью, включая Linear.
* Mac runner `gpoint-owner-mac` подключён как пользовательский сервис. Полная проверка preflight прошла 2026-10-01 в 13:20 МСК: [Validate, Mac execution, Linear и artifact — SUCCESS](<https://github.com/akhripunov/gpoint/actions/runs/36840381378>). Canonical результат PLANNED; production runtime/БД не изменены. Первое production apply через новый workflow ещё не выполнялось.
* Открыто: надёжность отображения worker status; отдельная проверка извлечения и обновления памяти. Эти задачи текущая автоматизация не решает.

---

# Исторический снимок repository — 2026-10-02

GitHub: [akhripunov/gpoint](<https://github.com/akhripunov/gpoint>); default branch: `main`.

Current main = production: `657ade5a8b836e682c6b9d9b62299a53fa792510`, сверено 2026-10-02, 17:11:15 МСК (14:11:15 UTC). [Prepare #37009720243](<https://github.com/akhripunov/gpoint/actions/runs/37009720243>) SUCCESS; [production Deploy #37016184144](<https://github.com/akhripunov/gpoint/actions/runs/37016184144>) SUCCESS / COMPLETE. Application main не менялся при публикации последнего релиза; runtime не отстаёт. Release tooling из main включает отдельный ledger, coordinator и Prepare stage timings.

## Историческая сводка repository — 2026-10-01

GitHub: [akhripunov/gpoint](<https://github.com/akhripunov/gpoint>); default branch: `main`.

Подтверждённый GitHub origin/main на 2026-10-01, 13:23 МСК:
`061eef3c7ace047302dbdd6b6329a5dc02daf496`.

[Prepare этого SHA](<https://github.com/akhripunov/gpoint/actions/runs/36848626426>). Перед выпуском он должен завершиться успешно, включая Linear. Этот commit уточняет только инструкцию установки runner; его приложение совпадает с ранее проверенным `259f442`.

Production runtime candidate:
`62d96acc67bd739599b7edf205e5fabeed6456ce`.

Main опережает runtime: после `05b9a35` внедрены CI/release automation и проверка прав файлов AGATA Docker image, затем опубликовано уточнение инструкции runner. Эти commits ещё не выкатывались в production; runtime остаётся `62d96ac`. Перед каждой следующей работой сверять current origin/main и live readback.

Последние application/release-tooling изменения:

* `ce38486ef416869e217c5fa41773c02f258d38e0`: character sync выбирает точный configured route tuple вместо любого включённого маршрута.
* `62d96acc67bd739599b7edf205e5fabeed6456ce`: пустая callback model → codex-default; большая cleanup-команда → защищённый файл с SHA256 guard.

Исторические production SHA, промежуточные tooling commits и попытки см. <document id="56f7f9f5-c6e1-42b7-8c29-5b24e6c41a06" href="https://linear.app/bestuzhev/document/zhurnal-relizov-640efa60dacf">Журнал релизов</document> и [GitHub history](<https://github.com/akhripunov/gpoint/commits/main>).

---

# Историческая production verification — 2026-10-02

GPoint VPS: `38.180.110.92`; SSH user: `root`; root: `/opt/gpoint`; public host: `gpoint.amiora.one`.

* GitHub `main` и production: `657ade5a8b836e682c6b9d9b62299a53fa792510` — exact SHA совпадает.
* Последний production workflow: [#37016184144](<https://github.com/akhripunov/gpoint/actions/runs/37016184144>), SUCCESS; canonical manifest COMPLETE, завершён 2026-10-02 в 16:59:01.814 МСК. Прямая сверка VPS: **2026-10-02, 17:11:15 МСК (14:11:15 UTC)**.
* API / Worker / Bot и оба компонента AGATA на этом SHA: running, restarts=0, OOM=false; API и оба AGATA health=healthy. Mini App build meta проверен на тот же SHA в 17:01:24 МСК.
* Prisma: `20260922090000_telegram_channel_avatar`; AGATA Alembic: `agata0008`; provider: `mac_worker`. Heads до/после совпали, новых миграций не было.
* Production ref: `gpoint-production-20261002T135526660Z`; previous production / runtime rollback point: `8c30d676809f8c0f6d3254891bf096b0b8740bd4`. Совместимость rollback с БД оценивать отдельно.
* [Актуальный canonical state](<https://github.com/akhripunov/gpoint/blob/gpoint-release-ledger/docs/release/state/production.json>) и полная история manifests находятся в **gpoint-release-ledger**, а не в application main. recoveryRequired=false.
* Технический выпуск завершён. Реальный ответ Telegram после этого выпуска владельцем ещё не подтверждён: **OWNER_SMOKE_PENDING**. Два ответа 1 октября — историческая приёмка того релиза, не новое подтверждение.
* Основной checkout чистый. Старый технический worktree удалён после сохранения четырёх журналов и архивного state в ledger; Git bundle и локальный архив проверены.

[Последний canonical manifest](<https://github.com/akhripunov/gpoint/blob/gpoint-release-ledger/docs/release/manifests/gpoint-release-20261002T135526660Z.json>).

## Историческая production verification — 2026-10-01

GPoint VPS: `38.180.110.92`; SSH user: `root`; root: `/opt/gpoint`; public host: `gpoint.amiora.one`; ранее проверенный hostname: `a594436006.local`.

Canonical state: [docs/release/state/production.json](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/state/production.json>).

Последняя technical verification — canonical apply 2026-10-01, 10:28:09 МСК / 07:28:09 UTC:

* currentProductionSha / activeRuntimeSha: `62d96acc67bd739599b7edf205e5fabeed6456ce`.
* Prisma head: `20260922090000_telegram_channel_avatar`.
* AGATA Alembic head: `agata0008`.
* currentProductionRef: `gpoint-production-20261001T072211071Z`.
* releaseStatus / lastReleaseStatus: **COMPLETE**; recoveryRequired: **false**.
* Previous production / runtime rollback point: `ce38486ef416869e217c5fa41773c02f258d38e0`. Перед rollback обязательно оценивать совместимость с текущей БД; наличие старого image не доказывает безопасный DB rollback.
* API / Worker / Bot / Mini App и AGATA API / worker сверены с candidate SHA; AGATA health PASS.
* provider: `mac_worker`; AGATA_CODEX_LOCAL_ENABLED=true.
* [Release manifest](<https://github.com/akhripunov/gpoint/blob/05b9a35b12c542e437760d0fa0c64611399b1f60/docs/release/manifests/gpoint-release-20261001T072211071Z.json>).

Пользовательская verification — **2026-10-01, 10:37 МСК**: владелец сообщил о полученном ответе и прислал скриншот с двумя ответами подряд в Models Hub. Подтверждены отправка, генерация и отображение ответов в открытом чате. Долговременная память, повторное открытие UI и длительная стабильность не включены в эту приёмку.

Audit/manifests до этого подтверждения сохраняют историческую отметку USER_SMOKE_PENDING; последующее подтверждение записано в журнале без переписывания исходных доказательств.

## Дополнительные исторические release reports

## 2026-10-02 — аудит задержек и исправленный кандидат

Текущий проверенный кандидат: `5a1e25741b79d20669f2e12df414cbf35f444b54`. [Prepare 36964621192](<https://github.com/akhripunov/gpoint/actions/runs/36964621192>) success, `PREPARE_IMAGE_ROUNDTRIP=PASS`. [Preflight 36965012481](<https://github.com/akhripunov/gpoint/actions/runs/36965012481>) success, canonical `PLANNED`, Publish Linear success. Archive checksum, source parity, установленный Mac worker, runtime config и rollback gates PASS. Baseline: `62d96acc67bd739599b7edf205e5fabeed6456ce`, Prisma `20260922090000_telegram_channel_avatar`, AGATA `agata0008`; API healthy, rollback image доступен, свободно `15058392 KiB`. Новых миграций нет. Production этого кандидата ещё не запускался: запрошено owner approval exact SHA.

Предыдущий разрешённый production `7b243329…` остановлен до switch в `CI_ARTIFACT_UPLOAD`: 348 MiB archive дважды прерван на 60s ETIMEDOUT. Исправлены два уровня deadline: adapter и опубликованный SCP helper. Deadline зависит от размера (30s setup + 1s/MiB, min 60s, max 600s), две попытки максимум. Checksum/image ID gates не ослаблены. Установлен только исправленный GPoint SCP helper; credentials/LaunchAgents/Incy не менялись. 99 release tests PASS; repo start/end clean PASS.

В следующем Prepare выявлен отдельный Unix-socket readiness race тестового PostgreSQL: временный initdb server выдавал ready перед остановкой. Исправлено authenticated TCP SQL probe; полный новый Prepare PASS. Эти две причины подтверждены логами; исходный механизм прежнего image ID mismatch остаётся недоказанным и не подменяется догадкой.

Причина долгого общего ожидания: несколько последовательных циклов Prepare (\~6 минут каждый), сетевой transfer каждого нового immutable artifact (\~365 MB), жёсткий 60s SCP cutoff и повторные запуски устаревшего SHA после bookkeeping commit. Verified cache ускоряет повтор того же кандидата; 30-минутный bounded retry восстанавливает сеть, но сам по себе не ускоряет transfer. Дальнейшие архитектурные улучшения transport/bookkeeping должны идти отдельным изменением после выпуска, сохраняя exact SHA и provenance gates.

## 2026-10-02 — подтверждённая identity первопричина и READY кандидат

[Production 36965675985](<https://github.com/akhripunov/gpoint/actions/runs/36965675985>) для `5a1e257…` остановлен до runtime switch: upload PASS 42.4s, checksum/load PASS, identity gate FAIL. Read-only диагностика доказала несовместимость сравнения `.Id`: classic CI использует config digest, VPS Docker containerd store — manifest digest. Оба VPS manifest ссылаются на exact receipt config digest; архив корректен. Предыдущий классический save/load round-trip не проверял containerd identity semantics.

Исправленный exact SHA `61d6872249919e7a1fa8b2a6a219201ba483dbf8` опубликован в current origin/main. Общий import/reuse verifier хеширует native manifest и config blob, сверяет receipt config identity, platform, revision и rootfs diff IDs. Проверки не заменены labels-only; неизвестные/multi-platform descriptors и повреждённый content fail closed. Native IDs сохраняются для runtime parity. 100 release tests PASS; read-only verifier на обоих actual VPS images PASS.

[Prepare 36966272034](<https://github.com/akhripunov/gpoint/actions/runs/36966272034>) success, обязательный `PREPARE_IMAGE_ROUNDTRIP=PASS`, Publish Linear success. [Preflight 36966755070](<https://github.com/akhripunov/gpoint/actions/runs/36966755070>) success, canonical `PLANNED`, Publish Linear success. Live baseline SHA `62d96acc67bd739599b7edf205e5fabeed6456ce`, Prisma `20260922090000_telegram_channel_avatar`, AGATA `agata0008`, API healthy, rollback image available, free `13178480 KiB`. Новых миграций нет, repo clean-end PASS. Production нового SHA не запускался; запрошено owner exact-SHA approval.

## 2026-10-02 — downstream identity correction READY

Текущий exact candidate `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`. [Prepare 36967671335](<https://github.com/akhripunov/gpoint/actions/runs/36967671335>) success, 100 release tests и mandatory image round-trip PASS; Publish Linear success. [Preflight 36968120980](<https://github.com/akhripunov/gpoint/actions/runs/36968120980>) success, canonical `PLANNED`, Publish Linear success. Live baseline `62d96acc67bd739599b7edf205e5fabeed6456ce`, Prisma `20260922090000_telegram_channel_avatar`, AGATA `agata0008`, API healthy, rollback available, free `11747536 KiB`. Новых миграций нет, repository clean-end PASS.

Предыдущий authorized run `36967152577` подтвердил upload PASS 72.4s и strict import identity PASS 42.2s, затем остановился до switch на пропущенном downstream `.Id == receipt config` сравнении. Все downstream сравнения найдены; preparation/reuse/parity используют общий config identity verifier и native-to-native proof parity. Actual VPS full identity → native capture → parity PASS; временный невыполняемый контейнер без сети подтвердил `.Image` == native `.Id` и удалён. Runtime/БД не менялись. Исправление опубликовано и протестировано; production нового SHA не запускался, запрошено exact-SHA owner approval.

## 2026-10-02 — production COMPLETE, итог после исправления release tooling

Owner разрешил exact SHA `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`. [GPoint Deploy 36968491432](<https://github.com/akhripunov/gpoint/actions/runs/36968491432>) завершился SUCCESS, включая публикацию в Linear. Canonical manifest: `docs/release/manifests/gpoint-release-20261002T052102159Z.json`, status COMPLETE, production ref `gpoint-production-20261002T052102159Z`. До релиза: `62d96acc67bd739599b7edf205e5fabeed6456ce`; после: `820c943b0b5d7db94a05ab9bd5fa26e2238a6131`. Live SHA и labels API/AGATA независимо проверены на VPS; оба healthy. API/Worker/Bot/Mini App и AGATA artifact parity PASS; runtime health и reconciliation PASS. Prisma head до/после `20260922090000_telegram_channel_avatar`, AGATA head до/после `agata0008`; новых миграций нет.

Apply занял 200.1 с: upload 41.1 с, load/verification 41.4 с, runtime switch 13 с, container health 6.4 с. Причины прежних срывов устранены в GitHub: фиксированный SCP timeout 60 с, гонка готовности disposable PostgreSQL, ошибочное приравнивание Docker config digest к OCI manifest digest после import (включая downstream parity). Проверка checksum/config/rootfs/source SHA сохранена; application images потреблены из Prepare 36967671335 без rebuild на apply. 100 release tests, Prepare и preflight PASS.

MaxiBot также технически COMPLETE: [Deploy 36963856848](<https://github.com/akhripunov/maxibot/actions/runs/36963856848>), SHA `206a1a144be4e0c5de52238c273f89e908d4a84d`, migration head `0098_durable_autopilot_execution_claim_v1`, parity/health/finalization PASS. Его полный результат уже записан в собственных журнале и Handoff.

Реальный Telegram reply владельцем ещё не подтверждён: GPoint manifest `applicationVerification=OWNER_SMOKE_PENDING`, `productionUserSmoke=NOT_RUN`. Technical COMPLETE не является подтверждением E2E ответа. Synthetic Messages jobs не создавались.

Локальный main синхронизирован с origin/main (`a9c6eec`, release bookkeeping); `pnpm repo:clean:end` → `TASK_END_REPO_CLEAN=PASS`. Технических blockers для этих релизов не осталось; следующий контроль — owner Telegram smoke.

### 2026-10-02 — Release completion supplement

GPoint 8c30d676809f8c0f6d3254891bf096b0b8740bd4 technically deployed COMPLETE. GitHub Deploy 36988364163 SUCCESS including publication. API/Worker/Bot/Mini App and Agata artifact parity and health verified; unchanged Prisma/Agata heads. Canonical apply 196.5s; artifact upload 36.6s, load 43.2s. Main checkout clean gate PASS. Owner-confirmed actual Telegram reply is a separate pending fact (OWNER_SMOKE_PENDING), not inferred from heartbeat.

<!-- GPOINT_HANDOFF_ARCHIVE_20261003_END -->

### GPOINT_RELEASE_EVENT_37139722335_attempt_1_candidate_READY

Время: 2026-10-03T17:20:31.564108+00:00.

Этап: candidate; статус: **READY**.

SHA: `68bf0212d7aa5125394b0b39ffba9226eb106c0d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37139722335/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37140204607_attempt_1_preflight_PLANNED

Время: 2026-10-03T17:25:08.125728+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `68bf0212d7aa5125394b0b39ffba9226eb106c0d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37140204607/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_37140588175_attempt_1_preflight_PLANNED

Время: 2026-10-03T17:33:15.237191+00:00.

Этап: preflight; статус: **PLANNED**.

SHA: `68bf0212d7aa5125394b0b39ffba9226eb106c0d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37140588175/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

### GPOINT_RELEASE_EVENT_37140588175_attempt_1_production_COMPLETE

Время: 2026-10-03T17:33:26.979067+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `68bf0212d7aa5125394b0b39ffba9226eb106c0d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37140588175/attempts/1>).

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261003T172830088Z`.

До релиза: SHA `46d052d7ee57058c114f98bf910388ec0bb9767d`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0009`.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

## Identity reconciliation — 2026-10-04

Strict read-only audit; production was not changed.

* Live `AGATA_TENANT_ID` is `81a4ba28-6de5-4b03-8332-0661ba4d46f4`, matching VPS runtime configuration. It is an active registered tenant with an active GPOINT regional profile and GPOINT user/character/conversation data. Release provisioning generates a UUID4 only when this setting is absent; the baseline migration registers the configured ID and seeds its GPOINT profile. The row timestamp is consistent with the nearby initial bootstrap, but no specific release run is asserted as its writer.
* `7508f36f-774a-48aa-8c6b-42f6f160415d` originates in candidate/test fixtures and a draft worker target map; it is not the live runtime tenant. The queue snapshot now uses the tenant already selected by the runtime and verifies the active registry/profile under read-only tenant context. No GUC override or `production.env` edit.
* The reported SHA discrepancy was an incorrect source comparison. `8c30d676809f8c0f6d3254891bf096b0b8740bd4` is the previous-production value in the historical application-main state snapshot. Canonical `gpoint-release-ledger`, production ref `gpoint-production-20261003T172830088Z`, successful production Deploy run `37140588175`, immutable receipts, live API/Core images and this journal all identify `68bf0212d7aa5125394b0b39ffba9226eb106c0d`. GitHub compare confirms 68bf is 35 commits ahead of 8c30. No ledger reconciliation or deploy was needed.
* Fresh read-only Queue Preflight passed at `2026-10-04T07:49:08.177809Z`: runtime/API/AGATA SHA parity 68bf; API image `sha256:940236e984cff5e0123af62c58cea6ae5986fe879ca219daa2c91ff6ca8620e`; AGATA image `sha256:61299c506ed77971df0f75cb64cb91f739644ebbddfafc21d5cb903c4488dbb2`; pending=0, running=0, effective inference TTL=420s. GO applies only to Queue Preflight. Real Codex QA remains NO-GO/not performed pending separate approval and its full safety gates.
* P0 worker-token exposure remains open. The exact-SHA Mac/VPS rotation handoff is in the GPoint repository security docs. No token value, fragment, hash, or secret was recorded here; no rotation was performed.

## G Sales V1.1 final integration — 2026-10-04

Candidate `df045e0df2418aef9ccb8997bbcf0927c7a53e05` is pushed to the existing Draft PR [akhripunov/gpoint#11](<https://linear.app/bestuzhev/review/g-sales-v11-canonical-path-and-isolated-shared-mac-worker-targets-4ae6c0ec7b96>); no new PR was created and it remains Draft.

The independent reviewer found (1) the V2 GPOINT identity-less claim compatibility path did not enforce a trusted gateway binding, and (2) prior preflight documentation incorrectly presented a historical snapshot as current. Both were fixed. Legacy GPOINT claims without tenant fields now pass only through the exact audited production gateway URL; any other identity-less endpoint is rejected before inference. The reviewer rechecked the fixes and confirmed both findings closed, with no new material issue. The 07:49Z observation is documented as historical and insufficient for authorization.

Repeat verification on the pushed candidate:

* Disposable PostgreSQL: G Sales 40 passed; dedicated Core 1 passed; baseline migration 1 passed; full Core regression 112 passed, 0 skipped.
* Worker/controller focused suite: 32 passed.
* Release regression: 109 passed, 0 skipped.
* Ruff, G Sales mypy (18 source files), workspace typecheck, and diff checks passed.
* Fresh exact-candidate read-only Queue Preflight at 2026-10-04T08:27:49.983146Z: PASS. Live pending=0, running=0, effective inference TTL=420s; runtime tenant is 81a4ba28-6de5-4b03-8332-0661ba4d46f4; production release SHA is 68bf0212d7aa5125394b0b39ffba9226eb106c0d; API and AGATA image identities match the established release.
* Installed Mac LaunchAgent remains loaded and idle, with fresh polling at 08:27:57Z. Original plist, worker and wrapper hashes match their prior audited values.

The Queue Preflight is GO for its read-only gate only. Worker switching and Real Codex QA remain NO-GO pending separate OWNER approvals: (1) exact-SHA Mac/VPS worker-token rotation for the currently verified production runtime, and (2) one temporary Real Codex QA window for candidate df045e0df2418aef9ccb8997bbcf0927c7a53e05. No bootout, credential mutation, production deploy/migration, real Codex generation or Telegram sending occurred. P0 token exposure remains open; no secret value, fragment or fingerprint is included.

## Аудит текущего блокера G Sales — 2026-10-04, 20:18 МСК

Статус: **BLOCKED / NOT_READY_FOR_PREPARE**. Новый runtime release не запускался. Источник запроса: Codex «Устранить блокер релиза GPoint», chat 01a107e2-a792-7032-880f-174797265532; исследован Dev 01a10272-9483-7763-9aed-723929120970, его последние задания/отчёты, production integration attachment, Release Rules, Handoff, Release Journal и GSales Canonical Architecture.

### Проверенные текущие факты

* GitHub current main: `68bf0212d7aa5125394b0b39ffba9226eb106c0d`. Последний Prepare #37139722335 SUCCESS; production Deploy #37140588175 SUCCESS, включая publication. Активного Prepare/Deploy в полученной актуальной выборке runs нет; gpoint-owner-mac online, busy=false.
* Fresh canonical VPS read-only inspection 04.10: API/Worker/Bot/Mini App и оба AGATA runtime на том же SHA; API/AGATA health healthy; Prisma `20260922090000_telegram_channel_avatar`, AGATA `agata0010`; rollback image доступен. Это техническая проверка; новый owner Telegram smoke не выполнялся.
* Повторённый `gh auth status` внутри sandbox показывает invalid token, но с минимально необходимым разрешённым доступом к Keychain/network он SUCCESS, `gh api user` подтверждает akhripunov, Actions API работает. Docker socket тоже доступен с разрешённым доступом. Следовательно, заявленный в последнем Dev-отчёте «токен недействителен — повторная авторизация необходима» не подтверждается вне sandbox. Повторный OAuth login не нужен.
* PR [akhripunov/gpoint#11](<https://linear.app/bestuzhev/review/g-sales-v11-safe-real-codex-qa-recovery-watchdog-4ae6c0ec7b96>) Draft, head `eef5dc8eadf8dc9b58ea739ac4c884864add49a1`; PR [akhripunov/gpoint#15](<https://linear.app/bestuzhev/review/unified-agata-worker-v3-add-isolated-conversation-lab-lane-f7221091e8e6>) Draft, head `e933b8a79e24f24d831982b691e237c860507050`. Последние integration changes остаются незакоммиченными в V3 worktree; clean isolated copy на основе [akhripunov/gpoint#11](<https://linear.app/bestuzhev/review/g-sales-v11-safe-real-codex-qa-recovery-watchdog-4ae6c0ec7b96>) находится в /private/tmp/gpoint-gsales-candidate-tests, но содержит patch и является dirty. Финального integration SHA и успешного Prepare для него нет.

### Подтверждённые блокеры

1. **Не завершён canonical Deploy G Sales.** Patch добавляет третий image в Prepare/receipt/import/identity rehearsal. Однако scripts/release-gpoint.mjs и scripts/release-gpoint-vps-adapter.mjs не подключают docker-compose.gsales.yml; не вызывают G Sales checksum migrator app.db.migrate; нет post-switch проверки gsales-api runtime identity. Добавить image в bundle недостаточно, чтобы выкатить сервис.
2. **Mac worker parity FAIL на использованном scratch candidate.** Installed V3 source SHA256 `ac9266ec58f936a97c3d26985af20b71995fd4e0340fbb498e103d09318ba553`; scratch на [akhripunov/gpoint#11](<https://linear.app/bestuzhev/review/g-sales-v11-safe-real-codex-qa-recovery-watchdog-4ae6c0ec7b96>) содержит `81fc0fe5e86fb2794f19b6fecfafd336b7b6fd094e6b52d82b35ee1e7831162b`. scripts/automated-release.mjs остановится до canonical preflight. V3 уже работает локально на Mac и на VPS не развёртывается. Требуется согласовать source final candidate с уже одобренной установленной V3, без повторной установки worker и не вливая автоматически отдельные research PR [akhripunov/gpoint#10](<https://linear.app/bestuzhev/review/add-staged-agata-conversation-dataset-lab-edaba1c524a2>)/#12/#13/#14.
3. **Production provisioning отсутствует.** Без чтения значений проверено: отсутствуют GSALES\_\* DB/tenant/owner/worker/AGATA настройки, AGATA_ROLE_TENANT_BINDINGS и AGATA_MIGRATOR_DATABASE_URL. Read-only DB query показал отсутствие ожидаемых sales runtime roles и схем gsales/gsales_meta/agata_identity. Текущий AGATA login gpoint — NOSUPERUSER/NOBYPASSRLS/NOCREATEDB/NOCREATEROLE. Количество visible tenants=0 без tenant context не является полным census; при RLS его нельзя считать доказательством отсутствия tenants.
4. **Mac provider config не проведён через Compose.** Overlay не передаёт AGATA_CODEX_LOCAL_ENABLED и GPOINT_AGATA_WORKER_TOKEN для sales Core; типизированный validator требует корректный mac_worker config. Кроме того, GSALES lane установленного V3 ещё не подключена. OWNER уже утвердил Mac как постоянный provider — повторный выбор или обязательный paid/always-on API требовать нельзя.
5. **Финальная проверка release-контракта отсутствует.** В текущем scratch отдельно повторены 111/111 release tests и 4/4 Compose tests, 0 skipped. Эти PASS не доказывают G Sales migration/runtime/parity/health и не закрывают пункты 1–4. Dirty local images с label local-dirty-rehearsal из Dev не являются immutable Prepare bundle.

### Причина повторяющихся многочасовых циклов

Вывод по журналу и текущему коду: границы source → bundle → runtime configuration → DB identities → installed worker → actual service rollout проверяются раздельно; readiness часто сообщается по успешным частям до доказательства полного Deploy-контракта. На следующем этапе обнаруживается новая несовместимость и начинается новый SHA/Prepare/approval. Исторически это были classic/config vs containerd/manifest identity, downstream parity, SCP timeout, PostgreSQL startup race, неполный tenant census/backup и GitHub Actions artifact quota. Сейчас транспорт immutable GitHub Releases работает; проблема нового G Sales — незавершённая интеграция и ложная sandbox auth диагностика.

### Минимальный путь до реальной выкладки

1. Один integration checkout: approved G Sales + необходимый installed V3 source + текущий patch. Зафиксировать полный cumulative production→candidate diff; исследовательские изменения отдельно.
2. Закрыть **существующий** canonical Deploy-контракт: separate G Sales checksum migration, overlay/config, runtime start, health, image parity, failure/recovery и evidence. Предусмотреть gsales_meta в полном backup наряду с public/agata/gsales/agata_identity.
3. Подготовить конкретный secret-management/provisioning план с настоящим tenant, отдельными безопасными login identities/credentials и approved mac_worker. Секреты не публиковать. Production provisioning/миграции выполнять только под предусмотренным exact-SHA/schema-impact решением.
4. В disposable/restored DB доказать полный путь agata0010→0011→G Sales migration, role/tenant census, FORCE RLS, isolation, configuration и actual production image health/recovery. Повторить installed V3 source parity; не объявлять Linux private Docker 29.1.3 rehearsal локально выполненным на Mac Docker 29.6.1.
5. Только после локального PASS и независимого cumulative review — один final integration SHA, один main Prepare, immutable bundle + publication. Не менять main во время release. Отдельный повторный preflight run не обязателен, встроенный canonical preflight остаётся.
6. Перед apply предоставить OWNER конкретный SHA/состав/схемы/backup/риски. Для этого DB/RLS/release-infrastructure выпуска TASK_SCOPED_AUTO неприменим по действующим правилам; общий запрос «выкати» не заменяет exact-SHA approval. Затем один штатный production Deploy, подтверждённый read-back и отдельная Telegram acceptance.

### Реалистичная скорость

Последний успешный production workflow #37140588175: 17:27:47–17:33:40 UTC, **5 мин 53 с**; deploy job **5 мин 04 с**. Это исторический измеренный baseline подготовленного двухобразного GPoint, не SLA нового G Sales с миграцией/третьим образом. Главный резерв — устранить повторные release cycles и ложные остановки до Prepare; новый VPS, новый CI/CD и удаление safety gates для этого не требуются.

Основной checkout не изменён; repo:clean:start/end PASS. Существующие uncommitted integration files сохранены, задача не объявлена COMPLETE. Один read-only SSH запрос дал banner timeout; один повтор прошёл, бесконечных retry не было.

## GSales — продолжение операторского аудита, 2026-10-04

OWNER потребовал довести GSales deploy до конца. Подготовлен и опубликован кандидат `7636f92924ebf96f315df03a5a34bf0e6df5737b`, PR [akhripunov/gpoint#17](<https://linear.app/bestuzhev/review/complete-gsales-production-integration-with-existing-mac-worker-0d291c177984>). Production остаётся на `68bf0212d7aa5125394b0b39ffba9226eb106c0d`; production apply и изменения Mac worker не выполнялись.

Закрыты блокеры: шлюз Mac worker больше не требует уже работающего inference для первого claim; runtime проверяет tenant login binding; GSales overlay/мигратор/runtime evidence подключены к каноническому релизу; elevated migration DSN вынесены в отдельный migration.env; backup включает gsales_meta и необходимые расширения; AGATA0011 переносит DDL ownership от runtime и запрещает обход tenant RLS владельцем таблиц. Отправка outreach/auto reply/management/listener остаётся OFF.

Проверено: workspace build PASS; 117 release tests PASS; 17 Mac controller tests PASS; disposable PostgreSQL GSales/Core suite PASS; реальные hardened-image migration/provisioning и restore baseline0010/head0011 PASS. Четыре установленных Mac source assets совпадают с кандидатом. Worker V3 остаётся локально, source reinstall не планируется; после exact-SHA approval требуется только подключение GSales lane к существующему LaunchAgent.

GitHub API workflow_dispatch требует именованный ref. Проверочный tag `gpoint-rehearsal-7636f92924ebf96f315df03a5a34bf0e6df5737b` проверен через ls-remote на этот exact SHA; run [https://github.com/akhripunov/gpoint/actions/runs/37222558080](<https://github.com/akhripunov/gpoint/actions/runs/37222558080>) подтверждает headSha. Статус проверки IN_PROGRESS, это ещё не production readiness. После PASS: один финальный main Prepare с immutable receipt/Linear publication; затем отдельное OWNER exact-SHA/schema-impact approval согласно RELEASE_AUTHORIZATION.md и production Deploy.

### GPOINT_RELEASE_EVENT_37223324536_attempt_1_candidate_READY

Время: 2026-10-04T18:18:10.422172+00:00.

Этап: candidate; статус: **READY**.

SHA: `7636f92924ebf96f315df03a5a34bf0e6df5737b`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37223324536/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

## OWNER authorization and production approval record — GSales

* OWNER instruction received 2026-10-05 in this chat: “разркшение даю заранее вытывай”. Context: the immediately preceding message identified the exact prepared main SHA and explicitly stated that production apply changes database schemas and permissions.
* Authorized production candidate: `7636f92924ebf96f315df03a5a34bf0e6df5737b` (same SHA on GitHub main, Draft PR [akhripunov/gpoint#17](<https://linear.app/bestuzhev/review/complete-gsales-production-integration-with-existing-mac-worker-0d291c177984>) head, successful main Prepare run 37223324536; publication job 111499505960 succeeded).
* Exact-SHA mode: `EXACT_SHA`. This instruction approves applying the exact candidate in the prepared run below, including the stated AGATA and GSales migration/schema/permission changes. No other SHA or scope is authorized.
* Full unreleased diff from production/main baseline `68bf0212d7aa5125394b0b39ffba9226eb106c0d`: 174 files; 17,663 insertions, 296 deletions; diff SHA-256 `5bd70d87eab59a6f7b6b94cb64db98e703f3b766ed5993f6a052b287367d6245`. Review includes the merged approved GSales/V3 changes and canonical deployment/protection code; no separate research PR is included.
* Main Prepare immutable release: `gpoint-prepare-7636f92924ebf96f315df03a5a34bf0e6df5737b-37223324536-1`; `bundle.zip` SHA-256 per published asset digest: `deec1228bc2238d8439fe1cfff32e75c275c017fd6d66868c3cfaa5dbdd128d0`; run [https://github.com/akhripunov/gpoint/actions/runs/37223324536](<https://github.com/akhripunov/gpoint/actions/runs/37223324536>). Unit, PostgreSQL, all three image builds, migration/provisioning/restore rehearsal, image identity rehearsal and Linear publication all succeeded.
* Fresh read-only pre-apply production baseline at 2026-10-05 01:47:42 UTC: runtime SHA `68bf0212d7aa5125394b0b39ffba9226eb106c0d`; Prisma head `20260922090000_telegram_channel_avatar`; AGATA head `agata0010`; GSales schema absent; GPoint API and AGATA API/memory worker healthy; installed Docker `29.1.3`; VPS free disk `13,120,000 KiB`; prior release images available. No active production Deploy. Mac production runner online and idle; installed four V3 worker assets matched the candidate in the earlier preflight; GPoint worker was polling, no active GPoint inference was reported. Production remains at the recorded baseline at approval time.
* Authorized production behavior: tenant-scoped first-time GSales provisioning and six business tables; AGATA `agata0011` login-bound forced RLS and removal of runtime ownership; GSales checksum migration `001_gsales`; combined immutable image/runtime apply; existing Mac V3 source retained and authenticated GSales lane added through the idle-worker controller; complete GPoint-only backup before data migration; API/Core/worker source and readiness parity proof.
* GSales outreach, auto-replies, management, and Telegram listeners remain disabled. A real Telegram reply and OWNER device smoke are still separate acceptance facts.
* Inference from the owner instruction: it answers approval for the exact candidate and schema/permission scope described in this thread, under existing release rules. This record does not claim technical completion; only the authorized production Deploy result may do that.

### GPOINT_RELEASE_EVENT_37252899305_attempt_1_preflight_BLOCKED

Время: 2026-10-05T02:06:13.693627+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `7636f92924ebf96f315df03a5a34bf0e6df5737b`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37252899305/attempts/1>).

Это результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.

## 2026-10-05 — GSales Deploy попытка #37252899305 — требуется recovery

* OWNER заранее разрешил выкладку точного candidate SHA `7636f92924ebf96f315df03a5a34bf0e6df5737b`; релиз применил этот SHA после успешного current-main Prepare `#37223324536` и подтверждения immutable artifact.
* Исходный production baseline перед запуском: SHA `68bf0212d7aa5125394b0b39ffba9226eb106c0d`, Prisma `20260922090000_telegram_channel_avatar`, AGATA `agata0010`; GSales schema отсутствовала. Worker V3 source на Mac был сравнен с candidate, переустановка не выполнялась.
* Run: [GitHub Actions #37252899305](<https://github.com/akhripunov/gpoint/actions/runs/37252899305>). Подготовка artifact заняла 151.7 с (cache MISS); production preflight — 5 с; migration apply — 17.8 с; pre-switch preparation — 1.5 с; AGATA pre-switch readiness — 15.5 с; GSales pre-switch start — 19.3 с; runtime/AGATA/GSales post-switch reconciliation — 1.9/2.7/4.1 с; post-release cleanup — 56.7 с.
* Runtime switch и migration apply прошли; GSales runtime reconciliation завершился кодом 0. Final status — **RECOVERY_REQUIRED / POST_SWITCH**: release gate сообщил `BLOCKED: AGATA Mac worker authentication/status is not healthy`. Лог не устанавливает точнее, какое поле worker status было нарушено; heartbeat/poll нельзя объявлять исправными. Не создано синтетических production jobs, источники worker на Mac не менялись.
* Canonical recovery evidence/ref не сохранились. Bookkeeping push упал на Mac runner Keychain (`-25308`, `Device not configured`), а публикация immutable result ref — на GitHub API TLS handshake timeout. Publish workflow записал только результат preflight, не production COMPLETE. Прямой VPS readback, exact post-migration heads и фактический runtime SHA ещё требуется выполнить.
* Следующий безопасный шаг: read-only reconciliation production (VPS SHA/images, Prisma/AGATA/GSales heads, worker status, lock, release refs); определить причину локальной AGATA worker аутентификации/heartbeat. До этого новый Deploy/Apply и credential rotation запрещены; уже задокументированная GPOINT worker-token rotation процедура требует отдельного OWNER approval. После сверки сохранить recovery manifest/ledger, затем планировать отдельное восстановление с точным подтверждением.»

## 2026-10-05 — reconciliation #37252899305 и отделение Mac Worker availability

OWNER поручил read-only reconciliation перед изменением release contract. Новый apply, rollback, migration, Worker V3/LAB и GSales business flags не выполнялись. Все business flags остались false.

Полный audit с runtime/image IDs, migration checksum, backup SHA256 и разбором утраченной публикации: [factual reconciliation](<https://github.com/akhripunov/gpoint/blob/3248c6fbf36d0751363c2b355f4781d8bb4465c7/docs/release/audits/2026-10-05-gsales-run-37252899305.md>). [akhripunov/gpoint#18](<https://linear.app/bestuzhev/review/separate-vps-deployment-completion-from-mac-worker-readiness-bfd6c1f160b4>) merged в main `3248c6fbf36d0751363c2b355f4781d8bb4465c7`.

Серверный результат доказан COMPLETE: migrations/runtime switch/VPS health/image parity прошли. Исторический failed run не переписан; canonical ledger/ref ещё требуют PUBLICATION_REPAIR_REQUIRED вследствие Mac Keychain -25308 и TLS publication timeout. Временная Worker availability является отдельной dependency readiness, реальный E2E inference — отдельной acceptance fact.

До apply остаются fail-closed source/protocol/API/schema/tenant/lane/effective launch config compatibility; backup, migration, RLS, image identity и серверные health gates сохранены. После switch polling/auth/status/network timeout/heartbeat/worker_target_unavailable сохраняются как dependency evidence и не вызывают server recovery или rollback.

Новая команда `pnpm release:gpoint --recheck-worker --candidate <exact-completed-server-sha>` обновляет dependency/readiness state под lock без runtime/database apply и без изменения production SHA/ref/release ID. Она допускает только уже COMPLETE canonical release; stale ledger текущего run сначала требует отдельного factual bookkeeping repair.

Регрессии: COMPLETE/READY при healthy; COMPLETE/DEGRADED при unavailable; recovery Worker→READY без redeploy; server failure→FAILED/RECOVERY_REQUIRED; compatibility mismatch→BLOCKED до apply; operational timeout не вызывает rollback; timeout GPoint не стирает healthy GSales evidence; launch environment overrides включая пустые значения блокируются. 127 release, 30 Linear/compatibility и 17 Mac lane tests PASS. Независимый review: существенных замечаний после исправления нет.

### GPOINT_RELEASE_EVENT_37262816607_attempt_1_candidate_CANCELLED

Время: 2026-10-05T04:23:16.526876+00:00.

Этап: candidate; статус: **CANCELLED**.

SHA: `3248c6fbf36d0751363c2b355f4781d8bb4465c7`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37262816607/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37263243618_attempt_1_candidate_READY

Время: 2026-10-05T04:32:20.470607+00:00.

Этап: candidate; статус: **READY**.

SHA: `343f7b0822f18faf5eb0908a2020ddda4d39bff6`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37263243618/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

## 2026-10-05 — publication repair и Worker dependency recheck

Восстановлена canonical production запись для 7636f92924ebf96f315df03a5a34bf0e6df5737b, release ID gpoint-release-20261005T015237295Z. Production ref: gpoint-production-20261005T015237295Z. Сверка перед записью подтвердила Prisma 20260922090000_telegram_channel_avatar, AGATA agata0011, GSales 001_gsales и checksum 8de19ec2efd81848010379645d51243ac3f4a4cd37af0be211d71cdf36bcd111; образы, ревизии, health, tenant/RLS/Core runtime PASS. Ledger published through the normal release-bookkeeping path after read-only Git auth verification.

Оригинальный GitHub Deploy run [#37252899305](<https://github.com/akhripunov/gpoint/actions/runs/37252899305>) остаётся failure; его historical RECOVERY_REQUIRED result и причина POST_SWITCH не переписаны. Новый manifest/ledger отражают reconciled serverDeployStatus=COMPLETE, отдельно исходное failure и publication repair.

Команда pnpm release:gpoint --recheck-worker --candidate 7636f92924ebf96f315df03a5a34bf0e6df5737b создала dependency event gpoint-dependency-20261005T043634340Z: GPOINT и GSALES Worker HEALTHY, overall READY. В evidence: runtimeSwitchOccurred=false, databaseApplyOccurred=false.

Один настоящий GSales inference smoke пока не исполнен. Tenant-scoped read-only inventory показал ноль sellers и leads и нет утверждённой тестовой сущности; поэтому inference job/CRM data не создавались. Acceptance остаётся OWNER_SMOKE_PENDING до определения тестовой цели.

### GPOINT_RELEASE_EVENT_37285929225_attempt_1_candidate_FAILED

Время: 2026-10-05T08:50:36.436322+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `bddac7e3d9f5f556ae5cd4a2d78403a7771ef132`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37285929225/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37286451311_attempt_1_candidate_CANCELLED

Время: 2026-10-05T08:56:31.564075+00:00.

Этап: candidate; статус: **CANCELLED**.

SHA: `9589fa7138bd3c27f28d3c581f98399173c582e5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37286451311/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37286726965_attempt_1_candidate_READY

Время: 2026-10-05T09:05:21.394674+00:00.

Этап: candidate; статус: **READY**.

SHA: `d8baa160871079d1279c0428a829e8745edc4429`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37286726965/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37293948914_attempt_1_candidate_READY

Время: 2026-10-05T10:11:18.111596+00:00.

Этап: candidate; статус: **READY**.

SHA: `820712a151efbbeb86be1c6795840ce4d0f9ce65`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37293948914/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37299050933_attempt_1_candidate_CANCELLED

Время: 2026-10-05T10:57:33.459667+00:00.

Этап: candidate; статус: **CANCELLED**.

SHA: `55ea6d587e86b41d50789b6b0075615ca8b2a288`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37299050933/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37299773894_attempt_1_candidate_CANCELLED

Время: 2026-10-05T11:05:40.480233+00:00.

Этап: candidate; статус: **CANCELLED**.

SHA: `41187e8b2ea39932de496473eda5e4b936016d41`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37299773894/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37300593288_attempt_1_candidate_CANCELLED

Время: 2026-10-05T11:14:42.042407+00:00.

Этап: candidate; статус: **CANCELLED**.

SHA: `831a254dde5f57fc19c5fb573c05dcd3cc364746`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37300593288/attempts/1>).

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37301617538_attempt_1_candidate_READY

Время: 2026-10-05T11:23:40.620818+00:00.

Этап: candidate; статус: **READY**.

SHA: `a4b7c76650f58d2bd33ca1b33e69006ef7a1ae26`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37301617538/attempts/1>).

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37303817993_attempt_1_candidate_READY

Время: 2026-10-05T11:42:51.686127+00:00.

Этап: candidate; статус: **READY**.

SHA: `4c7b0c45e7149fb633a5c3091a00ac64ddde7d3e`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37303817993/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `493`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37304772550_attempt_1_preflight_FAILED

Время: 2026-10-05T11:51:18.640435+00:00.

Этап: preflight; статус: **FAILED**.

SHA: `4c7b0c45e7149fb633a5c3091a00ac64ddde7d3e`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37304772550/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `513`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `463`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37312657877_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-05T12:55:47.111141+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `4c7b0c45e7149fb633a5c3091a00ac64ddde7d3e`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37312657877/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `514`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `168`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

**GPoint SHA 4c7b0c45e7149fb633a5c3091a00ac64ddde7d3e полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

## 2026-10-05T13:00:55Z — exact-SHA read-only preflight recovery: READY_FOR_OWNER

* Candidate / GitHub main: `4c7b0c45e7149fb633a5c3091a00ac64ddde7d3e`.
* Prepare: [#37303817993](<https://github.com/akhripunov/gpoint/actions/runs/37303817993>), SUCCESS; immutable bundle Release and SHA256 verified.
* Automatic preflight [#37304772550](<https://github.com/akhripunov/gpoint/actions/runs/37304772550>) stopped before any mutation because local disk had 4.10 GiB free, below the mandatory 10 GiB floor. Thresholds were not changed.
* One exact-SHA recovery preflight [#37312657877](<https://github.com/akhripunov/gpoint/actions/runs/37312657877>), mode=preflight, completed SUCCESS. Cache HIT avoided redownloading the verified bundle; remaining disk was 13.14 GiB (warning vs 15 GiB target, above blocking floor). Canonical dry-run and remote read-only preflight passed; immutable evidence and Linear READY_FOR_OWNER publication passed.
* Telemetry for whole measured chain: 6 hosted jobs / 552 runner-seconds; 3 self-hosted jobs / 590 runner-seconds. No duplicate image build. Production SHA remains `7636f92924ebf96f315df03a5a34bf0e6df5737b`; production and database were not changed.

### GPOINT_RELEASE_EVENT_37323003991_attempt_1_candidate_READY

Время: 2026-10-05T14:22:36.661472+00:00.

Этап: candidate; статус: **READY**.

SHA: `9213693b271d631276bf5b4464172b35f8b498ff`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37323003991/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `525`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37324408907_attempt_1_preflight_FAILED

Время: 2026-10-05T14:53:29.021854+00:00.

Этап: preflight; статус: **FAILED**.

SHA: `9213693b271d631276bf5b4464172b35f8b498ff`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37324408907/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `580`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `1779`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37333539530_attempt_1_candidate_READY

Время: 2026-10-05T15:39:17.489695+00:00.

Этап: candidate; статус: **READY**.

SHA: `6c2dc570b7f484837cd400e15a182dc04a3e5078`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37333539530/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `451`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37334644825_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-05T15:43:46.052255+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `6c2dc570b7f484837cd400e15a182dc04a3e5078`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37334644825/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `480`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `223`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 6c2dc570b7f484837cd400e15a182dc04a3e5078 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37335702948_attempt_1_candidate_READY

Время: 2026-10-05T15:56:04.691848+00:00.

Этап: candidate; статус: **READY**.

SHA: `afc07df97b0a6d4b96eae38229d6b0c78183c799`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37335702948/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `489`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37336912397_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-05T15:59:55.529728+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `afc07df97b0a6d4b96eae38229d6b0c78183c799`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37336912397/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `516`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `184`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA afc07df97b0a6d4b96eae38229d6b0c78183c799 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37338932286_attempt_1_candidate_READY

Время: 2026-10-05T16:19:49.447140+00:00.

Этап: candidate; статус: **READY**.

SHA: `a8fb73ed7bba776e7c84b9f8aec3a2aeb84b3aa7`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37338932286/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `475`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37340057625_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-05T16:24:04.806204+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `a8fb73ed7bba776e7c84b9f8aec3a2aeb84b3aa7`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37340057625/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `501`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `213`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA a8fb73ed7bba776e7c84b9f8aec3a2aeb84b3aa7 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37341079716_attempt_1_candidate_READY

Время: 2026-10-05T16:36:49.458416+00:00.

Этап: candidate; статус: **READY**.

SHA: `05dfccbf659011d1961b7966444c9e1d9975f8fe`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37341079716/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `507`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37342252555_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-05T16:40:51.245817+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `05dfccbf659011d1961b7966444c9e1d9975f8fe`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37342252555/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `529`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `208`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 05dfccbf659011d1961b7966444c9e1d9975f8fe полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37344804964_attempt_1_candidate_FAILED

Время: 2026-10-05T17:04:47.805957+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `f6143df801384108153ab2f59ad9b019d3738ee5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37344804964/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `424`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37346355392_attempt_1_candidate_READY

Время: 2026-10-05T17:18:16.791949+00:00.

Этап: candidate; статус: **READY**.

SHA: `b164725f5f054de9647cd710ad779fb8eae62f01`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37346355392/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `501`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37347496430_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-05T17:22:45.803431+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `b164725f5f054de9647cd710ad779fb8eae62f01`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37347496430/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `527`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `228`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA b164725f5f054de9647cd710ad779fb8eae62f01 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37350197831_attempt_1_candidate_READY

Время: 2026-10-05T17:50:40.165396+00:00.

Этап: candidate; статус: **READY**.

SHA: `5a504d6ad37f6ab1f6f0bfb624aecccf163ebfde`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37350197831/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `598`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37351572818_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-05T17:55:54.349902+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `5a504d6ad37f6ab1f6f0bfb624aecccf163ebfde`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37351572818/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `620`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `281`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 5a504d6ad37f6ab1f6f0bfb624aecccf163ebfde полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37353643939_attempt_1_candidate_READY

Время: 2026-10-05T18:18:40.383913+00:00.

Этап: candidate; статус: **READY**.

SHA: `40bc9bb620e257d232e1a8aa05d43ccce4851e3c`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37353643939/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `631`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37355044316_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-05T18:23:04.861003+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `40bc9bb620e257d232e1a8aa05d43ccce4851e3c`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37355044316/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `653`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `228`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 40bc9bb620e257d232e1a8aa05d43ccce4851e3c полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37355985200_attempt_1_candidate_CANCELLED

Время: 2026-10-05T18:33:50.014832+00:00.

Этап: candidate; статус: **CANCELLED**.

SHA: `1473b856f153d46fed862a3e7fedc51a75c84822`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37355985200/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `423`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37356703572_attempt_1_candidate_READY

Время: 2026-10-05T18:43:41.898659+00:00.

Этап: candidate; статус: **READY**.

SHA: `430e399202fb691010af3863899c750c146a8240`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37356703572/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `571`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37358216918_attempt_1_preflight_BLOCKED

Время: 2026-10-05T18:46:48.678696+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `430e399202fb691010af3863899c750c146a8240`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37358216918/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `598`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `145`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

## 2026-10-05 — переход GPoint на Release Orchestrator V2

Новая каноническая orchestration принята:

`commit → Prepare → automatic preflight → READY_FOR_OWNER → OWNER approval → canonical Deploy → smoke/verify → COMPLETE`.

Чистое доказательство без production:

* SHA: `40bc9bb620e257d232e1a8aa05d43ccce4851e3c`
* Prepare: #37353643939 — SUCCESS
* Auto-controller: #37355005084 — SUCCESS
* Read-only preflight: #37355044316 — SUCCESS / READY_FOR_OWNER
* Manual intervention / rerun / recovery: 0
* Commit→READY: 16:04
* Исторический manual baseline: 11:52
* Raw результат: +4:12 медленнее
* В новом Prepare есть обязательный PostgreSQL/GSales regression gate около 401 s, которого не было в baseline; поэтому raw speedup самого Orchestrator не установлен
* Production changed: NO

Отдельный последующий main SHA `430e399202fb691010af3863899c750c146a8240` дошёл через automatic controller до fail-closed preflight BLOCKED из-за несовпадения установленного Mac Worker с candidate; это не ошибка orchestration и не чистый benchmark.

Первый production apply по новой orchestration ещё не выполнялся.

### GPOINT_RELEASE_EVENT_37527441939_attempt_1_candidate_READY

Время: 2026-10-06T20:45:58.374080+00:00.

Этап: candidate; статус: **READY**.

SHA: `945598b5e082677b3e77e03b53006a2989eef2eb`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37527441939/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `697`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37528986712_attempt_1_preflight_BLOCKED

Время: 2026-10-06T20:48:33.414729+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `945598b5e082677b3e77e03b53006a2989eef2eb`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37528986712/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `721`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `118`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37537409132_attempt_1_candidate_READY

Время: 2026-10-06T22:06:49.671023+00:00.

Этап: candidate; статус: **READY**.

SHA: `c410c6645847ff29198ddebf0647e03e7a339394`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37537409132/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `573`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37538535571_attempt_1_preflight_FAILED

Время: 2026-10-06T22:09:30.347479+00:00.

Этап: preflight; статус: **FAILED**.

SHA: `c410c6645847ff29198ddebf0647e03e7a339394`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37538535571/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `595`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `123`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37538535571_attempt_2_preflight_FAILED

Время: 2026-10-07T03:49:15.773076+00:00.

Этап: preflight; статус: **FAILED**.

SHA: `c410c6645847ff29198ddebf0647e03e7a339394`.

Попытка GitHub Actions: `2`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37538535571/attempts/2>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `594`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `88`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

Это повторный запуск. Предыдущая запись сохранена; данный статус относится к указанной попытке.

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37570849039_attempt_1_candidate_READY

Время: 2026-10-07T04:28:17.626791+00:00.

Этап: candidate; статус: **READY**.

SHA: `8f6462471d6ea685b61ff5ead46aadfa5bd60b4a`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37570849039/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `569`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37571661980_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-07T04:31:41.087884+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `8f6462471d6ea685b61ff5ead46aadfa5bd60b4a`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37571661980/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `591`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `168`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 8f6462471d6ea685b61ff5ead46aadfa5bd60b4a полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37574033232_attempt_1_candidate_FAILED

Время: 2026-10-07T05:02:24.382425+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `90e2b7208a0ab64f8af65a3d802a6f403653cef2`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37574033232/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `221`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37575739182_attempt_1_candidate_READY

Время: 2026-10-07T05:29:49.560942+00:00.

Этап: candidate; статус: **READY**.

SHA: `37e47dbc1a8d29d9f3edb68c87a70c9c18aec4a1`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37575739182/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `612`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37576632994_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-07T05:33:34.332897+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `37e47dbc1a8d29d9f3edb68c87a70c9c18aec4a1`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37576632994/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `638`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `151`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 37e47dbc1a8d29d9f3edb68c87a70c9c18aec4a1 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37578150797_attempt_1_candidate_FAILED

Время: 2026-10-07T05:56:50.845683+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `f34a73831c35d0d872ef5704e2167e24f5f96875`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37578150797/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `497`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37578150797_attempt_2_candidate_READY

Время: 2026-10-07T06:08:03.119329+00:00.

Этап: candidate; статус: **READY**.

SHA: `f34a73831c35d0d872ef5704e2167e24f5f96875`.

Попытка GitHub Actions: `2`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37578150797/attempts/2>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `520`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это повторный запуск. Предыдущая запись сохранена; данный статус относится к указанной попытке.

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37579918089_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-07T06:10:55.506531+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `f34a73831c35d0d872ef5704e2167e24f5f96875`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37579918089/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `550`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `130`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA f34a73831c35d0d872ef5704e2167e24f5f96875 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37583799068_attempt_1_preflight_BLOCKED

Время: 2026-10-07T06:51:16.625243+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `f34a73831c35d0d872ef5704e2167e24f5f96875`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37583799068/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `11`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37585976626_attempt_1_candidate_FAILED

Время: 2026-10-07T07:15:00.525310+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `af7a2c2ac0c9c9449c4c9ab3f3a2bf3002108e18`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37585976626/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `110`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37586465652_attempt_1_candidate_READY

Время: 2026-10-07T07:26:18.783923+00:00.

Этап: candidate; статус: **READY**.

SHA: `0eccac9eaadf0e4c8ae7dfef0231377181540549`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37586465652/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `499`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37587363931_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-07T07:29:11.384958+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `0eccac9eaadf0e4c8ae7dfef0231377181540549`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37587363931/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `521`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `140`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 0eccac9eaadf0e4c8ae7dfef0231377181540549 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37588068849_attempt_1_preflight_BLOCKED

Время: 2026-10-07T07:34:22.095207+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `0eccac9eaadf0e4c8ae7dfef0231377181540549`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37588068849/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `569`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `161`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37602996919_attempt_1_candidate_READY

Время: 2026-10-07T09:57:01.764640+00:00.

Этап: candidate; статус: **READY**.

SHA: `b25b223786491c9bb0d015efb24fcf792491ed0f`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37602996919/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `579`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37604151225_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-07T09:59:59.558835+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `b25b223786491c9bb0d015efb24fcf792491ed0f`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37604151225/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `606`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `137`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA b25b223786491c9bb0d015efb24fcf792491ed0f полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37617667589_attempt_1_candidate_READY

Время: 2026-10-07T12:07:04.899899+00:00.

Этап: candidate; статус: **READY**.

SHA: `cf606812279e0cf9477d1816efb73f91bb2045f3`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37617667589/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `539`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37618807908_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-07T12:10:02.880461+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `cf606812279e0cf9477d1816efb73f91bb2045f3`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37618807908/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `565`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `135`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA cf606812279e0cf9477d1816efb73f91bb2045f3 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37621372588_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-07T12:35:41.594197+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `cf606812279e0cf9477d1816efb73f91bb2045f3`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37621372588/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `596`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `475`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

**GPoint SHA cf606812279e0cf9477d1816efb73f91bb2045f3 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37621372588_attempt_1_production_COMPLETE

Время: 2026-10-07T12:35:55.196588+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `cf606812279e0cf9477d1816efb73f91bb2045f3`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37621372588/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `596`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `475`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261007T123043005Z`.

До релиза: SHA `7636f92924ebf96f315df03a5a34bf0e6df5737b`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0011`.

Server deploy: **COMPLETE**; Mac worker: **HEALTHY**; service readiness: **READY**.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### GPOINT_RELEASE_EVENT_37628220147_attempt_1_candidate_READY

Время: 2026-10-07T13:35:01.525455+00:00.

Этап: candidate; статус: **READY**.

SHA: `7815362bee308fa1849671f2e66d242c28009151`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37628220147/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `633`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37629747888_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-07T13:38:19.665266+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `7815362bee308fa1849671f2e66d242c28009151`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37629747888/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `665`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `148`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 7815362bee308fa1849671f2e66d242c28009151 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37631254518_attempt_1_candidate_READY

Время: 2026-10-07T13:58:39.338663+00:00.

Этап: candidate; статус: **READY**.

SHA: `6859932bb9c97ae67e0026d34f9e2754bd4e7dee`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37631254518/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `634`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37632939421_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-07T14:01:30.120682+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `6859932bb9c97ae67e0026d34f9e2754bd4e7dee`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37632939421/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `668`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `123`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 6859932bb9c97ae67e0026d34f9e2754bd4e7dee полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37665066963_attempt_1_candidate_FAILED

Время: 2026-10-07T18:21:26.344955+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `db38e0936ec0e00ea272b29b522718c832f0479d`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37665066963/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `515`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37666648230_attempt_1_candidate_READY

Время: 2026-10-07T18:37:22.868118+00:00.

Этап: candidate; статус: **READY**.

SHA: `215776691e2613ce3bea64a3b1eb52129f69d519`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37666648230/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `738`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37668319026_attempt_1_preflight_BLOCKED

Время: 2026-10-07T18:50:17.422785+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `215776691e2613ce3bea64a3b1eb52129f69d519`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37668319026/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `769`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `738`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_37732782486_attempt_1_candidate_READY

Время: 2026-10-08T05:41:40.626057+00:00.

Этап: candidate; статус: **READY**.

SHA: `bb89256c58c59c27b5a1a8e365a4d1d23c128662`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37732782486/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `624`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37734858501_attempt_1_candidate_READY

Время: 2026-10-08T06:07:15.226937+00:00.

Этап: candidate; статус: **READY**.

SHA: `fae6ff05f9af769004acb9417429efb68a7445f4`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37734858501/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `666`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37736608715_attempt_1_candidate_FAILED

Время: 2026-10-08T06:21:12.496553+00:00.

Этап: candidate; статус: **FAILED**.

SHA: `8a2c16168e760b038367daa374c10630c8a49a02`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37736608715/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `305`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Выкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.

### GPOINT_RELEASE_EVENT_37737662989_attempt_1_candidate_READY

Время: 2026-10-08T06:37:32.948478+00:00.

Этап: candidate; статус: **READY**.

SHA: `70b15451a57a20cacef07b5651171c7ab23c36e2`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37737662989/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `623`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_37752671786_attempt_1_candidate_READY

Время: 2026-10-08T09:02:03.799382+00:00.

Этап: candidate; статус: **READY**.

SHA: `0b92fa016d92bda092429ba23d60c0b254e89324`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/37752671786/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `609`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_38214088790_attempt_1_candidate_READY

Время: 2026-10-12T06:54:26.320536+00:00.

Этап: candidate; статус: **READY**.

SHA: `462d4fba8204b343fe18d066b2ce24c6e04ef803`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38214088790/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `541`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_38214918373_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-12T06:57:07.989006+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `462d4fba8204b343fe18d066b2ce24c6e04ef803`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38214918373/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `575`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `115`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 462d4fba8204b343fe18d066b2ce24c6e04ef803 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38217057708_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-12T07:26:09.154522+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `462d4fba8204b343fe18d066b2ce24c6e04ef803`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38217057708/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `608`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `516`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

**GPoint SHA 462d4fba8204b343fe18d066b2ce24c6e04ef803 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38217057708_attempt_1_production_COMPLETE

Время: 2026-10-12T07:26:22.705169+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `462d4fba8204b343fe18d066b2ce24c6e04ef803`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38217057708/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `608`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `516`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261012T072003704Z`.

До релиза: SHA `cf606812279e0cf9477d1816efb73f91bb2045f3`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0011`.

Server deploy: **COMPLETE**; Mac worker: **HEALTHY**; service readiness: **READY**.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### GPOINT_RELEASE_EVENT_38221981366_attempt_1_candidate_READY

Время: 2026-10-12T08:21:16.070156+00:00.

Этап: candidate; статус: **READY**.

SHA: `ffa768d9028a9591f1331838a29fe1429056e504`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38221981366/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `632`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_38223112241_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-12T08:23:56.107744+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `ffa768d9028a9591f1331838a29fe1429056e504`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38223112241/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `663`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `116`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA ffa768d9028a9591f1331838a29fe1429056e504 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38223496328_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-12T08:31:52.113522+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `ffa768d9028a9591f1331838a29fe1429056e504`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38223496328/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `706`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `455`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

**GPoint SHA ffa768d9028a9591f1331838a29fe1429056e504 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38223496328_attempt_1_production_COMPLETE

Время: 2026-10-12T08:32:15.434131+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `ffa768d9028a9591f1331838a29fe1429056e504`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38223496328/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `706`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `455`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261012T082646460Z`.

До релиза: SHA `462d4fba8204b343fe18d066b2ce24c6e04ef803`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0011`.

Server deploy: **COMPLETE**; Mac worker: **HEALTHY**; service readiness: **READY**.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### GPOINT_RELEASE_EVENT_38224325297_attempt_1_candidate_READY

Время: 2026-10-12T08:43:30.386874+00:00.

Этап: candidate; статус: **READY**.

SHA: `97415945d9746e263db6f1faf01ab0e3cda77bbd`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38224325297/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `573`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_38225386119_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-12T08:46:39.926468+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `97415945d9746e263db6f1faf01ab0e3cda77bbd`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38225386119/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `600`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `114`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 97415945d9746e263db6f1faf01ab0e3cda77bbd полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38227070337_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-12T09:07:25.852883+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `97415945d9746e263db6f1faf01ab0e3cda77bbd`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38227070337/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `639`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `522`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

**GPoint SHA 97415945d9746e263db6f1faf01ab0e3cda77bbd полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38227070337_attempt_1_production_COMPLETE

Время: 2026-10-12T09:07:43.721507+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `97415945d9746e263db6f1faf01ab0e3cda77bbd`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38227070337/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `639`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `522`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261012T090120871Z`.

До релиза: SHA `ffa768d9028a9591f1331838a29fe1429056e504`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0011`.

Server deploy: **COMPLETE**; Mac worker: **HEALTHY**; service readiness: **READY**.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### GPOINT_RELEASE_EVENT_38280837612_attempt_1_candidate_READY

Время: 2026-10-12T17:04:48.814782+00:00.

Этап: candidate; статус: **READY**.

SHA: `6afdf62692a58f8e8333c9da99ac7abbe603adc5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38280837612/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `512`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_38281890548_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-12T17:07:35.077873+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `6afdf62692a58f8e8333c9da99ac7abbe603adc5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38281890548/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `540`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `122`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA 6afdf62692a58f8e8333c9da99ac7abbe603adc5 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38282446549_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-12T17:17:22.544202+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `6afdf62692a58f8e8333c9da99ac7abbe603adc5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38282446549/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `579`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `504`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

**GPoint SHA 6afdf62692a58f8e8333c9da99ac7abbe603adc5 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38282446549_attempt_1_production_COMPLETE

Время: 2026-10-12T17:17:47.773072+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `6afdf62692a58f8e8333c9da99ac7abbe603adc5`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38282446549/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `579`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `504`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261012T171129127Z`.

До релиза: SHA `97415945d9746e263db6f1faf01ab0e3cda77bbd`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0011`.

Server deploy: **COMPLETE**; Mac worker: **HEALTHY**; service readiness: **READY**.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### GPOINT_RELEASE_EVENT_38298513885_attempt_1_candidate_READY

Время: 2026-10-12T19:43:33.400520+00:00.

Этап: candidate; статус: **READY**.

SHA: `dd6ca175ecb78362aa352b82f15767ba724b2744`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38298513885/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `557`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_38299616155_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-12T19:46:26.400915+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `dd6ca175ecb78362aa352b82f15767ba724b2744`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38299616155/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `592`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `125`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA dd6ca175ecb78362aa352b82f15767ba724b2744 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38300045099_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-12T19:55:27.168872+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `dd6ca175ecb78362aa352b82f15767ba724b2744`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38300045099/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `628`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `534`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

**GPoint SHA dd6ca175ecb78362aa352b82f15767ba724b2744 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38300045099_attempt_1_production_COMPLETE

Время: 2026-10-12T19:55:49.530235+00:00.

Этап: production; статус: **COMPLETE**.

SHA: `dd6ca175ecb78362aa352b82f15767ba724b2744`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38300045099/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `6`
* GITHUB_HOSTED_RUNNER_SECONDS: `628`
* SELF_HOSTED_JOB_COUNT: `3`
* SELF_HOSTED_RUNNER_SECONDS: `534`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

Canonical status: **COMPLETE**. Production ref: `gpoint-production-20261012T194913088Z`.

До релиза: SHA `6afdf62692a58f8e8333c9da99ac7abbe603adc5`; Prisma `20260922090000_telegram_channel_avatar`; Alembic `agata0011`.

Server deploy: **COMPLETE**; Mac worker: **HEALTHY**; service readiness: **READY**.

Пользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.

### GPOINT_RELEASE_EVENT_38358463740_attempt_1_candidate_READY

Время: 2026-10-13T06:54:25.913866+00:00.

Этап: candidate; статус: **READY**.

SHA: `a6234fc11fd5c80d63b3faa4eee5c95cb95f9edf`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38358463740/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `566`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_38359467508_attempt_1_preflight_BLOCKED

Время: 2026-10-13T07:06:06.195395+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `a6234fc11fd5c80d63b3faa4eee5c95cb95f9edf`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38359467508/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `595`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `372`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38359467508_attempt_2_preflight_BLOCKED

Время: 2026-10-13T13:26:31.090902+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `a6234fc11fd5c80d63b3faa4eee5c95cb95f9edf`.

Попытка GitHub Actions: `2`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38359467508/attempts/2>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `597`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `697`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

Это повторный запуск. Предыдущая запись сохранена; данный статус относится к указанной попытке.

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38413228260_attempt_1_candidate_READY

Время: 2026-10-13T14:37:27.894306+00:00.

Этап: candidate; статус: **READY**.

SHA: `360ad13a697c6b5b328ce2a09b54e0a2590ca734`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38413228260/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `638`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_38415302982_attempt_1_preflight_BLOCKED

Время: 2026-10-13T14:45:17.401390+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `360ad13a697c6b5b328ce2a09b54e0a2590ca734`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38415302982/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `673`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `440`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38415302982_attempt_2_preflight_FAILED

Время: 2026-10-13T15:21:02.371208+00:00.

Этап: preflight; статус: **FAILED**.

SHA: `360ad13a697c6b5b328ce2a09b54e0a2590ca734`.

Попытка GitHub Actions: `2`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38415302982/attempts/2>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `673`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `535`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это повторный запуск. Предыдущая запись сохранена; данный статус относится к указанной попытке.

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38415302982_attempt_3_preflight_BLOCKED

Время: 2026-10-13T15:36:48.826236+00:00.

Этап: preflight; статус: **BLOCKED**.

SHA: `360ad13a697c6b5b328ce2a09b54e0a2590ca734`.

Попытка GitHub Actions: `3`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38415302982/attempts/3>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `673`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `742`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

Это повторный запуск. Предыдущая запись сохранена; данный статус относится к указанной попытке.

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38415302982_attempt_4_preflight_FAILED

Время: 2026-10-13T16:03:21.578637+00:00.

Этап: preflight; статус: **FAILED**.

SHA: `360ad13a697c6b5b328ce2a09b54e0a2590ca734`.

Попытка GitHub Actions: `4`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38415302982/attempts/4>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `673`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `569`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `YES`

Это повторный запуск. Предыдущая запись сохранена; данный статус относится к указанной попытке.

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.

### GPOINT_RELEASE_EVENT_38428563077_attempt_1_candidate_READY

Время: 2026-10-13T16:22:09.174647+00:00.

Этап: candidate; статус: **READY**.

SHA: `ee00d64665d4f63574f0ca2920f399ae01488b51`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38428563077/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `2`
* GITHUB_HOSTED_RUNNER_SECONDS: `584`
* SELF_HOSTED_JOB_COUNT: `0`
* SELF_HOSTED_RUNNER_SECONDS: `0`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

READY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.

### GPOINT_RELEASE_EVENT_38435061489_attempt_1_preflight_READY_FOR_OWNER

Время: 2026-10-13T17:02:30.552362+00:00.

Этап: preflight; статус: **READY_FOR_OWNER**.

SHA: `ee00d64665d4f63574f0ca2920f399ae01488b51`.

Попытка GitHub Actions: `1`.

[Проверки и логи](<https://github.com/akhripunov/gpoint/actions/runs/38435061489/attempts/1>).

Runner/artifact telemetry:

* GITHUB_HOSTED_JOB_COUNT: `4`
* GITHUB_HOSTED_RUNNER_SECONDS: `622`
* SELF_HOSTED_JOB_COUNT: `2`
* SELF_HOSTED_RUNNER_SECONDS: `156`
* DUPLICATE_HOSTED_WORK_REMOVED: `YES`
* CACHE_PREVENTED_DOWNLOAD: `NO`

**GPoint SHA ee00d64665d4f63574f0ca2920f399ae01488b51 полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**

Это результат read-only preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.