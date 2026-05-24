FROM python:3.12-slim

WORKDIR /app

# Install dependencies first to leverage layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . .

# Default command shows usage; override with `docker run ... python <script>`
CMD ["python", "populate_deterministic.py"]
