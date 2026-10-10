# OMS1 Auth Service

`OMS1` - микросервис авторизации. Он отвечает за выпуск и проверку JWT-токенов для пользователей и для межсервисного взаимодействия.

## Функции

- Авторизация пользователя по логину и паролю.
- Выпуск JWT access token для пользователя.
- Выпуск JWT access token для микросервисов `OMS2`, `OMS3`, `OMS4`, `OMS5`.
- Проверка JWT-токена через endpoint `/auth/verify`.
- Публикация событий авторизации в Kafka.

## Технологии

- Python 3.11
- FastAPI
- Uvicorn
- python-jose
- passlib bcrypt
- SQLAlchemy
- Alembic
- PostgreSQL
- confluent-kafka

## API

### Health check

```http
GET /health
```

Ответ:

```json
{
  "status": "ok",
  "service": "OMS1"
}
```

### Получить пользовательский токен

```http
POST /auth/token
Content-Type: application/json
```

Тело запроса:

```json
{
  "username": "admin",
  "password": "admin"
}
```

Ответ:

```json
{
  "access_token": "jwt-token",
  "token_type": "bearer",
  "expires_at": "2026-01-20T10:00:00Z"
}
```

### Получить сервисный токен

```http
POST /auth/service-token
Content-Type: application/json
```

Тело запроса:

```json
{
  "client_id": "OMS2",
  "client_secret": "oms2-secret"
}
```

### Проверить токен

```http
GET /auth/verify
Authorization: Bearer <token>
```

Ответ:

```json
{
  "active": true,
  "subject": "admin",
  "type": "user",
  "scopes": ["users", "services", "employees"]
}
```

## Kafka events

- `auth.user_logged_in` - пользователь успешно получил токен.
- `auth.service_token_issued` - микросервис получил сервисный токен.

## Переменные окружения

| Переменная | Значение по умолчанию | Назначение |
| --- | --- | --- |
| `SERVICE_NAME` | `OMS1` | Имя сервиса |
| `JWT_SECRET_KEY` | `change-me` | Секрет подписи JWT |
| `JWT_ALGORITHM` | `HS256` | Алгоритм JWT |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Время жизни токена |
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka.oms.svc.cluster.local:9092` | Kafka bootstrap servers |
| `DATABASE_URL` | `postgresql+psycopg://oms1@oms1-postgres.oms.svc.cluster.local:5432/oms1` | PostgreSQL connection URL. В k8s задается через Secret-backed env |
| `ADMIN_INITIAL_PASSWORD` | _нет_ | Начальный пароль `admin` для seed, не хранить в Git |
| `OMS2_SERVICE_SECRET` | _нет_ | Service secret `OMS2` для seed, не хранить в Git |
| `OMS3_SERVICE_SECRET` | _нет_ | Service secret `OMS3` для seed, не хранить в Git |
| `OMS4_SERVICE_SECRET` | _нет_ | Service secret `OMS4` для seed, не хранить в Git |
| `OMS5_SERVICE_SECRET` | _нет_ | Service secret `OMS5` для seed, не хранить в Git |

## PostgreSQL storage

`OMS1` хранит пользователей и service clients в PostgreSQL. Таблицы создаются Alembic migration:

```bash
alembic upgrade head
```

Начальные auth-данные создаются явной seed-командой:

```bash
python -m app.seed
```

Seed создает пользователя `admin` и service clients `OMS2`, `OMS3`, `OMS4`, `OMS5`. Пароли и service secrets берутся только из env-переменных и сохраняются в БД только в виде hash.

В Kubernetes PostgreSQL запускается отдельным workload/service `oms1-postgres`, не sidecar-контейнером в pod приложения `OMS1`. Данные локального kind-окружения сохраняются через PV/PVC в host-каталог:

```text
D:/ProjectsDocker/extrawork/.runtime/oms1-postgres-data
```

Если эту папку не удалять, данные сохраняются после перезагрузки компьютера или Docker Desktop. Восстановление окружения выполняется по `platform/docs/K8S_FULL_SETUP.md`, пункт 17.

Перед применением manifests создать реальный Kubernetes Secret `oms1-secret` из непубличных значений. Файл `k8s/secret.example.yaml` содержит только пример и использует имя `oms1-secret-example`, чтобы `kubectl apply -f k8s/` не перезаписывал runtime Secret.

## Локальный запуск

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head
python -m app.seed
uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

Встроенный Swagger UI FastAPI для OMS1 будет доступен по адресу:

```text
http://localhost:8001/docs
```

Это Swagger только текущего сервиса OMS1. Общая Swagger UI страница для единой спецификации `platform/contracts/openapi_oms_microservices.json` запускается отдельно из корня проекта и открывается без `/docs`:

```powershell
docker run --rm `
  --name oms-swagger-ui `
  -p 8088:8080 `
  -e SWAGGER_JSON=/spec/platform/contracts/openapi_oms_microservices.json `
  -v "D:/ProjectsDocker/extrawork:/spec" `
  swaggerapi/swagger-ui:v5.17.14
```

```text
http://localhost:8088
```

Если нужен порт `8001` именно для общей Swagger UI страницы, освободите этот порт от OMS1 и запустите контейнер с `-p 8001:8080`. URL будет `http://localhost:8001`, не `http://localhost:8001/docs`.

Через Ingress сервис доступен без port-forward. Встроенный Swagger UI OMS1:

```text
http://oms.local/oms1/docs
```

Браузером можно открыть `GET /health`; token endpoints нужно вызывать как `POST` из Swagger UI, Postman или `curl`:

```text
GET  http://oms.local/oms1/health
POST http://oms.local/oms1/auth/token
```

## Docker

```bash
docker build -t oms1:latest .
docker run --rm -p 8001:8000 -e JWT_SECRET_KEY=dev-secret oms1:latest
```

## Kubernetes

```bash
kubectl apply -f ../platform/k8s/namespace.yaml
kubectl -n oms create secret generic oms1-secret --from-env-file .env.k8s --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/
kubectl -n oms get pods -l app=oms1
```

Для локального доступа:

```bash
kubectl -n oms port-forward svc/oms1 8001:80
```

После port-forward встроенный Swagger UI OMS1 доступен по адресу:

```text
http://localhost:8001/docs
```
