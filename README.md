# Employee Attendance & Analytics API

A FastAPI service for employee setup, attendance punch-in/out, manual corrections, and MongoDB-backed attendance analytics. The API contract and rules are in `openapi.yaml`; employee and attendance BSON shapes are in `DATA_MODEL.md`. Application logic is kept in `app/main.py`.

## Prerequisites

- Python 3.11 or newer
- MongoDB Atlas, or another MongoDB 6.0+ deployment with TLS enabled
- A MongoDB user with read/write access to the selected database and permission to create indexes

The API does not fall back to a local MongoDB server. It reads `MONGO_URI` and `MONGO_DB` from the process environment; a local `.env` file may be used, and process environment values take precedence.

## Install

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### macOS / Linux shell

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

## Configure MongoDB Atlas

1. In Atlas, create or select a cluster and create a database user with the minimum needed read/write and index-creation privileges for this assignment.
2. Add your actual development IP under **Network Access**. Avoid `0.0.0.0/0` except as a temporary, deliberate choice.
3. Select **Connect → Drivers → Python** and copy the SRV connection URI.
4. Replace the username, URL-encoded password, and cluster placeholder locally. Do not put the real URI in source control or paste it into a shared issue/chat.
5. Set `MONGO_URI` and `MONGO_DB` in the VS Code terminal environment, or in the ignored local `.env` file. `MONGO_DB` defaults to `attendance_db`.

Example local `.env` shape (replace placeholders only on your machine):

```dotenv
MONGO_URI=mongodb+srv://<username>:<password>@<cluster-host>/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

Restart the terminal/server after changing its environment. Startup creates indexes idempotently. `/health` returns 200 only after MongoDB responds and index initialization succeeds; otherwise it returns 503 with a safe diagnostic.

## Optional sample seed

The supplied sample documents are examples, not a full test dataset. Seeding is deliberately protected: it requires both collections to be empty and an explicit confirmation variable. Confirm that the selected Atlas database is an empty development database before running this.

### PowerShell

```powershell
$env:SEED_SAMPLE_DATA = "YES"
python sample_seed.py
$env:SEED_SAMPLE_DATA = $null
```

### macOS / Linux

```sh
SEED_SAMPLE_DATA=YES python sample_seed.py
```

The seed script does not delete existing documents. Do not point it at production data.

## Start the server

Run from the project root:

```powershell
uvicorn app.main:app --port 8000
```

For development auto-reload:

```powershell
uvicorn app.main:app --port 8000 --reload
```

Open the interactive OpenAPI UI at <http://127.0.0.1:8000/docs>.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | MongoDB readiness |
| POST / GET | `/employees` | Create employees; filter and page employee list |
| POST | `/attendance/punch-in` | Record a unique attendance-day punch-in |
| POST | `/attendance/punch-out` | Atomically close the latest eligible open punch |
| GET | `/attendance` | Filter, sort, and page attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Correct a record and append audit history |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly employee totals |
| GET | `/analytics/departments/summary` | Monthly department summary |
| GET | `/analytics/leaderboard/late` | Ranked monthly late leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily trend and seven-row moving average |
| GET | `/admin/explain/{endpoint}` | MongoDB `executionStats` explain output |

All instants in request/response bodies use epoch milliseconds. Calendar dates and shift times are strings. See `/docs` for the full schemas and query parameters.

## Run tests

```powershell
python -m pytest -q
```

The automated suite uses an isolated in-memory MongoDB-compatible test double for API/helper behavior. Aggregation operators such as `$setWindowFields`, `$lookup` pipelines, `$dateDiff`, and actual `IXSCAN` explain plans must be verified against a real MongoDB 6.0+ database; the test double does not prove those database behaviors.

## Known limitations

- The assignment intentionally has no authentication or authorization layer.
- Department trend generates one employee/day row in MongoDB (maximum 92 days); very large headcounts may warrant a separately measured daily-summary design.
- A successful local test run is not a substitute for checking `/health`, aggregations, and explain plans against the configured Atlas cluster.
