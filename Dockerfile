FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system bot && useradd --system --gid bot bot
COPY --chown=bot:bot bot ./bot
COPY --chown=bot:bot main.py .
COPY --chown=bot:bot moderation_rules.json .
USER bot
RUN python -c "import main; import bot.application; import bot.__main__"
CMD ["python", "/app/main.py"]
