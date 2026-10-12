FROM python:3.12-slim

WORKDIR /srv/app
# PYTHONPATH 指向源码目录：arq 等控制台脚本进程的 sys.path 不含工作目录，若不显式声明，
# `import app` 会命中 site-packages 中的安装副本，使 default_skills_root() 解析到不存在的
# site-packages/skills，导致 Skill 目录装载为空、Agent 装配期 SkillActivationError。
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/srv/app

COPY pyproject.toml README.md ./
COPY app ./app
COPY skills ./skills
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --upgrade pip && pip install .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
