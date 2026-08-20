FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY flask_app.py .

CMD ["gunicorn", "-w", "1", "flask_app:app"] 