# ── Stage 1: Build the React frontend ────────────────────────────────────────
FROM node:22-alpine AS frontend-builder

WORKDIR /build/web
COPY web/package*.json ./
RUN npm install --quiet
COPY web/ ./
RUN npm run build

# ── Stage 2: Runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim

# Install AWS CLI (for S3 data sync) and tini (proper PID 1 signal handling)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        tini \
    && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip \
    && apt-get remove -y curl unzip \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY api/ ./api/
COPY common/ ./common/
COPY config/ ./config/
COPY controllers/ ./controllers/
COPY data/database/sql/ ./data/database/sql/
COPY data/mappings/ ./data/mappings/
COPY helpers/ ./helpers/
COPY services/ ./services/
COPY tools/ ./tools/
COPY server.py ./

# Copy built frontend from stage 1
COPY --from=frontend-builder /build/web/dist ./web/dist

# Create writable data directories
RUN mkdir -p data/database/db data/downloads data/transformations data/results logs

# Copy entrypoint script
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
