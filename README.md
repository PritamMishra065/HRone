# Employee Attendance and Analytics API

FastAPI and MongoDB service for employee attendance, corrections with an audit history, and attendance analytics.

## Run locally

Start MongoDB 7 locally, for example:

```powershell
docker run -d --name attendance-mongo -p 27017:27017 mongo:7
```

Create and activate a virtual environment, install dependencies, seed the sample data, and start the API:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000
```

The interactive API documentation is available at `http://127.0.0.1:8000/docs`.

Configuration uses `MONGO_URI` and `MONGO_DB`; real environment variables take priority over values in a local `.env` file. MongoDB 6.0 or later is required.
