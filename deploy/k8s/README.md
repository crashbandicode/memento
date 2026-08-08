# deploy/k8s

GitOps 部署清单(Fleet 拉取,见仓库根 CLAUDE.md)。镜像 tag 由 CI 回写,勿手改。

## GitHub OAuth(可选)

client id / secret **不进仓库**,手工写进集群内 Secret(api 已 envFrom `memento-secret`,加了即生效):

```bash
kubectl -n memento patch secret memento-secret --type merge -p '{"stringData":{"MEMENTO_GITHUB_CLIENT_ID":"...","MEMENTO_GITHUB_CLIENT_SECRET":"..."}}'
```

GitHub OAuth App 的 callback URL 须设为 `https://mem.ihasy.com/api/auth/github/callback`。

## Online index migration rollout

API startup only applies the short schema transaction. Large concurrent index
builds run in the `online-index-migrations` CronJob so API readiness and a
Fleet rollout never wait for them. The CronJob reconciles daily, forbids
overlap, has one bounded controller retry, and is additionally protected by a
PostgreSQL advisory lock.

After Fleet has reconciled a server image, start that image's migration
immediately instead of waiting for the daily safety-net run:

```bash
JOB="online-index-migrations-manual-$(date +%s)"
kubectl -n memento create job --from=cronjob/online-index-migrations "$JOB"
kubectl -n memento logs -f "job/$JOB"
```

Observe durable per-step state, catalog validity/readiness, and live
`pg_stat_progress_create_index` counters from the running API image:

```bash
kubectl -n memento exec deploy/api -- \
  python -m server.scripts.online_index_migrations --status
kubectl -n memento get cronjob,job,pod -l app=online-index-migrations
```

The run is complete only when `idx_documents_content_tsv` reports
`valid: true` and `ready: true`. The runner checks that condition immediately
before each old/redundant index drop. If a pod is interrupted, PostgreSQL may
leave an invalid concurrent index; create another Job from the CronJob. The
next run records a new attempt, drops only the invalid replacement
concurrently, and rebuilds it before continuing.

For Docker Compose deployments, wait for the API to become healthy and run the
same one-shot operator command:

```bash
docker compose run --rm api \
  python -m server.scripts.online_index_migrations --apply
docker compose exec api \
  python -m server.scripts.online_index_migrations --status
```
