FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV TICKET_HUB_PORT=8777
ENV TICKET_HUB_HOST=0.0.0.0
CMD ["python","api.py"]
