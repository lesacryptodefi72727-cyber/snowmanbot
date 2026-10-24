FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py crypto.py storefront.py ./
RUN python -m py_compile bot.py crypto.py storefront.py
CMD ["python", "bot.py"]
