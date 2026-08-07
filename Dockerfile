FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
COPY trafix/ trafix/
COPY mocks/ mocks/
COPY cli/ cli/
COPY config/ config/

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000

CMD ["trafix-server", "--env", "docker"]
