FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_WORKSPACE=/workspace

RUN useradd --create-home --uid 1000 agent && mkdir /workspace && chown agent /workspace

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py approval.py sandbox.py tools.py ./

USER agent
CMD ["python", "agent.py"]
