# Edge Device Telemetry System

A minimal, production-grade edge telemetry ingestion system demonstrating reliability patterns for unstable device-to-cloud communication.

## Problem Statement

IoT devices operate in unreliable environments with:
- Intermittent network connectivity
- Power interruptions causing reboots
- Network jitter and packet loss
- Potential for duplicate message transmission

This project demonstrates how to build a resilient telemetry ingestion pipeline that handles these real-world failure modes gracefully.

## System Architecture
```
┌─────────────┐         HTTP POST          ┌──────────────┐
│   Device    │ ──────────────────────────> │ Edge Service │
│ Simulator   │    /ingest endpoint         │  (FastAPI)   │
│             │ <────────────────────────── │              │
│ - Sends     │    200/409/503/400          │ - Idempotent │
│   telemetry │                             │ - Metrics    │
│ - Simulates │                             │ - Logging    │
│   failures  │                             │              │
└─────────────┘                             └──────────────┘
```

## Key Features

### Device Simulator (`device/simulated_device.py`)
- **Network Instabilities:**
  - Packet drops (15% probability)
  - Network jitter with random delays (20% probability, up to 2 seconds)
  - Duplicate transmissions (10% probability)

- **Retry Logic:**
  - Exponential backoff with jitter for transient errors (429, 503, timeouts)
  - No retry for non-transient errors (400 Bad Request, 409 Conflict)
  - Maximum 5 retry attempts with configurable backoff limits

- **Structured Logging:**
  - JSON-formatted logs with timestamps, correlation IDs, and event types
  - Full observability into device behavior and retry attempts

### Edge Ingestion Service (`edge/app.py`)
- **Idempotency:**
  - Uses `(device_id, sequence_id)` tuple to detect duplicates
  - In-memory TTL cache (300-second default) for seen messages
  - Returns 409 Conflict for duplicate messages

- **Error Classification:**
  - **Transient errors** (should retry): 503 Service Unavailable, 429 Too Many Requests
  - **Non-transient errors** (should not retry): 400 Bad Request, 409 Conflict

- **Backpressure Simulation:**
  - Randomly returns 503 (10% probability) to simulate edge service overload
  - Allows testing of device retry logic

- **Observability:**
  - Structured JSON logs with correlation IDs
  - `/metrics` endpoint exposing counters
  - `/health` endpoint for monitoring

## Design Decisions

### 1. Idempotency via Sequence IDs
Messages are deduplicated using `(device_id, sequence_id)` pairs. This ensures:
- Devices can safely retry without creating duplicate data
- Sequence gaps (from packet drops) are acceptable
- TTL-based cache prevents unbounded memory growth

### 2. Exponential Backoff with Jitter
Retry delays use exponential backoff with randomized jitter to:
- Prevent thundering herd problems
- Distribute load over time during outages
- Avoid synchronized retry storms

### 3. Error Code Semantics
- **200 OK**: Message accepted and processed
- **409 Conflict**: Duplicate message (already processed, safe to continue)
- **503 Service Unavailable**: Temporary overload (device should retry)
- **400 Bad Request**: Invalid data (device should not retry)

### 4. In-Memory Cache (No Database)
For this MVP, we use an in-memory cache with TTL instead of a database:
- **Pros**: Simple, fast, no external dependencies
- **Cons**: Cache resets on service restart
- **Production consideration**: Replace with Redis or similar for persistence

## Project Structure
```
edge-device-telemetry/
├── device/
│   └── simulated_device.py       # Device simulator with retry logic
├── edge/
│   ├── app.py                    # FastAPI edge service
│   ├── requirements.txt          # Python dependencies
│   ├── Dockerfile                # Container image definition
│   └── venv/                     # Python virtual environment
├── docker-compose.yml            # Container orchestration
└── README.md                     # This file
```

## Prerequisites

- Python 3.9+ (tested with 3.11)
- pip
- Docker Desktop (for containerized deployment)
- Windows with PowerShell (for commands shown below)

## Installation & Running

### Option 1: Local Development (No Docker)

**1. Set up edge service:**
```powershell
cd edge
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Edge service will start on `http://localhost:8000`

**2. In a separate terminal, run device simulator:**
```powershell
cd device
pip install requests==2.31.0
python simulated_device.py
```

**3. Monitor logs and metrics:**
- Watch both terminal outputs for structured JSON logs
- Check metrics: `http://localhost:8000/metrics`
- Check health: `http://localhost:8000/health`

### Option 2: Containerized Edge Service

**1. Build and start edge container:**
```powershell
docker compose up -d
```

**2. Verify container is running:**
```powershell
docker ps
docker logs edge-telemetry-service
```

**3. Run device simulator from host:**
```powershell
cd device
python simulated_device.py
```

**4. Monitor:**
```powershell
# Container logs
docker logs edge-telemetry-service --follow

# Metrics
Invoke-RestMethod -Uri "http://localhost:8000/metrics" | ConvertTo-Json
```

**5. Stop the system:**
```powershell
# Stop device: Ctrl+C in device terminal
# Stop container:
docker compose down
```

## Testing & Validation

### What to Look For

**Device Logs:**
- `packet_dropped`: Network instability simulation
- `jitter_applied`: Delay injected before sending
- `duplicate_triggered`: Message will be sent twice
- `transient_error_received`: Got 503, will retry
- `retrying_after_backoff`: Exponential backoff in action
- `duplicate_acknowledged`: Received 409 Conflict for duplicate

**Edge Logs:**
- `telemetry_received`: Message arrived
- `telemetry_accepted`: New message accepted (200 OK)
- `duplicate_detected`: Idempotency check caught duplicate (409 Conflict)
- `overload_simulated`: Returned 503 to test retry logic
- `validation_failed`: Invalid data rejected (400 Bad Request)

### Example Failure Scenarios

**1. Packet Drop:**
```
Device seq 5: sent successfully
Device seq 6: packet_dropped
Device seq 7: sent successfully
```
Result: Edge only sees seq 5 and 7 (gap is acceptable)

**2. Duplicate Transmission:**
```
Device seq 10: sent → 200 OK
Device seq 10 (duplicate): sent → 409 Conflict (already processed)
```
Result: Device acknowledges duplicate and continues

**3. Transient Error with Retry:**
```
Device seq 15: sent → 503 Service Unavailable
Device: retrying_after_backoff (1.2 seconds)
Device seq 15 (retry): sent → 200 OK
```
Result: Message eventually delivered despite transient failure

**4. Non-Transient Error:**
```
Device seq -1: sent → 400 Bad Request (invalid sequence_id)
```
Result: Device does not retry (permanent error)

## Metrics Endpoint

**GET** `http://localhost:8000/metrics`

Returns:
```json
{
  "received_total": 45,
  "accepted_total": 38,
  "duplicates_total": 3,
  "rejected_total": 1,
  "transient_503_total": 3
}
```

- `received_total`: Total POST requests received
- `accepted_total`: Unique messages successfully ingested
- `duplicates_total`: Messages rejected as duplicates (idempotency working)
- `rejected_total`: Messages rejected due to validation errors
- `transient_503_total`: Number of 503 responses sent (backpressure simulation)

## Configuration

### Device Simulator (`device/simulated_device.py`)
```python
EDGE_URL = "http://localhost:8000/ingest"
DEVICE_ID = "device-001"
TELEMETRY_INTERVAL_SECONDS = 3

# Failure simulation
PACKET_DROP_PROBABILITY = 0.15      # 15%
JITTER_PROBABILITY = 0.20           # 20%
MAX_JITTER_SECONDS = 2.0
DUPLICATE_PROBABILITY = 0.10        # 10%

# Retry configuration
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
BACKOFF_JITTER_RANGE = 0.5          # +/- 50%
```

### Edge Service (`edge/app.py`)
```python
# Cache TTL (seconds)
message_cache = MessageCache(ttl_seconds=300)

# Overload simulation probability
OVERLOAD_PROBABILITY = 0.1  # 10%
```

## Production Considerations

This is an MVP demonstration. For production use, consider:

1. **Persistence Layer:**
   - Replace in-memory cache with Redis or database
   - Persist metrics to time-series database (Prometheus, InfluxDB)

2. **Scalability:**
   - Add load balancer for multiple edge service instances
   - Use distributed cache (Redis Cluster)
   - Consider message queue (Kafka, RabbitMQ) for buffering

3. **Security:**
   - Add authentication/authorization (API keys, mTLS)
   - Implement rate limiting per device
   - Add TLS/HTTPS encryption

4. **Monitoring:**
   - Integrate with Prometheus + Grafana
   - Set up alerting for error rate thresholds
   - Add distributed tracing (OpenTelemetry)

5. **Device Management:**
   - Device registration and provisioning
   - Schema validation for telemetry payloads
   - Device health tracking and alerting

## License

MIT License - feel free to use as a learning resource or template for your own projects.

## Author

Built as a demonstration of edge computing reliability patterns and device-to-c