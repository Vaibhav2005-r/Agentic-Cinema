FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    golang-go \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install MCP Grafana
RUN go install github.com/grafana/mcp-grafana/cmd/mcp-grafana@latest && \
    cp /root/go/bin/mcp-grafana /usr/local/bin/

# Copy project files
COPY pyproject.toml .
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -e "."

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=8080

# Run the console
CMD ["slo-watchdog", "serve", "--host", "0.0.0.0", "--port", "8080"]
